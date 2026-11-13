import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shelf.analytics_support import annotate_records, freshness, watch_time_diagnostics
from shelf.store import Store
from shelf.youtube import AnalyticsQuery, GoogleAPIError, YouTube


class IntegrityTests(unittest.TestCase):
    def test_raw_codes_and_unknown_values_survive_labeling(self):
        raw = [{"insightTrafficSourceType": "SUBSCRIBER", "views": 5},
               {"insightTrafficSourceType": "NEW_SOURCE", "views": 3}]
        records = annotate_records(raw)
        self.assertIn("Browse", records[0]["insightTrafficSourceTypeLabel"])
        self.assertEqual(records[0]["insightTrafficSourceType"], "SUBSCRIBER")
        self.assertIn("NEW_SOURCE", records[1]["insightTrafficSourceTypeLabel"])
        self.assertNotIn("insightTrafficSourceTypeLabel", raw[0])

    def test_reporting_coverage_is_observed_not_requested(self):
        info = freshness("analytics", "fetch", records=[{"day": "2025-01-02"}, {"day": "2025-01-01"}])
        self.assertEqual(info["latest_observed_reporting_date"], "2025-01-02")
        self.assertIn("this_page", info["coverage_basis"])
        self.assertIsNone(freshness("analytics", "fetch", records=[{"views": 10}])["latest_observed_reporting_date"])
        self.assertIsNone(freshness("data", "fetch")["reporting_timezone"])

    def test_discrepancy_is_reported_without_changing_values(self):
        row = {"views": 100, "engagedViews": 20, "estimatedMinutesWatched": 10, "averageViewDuration": 30}
        result = watch_time_diagnostics([row])
        self.assertFalse(result["identity_assumed"])
        self.assertEqual(result["comparisons"][0]["minutes_from_views_times_duration"], 50)
        self.assertEqual(result["comparisons"][0]["minutes_from_engaged_views_times_duration"], 10)
        self.assertEqual(row["estimatedMinutesWatched"], 10)
        self.assertEqual(watch_time_diagnostics([{"views": None}])["comparisons"], [])

    def test_archive_retains_every_fetch_but_not_cache_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = Store(directory / "store.sqlite3")
            yt = YouTube(SimpleNamespace(data=directory, channel_id="owned"), store)
            yt.channel = Mock()
            (directory / "youtube-token.json").write_text("{}")
            response = Mock(status_code=200)
            response.json.return_value = {"columnHeaders": [{"name": "day"}, {"name": "views"}],
                                          "rows": [["2025-01-01", 5]]}
            query = AnalyticsQuery(start_date="2025-01-01", end_date="2025-01-02", metrics="views")
            with patch("shelf.youtube.Credentials.from_authorized_user_file", return_value=Mock(valid=True, token="test-only")), patch(
                    "shelf.youtube.requests.get", return_value=response) as get:
                a = yt.analytics(query, True)
                cached = yt.analytics(query)
                response.json.return_value["rows"][0][1] = 6
                b = yt.analytics(query, True)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(a["observation_id"], cached["observation_id"])
            self.assertEqual(a["fetched_at"], cached["fetched_at"])
            self.assertNotEqual(a["observation_id"], b["observation_id"])
            store.clear_auth()
            store.put("cache", "expired", {}, -1)
            store.put("cache", "cleanup", {}, 1)
            history = Store(directory / "store.sqlite3").analytics_history("owned", yt.analytics_params(query))
            self.assertEqual([r["response"]["data"]["rows"][0][1] for r in history], [6, 5])
            first = yt.analytics_history(query, limit=1)
            second = yt.analytics_history(query, before_id=first["next_before_id"], limit=1)
            self.assertNotEqual(first["observations"][0]["observation_id"], second["observations"][0]["observation_id"])
            self.assertEqual(store.analytics_history("other", yt.analytics_params(query)), [])

    def test_actionable_error_categories(self):
        cases = [(400, (), "invalid_query", False), (401, (), "authorization", False),
                 (403, ("quotaExceeded",), "quota", False),
                 (403, ("rateLimitExceeded",), "rate_limit", True),
                 (403, ("accessNotConfigured",), "configuration", False),
                 (404, (), "unavailable_data", False), (429, (), "rate_limit", True),
                 (503, (), "upstream", True)]
        for status, reasons, category, retryable in cases:
            with self.subTest(status=status, reasons=reasons):
                error = GoogleAPIError(status, "message", source="analytics", reasons=reasons)
                self.assertEqual(error.as_dict()["category"], category)
                self.assertEqual(error.retryable, retryable)
                self.assertIn(error.action, str(error))

    def test_retry_after_is_optional_and_bounded(self):
        response = Mock(status_code=429, headers={"Retry-After": "30"})
        response.json.return_value = {"error": {"message": "Try later"}}
        self.assertEqual(GoogleAPIError.from_response(response, "analytics").retry_after_seconds, 30)
        response.headers["Retry-After"] = "untrusted"
        self.assertIsNone(GoogleAPIError.from_response(response, "analytics").retry_after_seconds)

    def test_malformed_report_table_is_not_silently_zipped_or_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt = YouTube(SimpleNamespace(data=Path(tmp), channel_id='owned'), Mock())
            yt.channel = Mock()
            yt.request = Mock(return_value={'data': {'columnHeaders': [{'name': 'views'}], 'rows': [[1, 2]]}})
            with self.assertRaisesRegex(GoogleAPIError, 'not silently truncated'):
                yt.analytics(AnalyticsQuery(start_date='2025-01-01', end_date='2025-01-02'))
