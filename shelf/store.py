"""Small durable stores; bearer tokens are indexed by SHA-256, never stored raw."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))")
        path.chmod(0o600)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            yield db

    @staticmethod
    def key(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def put(self, kind, key, value, ttl):
        with self.db() as db:
            db.execute("DELETE FROM kv WHERE expires < ?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)", (kind, self.key(key), json.dumps(value), time.time() + ttl))

    def get(self, kind, key, consume=False):
        with self.db() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value,expires FROM kv WHERE kind=? AND key=?", (kind, self.key(key))).fetchone()
            if consume:
                db.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, self.key(key)))
            return json.loads(row[0]) if row and row[1] > time.time() else None

    def clear_auth(self):
        with self.db() as db:
            db.execute("DELETE FROM kv WHERE kind != 'cache'")
