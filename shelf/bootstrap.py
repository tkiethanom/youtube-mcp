"""Run on your computer, whose browser can reach its own loopback listener."""
import argparse
import json
import secrets
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow

from .auth import verify_identity
from .youtube import SCOPES, atomic_private_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["identity", "youtube"])
    parser.add_argument("--client", type=Path, required=True, help="Downloaded Google Desktop OAuth client JSON")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--channel-id", default="UCKs0rsgO2x1O2AZXXAe1i0A")
    args = parser.parse_args()
    scopes = ["openid", "https://www.googleapis.com/auth/userinfo.email"] if args.action == "identity" else SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client), scopes=scopes, autogenerate_code_verifier=True)
    nonce = secrets.token_urlsafe(32)
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
        access_type="offline", prompt="consent select_account", nonce=nonce,
        success_message="Authorization received. You may close this tab.")
    if args.action == "identity":
        identity = verify_identity(flow.oauth2session.token["id_token"], flow.client_config["client_id"])
        if identity.get("nonce") != nonce:
            raise RuntimeError("Identity nonce verification failed")
        atomic_private_write(args.data / "identity.env", "ALLOWED_GOOGLE_SUB=" + identity["sub"] + "\n")
        print("Verified Google account:", identity.get("email", "(email not returned)"))
        print("Account ID saved to", args.data / "identity.env")
    else:
        if not credentials.refresh_token:
            raise RuntimeError("No refresh token returned. Revoke the old grant and authorize again.")
        response = requests.get("https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": "Bearer " + credentials.token}, timeout=30)
        response.raise_for_status()
        channels = response.json().get("items", [])
        selected = next((v for v in channels if v["id"] == args.channel_id), None)
        if not selected:
            raise RuntimeError("Wrong channel selected. Choose the owner account and Intergalactic Shelf Brand Account, then rerun. No token was saved.")
        atomic_private_write(args.data / "youtube-token.json", credentials.to_json())
        print("Connected:", selected["snippet"]["title"], selected["id"])
        print("Private YouTube credential saved to", args.data / "youtube-token.json")


if __name__ == "__main__":
    main()
