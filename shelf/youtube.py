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

from .analytics_support import annotate_records, freshness, utc_now, watch_time_diagnostics

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
            if self.start_index + self.max_results - 1 > 25:
                raise ValueError("insightTrafficSourceDetail is a top-25 report: "
                                 "start_index + max_results - 1 must be <= 25; "
                                 "use YouTube Reporting API bulk reports for broader coverage")
            if self.sort not in ("-views", "-estimatedMinutesWatched"):
                raise ValueError("insightTrafficSourceDetail requires sort=-views or -estimatedMinutesWatched")
            if self.sort[1:] not in self.metrics.split(","):
                raise ValueError("The traffic-source detail sort metric must be included in metrics")
            # Do not infer an exhaustive metric allowlist from the report table:
            # live RELATED_VIDEO queries also accept averageViewDuration.
            # Google remains the authority on metric-combination support.
            if not any(re.fullmatch(r"insightTrafficSourceType==[A-Z_]+", part)
                       for part in self.filters.split(";")):
                raise ValueError("insightTrafficSourceDetail requires an insightTrafficSourceType filter, "
                                 "for example video==VIDEO_ID;insightTrafficSourceType==RELATED_VIDEO")
        return self


class GoogleAPIError(Exception):
    def __init__(self, status, message, *, source=None, reasons=(), category=None, retry_after=None):
        self.status, self.message = status, message
        self.source = source
        self.reasons = tuple(dict.fromkeys(reasons))
        self.occurred_at = datetime.now(timezone.utc).isoformat()
        self.category = category or self.classify(status, self.reasons)
        actions = {
            "invalid_query": "Check the report's supported metrics, dimensions, filters, sort and date range.",
            "authorization": "Check the connected channel and granted scopes; renew authorization if necessary.",
            "rate_limit": "Wait before retrying; honor retry_after_seconds when supplied.",
            "quota": "Check the Google project's quota/reset time; do not repeatedly retry.",
            "unavailable_data": "Check resource ownership and availability; missing data is not zero activity.",
            "upstream": "Retry later; retain the Google status/reason if the failure persists.",
            "configuration": "Check API enablement and project configuration.",
            "transport": "Check connectivity to Google and retry later.",
        }
        self.action = actions.get(self.category, "Inspect the Google status and reason before retrying.")
        self.retryable = self.category in ("rate_limit", "upstream", "transport")
        self.retry_after_seconds = retry_after
        # FastMCP forwards exception text; include provenance even when Google's
        # message is only "Internal error encountered." Keep raw bodies private.
        context = [f"status={status}"]
        if source:
            context.append(f"source={source}")
        if self.reasons:
            context.append("reason=" + ",".join(self.reasons))
        context.append("category=" + self.category)
        super().__init__(f"Google API error ({'; '.join(context)}): {message} Next: {self.action}")

    @staticmethod
    def classify(status, reasons):
        reasons = {r.lower() for r in reasons}
        if reasons & {"quotaexceeded", "dailylimitexceeded", "dailylimitexceededunreg"}:
            return "quota"
        if status == 429 or reasons & {"ratelimitexceeded", "userratelimitexceeded", "resource_exhausted"}:
            return "rate_limit"
        if reasons & {"accessnotconfigured", "servicedisabled", "api_disabled"}:
            return "configuration"
        if status in (401, 403):
            return "authorization"
        if status == 404:
            return "unavailable_data"
        if status == 400:
            return "invalid_query"
        return "upstream" if status >= 500 else "unknown"

    @classmethod
    def from_response(cls, result, source):
        message, reasons = "Google rejected the request", []
        try:
            payload = result.json()
        except ValueError:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            if isinstance(error.get("message"), str):
                message = error["message"][:1000]
            entries = error.get("errors", [])
            if isinstance(entries, list):
                for entry in entries[:10]:
                    reason = entry.get("reason") if isinstance(entry, dict) else None
                    if isinstance(reason, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", reason):
                        reasons.append(reason)
            # Some Google APIs only provide the canonical status name.
            canonical = error.get("status")
            if isinstance(canonical, str) and re.fullmatch(r"[A-Z][A-Z_]{0,99}", canonical):
                reasons.append(canonical)
        retry_header = result.headers.get("Retry-After", "")
        retry_after = int(retry_header) if isinstance(retry_header, str) and re.fullmatch(r"[0-9]{1,8}", retry_header) else None
        return cls(result.status_code, message, source=source, reasons=reasons, retry_after=retry_after)

    def as_dict(self):
        return {"error": self.message, "google_status": self.status, "source": self.source,
                "google_reasons": list(self.reasons), "occurred_at": self.occurred_at,
                "category": self.category, "action": self.action, "retryable": self.retryable,
                "retry_after_seconds": self.retry_after_seconds}


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

    def credentials(self):
        """Load/refresh the existing grant; never broaden scopes or replace its location."""
        with self.lock:
            token_path = self.settings.data / "youtube-token.json"
            if not token_path.exists():
                raise GoogleAPIError(503, "YouTube is not connected. Complete the local bootstrap.", category="authorization")
            try:
                credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                if not credentials.valid:
                    credentials.refresh(Request())
                    atomic_private_write(token_path, credentials.to_json())
                return credentials
            except Exception:
                raise GoogleAPIError(503, "YouTube authorization needs renewal. Run bootstrap again.", category="authorization") from None

    def request(self, service, path, params, refresh=False):
        key = json.dumps([self.settings.channel_id, service, path, params], sort_keys=True)
        with self.lock:
            if not refresh:
                cached = self.store.get("cache", key)
                if cached:
                    return {**cached, "cached": True, "retrieved_at": utc_now(),
                            "freshness": freshness(service, cached["fetched_at"])}
            credentials = self.credentials()
            try:
                result = requests.get(BASES[service] + path, params=params,
                    headers={"Authorization": "Bearer " + credentials.token}, timeout=(10, 45), allow_redirects=False)
            except requests.RequestException:
                raise GoogleAPIError(502, "Google API could not be reached", category="transport") from None
            if result.status_code != 200:
                # Google error messages aid query correction, but never expose request headers.
                raise GoogleAPIError.from_response(result, service)
            try:
                data = result.json()
            except ValueError:
                raise GoogleAPIError(502, "Google returned an invalid JSON response.", source=service) from None
            if not isinstance(data, dict):
                raise GoogleAPIError(502, "Google returned an unexpected response shape.", source=service)
            response = {"data": data, "cached": False,
                        "fetched_at": datetime.now(timezone.utc).isoformat(), "source": service}
            response.update(retrieved_at=utc_now(), freshness=freshness(service, response["fetched_at"]))
            if service == "analytics" and path == "reports":
                response["observation_id"] = self.store.save_analytics(self.settings.channel_id, params, response)
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
                "retrieved_at": utc_now(), "source": "data",
                "freshness": freshness("data", details["fetched_at"]),
                "note": "Title filtering applies to this page; follow next_page_token even if this page has no matches. Duration alone does not identify Shorts."}

    def analytics_params(self, query):
        params = {"ids": "channel==" + self.settings.channel_id,
                  "startDate": query.start_date.isoformat(), "endDate": query.end_date.isoformat(),
                  "metrics": query.metrics, "maxResults": query.max_results, "startIndex": query.start_index}
        for key in ("dimensions", "filters", "sort"):
            value = getattr(query, key)
            if value:
                params[key] = value
        return params

    def analytics(self, query: AnalyticsQuery, refresh=False):
        self.channel()  # Enforce channel identity before returning private analytics.
        params = self.analytics_params(query)
        result = self.request("analytics", "reports", params, refresh)
        headers = result["data"].get("columnHeaders", [])
        rows = result["data"].get("rows", [])
        if (not isinstance(headers, list) or any(not isinstance(h, dict) or not isinstance(h.get("name"), str) for h in headers)
                or not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(headers) for row in rows)):
            raise GoogleAPIError(502, "Google returned a malformed Analytics table; columns and rows were not silently truncated.", source="analytics")
        columns = [v["name"] for v in headers]
        records = [dict(zip(columns, row)) for row in rows]
        is_detail = "insightTrafficSourceDetail" in query.dimensions.split(",")
        next_index = query.start_index + len(rows) if len(rows) == query.max_results else None
        if is_detail and next_index is not None and next_index > 25:
            next_index = None
        coverage = {"coverage": {
            "type": "top_results", "rank_limit": 25,
            "completeness": "not_guaranteed",
            "broader_coverage_source": "youtube_reporting_api"}} if is_detail else {}
        return {**result, "records": annotate_records(records),
                "retrieved_at": utc_now(),
                "freshness": freshness("analytics", result.get("fetched_at"), records=records),
                "metric_diagnostics": watch_time_diagnostics(records),
                "requested": params, "next_start_index": next_index, **coverage,
                "note": "Data may lag or be suppressed; empty rows are not proof of zero activity. "
                        + ("Traffic-source detail is treated as a top-25 report ranked by the requested sort. "
                           "Pages are limited to positions 1–25; this is not a guaranteed complete referral list. "
                           "Use YouTube Reporting API bulk traffic-source reports for broader coverage, "
                           "subject to availability and privacy restrictions. " if is_detail else
                           "A full page may have more rows. ")
                        + "Metric/dimension combinations must be supported by Google."}

    def analytics_history(self, query, before_id=None, limit=20):
        if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
            raise ValueError("limit must be 1–100 and before_id must be positive")
        self.channel()
        rows = self.store.analytics_history(self.settings.channel_id, self.analytics_params(query), before_id, limit)
        return {"observations": rows, "retrieved_at": utc_now(),
                "next_before_id": rows[-1]["observation_id"] if len(rows) == limit else None,
                "note": "Every fresh Analytics response is retained. Cache reads create no new observation."}

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
            item = {"video_id": video_id, "totals": None, "daily": [], "observations": [],
                    "metadata_fetched_at": details["fetched_at"],
                    "freshness": freshness("data", details["fetched_at"])}
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
                        fetched_at=report["fetched_at"], cached=report["cached"],
                        freshness=freshness("analytics", report["fetched_at"], records=records))
            if complete:
                item["totals"] = {metric: sum(r[metric] for r in records) for metric in additive}
        return {"age_days": query.age_days, "basis": "publication_reporting_day_is_day_1",
                "retrieved_at": utc_now(), "metadata_fetched_at": details["fetched_at"],
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
