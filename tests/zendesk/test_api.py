"""
    uv run python -m unittest tests.zendesk.test_api
"""
import contextlib
import io
import unittest

import requests

from session_ops.shared.testing import FakeResponse, FakeSession, NonJsonResponse
from session_ops.zendesk import api, triage

URL = "https://acme.zendesk.com/api/v2/tickets/7/comments.json"


class TestFetchUser(unittest.TestCase):
    def test_an_unreachable_zendesk_is_an_unknown_author_not_an_error(self):
        """An unreachable Zendesk is an unknown author, which counts as a customer."""
        self.assertEqual(api.fetch_user(FakeSession([requests.ConnectionError("down")]),
                                        "acme", 5), {})

    def test_an_unreadable_body_is_an_unknown_author(self):
        self.assertEqual(api.fetch_user(FakeSession([NonJsonResponse()]), "acme", 5), {})

    def test_a_user_is_read(self):
        session = FakeSession([FakeResponse({"user": {"role": "agent"}})])
        self.assertEqual(api.fetch_user(session, "acme", 5), {"role": "agent"})


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


class TestHasOpenChat(unittest.TestCase):
    def audits(self, *events_per_audit):
        return FakeSession([FakeResponse({"audits": [
            {"events": [{"type": t} for t in events]} for events in events_per_audit]})])

    def test_the_newest_chat_event_decides(self):
        cases = {
            "started, not ended": (["Change", "ChatStartedEvent"], [], True),
            "ended after it started": (["ChatEndedEvent", "Change"], ["ChatStartedEvent"], False),
            "never a chat": (["Change"], [], False),
        }
        for name, (newest, older, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(api.has_open_chat(self.audits(newest, older), "acme", 7),
                              expected)

    def test_within_one_audit_the_last_event_is_the_newest(self):
        session = self.audits(["ChatStartedEvent", "ChatEndedEvent"])
        self.assertFalse(api.has_open_chat(session, "acme", 7))

    def test_audits_are_read_newest_first(self):
        session = self.audits([])
        api.has_open_chat(session, "acme", 7)
        _, url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/tickets/7/audits.json"))
        self.assertEqual(kwargs["params"]["sort_order"], "desc")

    def test_an_unreadable_history_is_unknown(self):
        for response in (FakeResponse({}, status_code=500), NonJsonResponse(),
                         requests.ConnectionError("down")):
            with self.subTest(response=type(response).__name__):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(api.has_open_chat(FakeSession([response]), "acme", 7))

    def test_only_sunshine_channels_are_messaging(self):
        self.assertTrue(api.is_messaging(
            {"via": {"channel": "sunshine_conversations_twitter_dm"}}))
        self.assertFalse(api.is_messaging({"via": {"channel": "email"}}))
        self.assertFalse(api.is_messaging({}))


if __name__ == "__main__":
    unittest.main()
