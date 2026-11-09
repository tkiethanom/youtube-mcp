# Intergalactic Shelf YouTube MCP

Read-only, authenticated access to the configured channel's YouTube data and analytics.
The existing OAuth setup uses local `.env`, `data/google-web-client.json`, and
`data/youtube-token.json` files. Never commit these files or the SQLite database.

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
