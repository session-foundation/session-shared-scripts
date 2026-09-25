"""
    uv run python -m unittest tests.crowdin.test_report_multiple_translations
"""
import contextlib
import io
import unittest

from unittest import mock

from session_ops.crowdin import report_multiple_translations as report
from session_ops.crowdin import sdk
from session_ops.shared.testing import FakeResponse, FakeSession, Patched


class WebhookSession(FakeSession):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCrowdinClient(unittest.TestCase):
    def test_a_client_error_raises_rather_than_returning(self):
        """Callers read the body straight off the response, so a 4xx has to stop them."""
        client = sdk.client("t", 1, session=FakeSession([FakeResponse({}, status_code=404)]))
        with self.assertRaises(sdk.APIException):
            client.projects.get_project()

    def test_crowdins_budget_is_ten_attempts_at_sixty_seconds(self):
        """A locale is ~1,400 requests near the rate limit: 429s are expected."""
        with mock.patch.object(report.sdk.http, "Session") as made:
            client = report.crowdin_client("t", 1)
            client.get_api_requestor().session.request("GET", "https://x")
        self.assertEqual(made.call_args.kwargs["attempts"], 10)
        self.assertEqual(made.call_args.kwargs["timeout"], 60)


class TestPostToDiscord(unittest.TestCase):
    def post(self, responses, messages):
        session = WebhookSession(responses)
        with Patched(report.http, Session=lambda: session), \
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
        session = WebhookSession([FakeResponse({}, status_code=400), FakeResponse({}, status_code=204)])
        with Patched(report.http, Session=lambda: session), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            report.post_to_discord("https://hook", [{"embeds": [{"title": "x" * 9000}]}])
        self.assertEqual(list(session.calls[1][2]["json"]), ["content"])


if __name__ == "__main__":
    unittest.main()
