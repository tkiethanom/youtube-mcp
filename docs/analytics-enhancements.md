# Analytics enhancements

## Reliability foundation

All changes extend existing successful payloads. Existing tool names, default
query metrics, OAuth scopes and stored tokens remain intact. SQLite migration
only adds tables/indexes; authentication, cache and video observations are kept.

Fresh Analytics responses are appended to `analytics_observations`, including the
actual Google query, raw result, fetch timestamp and observation ID. Every fresh
version is retained, including repeated identical values; cache reads create no
version. History survives auth clearing and cache expiry. No scheduler or polling
is introduced. Database size grows with use; include the existing persistent
database in backups. Pre-update expired cache data cannot be reconstructed.

`get_analytics_history(query, before_id=null, limit=20)` returns versions of the
exact query, newest first. Follow `next_before_id` for earlier observations.
HTTP equivalent: `POST /api/analytics-history` with an AnalyticsQuery JSON body
and optional `before_id`/`limit` query parameters. Channel identity is verified
before private history is read.

API envelopes add `retrieved_at` and `freshness`; cached reads retain original
`fetched_at`. Freshness distinguishes Data API lifetime statistics from delayed
Analytics reports. `latest_observed_reporting_date` is the maximum returned `day`
on this page, never the requested end date or proof of complete daily coverage.
It is null for aggregate reports with no day. Missing rows remain unknown.

Analytics records add `insightTrafficSourceTypeLabel` only when that dimension is
present. Original codes, unknown codes, detail values and raw Google tables are
unchanged. `SUBSCRIBER` is labeled as homepage/subscription Browse features, not
viewer subscription status. Studio category totals still need matched-date
comparison; unknown detail strings are not rejected.

`metric_diagnostics` reports arithmetic comparisons when views, watch minutes and
average duration are present. It never flags a mismatch as an implementation
error or overwrites raw metrics. Views and engagedViews can represent different
populations; average duration excludes looping clips. Google's August 27, 2026
revision explains that public views count from playback start while core
performance remains anchored on engaged metrics. Daily rounding, population
differences and reporting revisions can prevent exact reconciliation.

Errors retain legacy `error`, `google_status` and HTTP status mapping. Added
`category`, `action`, `retryable`, `retry_after_seconds`, source/reasons and
occurrence timestamp distinguish invalid queries, authorization, quota, rate
limits, unavailable resources, project configuration, transport and upstream
failures. Retry guidance does not automatically retry requests. Empty reports
remain successful reports, not errors or zeros. Reason codes are optional; the
server never invents a Google reason. Numeric Retry-After is exposed when supplied.

Verification: 34 mocked tests pass for this foundation, including raw value/code
preservation, observed reporting coverage, historical revisions, cache behavior,
history pagination/channel isolation, existing migrations/auth, error categories
and HTTP/MCP compatibility. Live confirmation requires deploying the new code;
private raw datasets are not committed.

References checked 2026-11-13:

- https://developers.google.com/youtube/analytics/dimensions
- https://developers.google.com/youtube/analytics/reference/reports/query
- https://developers.google.com/youtube/analytics/metrics
- https://developers.google.com/youtube/reporting/revision_history
- https://developers.google.com/youtube/reporting/v1/reports/dimensions

## On-demand bulk reports

`create_bulk_reporting_jobs({"report_types": ["channel_traffic_source_a3",
"channel_reach_basic_a1"]})` creates or reuses active jobs for those exact types.
This is the only Google mutation in these enhancements, is annotated as a write,
and requires an explicit invocation. The user authorized both job types. Existing
Google scopes support creation; no new grant is requested. Incomplete job-list
pagination stops creation rather than risking duplicate jobs. A network failure
during creation has an unknown outcome: list jobs before retrying.

`import_bulk_reports(job_id, max_reports=10, page_token="")` downloads a page of
1–20 generated CSVs. Follow `next_page_token`; no background importer runs. Per
report failures are returned alongside successful imports. Each CSV is validated
and imported atomically. The importer only sends credentials to HTTPS Reporting
API media URLs and refuses redirects, wrong-channel rows, malformed headers/rows,
non-daily periods and files larger than 16 MiB or one million rows. Imports are
serialized within the existing single-worker process to fit the container's
32 MiB temporary filesystem. Unknown CSV
columns and raw values are preserved. Download URLs/headers are not persisted in
the archive. Report IDs already imported are not downloaded again.

Each Google report revision is retained. Analysis selects the latest Google
`createTime` for a given report type and period, including revisions from duplicate
jobs, rather than adding backfills together. `missing_report_days` lists missing
imported reports, not missing video rows. Empty reports can cover a day while
still returning no identifiable sources. Bulk privacy/anonymization rules apply.

HTTP equivalents: `POST /api/bulk/jobs` with the setup object as JSON, and
`POST /api/bulk/import?job_id=JOB_ID&max_reports=10`.

After a separately authorized deployment, the same operations can run in the
configured NAS container without exposing credentials:

```bash
sudo docker compose exec -T shelf python -m shelf.manage_bulk setup
sudo docker compose exec -T shelf python -m shelf.manage_bulk import --job-id JOB_ID
```

Google generates daily reports and normally backfills the preceding 30 days for
a new job. Reports may take 24–48 hours to appear. Historical reports are available
for download for 30 days; other generated reports for 60 days. On-demand imports
must happen before expiry if that data is to be retained locally. No automatic
snapshotting or retrieval is introduced.

Traffic-source CSVs include referring IDs, views, watch minutes and engaged views.
Reach-basic CSVs include actual `video_thumbnail_impressions` and
`video_thumbnail_impressions_ctr`; these are not ad/card impressions. Both types
were returned by the account's reportTypes listing. No job has been created in
production by this local implementation, because the currently connected MCP
only exposes read/list operations; creation needs the new code to be deployed.

Verification at this stage: the full suite had 42 passing tests. Bulk fixtures verify
idempotent creation, pagination safety, archive/revision selection, malformed CSV
rollback, empty reports, size limits and credential destination restrictions.

Additional references:

- https://developers.google.com/youtube/reporting/v1/reference/rest/v1/jobs/create
- https://developers.google.com/youtube/reporting/v1/reports
- https://developers.google.com/youtube/reporting/v1/reports/channel_reports#reach-basic

## Consolidated performance

`get_video_performance(query, refresh=false)` takes `video_ids` (1–50),
`start_date`, and `end_date`. It returns one section per owned upload:

- Raw metadata/lifetime statistics, with Data API fetch provenance.
- Summary views, engaged views, estimated minutes watched, Google's reported
  average duration, likes, comments, shares and subscriber gains/losses.
- A matching daily report for observed reporting coverage, paginated in 200-row
  pages with an explicit bounded range (maximum 3660 days).
- Traffic-type and device reports, each queried separately using supported
  `views,estimatedMinutesWatched` metrics.
- Daily thumbnail impressions/CTR from imported reach-basic reports when present.

Failures are per section; an authorization or quota failure includes its category
and action instead of becoming zero data. Missing metadata/wrong-channel uploads
are not queried. Summary/daily metrics were verified against the live MCP before
implementation; traffic and device queries follow their separate report tables.
Daily sums are not substituted for Google's aggregate report. Reach percentages
remain their original daily values; no unweighted CTR average or inferred click
count is returned. Missing imports return `not_imported_or_no_rows`.

## Suggested-video sources and creator arguments

`get_suggested_video_sources` accepts one or multiple video IDs using a list,
one to four labeled date windows, and optional creator/source arguments:

```json
{
  "query": {
    "video_ids": ["VIDEO_ID_01"],
    "date_ranges": [
      {"label": "before", "start_date": "2026-10-01", "end_date": "2026-10-28"},
      {"label": "after", "start_date": "2026-10-29", "end_date": "2026-11-12"}
    ],
    "creator": "NoGoodFigures",
    "source": "analytics",
    "max_metadata_videos": 500
  }
}
```

`creator` is a query argument, never a hardcoded server creator. It accepts
`NoGoodFigures`, `@NoGoodFigures`, any other handle, or a stable `UC...` channel ID.
An empty value means all creators. `resolve_creator(creator, refresh=false)` can
resolve a handle first. Handles are normalized only by removing one optional
leading `@`; the Data API `channels.list(forHandle=...)` resolves the actual ID.
Names are not guessed or substring-matched. Referring video metadata is looked up
in batches of at most 50 IDs through `videos.list`, preserving absent/private/
deleted IDs and per-batch fetch times. The metadata resolution budget is
configurable from 1 to 5000 referring videos (default 500), ordered by observed
views. Unresolved IDs and lookup failures remain explicit; a creator-filtered
result is not claimed complete while identities remain unknown.

Analytics mode uses the confirmed baseline exactly: one target upload per query,
`insightTrafficSourceDetail`, `RELATED_VIDEO`, `views,estimatedMinutesWatched`,
`-views`, and 25 results. It does not request pages past 25. Bulk mode uses
on-demand imported traffic-source reports (`traffic_source_type=7`), retaining
unidentified rows and missing-report dates. The source is explicit; there is no
silent fallback between top-25 Analytics data and bulk data. Bulk mode streams
stored CSV rows while aggregating; a 20,000-group budget prevents unbounded
in-memory results and instructs callers to narrow date/upload selections.

Each range keeps per-upload sources and aggregates by referring video and stable
channel ID. Windows are compared separately; overlapping ranges are never added
together. Comparison deltas describe visible results only, and an absent source
in one window yields unknown delta rather than a zero. Duration remains null for
these aggregates: the baseline metrics do not establish a compatible denominator.
Reported average duration is available through the consolidated report; we never
substitute `minutes*60/views` for Google's duration.

## Cross-video discovery

`analyze_cross_video_discovery(query, refresh=false)` takes `new_video_id`,
`older_video_ids`, optional `days_before`/`days_after` (default 28 each, maximum
366), and optional explicit `before`/`after` date-range objects. The default
before window ends the day before publication; the after window starts on the
publication reporting day and includes that possibly partial day. Dates use
America/Los_Angeles, including DST. Explicit windows must be non-overlapping and
on the appropriate sides of publication.

Only owned videos published before the new upload are queried. Daily views,
engaged views and watch minutes are returned with page provenance, missing days,
totals and per-day rates. Missing days/metrics keep totals and deltas null.
Unfinished default after windows return `window_not_finished`, never silently
shortened windows. Unequal custom windows include per-day deltas; a zero baseline
has no percentage change. Results describe correlation, not causation or proven
referral traffic. Use the suggested-source tool for separate referral evidence.

## Capabilities and audience research

`get_analytics_capabilities(refresh=false)` inspects available report types and
returns documentation-based capability status. Official Reporting API schemas
and this account's reportTypes support reach-basic and traffic-source reports.
Reach metrics were added on January 15, 2026. The old `uniques` metric was
deprecated in September 2016; surviving catalog mentions are not support. No
current returning-viewer metric was found in the reviewed catalogs. Neither
unique nor returning viewers is inferred from subscribedStatus or video views.

If reportTypes has another page, use `list_bulk_reports(kind="reportTypes",
page_token=...)` to inspect it; capability flags explicitly describe this page.

## New HTTP equivalents and validation

All `/api/` routes require the existing bearer token with `youtube:read`.
Pass the inner `query` object as the HTTP JSON body (not the MCP wrapper).

| MCP tool | HTTP equivalent |
| --- | --- |
| get_video_performance | POST /api/video-performance |
| get_suggested_video_sources | POST /api/suggested-video-sources |
| analyze_cross_video_discovery | POST /api/cross-video-discovery |
| resolve_creator | GET /api/creator?creator=HANDLE |
| get_analytics_capabilities | GET /api/capabilities |

The three report POST routes accept optional `?refresh=true`. Metadata resolution
also supports refresh. Input limits/ranges are validated; invalid IDs/date windows
fail before private reports are fetched. Existing tool names and successful
response fields remain. New tables preserve all pre-existing data, and all
destructive production operations remain outside these code changes.

Full validation: **64 mocked tests pass**, including HTTP authentication and MCP
dispatch, normalized creator handles, stable-ID filtering, metadata batching and
failures, multi-window comparisons, unknown identities, bulk-source aggregation,
daily pagination, Pacific/DST windows, per-section errors, zero baselines and
unknown missing-day totals. Dependency consistency and whitespace checks pass.
The existing Starlette/httpx deprecation warning remains unchanged.

The new report features are implemented and not yet deployed. Google reporting jobs
are authorized but not yet created, because no production mutation tool or NAS
SSH connection is available in this session. After deployment, run
`create_bulk_reporting_jobs` (or the setup CLI above), then verify job IDs through
`list_bulk_reports(kind="jobs")` before importing generated reports. Actual CSV
import/reach/referral output needs live validation once Google generates files.

Further references:

- https://developers.google.com/youtube/v3/docs/channels/list#forHandle
- https://developers.google.com/youtube/v3/docs/videos/list
- https://developers.google.com/youtube/reporting/revision_history#january-15-2026
- https://developers.google.com/youtube/reporting/revision_history#september-27-2016

## Review and rollout status

The previous top-25/error-provenance fix is merged on `main` at `2d6a267`.
The enhancements in this document were developed as four separate commits on
`feature/analytics-backlog`, tested, and merged into `main` at the user's request.
Production deployment and reporting-job creation remain separate pending steps.

| Backlog item | Implementation/research | Remaining live verification |
| --- | --- | --- |
| Watch time | Raw values preserved; discrepancy diagnostics and engaged-metric definitions documented | Matched Studio export if exact remaining differences need investigation |
| Labels/Browse | Official source labels beside codes; unknown values preserved; supported type/detail queries documented | Compare matched Studio dates without assuming category-total equality |
| Freshness/history | Fetch/retrieval/observed-date metadata; every fresh Analytics version retained | Confirm migration/history after deployment on a backed-up database |
| Errors | Categories, safe status/reasons, next steps, retry guidance; malformed tables rejected | Observe genuine upstream errors without deliberately breaking auth or quota |
| Unified performance | Separate supported summary, daily coverage, traffic/device reports, optional imported reach | New tool dispatch and real returned sections after deployment |
| Cross-video discovery | Configurable windows; default 28 days each with upload day included after; correlation only | One historical before/after comparison through the new tool |
| Suggested referrals | Owned uploads/windows, top-25 or explicit bulk, metadata, configurable creators, channel/video aggregation | Resolve chosen handles and verify returned source/creator identities live |
| Bulk traffic/reach | Explicit job creation/reuse, on-demand imports, revision-aware storage | Create the two authorized jobs, wait for Google reports, import and inspect real CSVs |
| Audience/CTR research | Supported bulk thumbnail impressions/CTR; deprecated uniques and undocumented returning viewers clearly identified | Verify actual generated reach CSV fields and values |
| Development tracking | Existing issues #1–#8 retain the acceptance criteria | Close/update issues only as reviewed features are released and live-verified |

Retaining all observations grows the persistent database. On-demand bulk imports
also need to happen before Google's download expiry; no scheduler is being
introduced to solve that tradeoff. Keep database and credential backups private.
The code does not delete history, alter OAuth scopes, or deploy itself.
