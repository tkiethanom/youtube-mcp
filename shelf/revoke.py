"""Invalidate all MCP sessions and registrations; retain YouTube credentials/cache."""
import os
from pathlib import Path
from .store import Store

if __name__ == "__main__":
    Store(Path(os.environ.get("DATA_DIR", "/data")) / "shelf.sqlite3").clear_auth()
    print("All MCP clients and sessions revoked. Reconnect and sign in again.")
