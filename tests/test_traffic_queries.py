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
        params["start_index"] = 26
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
        self.assertEqual(sent[2]["maxResults"], 25)
        self.assertEqual(sent[2]["startIndex"], 26)
        self.assertEqual(sent[2]["filters"], params["filters"])
        self.assertEqual(sent[2]["metrics"], params["metrics"])
        self.assertTrue(sent[3])
        self.assertIs(result["data"], raw)
        self.assertEqual(result["records"][0]["estimatedMinutesWatched"], 2.5)

    def test_invalid_detail_queries_fail_with_actionable_messages(self):
        cases = [({"max_results": 26}, "max_results <= 25"),
                 ({"sort": ""}, "requires sort="),
                 ({"sort": "views"}, "requires sort="),
                 ({"sort": "-likes"}, "requires sort="),
                 ({"sort": "-estimatedMinutesWatched", "metrics": "views"}, "included in metrics"),
                 ({"metrics": "views,averageViewDuration"}, "supports metrics"),
                 ({"metrics": "views,subscribersGained"}, "supports metrics"),
                 ({"filters": "video==abcdefghijk"}, "requires an insightTrafficSourceType filter")]
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, message):
                AnalyticsQuery(**(detail() | changes))

    def test_watch_time_sort_and_optional_documented_dimension(self):
        params = detail() | {"sort": "-estimatedMinutesWatched",
                             "dimensions": "insightTrafficSourceDetail,creatorContentType",
                             "metrics": "views,estimatedMinutesWatched,engagedViews"}
        self.assertEqual(AnalyticsQuery(**params).sort, "-estimatedMinutesWatched")

    def test_other_reports_keep_existing_defaults(self):
        query = AnalyticsQuery(start_date="2025-01-01", end_date="2025-01-31")
        self.assertEqual(query.max_results, 200)
        self.assertEqual(query.sort, "")
        self.assertIn("averageViewDuration", query.metrics)

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
                valid = client.post("/api/analytics", headers=headers, json=detail())
                self.assertEqual(valid.status_code, 200)
                self.assertEqual(valid.json(), {"records": []})
                app.state.youtube.analytics.assert_called_once()


if __name__ == "__main__":
    unittest.main()
