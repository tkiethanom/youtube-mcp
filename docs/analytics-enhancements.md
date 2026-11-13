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
non-daily periods and files larger than 64 MiB or one million rows. Unknown CSV
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

Verification: the full suite now has 42 passing tests. Bulk fixtures verify
idempotent creation, pagination safety, archive/revision selection, malformed CSV
rollback, empty reports, size limits and credential destination restrictions.

Additional references:

- https://developers.google.com/youtube/reporting/v1/reference/rest/v1/jobs/create
- https://developers.google.com/youtube/reporting/v1/reports
- https://developers.google.com/youtube/reporting/v1/reports/channel_reports#reach-basic
