import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from shelf.app import create_app
from shelf.config import Settings
from shelf.store import Store
from shelf.youtube import GoogleAPIError, YouTube


def http_error(status=500, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload if payload is not None else {
        "error": {"message": "Internal error encountered.",
                  "errors": [{"reason": "backendError"}], "status": "INTERNAL"}}
    return result


class GoogleErrorTests(unittest.TestCase):
    def test_upstream_status_and_reason_survive_generic_message(self):
        error = GoogleAPIError.from_response(http_error(), "analytics")
        self.assertEqual(error.status, 500)
        self.assertEqual(error.message, "Internal error encountered.")
        self.assertEqual(error.reasons, ("backendError", "INTERNAL"))
        self.assertIn("status=500; source=analytics; reason=backendError,INTERNAL", str(error))
        self.assertIsNotNone(datetime.fromisoformat(error.occurred_at).tzinfo)

    def test_malformed_and_non_json_error_responses_keep_status(self):
        for payload in [[], "not an object", {"error": None}, {"error": []},
                        {"error": {"message": {}, "errors": None}},
                        {"error": {"errors": [None, "bad", {"reason": []}]}}]:
            with self.subTest(payload=payload):
                error = GoogleAPIError.from_response(http_error(403, payload), "data")
                self.assertEqual(error.status, 403)
                self.assertEqual(error.message, "Google rejected the request")
                self.assertEqual(error.reasons, ())
        result = http_error(502)
        result.json.side_effect = ValueError("not JSON")
        self.assertEqual(GoogleAPIError.from_response(result, "analytics").status, 502)

    def test_only_bounded_error_fields_are_exposed(self):
        payload = {"request_headers": {"Authorization": "Bearer PRIVATE_HEADER"},
                   "error": {"message": "x" * 1500,
                             "errors": [{"reason": "quotaExceeded"}, {"reason": "quotaExceeded"},
                                        {"reason": "Bearer PRIVATE_REASON"}, {"reason": "x" * 101}],
                             "details": "PRIVATE_DETAILS", "status": "RESOURCE_EXHAUSTED"}}
        error = GoogleAPIError.from_response(http_error(403, payload), "analytics")
        self.assertEqual(len(error.message), 1000)
        self.assertEqual(error.reasons, ("quotaExceeded", "RESOURCE_EXHAUSTED"))
        self.assertNotIn("PRIVATE_", str(error))

    def test_request_failure_is_not_cached_or_recorded_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = Store(directory / "test.sqlite3")
            yt = YouTube(SimpleNamespace(data=directory, channel_id="owned"), store)
            (directory / "youtube-token.json").write_text("{}")
            with patch("shelf.youtube.Credentials.from_authorized_user_file",
                       return_value=Mock(valid=True, token="test-only")), patch(
                           "shelf.youtube.requests.get", return_value=http_error()) as get:
                for _ in range(2):
                    with self.assertRaises(GoogleAPIError) as caught:
                        yt.request("analytics", "reports", {"startIndex": 26})
                    self.assertEqual(caught.exception.source, "analytics")
                self.assertEqual(get.call_count, 2)
            with store.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM kv WHERE kind='cache'").fetchone()[0], 0)

    def test_http_legacy_fields_and_mcp_error_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp), "https://youtube.example.com", "123", "owned",
                                "test-client", "test-secret", ["https://client.example.com/callback"])
            previous = os.umask(0o077)
            try:
                app = create_app(settings)
            finally:
                os.umask(previous)
            error = GoogleAPIError.from_response(http_error(), "analytics")
            app.state.youtube.analytics = Mock(side_effect=error)
            app.state.provider.load_access_token = AsyncMock(
                return_value=SimpleNamespace(scopes=["youtube:read"]))
            query = {"start_date": "2025-01-01", "end_date": "2025-01-02"}
            with TestClient(app, base_url=settings.public_url) as client:
                result = client.post("/api/analytics", json=query,
                                     headers={"Authorization": "Bearer test-token"})
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json()["error"], "Internal error encountered.")
                self.assertEqual(result.json()["google_status"], 500)
                self.assertEqual(result.json()["google_reasons"], ["backendError", "INTERNAL"])
                self.assertEqual(result.json()["source"], "analytics")
                self.assertEqual(result.json()["occurred_at"], error.occurred_at)
            with self.assertRaisesRegex(ToolError, "status=500; source=analytics; reason=backendError,INTERNAL"):
                asyncio.run(app.state.mcp.call_tool("query_analytics", {"query": query}))


if __name__ == "__main__":
    unittest.main()
