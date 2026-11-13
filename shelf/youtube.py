import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
        # Channel traffic-source detail has stricter limits than generic reports.
        # https://developers.google.com/youtube/analytics/channel_reports#traffic-source-detail
        if "insightTrafficSourceDetail" in self.dimensions.split(","):
            if self.max_results > 25:
                raise ValueError("insightTrafficSourceDetail requires max_results <= 25")
            if self.sort not in ("-views", "-estimatedMinutesWatched"):
                raise ValueError("insightTrafficSourceDetail requires sort=-views or -estimatedMinutesWatched")
            if self.sort[1:] not in self.metrics.split(","):
                raise ValueError("The traffic-source detail sort metric must be included in metrics")
            supported = {"views", "estimatedMinutesWatched", "engagedViews"}
            if set(self.metrics.split(",")) - supported:
                raise ValueError("insightTrafficSourceDetail supports metrics views,estimatedMinutesWatched,engagedViews; "
                                 "use views,estimatedMinutesWatched for referral reports")
            if not any(re.fullmatch(r"insightTrafficSourceType==[A-Z_]+", part)
                       for part in self.filters.split(";")):
                raise ValueError("insightTrafficSourceDetail requires an insightTrafficSourceType filter, "
                                 "for example video==VIDEO_ID;insightTrafficSourceType==RELATED_VIDEO")
        return self


class GoogleAPIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


class AgeComparisonQuery(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=50)
    age_days: int = Field(default=7, ge=1, le=90, strict=True)

    @model_validator(mode="after")
    def validate_ids(self):
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in self.video_ids):
            raise ValueError("Provide valid 11-character YouTube video IDs")
        self.video_ids = list(dict.fromkeys(self.video_ids))
        return self


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
            if service == "data" and path == "videos":
                self.observe_videos(response)
            self.store.put("cache", key, response, 900)
            return response

    def observe_videos(self, response):
        """Persist actual API samples, using fetch time rather than cache-read time."""
        observed = datetime.fromisoformat(response["fetched_at"])
        for video in response["data"].get("items", []):
            snippet = video.get("snippet", {})
            if snippet.get("channelId") != self.settings.channel_id:
                continue
            published = snippet.get("publishedAt")
            if not published or not video.get("statistics"):
                continue
            age = (observed - datetime.fromisoformat(published.replace("Z", "+00:00"))).total_seconds()
            if age < 0:
                continue
            self.store.save_observation(self.settings.channel_id, video["id"], {
                "observed_at": response["fetched_at"], "published_at": published,
                "age_seconds": age, "statistics": video["statistics"],
                "source": "youtube_data_api"})

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

    def compare_by_age(self, video_ids, age_days=7, refresh=False):
        query = AgeComparisonQuery(video_ids=video_ids, age_days=age_days)
        self.channel(refresh)
        details = self.request("data", "videos", {
            "part": "snippet,statistics", "id": ",".join(query.video_ids)}, refresh)
        videos = {v["id"]: v for v in details["data"].get("items", [])}
        reporting_zone = ZoneInfo("America/Los_Angeles")
        today = datetime.now(reporting_zone).date()
        results = []
        additive = ("views", "estimatedMinutesWatched", "subscribersGained", "subscribersLost")
        for video_id in query.video_ids:
            item = {"video_id": video_id, "totals": None, "daily": [], "observations": []}
            results.append(item)
            video = videos.get(video_id)
            if video is None:
                item["status"] = "unavailable"
                continue
            snippet = video["snippet"]
            if snippet["channelId"] != self.settings.channel_id:
                item["status"] = "wrong_channel"
                continue
            published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            start = published.astimezone(reporting_zone).date()
            end = start + timedelta(days=query.age_days - 1)
            item.update(title=snippet["title"], published_at=snippet["publishedAt"],
                        start_date=start.isoformat(), end_date=end.isoformat(),
                        metadata_fetched_at=details["fetched_at"], metadata_cached=details["cached"])
            item["observations"] = [
                {**o, "offset_from_target_seconds": o["age_seconds"] - query.age_days * 86400}
                for o in self.store.observations_near(self.settings.channel_id, video_id, query.age_days * 86400)
                if o["published_at"] == snippet["publishedAt"]]
            if end >= today:
                item["status"] = "window_not_finished"
                continue
            report = self.analytics(AnalyticsQuery(start_date=start, end_date=end,
                metrics=",".join(additive), dimensions="day", filters="video==" + video_id), refresh)
            records = report["records"]
            expected = {(start + timedelta(days=i)).isoformat() for i in range(query.age_days)}
            present = {r["day"] for r in records}
            complete = present == expected and len(records) == query.age_days and all(
                all(r.get(m) is not None for m in additive) for r in records)
            item.update(status="reported" if complete else "incomplete_data",
                        daily=[{**r, "age_day": (date.fromisoformat(r["day"]) - start).days + 1}
                               for r in records],
                        missing_days=sorted(expected - present),
                        fetched_at=report["fetched_at"], cached=report["cached"])
            if complete:
                item["totals"] = {metric: sum(r[metric] for r in records) for metric in additive}
        return {"age_days": query.age_days, "basis": "publication_reporting_day_is_day_1",
                "reporting_timezone": "America/Los_Angeles", "videos": results,
                "note": "Day 1 is the possibly partial publication day in YouTube's reporting timezone; "
                        "these are not exact elapsed 24-hour windows. Missing days are unknown, not zero. "
                        "Reported data may lag or be revised. Observations are lifetime Data API samples "
                        "at their stated ages, never estimates of exact milestone performance. "
                        "No automatic Shorts/long-form classification or ranking is applied."}

    def reporting(self, kind="reportTypes", job_id="", page_token=""):
        self.channel()
        if kind not in ("reportTypes", "jobs", "reports"):
            raise ValueError("kind must be reportTypes, jobs or reports")
        if kind == "reports" and not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("A valid job_id is required")
        path = "jobs/" + job_id + "/reports" if kind == "reports" else kind
        return self.request("reporting", path, {"pageSize": 100, **({"pageToken": page_token} if page_token else {})})
