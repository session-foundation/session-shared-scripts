#!/usr/bin/env python3
"""Tests for the digest's unprompted drafts from briefs support already sent.

Stdlib unittest, offline, same stubs as the rest. Run from anywhere:

    uv run python -m unittest tests.zendesk.test_auto_draft
"""
import os
import tempfile
import unittest
from datetime import date

from session_ops.zendesk import auto_draft, claude_api, note_reply, triage
from tests.zendesk.test_note_reply import (
    AGENT, API_USER, GERMAN, comment, draft_note, fake_session, posted_note)
from tests.zendesk.test_triage import FakeResponse, Patched

TODAY = date(2026, 11, 2)
CUSTOMER = 42
BOOK = {"groups": [{"key": "calls", "title": "Calls failing"}],
        "cells": {"calls|any": {"answer": "Ask for logs.", "n": 30,
                                "consistency": "high"}}}


def public(body, author=CUSTOMER, cid=1, at="2026-10-01T00:00:00Z"):
    return dict(comment(body, author=author, public=True, cid=cid), created_at=at)


def cmd(body, cid):
    return dict(comment(body, author=AGENT, cid=cid), created_at="2026-10-01T00:00:00Z")


def cache(*entries):
    return {"version": auto_draft.CACHE_VERSION, "scanned_through": None,
            "briefs": {str(tid): {"group": group, "platform": "unknown", "brief": brief,
                                  "sent_at": at}
                       for tid, group, brief, at in entries}}


class SentFirstBrief(unittest.TestCase):
    def test_an_unamended_brief_that_was_sent_is_known(self):
        found = auto_draft.sent_first_brief(
            [public("help"), cmd("claude: draft - need the logs", 2), cmd("claude: reply 1", 3),
             public("Please send logs", author=API_USER, cid=4, at="2026-10-02T00:00:00Z")],
            API_USER)
        self.assertEqual(found, ("need the logs", "2026-10-02T00:00:00Z"))

    def test_an_amended_brief_is_not_an_answer(self):
        """The second brief is a change to the first draft, not an answer on its own."""
        self.assertIsNone(auto_draft.sent_first_brief(
            [public("help"), cmd("claude: draft - need the logs", 2),
             cmd("claude: draft - also ask the version", 3), cmd("claude: reply", 4),
             public("Please send logs", author=API_USER, cid=5)], API_USER))

    def test_a_draft_nobody_sent_is_not_an_answer(self):
        self.assertIsNone(auto_draft.sent_first_brief(
            [public("help"), cmd("claude: draft - need the logs", 2)], API_USER))

    def test_a_follow_up_brief_is_not_a_first_answer(self):
        self.assertIsNone(auto_draft.sent_first_brief(
            [public("help"), public("first reply", author=API_USER, cid=2),
             cmd("claude: draft - thanks for the logs", 3), cmd("claude: reply", 4),
             public("second reply", author=API_USER, cid=5)], API_USER))


class Candidates(unittest.TestCase):
    def test_the_same_brief_is_merged_and_counted_most_used_first(self):
        rows = auto_draft.candidates(cache(
            (1, "calls", "need the logs", "2026-10-01T00:00:00Z"),
            (2, "calls", "Need  the logs", "2026-10-05T00:00:00Z"),
            (3, "calls", "we have no ETA", "2026-10-20T00:00:00Z"),
            (4, "other", "unrelated", "2026-10-20T00:00:00Z")), "calls")
        self.assertEqual([(r["uses"], r["source"]) for r in rows], [(2, "2"), (1, "3")])

    def test_old_briefs_age_out_and_the_scan_moves_on(self):
        stale = cache((1, "calls", "old", "2026-01-01T00:00:00Z"),
                      (2, "calls", "new", "2026-10-01T00:00:00Z"))
        stale["scanned_through"] = "2026-10-30"
        session = fake_session(FakeResponse({"results": [], "count": 0, "next_page": None}))
        refreshed = auto_draft.refresh_cache(session, "sub", API_USER, stale, TODAY)
        self.assertEqual(list(refreshed["briefs"]), ["2"])
        self.assertEqual(refreshed["scanned_through"], "2026-10-31")
        self.assertIn("updated>2026-10-30", str(session.calls[0]))

    def test_an_unreadable_cache_starts_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.json")
            with open(path, "w") as handle:
                handle.write("{not json")
            self.assertEqual(auto_draft.load_cache(path)["briefs"], {})


class Eligible(unittest.TestCase):
    def test_an_unanswered_ticket_is_eligible(self):
        self.assertTrue(auto_draft.eligible({"tags": []}, [public("help")], {CUSTOMER},
                                            API_USER))

    def test_a_ticket_support_answered_is_not(self):
        self.assertFalse(auto_draft.eligible(
            {"tags": []}, [public("reply", author=API_USER, cid=2), public("help")],
            {CUSTOMER}, API_USER))

    def test_a_ticket_an_agent_is_already_on_is_not(self):
        self.assertFalse(auto_draft.eligible(
            {"tags": []}, [cmd("claude: english", 2), public("help")], {CUSTOMER},
            API_USER))

    def test_a_ticket_already_drafted_is_not(self):
        self.assertFalse(auto_draft.eligible(
            {"tags": [note_reply.TAG_DRAFTED]}, [public("help")], {CUSTOMER}, API_USER))


class DraftTicket(unittest.TestCase):
    TICKET = {"id": 7, "requester_id": CUSTOMER, "subject": "calls fail",
              "tags": ["grp-calls", "plat-android"]}
    KNOWN = cache((99, "calls", "need the logs of both devices", "2026-10-20T00:00:00Z"))

    def run_draft(self, choice, dry_run=False):
        seen = {}

        def fake_compose(sample, brief, previous=None, precedent=None, thread=None):
            seen.update(brief=brief, previous=previous, precedent=precedent)
            return GERMAN

        session = fake_session(FakeResponse({"comments": [public("Anrufe gehen nicht")]}),
                               FakeResponse({"ticket": {}}))
        with Patched(claude_api, claude_json=lambda *a, **k: {"choice": choice,
                                                              "reason": "x"}), \
                Patched(note_reply, compose=fake_compose):
            source = auto_draft.draft_ticket(session, "sub", self.TICKET, self.KNOWN,
                                             BOOK, API_USER, TODAY, dry_run)
        return source, seen, session

    def test_a_known_brief_is_drafted_as_a_private_note(self):
        source, seen, session = self.run_draft(1)
        self.assertEqual(source, "99")
        self.assertEqual(seen["brief"], "need the logs of both devices")
        self.assertIsNone(seen["previous"])
        self.assertIn("Ask for logs.", seen["precedent"])
        writes = [kw["json"] for m, u, kw in session.calls if m == "PUT"]
        note = writes[0]["ticket"]["comment"]
        self.assertIs(note["public"], False)
        self.assertNotIn("status", writes[0]["ticket"])
        self.assertIn(note_reply.auto_marker("99"), note["html_body"])
        self.assertIn("Nobody has reviewed it yet", note["html_body"])
        self.assertIn(note_reply.TAG_DRAFTED, writes[1]["tags"])

    def test_no_matching_brief_writes_nothing(self):
        source, seen, session = self.run_draft(0)
        self.assertIsNone(source)
        self.assertEqual(seen, {})
        self.assertFalse([c for c in session.calls if c[0] != "GET"])

    def test_a_dry_run_writes_nothing(self):
        source, _, session = self.run_draft(1, dry_run=True)
        self.assertEqual(source, "99")
        self.assertFalse([c for c in session.calls if c[0] != "GET"])


class StandardReply(unittest.TestCase):
    TICKET = {"id": 8, "requester_id": CUSTOMER, "subject": "Police Request",
              "tags": ["grp-law-enforcement-requests"]}

    def run_draft(self):
        def no_claude(*a, **k):
            raise AssertionError("a standard reply is never composed")
        session = fake_session(FakeResponse({"comments": [public("Dear Sir or Madam")]}),
                               FakeResponse({"ticket": {}}))
        with Patched(claude_api, claude_json=no_claude), \
                Patched(note_reply, compose=no_claude):
            source = auto_draft.draft_ticket(session, "sub", self.TICKET, cache(), BOOK,
                                             API_USER, TODAY, dry_run=False)
        return source, session

    def test_it_is_drafted_word_for_word_as_one_private_option(self):
        source, session = self.run_draft()
        self.assertEqual(source, "law-enforcement-requests")
        note = session.calls[1][2]["json"]["ticket"]["comment"]
        self.assertIs(note["public"], False)
        self.assertIn("Nobody has reviewed it yet", note["html_body"])
        _, text = auto_draft.STANDARD_REPLIES["law-enforcement-requests"]
        self.assertEqual(note_reply.find_draft([posted_note(note["html_body"])], API_USER),
                         {1: text})


class AbuseReport(unittest.TestCase):
    def test_it_is_composed_from_the_standing_brief_without_placement(self):
        seen = {}

        def fake_compose(sample, brief, previous=None, precedent=None, thread=None):
            seen.update(brief=brief)
            return GERMAN

        def no_claude(*a, **k):
            raise AssertionError("an abuse report needs no placement or brief choice")
        session = fake_session(FakeResponse({"comments": [public("Bitte sperren")]}),
                               FakeResponse({"ticket": {}}))
        with Patched(claude_api, claude_json=no_claude), \
                Patched(note_reply, compose=fake_compose):
            source = auto_draft.draft_ticket(
                session, "sub", {"id": 9, "requester_id": CUSTOMER, "tags": []}, cache(),
                BOOK, API_USER, TODAY, False, category="abuse_report")
        self.assertEqual(source, "abuse_report")
        self.assertIn("communities Session itself operates", seen["brief"])
        note = session.calls[1][2]["json"]["ticket"]["comment"]
        self.assertIs(note["public"], False)
        self.assertIn("Standing brief:", note["html_body"])
        self.assertIn(note_reply.auto_marker("abuse_report"), note["html_body"])


class AfterAnUnpromptedDraft(unittest.TestCase):
    def auto_note(self):
        note = note_reply.build_draft_note(GERMAN, "need the logs", None,
                                           unprompted=("a reply", "Brief:", 99))
        return posted_note(note, cid=10)

    def test_the_note_is_not_a_command(self):
        text = note_reply.comment_text(self.auto_note())
        self.assertIsNone(note_reply.parse_command(text))

    def test_reply_sends_its_option(self):
        self.assertEqual(note_reply.find_draft([self.auto_note()], API_USER),
                         {1: GERMAN["options"][0]["translated"]})

    def test_a_new_brief_replaces_it_rather_than_amending_it(self):
        """Amending keeps the wording an agent approved, and nobody approved this one."""
        seen = {}

        def fake(sample, brief, previous=None, precedent=None, thread=None):
            seen.update(previous=previous)
            return GERMAN
        with Patched(note_reply, compose=fake):
            note_reply.run_draft(
                fake_session(FakeResponse({"ticket": {}})), "sub",
                {"id": 7, "requester_id": CUSTOMER},
                [comment("claude: draft - x", cid=11), self.auto_note()],
                {"id": 11, "author": AGENT, "action": "draft", "brief": "x"},
                API_USER, dry_run=False)
        self.assertIsNone(seen["previous"])

    def test_a_reviewed_draft_is_still_amended(self):
        seen = {}

        def fake(sample, brief, previous=None, precedent=None, thread=None):
            seen.update(previous=previous)
            return GERMAN
        with Patched(note_reply, compose=fake):
            note_reply.run_draft(
                fake_session(FakeResponse({"ticket": {}})), "sub",
                {"id": 7, "requester_id": CUSTOMER},
                [comment("claude: draft - x", cid=11), draft_note("Erster", cid=10)],
                {"id": 11, "author": AGENT, "action": "draft", "brief": "x"},
                API_USER, dry_run=False)
        self.assertIn("Erster", seen["previous"])


class DigestLine(unittest.TestCase):
    def test_the_digest_lists_the_drafted_tickets(self):
        line, covers = triage.build_drafted_line([5, 6], "sub")
        self.assertIn("**2** drafted", line)
        self.assertIn("claude: reply N", line)
        self.assertEqual(covers, set())

    def test_no_drafts_no_line(self):
        self.assertIsNone(triage.build_drafted_line([], "sub"))


if __name__ == "__main__":
    unittest.main()
