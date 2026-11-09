"""MCP OAuth broker: only the configured, Google-verified subject may authorize."""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from mcp.server.auth.provider import AuthorizationCode, AccessToken, RefreshToken, AuthorizeError, TokenError, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def verify_identity(encoded, audience):
    # Google's verifier checks signature, audience, issuer and time claims.
    return verify_oauth2_token(encoded, GoogleRequest(), audience=audience)


class Provider:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def get_client(self, client_id):
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or any(str(u) not in self.settings.redirect_uris for u in client_info.redirect_uris):
            print("Rejected OAuth redirect URIs:", [str(u) for u in (client_info.redirect_uris or [])], flush=True)
            raise RegistrationError("invalid_redirect_uri", "Callback URL is not allowed")
        self.store.put("client", client_info.client_id, client_info.model_dump(mode="json"), 365 * 86400)

    async def authorize(self, client, params):
        if params.resource not in (None, self.settings.resource):
            raise AuthorizeError("invalid_target", "Incorrect resource")
        if not set(params.scopes or []).issubset({"youtube:read"}):
            raise AuthorizeError("invalid_scope", "Only youtube:read is supported")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.store.put("login", state, {"client_id": client.client_id,
            "params": params.model_dump(mode="json"), "nonce": nonce, "verifier": verifier}, 600)
        # An intermediate route binds the authorization flow to this browser.
        return self.settings.public_url + "/login/start?" + urlencode({"state": state})

    async def login_start(self, request: Request):
        state = request.query_params.get("state", "")
        pending = self.store.get("login", state)
        if not pending:
            return JSONResponse({"error": "Invalid or expired sign-in"}, 400)
        browser = secrets.token_urlsafe(32)
        pending["browser"] = hashlib.sha256(browser.encode()).hexdigest()
        self.store.put("login", state, pending, 600)
        query = {"client_id": self.settings.web_client_id,
            "redirect_uri": self.settings.public_url + "/google/callback",
            "response_type": "code", "scope": "openid email", "state": state,
            "nonce": pending["nonce"], "code_challenge": challenge(pending["verifier"]),
            "code_challenge_method": "S256", "prompt": "select_account"}
        response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query), 302)
        response.set_cookie("__Host-shelf-login", browser, max_age=600, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def callback(self, request: Request):
        state = request.query_params.get("state", "")
        pending = self.store.get("login", state, consume=True)
        browser = request.cookies.get("__Host-shelf-login", "")
        if not pending or not browser or not secrets.compare_digest(pending.get("browser", ""), hashlib.sha256(browser.encode()).hexdigest()):
            return JSONResponse({"error": "Invalid or expired sign-in. Start again."}, 400)
        if "error" in request.query_params or not request.query_params.get("code"):
            return JSONResponse({"error": "Google sign-in was not completed"}, 403)
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                result = await http.post("https://oauth2.googleapis.com/token", data={
                    "grant_type": "authorization_code", "code": request.query_params["code"],
                    "client_id": self.settings.web_client_id,
                    "client_secret": self.settings.web_client_secret,
                    "redirect_uri": self.settings.public_url + "/google/callback",
                    "code_verifier": pending["verifier"]})
                result.raise_for_status()
            identity = await run_in_threadpool(verify_identity, result.json()["id_token"], self.settings.web_client_id)
            if identity.get("nonce") != pending["nonce"] or identity.get("sub") != self.settings.allowed_sub:
                return JSONResponse({"error": "This Google account is not authorized"}, 403)
        except Exception:
            # Never send tokens, authorization codes or Google responses to clients/logs.
            return JSONResponse({"error": "Google identity verification failed. Start again."}, 403)
        params = pending["params"]
        code = secrets.token_urlsafe(32)
        info = AuthorizationCode(code=code, scopes=params["scopes"] or ["youtube:read"],
            expires_at=time.time() + 120, client_id=pending["client_id"],
            code_challenge=params["code_challenge"], redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=self.settings.resource, subject=identity["sub"])
        data = info.model_dump(mode="json")
        data.pop("code")
        self.store.put("code", code, data, 120)
        query = {"code": code}
        if params["state"] is not None:
            query["state"] = params["state"]
        separator = "&" if "?" in params["redirect_uri"] else "?"
        response = RedirectResponse(params["redirect_uri"] + separator + urlencode(query), 302)
        response.delete_cookie("__Host-shelf-login", path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def load_authorization_code(self, client, authorization_code):
        data = self.store.get("code", authorization_code)
        if data and data["client_id"] == client.client_id:
            return AuthorizationCode(code=authorization_code, **data)

    def issue(self, client_id, scopes, subject, family=None):
        if subject != self.settings.allowed_sub:
            raise TokenError("invalid_grant", "Account no longer allowed")
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        family = family or secrets.token_urlsafe(32)
        common = dict(client_id=client_id, scopes=scopes, subject=subject, resource=self.settings.resource, family=family)
        self.store.put("access", access, {**common, "expires_at": int(time.time()) + 3600}, 3600)
        self.store.put("refresh", refresh, {**common, "expires_at": int(time.time()) + 30 * 86400}, 30 * 86400)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.store.get("code", authorization_code.code, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Code expired or already used")
        return self.issue(client.client_id, data["scopes"], data["subject"])

    async def load_refresh_token(self, client, refresh_token):
        data = self.store.get("refresh", refresh_token)
        if data and data["client_id"] == client.client_id and self.valid(data):
            return RefreshToken(token=refresh_token, **{k:v for k,v in data.items() if k != "family"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.store.get("refresh", refresh_token.token, consume=True)
        if not data or data["client_id"] != client.client_id or not self.valid(data):
            raise TokenError("invalid_grant", "Refresh token expired or already used")
        if not set(scopes).issubset(data["scopes"]):
            raise TokenError("invalid_scope", "Cannot broaden access")
        return self.issue(client.client_id, scopes, data["subject"], data["family"])

    def valid(self, data):
        return (data["subject"] == self.settings.allowed_sub and
                data["resource"] == self.settings.resource and
                data["expires_at"] > time.time() and not self.store.get("revoked", data["family"]))

    async def load_access_token(self, token):
        data = self.store.get("access", token)
        if data and self.valid(data):
            return AccessToken(token=token, **{k:v for k,v in data.items() if k != "family"})

    async def revoke_token(self, token):
        for kind in ("access", "refresh"):
            data = self.store.get(kind, token.token, consume=True)
            if data:
                self.store.put("revoked", data["family"], True, 31 * 86400)
