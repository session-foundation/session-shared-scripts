#!/usr/bin/env python3
"""Tests for the digest's unprompted drafts from briefs support already sent.

Stdlib unittest, offline, same stubs as the rest. Run from anywhere:

    uv run python -m unittest tests.zendesk.test_auto_draft
"""
import os
import tempfile
import unittest
from datetime import date

import requests

from session_ops.zendesk import api, auto_draft, claude_api, note_reply, triage
from tests.zendesk.test_note_reply import (
    AGENT, API_USER, GERMAN, comment, draft_note, fake_session, posted_note)
from tests.zendesk.test_triage import FakeResponse, Patched

TODAY = date(2026, 11, 2)
CUSTOMER = 42
BOOK = {"groups": [{"key": "call-failures-and-quality", "title": "Calls failing"}],
        "cells": {"call-failures-and-quality|any": {"answer": "Ask for logs.", "n": 30,
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
            (1, "call-failures-and-quality", "need the logs", "2026-10-01T00:00:00Z"),
            (2, "call-failures-and-quality", "Need  the logs", "2026-10-05T00:00:00Z"),
            (3, "call-failures-and-quality", "we have no ETA", "2026-10-20T00:00:00Z"),
            (4, "other", "unrelated", "2026-10-20T00:00:00Z")), "call-failures-and-quality")
        self.assertEqual([(r["uses"], r["source"]) for r in rows], [(2, "2"), (1, "3")])

    def test_old_briefs_age_out_and_the_scan_moves_on(self):
        stale = cache((1, "call-failures-and-quality", "old", "2026-01-01T00:00:00Z"),
                      (2, "call-failures-and-quality", "new", "2026-10-01T00:00:00Z"))
        stale["scanned_through"] = "2026-10-30"
        session = fake_session(FakeResponse({"results": [], "count": 0, "next_page": None}))
        refreshed = auto_draft.refresh_cache(session, "sub", API_USER, stale, TODAY)
        self.assertEqual(list(refreshed["briefs"]), ["2"])
        self.assertEqual(refreshed["scanned_through"], "2026-10-31")
        self.assertIn("updated>2026-10-30", str(session.calls[0]))

    def refresh(self, tickets, matched, comments):
        start = cache()
        start["scanned_through"] = "2026-10-30"
        seen = {}

        def fake_search(session, subdomain, query, max_tickets):
            seen["query"] = query
            return tickets, matched
        with Patched(api, fetch_every_ticket=fake_search,
                     fetch_comments=lambda *a, **k: comments):
            return auto_draft.refresh_cache(None, "sub", API_USER, start, TODAY), seen

    def test_a_first_run_reads_only_the_retention_window(self):
        seen = {}

        def fake_search(session, subdomain, query, max_tickets):
            seen["query"] = query
            return [], 0
        with Patched(api, fetch_every_ticket=fake_search):
            auto_draft.refresh_cache(None, "sub", API_USER, cache(), TODAY)
        self.assertIn("updated>2026-05-06", seen["query"])
        self.assertIn("order_by:created_at sort:desc", seen["query"])

    def test_a_failed_comment_read_is_retried_next_run(self):
        refreshed, _ = self.refresh([{"id": 5, "tags": ["grp-call-failures-and-quality"]}],
                                    1, None)
        self.assertEqual(refreshed["scanned_through"], "2026-10-30")

    def test_a_truncated_search_is_retried_next_run(self):
        refreshed, _ = self.refresh([], 1500, [])
        self.assertEqual(refreshed["scanned_through"], "2026-10-30")

    def test_a_complete_scan_moves_on(self):
        refreshed, seen = self.refresh(
            [{"id": 5, "tags": ["grp-call-failures-and-quality"]}], 1,
            [public("help"), cmd("claude: draft - need the logs", 2),
             cmd("claude: reply", 3), public("logs?", author=API_USER, cid=4,
                                             at="2026-10-31T00:00:00Z")])
        self.assertEqual(refreshed["scanned_through"], "2026-10-31")
        self.assertEqual(refreshed["briefs"]["5"]["brief"], "need the logs")
        self.assertIn("updated>2026-10-30", seen["query"])

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

    def test_a_ticket_a_person_left_a_note_on_is_not(self):
        self.assertFalse(auto_draft.eligible(
            {"tags": []}, [cmd("escalated to devs", 2), public("help")], {CUSTOMER},
            API_USER))

    def test_a_note_from_the_api_user_does_not_count(self):
        self.assertTrue(auto_draft.eligible(
            {"tags": []}, [comment("translation", author=API_USER, cid=2),
                           public("help")], {CUSTOMER}, API_USER))

    def test_a_ticket_already_drafted_is_not(self):
        self.assertFalse(auto_draft.eligible(
            {"tags": [note_reply.TAG_DRAFTED]}, [public("help")], {CUSTOMER}, API_USER))


class DraftTicket(unittest.TestCase):
    TICKET = {"id": 7, "requester_id": CUSTOMER, "subject": "calls fail",
              "tags": ["grp-call-failures-and-quality", "plat-android"]}
    KNOWN = cache((99, "call-failures-and-quality", "need the logs of both devices", "2026-10-20T00:00:00Z"))

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

    def test_a_group_not_in_the_list_is_not_drafted(self):
        def no_claude(*a, **k):
            raise AssertionError("an unlisted group is never drafted")
        session = fake_session(FakeResponse({"comments": [public("add dark mode")]}))
        with Patched(claude_api, claude_json=no_claude), \
                Patched(note_reply, compose=no_claude):
            source = auto_draft.draft_ticket(
                session, "sub", {"id": 7, "requester_id": CUSTOMER,
                                 "tags": ["grp-feature-requests"]},
                cache((99, "feature-requests", "noted", "2026-10-20T00:00:00Z")),
                BOOK, API_USER, TODAY, False)
        self.assertIsNone(source)
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
    def run_draft(self, group):
        seen = {}

        def fake_compose(sample, brief, previous=None, precedent=None, thread=None):
            seen.update(brief=brief)
            return GERMAN

        def no_claude(*a, **k):
            raise AssertionError("an abuse report needs no brief choice")
        session = fake_session(FakeResponse({"comments": [public("Bitte sperren")]}),
                               FakeResponse({"ticket": {}}))
        with Patched(claude_api, claude_json=no_claude), \
                Patched(note_reply, compose=fake_compose):
            source = auto_draft.draft_ticket(
                session, "sub", {"id": 9, "requester_id": CUSTOMER,
                                 "tags": [f"grp-{group}"]}, cache(),
                BOOK, API_USER, TODAY, False, category="abuse_report")
        return source, seen, session

    def test_a_police_request_about_abuse_gets_the_fixed_legal_text(self):
        source, seen, _ = self.run_draft("law-enforcement-requests")
        self.assertEqual(source, "law-enforcement-requests")
        self.assertEqual(seen, {})

    def test_it_is_composed_from_the_standing_brief(self):
        source, seen, session = self.run_draft("reporting-illegal-content")
        self.assertEqual(source, "abuse_report")
        self.assertIn("communities Session itself operates", seen["brief"])
        note = session.calls[1][2]["json"]["ticket"]["comment"]
        self.assertIs(note["public"], False)
        self.assertIn("Standing brief:", note["html_body"])
        self.assertIn(note_reply.auto_marker("abuse_report"), note["html_body"])


class DraftKnown(unittest.TestCase):
    def test_a_transport_error_never_stops_the_digest(self):
        def down(*a, **k):
            raise requests.ConnectionError("down")
        tickets = [{"id": n, "tags": []} for n in (1, 2, 3, 4)]
        findings = [{"id": n, "category": "bug_report"} for n in (1, 2, 3, 4)]
        with tempfile.TemporaryDirectory() as tmp, \
                Patched(note_reply, load_house=lambda: BOOK,
                        api_user_id=lambda *a: API_USER), \
                Patched(auto_draft, refresh_cache=lambda s, sd, u, c, t: c), \
                Patched(api, fetch_comments=down):
            drafted = auto_draft.draft_known(None, "sub", tickets, findings,
                                             os.path.join(tmp, "k.json"))
        self.assertEqual(drafted, [])

    def test_a_failed_cache_refresh_skips_drafting(self):
        def down(*a, **k):
            raise requests.ConnectionError("down")
        with Patched(note_reply, load_house=lambda: BOOK,
                     api_user_id=lambda *a: API_USER), \
                Patched(auto_draft, refresh_cache=down):
            self.assertEqual(auto_draft.draft_known(None, "sub", [], [], "/nonexistent"),
                             [])


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
