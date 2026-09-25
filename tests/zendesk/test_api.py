"""
    uv run python -m unittest tests.zendesk.test_api
"""
import contextlib
import io
import unittest

import requests

from session_ops.shared.testing import FakeResponse, FakeSession
from session_ops.zendesk import api, triage

URL = "https://acme.zendesk.com/api/v2/tickets/7/comments.json"


class TestFetchComments(unittest.TestCase):
    """One fetcher for the three callers that each used to have their own; each
    still asks Zendesk for exactly what it did."""

    def request(self, session):
        method, url, kwargs = session.calls[0]
        return url, kwargs.get("params"), kwargs.get("attempts")

    def test_a_command_reads_the_newest_page_and_stops_the_run_on_failure(self):
        session = FakeSession([FakeResponse({"comments": [{"id": 1}]})])
        self.assertEqual(api.fetch_comments(session, "acme", 7), [{"id": 1}])
        self.assertEqual(self.request(session),
                         (URL, {"per_page": 100, "sort_order": "desc"}, None))
        with self.assertRaises(SystemExit):
            api.fetch_comments(FakeSession([FakeResponse({}, status_code=500)]), "acme", 7)

    def test_a_transcript_reads_oldest_first_on_a_short_budget(self):
        session = FakeSession([FakeResponse({"comments": []})])
        api.conversation_turns(session, "acme", {"id": 7, "requester_id": 5})
        self.assertEqual(self.request(session),
                         (URL, {"per_page": 100, "sort_order": "asc"}, 2))

    def test_hydration_reads_ten_in_zendesks_own_order_on_a_short_budget(self):
        session = FakeSession([FakeResponse({"comments": []})])
        ticket = {"id": 7, "subject": "Conversation with x",
                  "description": "Conversation with x"}
        with contextlib.redirect_stdout(io.StringIO()):
            triage.hydrate_descriptions(session, "acme", [ticket])
        self.assertEqual(self.request(session), (URL, {"per_page": 10}, 2))

    def test_an_optional_read_notes_every_failure_and_returns_none(self):
        for response in (FakeResponse({}, status_code=503), requests.ConnectionError("down")):
            with self.subTest(response=type(response).__name__):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertIsNone(api.fetch_comments(FakeSession([response]), "acme", 7,
                                                         optional=True))
                self.assertIn("#7", out.getvalue())


if __name__ == "__main__":
    unittest.main()
