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
            db.execute("""CREATE TABLE IF NOT EXISTS video_observations (
                channel_id TEXT, video_id TEXT, observed_at TEXT, age_seconds REAL,
                value TEXT, PRIMARY KEY(channel_id, video_id, observed_at))""")
            db.execute("""CREATE INDEX IF NOT EXISTS observations_by_age
                ON video_observations(channel_id, video_id, age_seconds)""")
            db.execute("""CREATE TABLE IF NOT EXISTS analytics_observations (
                id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, request_key TEXT NOT NULL,
                request_json TEXT NOT NULL, fetched_at TEXT NOT NULL, response_json TEXT NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS analytics_observations_by_query
                ON analytics_observations(channel_id, request_key, id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS bulk_reports (
                channel_id TEXT, report_id TEXT, job_id TEXT, report_type TEXT,
                start_time TEXT, end_time TEXT, create_time TEXT, imported_at TEXT,
                content_sha TEXT, metadata_json TEXT,
                PRIMARY KEY(channel_id, report_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS bulk_rows (
                channel_id TEXT, report_id TEXT, row_number INTEGER, day TEXT,
                video_id TEXT, source_type TEXT, source_detail TEXT, value_json TEXT,
                PRIMARY KEY(channel_id, report_id, row_number))""")
            db.execute("""CREATE INDEX IF NOT EXISTS bulk_rows_by_day
                ON bulk_rows(channel_id, day, video_id)""")
        path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

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

    def save_observation(self, channel_id, video_id, observation):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO video_observations VALUES (?,?,?,?,?)",
                       (channel_id, video_id, observation["observed_at"],
                        observation["age_seconds"], json.dumps(observation)))

    def observations_near(self, channel_id, video_id, age_seconds):
        # Return the closest actual samples on either side, never interpolate.
        with self.db() as db:
            rows = []
            for operator, order in (("<=", "DESC"), (">", "ASC")):
                rows.extend(db.execute(f"""SELECT value FROM video_observations
                    WHERE channel_id=? AND video_id=? AND age_seconds {operator} ?
                    ORDER BY age_seconds {order}, observed_at DESC LIMIT 1""",
                    (channel_id, video_id, age_seconds)).fetchall())
            return [json.loads(row[0]) for row in rows]

    def save_analytics(self, channel_id, params, response):
        request_json = json.dumps(params, sort_keys=True)
        with self.db() as db:
            cursor = db.execute("""INSERT INTO analytics_observations
                (channel_id,request_key,request_json,fetched_at,response_json) VALUES (?,?,?,?,?)""",
                (channel_id, self.key(request_json), request_json, response["fetched_at"], json.dumps(response)))
            return cursor.lastrowid

    def analytics_history(self, channel_id, params, before_id=None, limit=20):
        request_key = self.key(json.dumps(params, sort_keys=True))
        with self.db() as db:
            rows = db.execute("""SELECT id,request_json,response_json FROM analytics_observations
                WHERE channel_id=? AND request_key=? AND (? IS NULL OR id<?)
                ORDER BY id DESC LIMIT ?""", (channel_id, request_key, before_id, before_id, limit)).fetchall()
            return [{"observation_id": row[0], "requested": json.loads(row[1]),
                     "response": json.loads(row[2])} for row in rows]
