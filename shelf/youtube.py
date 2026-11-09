import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timezone

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field, model_validator

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
BASES = {"data": "https://www.googleapis.com/youtube/v3/",
         "analytics": "https://youtubeanalytics.googleapis.com/v2/",
         "reporting": "https://youtubereporting.googleapis.com/v1/"}


def atomic_private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class AnalyticsQuery(BaseModel):
    start_date: date
    end_date: date
    metrics: str = Field(default="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost", max_length=2000)
    dimensions: str = Field(default="day", max_length=500)
    filters: str = Field(default="", max_length=5000)
    sort: str = Field(default="", max_length=500)
    max_results: int = Field(default=200, ge=1, le=200)
    start_index: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_date > self.end_date:
            raise ValueError("start_date must be before or equal to end_date")
        if self.end_date > date.today():
            raise ValueError("end_date cannot be in the future")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(,[A-Za-z][A-Za-z0-9]*)*", self.metrics):
            raise ValueError("metrics must be a comma-separated list of metric names")
        return self


class GoogleAPIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


class YouTube:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.lock = threading.RLock()

    def request(self, service, path, params, refresh=False):
        key = json.dumps([self.settings.channel_id, service, path, params], sort_keys=True)
        with self.lock:
            if not refresh:
                cached = self.store.get("cache", key)
                if cached:
                    return {**cached, "cached": True}
            token_path = self.settings.data / "youtube-token.json"
            if not token_path.exists():
                raise GoogleAPIError(503, "YouTube is not connected. Complete the local bootstrap.")
            try:
                credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                if not credentials.valid:
                    credentials.refresh(Request())
                    atomic_private_write(token_path, credentials.to_json())
            except Exception:
                raise GoogleAPIError(503, "YouTube authorization needs renewal. Run bootstrap again.") from None
            try:
                result = requests.get(BASES[service] + path, params=params,
                    headers={"Authorization": "Bearer " + credentials.token}, timeout=(10, 45), allow_redirects=False)
            except requests.RequestException:
                raise GoogleAPIError(502, "Google API could not be reached") from None
            if result.status_code != 200:
                # Google error messages aid query correction, but never expose request headers.
                try:
                    message = result.json().get("error", {}).get("message", "Google rejected the request")
                except ValueError:
                    message = "Google rejected the request"
                raise GoogleAPIError(result.status_code, str(message)[:1000])
            response = {"data": result.json(), "cached": False,
                        "fetched_at": datetime.now(timezone.utc).isoformat(), "source": service}
            self.store.put("cache", key, response, 900)
            return response

    def channel(self, refresh=False):
        result = self.request("data", "channels", {"part": "snippet,statistics,contentDetails", "mine": "true"}, refresh)
        items = result["data"].get("items", [])
        matches = [v for v in items if v["id"] == self.settings.channel_id]
        if not matches:
            raise GoogleAPIError(403, "Connected YouTube account does not match YOUTUBE_CHANNEL_ID. Reauthorize the channel owner.")
        return {**result, "data": matches[0]}

    def videos(self, query="", page_token="", refresh=False):
        channel = self.channel(refresh)
        playlist = channel["data"]["contentDetails"]["relatedPlaylists"]["uploads"]
        page = self.request("data", "playlistItems", {"part": "contentDetails", "playlistId": playlist,
            "maxResults": 50, **({"pageToken": page_token} if page_token else {})}, refresh)
        ids = [v["contentDetails"]["videoId"] for v in page["data"].get("items", [])]
        details = self.request("data", "videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids)}, refresh) if ids else {"data": {"items": []}, "fetched_at": page["fetched_at"], "cached": page["cached"]}
        items = [v for v in details["data"].get("items", []) if query.casefold() in v["snippet"]["title"].casefold()]
        return {"videos": items, "next_page_token": page["data"].get("nextPageToken"),
                "fetched_at": details["fetched_at"], "cached": details["cached"],
                "note": "Title filtering applies to this page; follow next_page_token even if this page has no matches. Duration alone does not identify Shorts."}

    def analytics(self, query: AnalyticsQuery, refresh=False):
        self.channel()  # Enforce channel identity before returning private analytics.
        params = {"ids": "channel==" + self.settings.channel_id,
                  "startDate": query.start_date.isoformat(), "endDate": query.end_date.isoformat(),
                  "metrics": query.metrics, "maxResults": query.max_results, "startIndex": query.start_index}
        for key in ("dimensions", "filters", "sort"):
            value = getattr(query, key)
            if value:
                params[key] = value
        result = self.request("analytics", "reports", params, refresh)
        columns = [v["name"] for v in result["data"].get("columnHeaders", [])]
        rows = result["data"].get("rows", [])
        return {**result, "records": [dict(zip(columns, row)) for row in rows],
                "requested": params, "next_start_index": query.start_index + len(rows) if len(rows) == query.max_results else None,
                "note": "Data may lag or be suppressed; empty rows are not proof of zero activity. A full page may have more rows. Metric/dimension combinations must be supported by Google."}

    def retention(self, video_id, start_date, end_date, refresh=False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Expected an 11-character YouTube video ID")
        return self.analytics(AnalyticsQuery(start_date=start_date, end_date=end_date,
            metrics="audienceWatchRatio,relativeRetentionPerformance", dimensions="elapsedVideoTimeRatio",
            filters="video==" + video_id), refresh)

    def compare(self, video_ids, start_date, end_date, refresh=False):
        ids = list(dict.fromkeys(video_ids))
        if not 1 <= len(ids) <= 50 or any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in ids):
            raise ValueError("Provide 1–50 valid video IDs")
        return self.analytics(AnalyticsQuery(start_date=start_date, end_date=end_date,
            dimensions="video", filters="video==" + ",".join(ids), sort="-views"), refresh)

    def reporting(self, kind="reportTypes", job_id="", page_token=""):
        self.channel()
        if kind not in ("reportTypes", "jobs", "reports"):
            raise ValueError("kind must be reportTypes, jobs or reports")
        if kind == "reports" and not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("A valid job_id is required")
        path = "jobs/" + job_id + "/reports" if kind == "reports" else kind
        return self.request("reporting", path, {"pageSize": 100, **({"pageToken": page_token} if page_token else {})})
