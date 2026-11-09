import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    data: Path
    public_url: str
    allowed_sub: str
    channel_id: str
    web_client_id: str
    web_client_secret: str
    redirect_uris: list[str]

    @property
    def resource(self):
        return self.public_url + "/mcp"

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("DATA_DIR", "/data"))
        public_url = os.environ["PUBLIC_URL"].rstrip("/")
        url = urlsplit(public_url)
        if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username:
            raise ValueError("PUBLIC_URL must be an HTTPS origin, with no path or credentials")
        sub = os.environ["ALLOWED_GOOGLE_SUB"].strip()
        if not sub.isdigit():
            raise ValueError("Set ALLOWED_GOOGLE_SUB to your verified Google subject ID")
        client = json.loads((data / "google-web-client.json").read_text())["web"]
        redirects = [s.strip() for s in os.environ["MCP_REDIRECT_URIS"].split(",") if s.strip()]
        if not redirects or any(urlsplit(u).scheme != "https" or urlsplit(u).fragment or urlsplit(u).username for u in redirects):
            raise ValueError("Configure exact HTTPS MCP client callback URLs")
        return cls(data, public_url, sub, os.environ["YOUTUBE_CHANNEL_ID"],
                   client["client_id"], client["client_secret"], redirects)
