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
