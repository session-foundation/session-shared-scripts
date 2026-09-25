"""
    uv run python -m unittest tests.crowdin.test_sdk
"""
import threading
import unittest
from unittest import mock

from crowdin_api.exceptions import APIException

from session_ops.crowdin import sdk
from session_ops.shared.testing import FakeResponse, FakeSession, RecordedSession

API = "https://api.crowdin.com/api/v2"


class TestClient(unittest.TestCase):
    def test_the_sdks_own_retry_loop_is_off(self):
        """It retries 5xx on a fixed 100 ms and never a 429; shared.http does both."""
        self.assertEqual(sdk.client("t", 1).get_api_requestor()._max_retries, 1)

    def test_requests_carry_the_token_to_an_injected_session(self):
        session = FakeSession([FakeResponse({"data": {"id": 1}})])
        sdk.client("tok", 1, session=session).projects.get_project()
        self.assertEqual(session.headers["Authorization"], "Bearer tok")
        method, url, _ = session.calls[0]
        self.assertEqual((method, url), ("get", f"{API}/projects/1"))

    def test_each_thread_gets_its_own_session_with_the_same_headers(self):
        """requests.Session is not guaranteed thread-safe, and the SDK shares one."""
        made = []

        class Made(FakeSession):
            def __init__(self, **kwargs):
                super().__init__([FakeResponse({})] * 2)
                made.append(self)

        with mock.patch.object(sdk.http, "Session", Made):
            shared = sdk.client("tok", 1).get_api_requestor().session
            shared.request("GET", "https://x")
            shared.request("GET", "https://x")
            other = threading.Thread(target=shared.request, args=("GET", "https://x"))
            other.start()
            other.join()
        self.assertEqual(len(made), 2)
        self.assertEqual({s.headers["Authorization"] for s in made}, {"Bearer tok"})

    def test_every_thread_draws_on_one_rate_limit(self):
        shared = sdk.client("tok", 1).get_api_requestor().session
        self.assertIs(shared._make().limiter, shared._make().limiter)


class TestFetchAll(unittest.TestCase):
    def test_pages_until_a_short_page_and_unwraps_each_row(self):
        rows = [{"data": {"id": i}} for i in range(500)]
        session = RecordedSession([
            {"method": "get", "url": f"{API}/projects/1/strings",
             "params": {"limit": 500, "offset": 0}, "response": {"json": {"data": rows}}},
            {"method": "get", "url": f"{API}/projects/1/strings",
             "params": {"limit": 500, "offset": 500},
             "response": {"json": {"data": [{"data": {"id": 500}}]}}},
        ])
        client = sdk.client("t", 1, session=session)
        ids = [row["id"] for row in sdk.fetch_all(client.source_strings, "list_strings")]
        self.assertEqual(ids, list(range(501)))


class TestErrorMessage(unittest.TestCase):
    def error(self, body):
        return APIException(http_status=400, context=body)

    def test_crowdins_envelope(self):
        self.assertEqual(sdk.error_message(self.error(b'{"error": {"message": "Nope"}}')), "Nope")

    def test_a_body_that_is_not_the_envelope_is_quoted(self):
        self.assertEqual(sdk.error_message(self.error(b"<html>502</html>")), "<html>502</html>")

    def test_an_empty_body(self):
        self.assertEqual(sdk.error_message(self.error(b"")), "Unknown error")


if __name__ == "__main__":
    unittest.main()
