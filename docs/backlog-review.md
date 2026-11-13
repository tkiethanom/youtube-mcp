# Backlog review — 2026-11-13

Reviewed repository baseline: `f43d47de904e0902f2cd5f03f72f06f790d3c8b9`.
This is a code and official-documentation review with mocked API tests, not a
production audit. No private Google credentials, NAS filesystem, or live channel
reports were available or accessed. No production deployment was performed.

## Existing architecture and contracts

The service is a small Python application, not a Google SDK wrapper framework:

- `shelf/app.py`: FastMCP tools, authenticated FastAPI endpoints, host/rate guards.
- `shelf/youtube.py`: validated query models, synchronous Google GET requests,
  OAuth refresh, 15-minute cache, Analytics record mapping and comparisons.
- `shelf/store.py`: SQLite cache/auth records and durable video observations.
- `shelf/auth.py`, `bootstrap.py`, `revoke.py`: MCP OAuth and local Google setup.
- `Dockerfile`, `compose.yaml`: Python 3.12, single-worker service, non-root user,
  read-only container, persistent `./data:/data` mount, localhost port 8787.

Existing MCP tools are `get_channel`, `list_videos`, `query_analytics`,
`get_retention`, `compare_videos`, `compare_videos_by_age`, and `list_bulk_reports`.
HTTP data routes are `/api/channel`, `/api/videos`, `/api/analytics`, and
`/api/compare-videos-by-age`. The OAuth and health routes also remain intact.

Raw request responses include `data`, `cached`, `fetched_at`, and `source`.
Analytics adds `records`, `requested`, `next_start_index`, and a caveat.
Successful contracts should be extended additively. The first patch changes
validation of invalid traffic-detail requests only; no new endpoint is introduced.

Channel identity is checked before Analytics queries. Video lists paginate 50
uploads at a time; title filtering is page-local. Calendar comparisons accept up
to 50 IDs. Equal-age comparisons validate ownership, use Pacific publication
reporting days, preserve missing days as unknown, and sum only additive metrics.
Retention is already queried with `elapsedVideoTimeRatio` and the retention pair.
Reporting integration lists types/jobs/report metadata only; it neither creates
jobs nor downloads CSVs.

SQLite saves actual lifetime Data API video statistics opportunistically, with
fetch timestamps, publication timestamps and elapsed ages. Cached reads do not
create samples. Samples survive cache expiry and auth clearing. No scheduler
exists. Raw Analytics history is currently only cached and is later evicted.

## Findings against the backlog

| Backlog | Status and evidence | Tracking |
| --- | --- | --- |
| P0.1 watch-time consistency | Not reproduced. `analytics()` retains Google's raw response and maps headers to records; it does not recalculate views, watch minutes or average duration. `compare_by_age()` sums minutes/views, never averages average duration. Live side-by-side evidence is still needed. | [#1](https://github.com/tkiethanom/youtube-mcp/issues/1) |
| P0.2 traffic labels | No label mapping exists, so a wrong server mapping is not confirmed. Raw codes are already retained. Google defines `SUBSCRIBER` as homepage feeds/subscription features, not viewer subscription status. | [#2](https://github.com/tkiethanom/youtube-mcp/issues/2) |
| P0.3 freshness/history | Partial. `fetched_at` is preserved on cached API reads, with service source. No explicit latest reporting date, and equal-age result/status paths omit some freshness fields. No durable raw Analytics archive. | [#3](https://github.com/tkiethanom/youtube-mcp/issues/3) |
| P0.4 errors/query integrity | Verified gaps. Errors carry only Google status/message; HTTP maps upstream statuses below 500 to 400, including auth/quota. No reason/category/retry guidance. Traffic-detail requests accept invalid generic defaults. | [#4](https://github.com/tkiethanom/youtube-mcp/issues/4) |
| P0.5 Browse | Generic queries can retrieve traffic types/details with supported parameters. No dedicated Browse workflow or documentation. No documented literal `BROWSE` source code; Studio category equivalence cannot be assumed. | [#2](https://github.com/tkiethanom/youtube-mcp/issues/2) |
| P1.6 unified performance | Components can be queried separately; no consolidated tool. Requires separate compatible reports for traffic, devices and engagement, rather than one cross-tab. | [#5](https://github.com/tkiethanom/youtube-mcp/issues/5) |
| P1.7 cross-video discovery | Calendar/equal-age comparisons exist, but no older-video pre/post-upload analysis or referral attribution. | [#6](https://github.com/tkiethanom/youtube-mcp/issues/6) |
| P1.8 suggested referrals | Generic detail query is possible; dedicated tool, metadata resolution, creator aggregation/filtering and comparison orchestration are absent. | [#7](https://github.com/tkiethanom/youtube-mcp/issues/7) |
| P2.9 audience | No returning/unique-viewer tool. These names are absent from the reviewed Analytics metrics catalog; Reporting/account capabilities require separate verification. | [#8](https://github.com/tkiethanom/youtube-mcp/issues/8) |
| P2.10 impressions/CTR | No tool. The Analytics catalog's ad impressions and card/annotation click rates do not establish thumbnail impressions/CTR support. Reporting research remains open. | [#8](https://github.com/tkiethanom/youtube-mcp/issues/8) |
| P2.11 development tracking | Eight issues created, with priorities in titles and acceptance criteria; related research is grouped. | [Issues](https://github.com/tkiethanom/youtube-mcp/issues) |

## Official query findings

Documentation checked on 2026-11-13:

1. [Metrics](https://developers.google.com/youtube/analytics/metrics): average
   duration is seconds; estimated watch time is minutes. Average duration excludes
   looping-clip traffic since 2021-12-13. `views` varies by report and `engagedViews`
   is separately defined. Never enforce `views * averageViewDuration / 60` as a
   source-data invariant. Check definitions, formats, query scope and aggregation
   before deriving a ratio; do not average daily averages without compatible weights.
2. [Dimensions](https://developers.google.com/youtube/analytics/dimensions):
   `RELATED_VIDEO` detail identifies referring video IDs. `SUBSCRIBER` includes
   homepage feeds/subscription features; detail values include `/`,
   `/my_subscriptions`, `uploaded`, and others. `subscribedStatus` is separate.
   A Browse investigation can use `insightTrafficSourceType` with supported
   viewing metrics, then a separate detail report filtered to `SUBSCRIBER`.
   Those values must not be presented as exact Studio Browse totals.
3. [Channel reports](https://developers.google.com/youtube/analytics/channel_reports#traffic-source-detail):
   traffic detail requires `maxResults <= 25`, an explicit supported sort
   (`-views` or `-estimatedMinutesWatched`) and a source-type filter. The listed
   metrics are `engagedViews`, `views`, `estimatedMinutesWatched`. Do not append
   `averageViewDuration`, subscriber gains/losses, `day`, or a `video` dimension
   to the confirmed referral query. Traffic-type reports have their own supported
   dimensions/filters and a queried-videos × days limit of 50,000.
4. [Query reference](https://developers.google.com/youtube/analytics/reference/reports/query):
   `startIndex` is 1-based pagination. Data ends at the last date for which **all
   requested metrics** are available. Requested `endDate` is not coverage proof.
   For aggregate reports, no returned daily dimension means latest reporting date
   is unknown unless independently established. A maximum day on one page is only
   an observed day, not a global completeness guarantee.

The dimensions and report catalogs are not perfectly aligned on every source's
detail availability (for example notifications/end screens). Do not turn the
dimensions list into a guarantee that every detail query works. `RELATED_VIDEO`
has the supplied working baseline and agrees with the channel report definition;
other sources need a compatible query and explicit unavailable/error handling.

The baseline referral query uses one target video's filter, the detail dimension,
`views,estimatedMinutesWatched`, `-views`, and 25 results. Follow supported
pagination while disclosing top-result/coverage limits. Resolve referring IDs in
Data API batches, keep unresolved IDs, and use stable channel IDs for creator
filters. Find/verify No Good Figures' channel rather than guessing its ID.
Comparisons should keep each upload and date window identifiable before aggregating.

## First bounded change

Verified defect: a traffic-detail query inherited `max_results=200`, blank sort
and incompatible subscriber/average-duration metrics. These requests were sent to
Google and rejected without local corrective guidance.

`AnalyticsQuery` now rejects invalid traffic-detail limits, sorting, metric lists
and absent source filters before network access. It provides the required names
and example filter in validation errors. It does not rewrite user queries or
source values, or claim to validate all Google report combinations. Existing
successful response contracts, OAuth files/scopes, database schema and deployment
configuration are unchanged.

Tests cover the valid referral query and pagination forwarding, raw watch-minute
preservation, invalid combinations, the alternate watch-time sort, optional
documented content-type dimension, unchanged generic defaults, and MCP/HTTP
validation before network access. Existing age-comparison, persistence, migration,
cache and authentication tests remain part of the full suite.

## Proposed implementation sequence

1. **This change:** document the baseline and add targeted traffic-detail query
   validation. Keep it independently reviewable under issue #4.
2. **P0 errors and watch-time evidence (#4, #1):** classify safe Google reason
   codes for both HTTP/MCP, preserve legacy fields, and gather paired raw/MCP
   reports with the same queries and fetch times. Use authorized private fixtures
   locally; commit only synthetic/anonymized regressions. Report any discrepancy
   without substituting source values.
3. **P0 traffic/Browse (#2):** add source labels beside raw codes, unknown-code
   fallback and documented query presets. Test SUBSCRIBER and supported detail
   queries, keeping Studio category equivalence unclaimed.
4. **P0 freshness/history (#3):** add fetch/retrieval/reporting-coverage metadata
   and durable on-demand Analytics observations through additive SQLite migration.
   Preserve auth/cache/video observations and document unknown coverage.
5. **P1 referrals (#7):** implement `get_suggested_video_sources` in its own change,
   with ownership checks, pagination, metadata resolution, per-upload/window
   records, creator filters, aggregation and explicit missing-data states.
6. **P1 consolidated report (#5), then pre/post discovery (#6):** reuse the
   validated report components; retain per-section provenance and distinguish
   association from observed referral evidence.
7. **P2 capability research (#8):** inspect official Reporting schemas and the
   account's available types/jobs without changing production scopes or creating
   jobs. Only propose endpoints once metric availability is established.

No production update is part of this assignment. Future releases should use a
separate reviewed change, verified backups and the existing persistent data mount.
