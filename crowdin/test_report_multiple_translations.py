"""
    cd crowdin && python -m unittest discover
"""
import contextlib
import io
import os
import sys
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report_multiple_translations as report  # noqa: E402
from shared.testing import FakeResponse, FakeSession, NoSleep, Patched  # noqa: E402


class TestRequestWithRetry(unittest.TestCase):
    def test_a_client_error_raises_rather_than_returning(self):
        """Callers read the body straight off the response, so a 4xx has to stop them."""
        with self.assertRaises(requests.HTTPError):
            report.request_with_retry(FakeSession([FakeResponse({}, status_code=404)]),
                                      "GET", "https://x")

    def test_crowdins_budget_is_ten_attempts_at_sixty_seconds(self):
        session = FakeSession([FakeResponse({}, status_code=503)] * 9 + [FakeResponse({"ok": 1})])
        with NoSleep():
            self.assertEqual(report.request_with_retry(session, "GET", "https://x").json(), {"ok": 1})
        self.assertEqual(len(session.calls), 10)
        self.assertEqual({kw["timeout"] for _, _, kw in session.calls}, {60})


class TestPostToDiscord(unittest.TestCase):
    def post(self, responses, messages):
        session = FakeSession(responses)
        with Patched(report.requests, Session=lambda: session), \
                contextlib.redirect_stdout(io.StringIO()):
            report.post_to_discord("https://hook", messages)
        return session

    def test_every_message_accepted_posts_nothing_else(self):
        session = self.post([FakeResponse({}, status_code=204)] * 2, [{"embeds": []}] * 2)
        self.assertEqual(len(session.calls), 2)

    def test_a_rejection_posts_a_plain_warning_then_exits(self):
        responses = [FakeResponse({}, status_code=204), FakeResponse({}, status_code=400),
                     FakeResponse({}, status_code=204)]
        with self.assertRaises(SystemExit) as caught:
            self.post(responses, [{"embeds": []}] * 3)
        self.assertIn("1 of 3", str(caught.exception))

    def test_the_warning_is_plain_content_the_webhook_cannot_reject_for_size(self):
        session = FakeSession([FakeResponse({}, status_code=400), FakeResponse({}, status_code=204)])
        with Patched(report.requests, Session=lambda: session), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            report.post_to_discord("https://hook", [{"embeds": [{"title": "x" * 9000}]}])
        self.assertEqual(list(session.calls[1][2]["json"]), ["content"])


if __name__ == "__main__":
    unittest.main()
