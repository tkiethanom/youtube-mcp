"""Report annotations; Google's raw values are never rewritten here."""
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# YouTube Analytics dimensions#Traffic_Source_Dimensions, checked 2026-11-13.
TRAFFIC_LABELS = {
    "ADVERTISING": "YouTube advertising", "ANNOTATION": "Video cards and annotations",
    "CAMPAIGN_CARD": "Campaign cards (content owner)", "END_SCREEN": "End screens",
    "EXT_URL": "External websites and apps", "HASHTAGS": "Hashtag pages",
    "LIVE_REDIRECT": "Live redirects", "NO_LINK_EMBEDDED": "Embedded players",
    "NO_LINK_OTHER": "Direct or unknown", "NOTIFICATION": "Notifications",
    "PLAYLIST": "Playlist playback", "PRODUCT_PAGE": "Product pages",
    "PROMOTED": "Unpaid YouTube promotion", "RELATED_VIDEO": "Suggested videos",
    "SHORTS": "Shorts feed", "SOUND_PAGE": "Shorts sound pages",
    "SUBSCRIBER": "Browse features (homepage feeds and subscription features)",
    "YT_CHANNEL": "Channel pages", "YT_OTHER_PAGE": "Other YouTube features",
    "YT_SEARCH": "YouTube search", "VIDEO_REMIXES": "Video remixes",
    "WATCH_WITH": "Watch With streams",
}


def annotate_records(records):
    return [{**row, **({"insightTrafficSourceTypeLabel": TRAFFIC_LABELS.get(
        row["insightTrafficSourceType"], "Unknown traffic source (" + str(row["insightTrafficSourceType"]) + ")")}
        if "insightTrafficSourceType" in row else {})} for row in records]


def freshness(service, fetched_at, retrieved_at=None, records=()):
    days = sorted({r["day"] for r in records if isinstance(r.get("day"), str)})
    return {"source": service, "fetched_at": fetched_at,
            "retrieved_at": retrieved_at or utc_now(),
            "reporting_timezone": "America/Los_Angeles" if service != "data" else None,
            "latest_observed_reporting_date": days[-1] if days else None,
            "coverage_basis": "returned_day_rows_on_this_page" if days else "unknown",
            "note": ("Lifetime Data API statistics at fetch time; not a historical Analytics report."
                     if service == "data" else
                     "Analytics processing commonly lags 48–72 hours and may be revised or suppressed. "
                     "An observed day is not proof of complete coverage; requested endDate is not actual coverage.")}


def watch_time_diagnostics(records):
    comparisons = []
    for index, row in enumerate(records):
        values = [row.get(k) for k in ("views", "estimatedMinutesWatched", "averageViewDuration")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            continue
        views, minutes, duration = values
        engaged = row.get("engagedViews")
        comparisons.append({"record_index": index,
            "reported_minutes": minutes, "reported_average_duration_seconds": duration,
            "minutes_from_views_times_duration": views * duration / 60,
            "difference_minutes": minutes - views * duration / 60,
            "minutes_from_engaged_views_times_duration": engaged * duration / 60
                if isinstance(engaged, (int, float)) and not isinstance(engaged, bool) else None})
    return {"identity_assumed": False, "comparisons": comparisons,
            "note": "Diagnostic arithmetic only; source values are unchanged. Views, engagedViews and "
                    "average duration can use different populations. Average duration excludes looping clips. "
                    "Google's August 2026 revision anchors core performance on engaged metrics. "
                    "Rounding, reporting revisions and aggregation can also differ; these are not equality checks."}
