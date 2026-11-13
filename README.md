# Intergalactic Shelf YouTube MCP

Read-only, authenticated access to the configured channel's YouTube data and analytics.
The existing OAuth setup uses local `.env`, `data/google-web-client.json`, and
`data/youtube-token.json` files. Never commit these files or the SQLite database.

## Review and traffic-source detail queries

See [the backlog review](docs/backlog-review.md) for verified findings, open
questions, GitHub issues, and the proposed implementation order.
See [analytics enhancements](docs/analytics-enhancements.md) for durable report
history, freshness/labels/error categories, bulk setup/imports, consolidated
performance, suggested sources, creator filtering and pre/post discovery tools.

`query_analytics` (HTTP: `POST /api/analytics`) already supports traffic-source
detail queries. These reports require explicit parameters rather than the
generic report defaults:

```json
{
  "query": {
    "start_date": "2026-10-01",
    "end_date": "2026-10-31",
    "dimensions": "insightTrafficSourceDetail",
    "filters": "video==VIDEO_ID_01;insightTrafficSourceType==RELATED_VIDEO",
    "metrics": "views,estimatedMinutesWatched",
    "sort": "-views",
    "max_results": 25,
    "start_index": 1
  }
}
```

For HTTP, send the inner `query` object as the JSON body. Replace `VIDEO_ID_01`
with an owned video ID. This returns raw referring video IDs, not resolved
titles or channels; `get_suggested_video_sources` remains planned.

Traffic detail validation now rejects `max_results > 25`, missing/unsupported
sort, a sort metric absent from `metrics`, a page extending past position 25,
or a missing
`insightTrafficSourceType` filter before any Google call. Supported metrics in
the current channel-report documentation are `views`, `estimatedMinutesWatched`,
and `engagedViews`; sort is `-views` or `-estimatedMinutesWatched`.
HTTP callers receive the existing validation format (422); MCP callers receive
a tool validation error. Values are never silently clamped or rewritten.
The metric list is not an exhaustive allowlist: live verification also accepted
`averageViewDuration` with `RELATED_VIDEO`. Google validates metric combinations
and source availability.

Traffic-source detail is treated as a **top-25 report** for the requested filters,
dates and sort order. Smaller pages within positions 1–25 still work. Requests
must satisfy `start_index + max_results - 1 <= 25`; pages crossing that boundary
fail validation rather than being silently shortened. At row 25,
`next_start_index` is `null`, including when the returned page is full.
Other reports keep their existing pagination behavior.

Detail responses add this coverage information:

```json
{
  "coverage": {
    "type": "top_results",
    "rank_limit": 25,
    "completeness": "not_guaranteed",
    "broader_coverage_source": "youtube_reporting_api"
  }
}
```

This describes the report's scope, not proof that exactly 25 sources exist or
that an empty/short report is complete. Broader referral analysis requires bulk
traffic-source reports, subject to availability and privacy restrictions. Bulk
job creation and CSV import remain future work; this change creates no jobs.
Raw data and existing response fields are preserved. Only the misleading next
index beyond the report boundary is removed.

`SUBSCRIBER` means homepage feeds and subscription features,
not the subscription status of the viewer. Do not equate it with Studio's entire
Browse category. See Google's [dimensions](https://developers.google.com/youtube/analytics/dimensions#Traffic_Source_Dimensions)
and [channel reports](https://developers.google.com/youtube/analytics/channel_reports#traffic-source-detail).

## Google error diagnostics

Google failures now include their status and API source in MCP error text,
plus Google's reason codes when supplied. For example:

```text
Google API error (status=500; source=analytics; reason=backendError,INTERNAL): Internal error encountered.
```

Reason codes are optional; the server does not invent a reason when Google omits
one. HTTP error responses retain `error`, `google_status`, and the existing HTTP
status mapping, and add `source`, `google_reasons`, and a UTC `occurred_at`.
Local connection/setup errors have no upstream source or reason. Successful
responses are unchanged. Full response bodies and request headers are not exposed;
Google's bounded error message continues to be forwarded as before.

This is diagnostic groundwork, not complete error classification. See
[the pagination investigation and coverage decision](docs/referral-pagination.md).

## Compare videos at the same age

MCP tool: `compare_videos_by_age`

```json
{
  "query": {
    "video_ids": ["VIDEO_ID_01", "VIDEO_ID_02"],
    "age_days": 7
  },
  "refresh": false
}
```

Use actual 11-character video IDs. Accepts 1–50 IDs and 1–90 days (default 7).
Duplicates are removed, and results preserve input order. Only videos belonging
to the configured channel are compared.

The authenticated HTTP equivalent is `POST /api/compare-videos-by-age` with
`{"video_ids": ["VIDEO_ID_01"], "age_days": 7}` as its JSON body.
Supply an MCP-issued bearer access token with the `youtube:read` scope.
Use the optional `?refresh=true` query parameter to bypass cached API responses.

Each video's window starts on its publication date in `America/Los_Angeles`,
YouTube's reporting timezone. Publication day is day 1 and can be partial.
These are **reporting-day comparisons**, not exact first-24/48/168-hour totals.
Daylight saving time is handled by the reporting timezone.

Responses contain each video's window, daily metrics, freshness, and status:

- `reported`: all requested days and metrics are present; additive totals are included.
- `window_not_finished`: the window includes today or a future reporting day; no historical query is made.
- `incomplete_data`: reporting days or metrics are missing; totals remain `null`.
- `unavailable`: metadata was not returned, for example for a deleted or inaccessible video.
- `wrong_channel`: the video belongs to another channel; no analytics are requested.

Metrics are views, estimated minutes watched, subscribers gained, and subscribers
lost. Missing rows never become zero. Even a fully reported window can later be
revised by YouTube. Shorts and long videos are not automatically classified or ranked.
Google API failures remain errors rather than being returned as zero or missing data.

## Cache and persistent observations

Every successful YouTube API response is cached in `data/shelf.sqlite3` for 15
minutes. Expired cache entries are removed on subsequent cache writes.
`refresh=true` requests a fresh response and updates the cache.

Fresh video-detail responses also save lifetime-statistics observations in a
separate SQLite table. This applies to `list_videos` and `compare_videos_by_age`.
Only observations from the configured channel with a nonnegative age are saved.
Cache hits do not create new samples or change their timestamps.

Observations persist beyond cache expiry and authentication revocation. They are
collected opportunistically when tools are called; there is no background polling.
The comparison returns the closest stored sample at/before the requested elapsed
age and the closest after it, if available. Each sample includes its actual age,
fetch time, publication time, and offset from `age_days * 86400` seconds. These
samples are separate from historical analytics: no interpolation, no replacement
of historical missing data, and no claim that a sample matches an exact milestone.
Observations using a different publication timestamp are excluded from the result.

The new table and index are created automatically on startup without replacing
existing authentication or cache tables. No additional Google scopes are needed.

## Update on Synology

The [GitHub deployment workflow](docs/github-deployment.md) runs tests, connects
over SSH, updates the checkout to the tested main commit, backs up private data,
and rebuilds/restarts the existing Compose service. Configure its Actions secrets
and host variable before enabling the first live deployment.

Before the first update, stop the service and back up the persistent database:

```bash
cd /volume1/docker/youtube-mcp-git
git pull --ff-only
sudo docker compose stop shelf
sudo cp -a data/shelf.sqlite3 "data/shelf.sqlite3.backup-$(date +%Y%m%d-%H%M%S)"
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8787/healthz
```

Continue only if the backup succeeds. The `./data:/data` bind mount preserves the
database, credentials, and tokens across rebuilds. Keep the old project stopped
so it does not compete for port 8787. Refresh your MCP client's tool list after
deploying to discover `compare_videos_by_age`.

## Tests

With Python 3.12 and the dependencies in `requirements.txt` installed:

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked Google responses. They do not require
live credentials or modify channel data.
