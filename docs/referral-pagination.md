# Referral pagination investigation — 2026-11-13

Read-only live tests used one owned video and the same date range throughout:
November 6–12, 2026. Each query used `insightTrafficSourceDetail`, the video
filter plus `insightTrafficSourceType==RELATED_VIDEO`, metrics
`views,estimatedMinutesWatched`, and sort `-views`. Responses were refreshed.
Private report rows are not included in this document.

| start_index | max_results | Result |
| --- | --- | --- |
| 1 | 5 | Five rows returned |
| 6 | 5 | Five rows returned |
| 21 | 5 | Five rows returned |
| 25 | 1 | One row returned |
| 26 | 1 | Tool error: `Internal error encountered.` |
| 26 | 25 | Same generic error |
| 2 | 25 | Same generic error |

The successful pages matched the corresponding slices of the 25-row first page
for this sample. Pagination within the first 25 positions works. All tested
requests extending past position 25 failed. This is evidence of a boundary for
this query, not proof of an official universal cap or a specific Google status.
The connector reported `UNAVAILABLE`, which is not itself a Google HTTP status.
Tie ordering can change across requests; these observations do not establish
stable pagination or complete referral coverage for every report.

The channel-report documentation specifies `maxResults <= 25` and labels the
report "Traffic source detail (<= 25 results)." The general query reference
describes 1-based `startIndex` pagination, but does not explicitly explain this
report's boundary behavior. The exact upstream status/reason for the observed
errors remains unknown.

## Agreed coverage behavior

The user explicitly chose to treat this endpoint as a top-25 report and use
bulk Reporting for broader referral analysis. This implementation decision does
not turn the generic errors into proof of a Google error code or universal API
guarantee.

- Validate `start_index + max_results - 1 <= 25` for traffic-source detail.
  Requests crossing the boundary fail before channel or report requests; there
  is no silent shortening. Smaller pages within the top 25 remain available.
- Return `next_start_index: null` at position 25, even for a full page, instead
  of advertising row 26. Earlier full pages can still provide an in-window hint.
- Add a `coverage` object identifying a top-results report, rank limit 25,
  completeness not guaranteed, and bulk Reporting as the broader-coverage source.
- Explain that raw source data may be delayed/suppressed and a short or empty
  result is not proof of completeness. Preserve raw values and requested filters,
  dates, sorting and fetch timestamps.
- Keep other Analytics reports' pagination unchanged. Do not create bulk jobs,
  download CSVs, change Google scopes, or introduce scheduled snapshots.

## Error visibility change

The existing request layer forwarded Google's message but dropped structured
reason codes and omitted the HTTP status from MCP exception text. FastMCP wraps
the exception text in a tool error. Thus a generic Google message left no usable
upstream status in the tool result. The actual live upstream status/reason cannot
be recovered through that existing result.

The local diagnostic change:

- Includes status, source API and available reason codes in exception text.
- Preserves legacy HTTP `error` and `google_status` fields and status mapping;
  adds source, reason list and occurrence timestamp.
- Handles malformed/non-JSON error bodies without losing the HTTP status.
- Keeps bounded error messages and reason codes, never forwards whole bodies or
  request headers, and never caches a failed response or replaces it with zeros.
- Preserves legacy successful fields, authentication and database schema. The
  top-25 coverage changes described above are the only pagination changes.

Tests cover generic 500 messages, canonical statuses and reason fields, malformed
error bodies, bounded fields, absence of raw headers/details, uncached failures,
and both HTTP and MCP propagation. Additional tests cover the 25-row boundary,
smaller pages, empty/short/full reports, original raw data and timestamps, optional
dimensions, unchanged pagination for other reports, and pre-network HTTP/MCP
rejection. The full mocked suite has 28 passing tests.

## Remaining verification

This patch has not been pushed or deployed. After a separately authorized update,
verify that an in-window query still returns raw values and coverage metadata,
and that a request for row 26 fails locally with an actionable top-25 message.
The new validation deliberately prevents another live request past that boundary;
capturing the old upstream error would require a separate diagnostic route and
is not necessary for the agreed top-25 contract. Verify status/reason visibility
using supported requests that encounter genuine Google errors, without exhausting
quota or altering authorization to manufacture failures. Full error categories,
quota/auth distinctions and retry policy remain separate work under issue #4.

References:

- https://developers.google.com/youtube/analytics/channel_reports#traffic-source-detail
- https://developers.google.com/youtube/analytics/reference/reports/query
