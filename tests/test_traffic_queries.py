import asyncio
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from shelf.youtube import AnalyticsQuery, YouTube


def detail():
    return dict(start_date="2025-01-01", end_date="2025-01-31",
                dimensions="insightTrafficSourceDetail",
                filters="video==abcdefghijk;insightTrafficSourceType==RELATED_VIDEO",
                metrics="views,estimatedMinutesWatched", sort="-views",
                max_results=25)


class TrafficQueryTests(unittest.TestCase):
    def test_confirmed_referral_query_and_pagination_are_forwarded_unchanged(self):
        params = detail()
        params.update(start_index=6, max_results=5)
        query = AnalyticsQuery(**params)
        yt = YouTube(Mock(channel_id="owned"), Mock())
        yt.channel = Mock()
        raw = {"columnHeaders": [{"name": "insightTrafficSourceDetail"},
                                {"name": "views"}, {"name": "estimatedMinutesWatched"}],
               "rows": [["referring-id", 10, 2.5]]}
        yt.request = Mock(return_value={"data": raw, "cached": False, "fetched_at": "sample-time"})
        result = yt.analytics(query, True)
        sent = yt.request.call_args.args
        self.assertEqual(sent[:2], ("analytics", "reports"))
        self.assertEqual(sent[2]["maxResults"], 5)
        self.assertEqual(sent[2]["startIndex"], 6)
        self.assertEqual(sent[2]["filters"], params["filters"])
        self.assertEqual(sent[2]["metrics"], params["metrics"])
        self.assertTrue(sent[3])
        self.assertIs(result["data"], raw)
        self.assertEqual(result["records"][0]["estimatedMinutesWatched"], 2.5)

    def test_invalid_detail_queries_fail_with_actionable_messages(self):
        cases = [({"max_results": 26}, "max_results <= 25"),
                 ({"start_index": 26, "max_results": 1}, "top-25 report"),
                 ({"start_index": 2, "max_results": 25}, "top-25 report"),
                 ({"start_index": 25, "max_results": 2}, "bulk reports for broader coverage"),
                 ({"sort": ""}, "requires sort="),
                 ({"sort": "views"}, "requires sort="),
                 ({"sort": "-likes"}, "requires sort="),
                 ({"sort": "-estimatedMinutesWatched", "metrics": "views"}, "included in metrics"),
                 ({"filters": "video==abcdefghijk"}, "requires an insightTrafficSourceType filter")]
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, message):
                AnalyticsQuery(**(detail() | changes))

    def test_watch_time_sort_and_optional_documented_dimension(self):
        params = detail() | {"sort": "-estimatedMinutesWatched",
                             "dimensions": "insightTrafficSourceDetail,creatorContentType",
                             "metrics": "views,estimatedMinutesWatched,engagedViews"}
        self.assertEqual(AnalyticsQuery(**params).sort, "-estimatedMinutesWatched")

    def test_live_supported_average_duration_is_not_blocked_by_catalog_omission(self):
        params = detail() | {"metrics": "views,estimatedMinutesWatched,averageViewDuration"}
        self.assertEqual(AnalyticsQuery(**params).metrics, params["metrics"])

    def test_other_reports_keep_existing_defaults(self):
        query = AnalyticsQuery(start_date="2025-01-01", end_date="2025-01-31")
        self.assertEqual(query.max_results, 200)
        self.assertEqual(query.sort, "")
        self.assertIn("averageViewDuration", query.metrics)

    def test_top_25_boundary_and_coverage_without_rewriting_raw_data(self):
        yt = YouTube(Mock(channel_id="owned"), Mock())
        yt.channel = Mock()
        for start, limit, count, expected_next in [
                (1, 25, 25, None), (1, 5, 5, 6), (21, 5, 5, None),
                (25, 1, 1, None), (1, 25, 4, None), (1, 25, 0, None)]:
            with self.subTest(start=start, limit=limit, count=count):
                raw = {"columnHeaders": [{"name": "insightTrafficSourceDetail"},
                                         {"name": "views"}],
                       "rows": [[f"referrer-{i}", 10] for i in range(count)]}
                yt.request = Mock(return_value={"data": raw, "cached": True,
                                                "fetched_at": "original-fetch"})
                query = AnalyticsQuery(**(detail() | {"start_index": start, "max_results": limit}))
                result = yt.analytics(query)
                self.assertIs(result["data"], raw)
                self.assertEqual(result["next_start_index"], expected_next)
                self.assertEqual(len(result["records"]), count)
                self.assertEqual(result["coverage"]["rank_limit"], 25)
                self.assertEqual(result["coverage"]["completeness"], "not_guaranteed")
                self.assertIn("bulk traffic-source reports", result["note"])
                self.assertEqual(result["fetched_at"], "original-fetch")
                self.assertTrue(result["cached"])

    def test_other_reports_can_still_paginate_past_25(self):
        yt = YouTube(Mock(channel_id="owned"), Mock())
        yt.channel = Mock()
        yt.request = Mock(return_value={"data": {"columnHeaders": [{"name": "views"}],
                                                 "rows": [[1]] * 25}})
        query = AnalyticsQuery(start_date="2025-01-01", end_date="2025-01-31",
                               start_index=26, max_results=25)
        result = yt.analytics(query)
        self.assertEqual(result["next_start_index"], 51)
        self.assertNotIn("coverage", result)

    def test_boundary_rule_also_applies_with_optional_dimension(self):
        with self.assertRaisesRegex(ValidationError, "top-25 report"):
            AnalyticsQuery(**(detail() | {"dimensions": "insightTrafficSourceDetail,creatorContentType",
                                          "start_index": 26, "max_results": 1}))

    def test_mcp_rejects_invalid_query_before_google_access(self):
        import os
        import tempfile
        from pathlib import Path
        from shelf.app import create_app
        from shelf.config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp), "https://youtube.example.com", "123", "owned",
                                "test-client", "test-secret", ["https://client.example.com/callback"])
            previous = os.umask(0o077)
            try:
                app = create_app(settings)
            finally:
                os.umask(previous)
            app.state.youtube.analytics = Mock()
            with self.assertRaisesRegex(Exception, "max_results <= 25"):
                asyncio.run(app.state.mcp.call_tool("query_analytics", {
                    "query": detail() | {"max_results": 200}}))
            app.state.youtube.analytics.assert_not_called()

            with self.assertRaisesRegex(Exception, "top-25 report"):
                asyncio.run(app.state.mcp.call_tool("query_analytics", {
                    "query": detail() | {"start_index": 26, "max_results": 1}}))
            app.state.youtube.analytics.assert_not_called()

            from fastapi.testclient import TestClient
            from types import SimpleNamespace
            from unittest.mock import AsyncMock
            app.state.provider.load_access_token = AsyncMock(
                return_value=SimpleNamespace(scopes=["youtube:read"]))
            app.state.youtube.analytics.return_value = {"records": []}
            with TestClient(app, base_url=settings.public_url) as client:
                headers = {"Authorization": "Bearer test-token"}
                invalid = client.post("/api/analytics", headers=headers,
                                      json=detail() | {"max_results": 200})
                self.assertEqual(invalid.status_code, 422)
                self.assertIn("max_results <= 25", invalid.text)
                app.state.youtube.analytics.assert_not_called()
                beyond = client.post("/api/analytics", headers=headers,
                                     json=detail() | {"start_index": 26, "max_results": 1})
                self.assertEqual(beyond.status_code, 422)
                self.assertIn("top-25 report", beyond.text)
                app.state.youtube.analytics.assert_not_called()
                valid = client.post("/api/analytics", headers=headers, json=detail())
                self.assertEqual(valid.status_code, 200)
                self.assertEqual(valid.json(), {"records": []})
                app.state.youtube.analytics.assert_called_once()


if __name__ == "__main__":
    unittest.main()
