#!/usr/bin/env python3
"""Tests for the Zendesk triage script.

Stdlib unittest so the repo needs no test dependency. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

Everything here is offline — no Zendesk, Anthropic, or Discord calls. The fetch
tests drive fetch_tickets with a stub session instead.
"""
import inspect
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage  # noqa: E402  (needs the path insert above)


STAMP = "%Y-%m-%dT%H:%M:%SZ"


def ticket(ticket_id, updated_at="2026-08-03T12:00:00Z", **extra):
    """A Zendesk-shaped ticket, only the fields the script actually reads."""
    row = {
        "id": ticket_id,
        "result_type": "ticket",
        "subject": f"Subject {ticket_id}",
        "description": f"Description {ticket_id}",
        "tags": [],
        "priority": "normal",
        "status": "open",
        "updated_at": updated_at,
    }
    row.update(extra)
    return row


def finding(ticket_id, **extra):
    """A classification result, with every key build_*_embed touches."""
    row = {
        "id": ticket_id,
        "category": "bug_report",
        "severity": "major",
        "affected_component": "sync",
        "summary": f"Summary {ticket_id}",
        "likely_root_cause": "cause",
        "language": "English",
        "priority_rank": 1,
        "worth_looking_into": True,
        "cluster": "",
    }
    row.update(extra)
    return row


def build_messages(*args, **kwargs):
    """triage.build_messages returns (messages, coverage); most tests want messages."""
    return triage.build_messages(*args, **kwargs)[0]


def digest_text(messages):
    """The digest as one string: every message's content, in order."""
    return "\n".join(m["content"] for m in messages)


class FakeResponse:
    # retry-after: 0 keeps the retry tests instant instead of sleeping through
    # the real backoff, and exercises the header-honoring path while it's at it.
    def __init__(self, payload, status_code=200, retry_after="0"):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"retry-after": retry_after}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class NonJsonResponse(FakeResponse):
    """A 200 whose body isn't JSON — a proxy error page, say."""

    def __init__(self):
        super().__init__({})
        self.text = "<html>maintenance</html>"

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)


class FakeSession:
    """Returns queued responses in order and records the requests made.

    A queued Exception is raised instead of returned, so transport failures can be
    exercised alongside HTTP status codes.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class NoSleep:
    """Patch out time.sleep so retry tests assert on delays without waiting."""

    def __enter__(self):
        self.slept = []
        self._real = triage.time.sleep
        triage.time.sleep = self.slept.append
        return self

    def __exit__(self, *exc):
        triage.time.sleep = self._real
        return False


# ---- Window construction ---------------------------------------------------


class TestWindowQuery(unittest.TestCase):
    def test_cutoff_is_the_requested_number_of_hours_back(self):
        query = triage.build_window_query(72)
        cutoff = query.split("updated>")[1].split(" ")[0]
        parsed = datetime.strptime(cutoff, STAMP).replace(tzinfo=timezone.utc)
        expected = datetime.now(timezone.utc) - timedelta(hours=72)
        self.assertLess(abs((parsed - expected).total_seconds()), 120)

    def test_the_window_is_on_updated_at_not_created_at(self):
        """A created-window would never re-fetch a ticket the requester adds detail to
        days after opening it. The dedup state, not the query, is what keeps the wider
        net from being noisy."""
        query = triage.build_window_query(72)
        self.assertIn("updated>", query)
        self.assertNotIn("created>", query)

    def test_query_keeps_unsolved_filter_and_newest_first_ordering(self):
        query = triage.build_window_query(72)
        self.assertIn("type:ticket", query)
        self.assertIn("status<solved", query)
        # Ordered by the same field the window bounds, so a fetch truncated at
        # --max-tickets drops the least recently touched rather than the oldest.
        self.assertIn("order_by:updated_at", query)
        self.assertIn("sort:desc", query)

    def test_a_longer_window_reaches_further_back(self):
        short = triage.build_window_query(48).split("updated>")[1].split(" ")[0]
        long = triage.build_window_query(168).split("updated>")[1].split(" ")[0]
        self.assertLess(long, short)  # ISO-8601 sorts chronologically

    def test_window_label_reads_naturally(self):
        self.assertEqual(triage.window_label(24), "updated in the past 1 day")
        self.assertEqual(triage.window_label(48), "updated in the past 2 days")
        self.assertEqual(triage.window_label(168), "updated in the past 7 days")
        self.assertEqual(triage.window_label(36), "updated in the past 36h")


# ---- Dedup state -----------------------------------------------------------


class TestPartitionByState(unittest.TestCase):
    def test_empty_state_makes_everything_new(self):
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1), ticket(2)], triage.empty_state()
        )
        self.assertEqual([t["id"] for t in new], [1, 2])
        self.assertEqual(changed, [])
        self.assertEqual(unchanged, [])

    def test_same_requester_activity_is_unchanged(self):
        state = {"seen": {"1": {"requester_updated_at": "2026-08-03T12:00:00Z"}}}
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1, updated_at="2026-08-03T12:00:00Z")], state
        )
        self.assertEqual((new, changed), ([], []))
        self.assertEqual([t["id"] for t in unchanged], [1])

    def test_moved_requester_activity_is_changed(self):
        state = {"seen": {"1": {"requester_updated_at": "2026-08-03T12:00:00Z"}}}
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1, updated_at="2026-08-04T09:00:00Z")], state
        )
        self.assertEqual((new, unchanged), ([], []))
        self.assertEqual([t["id"] for t in changed], [1])

    def test_our_own_reply_does_not_re_report(self):
        """updated_at moves on an agent reply, a tag edit, or the hourly automation
        that bumps tickets at :01. Only the requester coming back counts."""
        state = {"seen": {"1": {"requester_updated_at": "2026-08-03T12:00:00Z"}}}
        touched = ticket(1, updated_at="2026-08-04T09:01:00Z")
        touched["requester_updated_at"] = "2026-08-03T12:00:00Z"
        _, changed, unchanged = triage.partition_by_state([touched], state)
        self.assertEqual(changed, [])
        self.assertEqual([t["id"] for t in unchanged], [1])

    def test_a_missing_metric_set_falls_back_to_updated_at(self):
        """A failed sideload degrades to the old noisy behaviour, never to silence."""
        state = {"seen": {"1": {"requester_updated_at": "2026-08-03T12:00:00Z"}}}
        _, changed, _ = triage.partition_by_state(
            [ticket(1, updated_at="2026-08-04T09:00:00Z")], state
        )
        self.assertEqual([t["id"] for t in changed], [1])

    def test_mixed_batch_splits_three_ways(self):
        state = {
            "seen": {
                "1": {"requester_updated_at": "2026-08-03T12:00:00Z"},
                "2": {"requester_updated_at": "2026-08-01T00:00:00Z"},
            }
        }
        batch = [
            ticket(1, updated_at="2026-08-03T12:00:00Z"),  # unchanged
            ticket(2, updated_at="2026-08-04T09:00:00Z"),  # changed
            ticket(3),                                      # new
        ]
        new, changed, unchanged = triage.partition_by_state(batch, state)
        self.assertEqual([t["id"] for t in new], [3])
        self.assertEqual([t["id"] for t in changed], [2])
        self.assertEqual([t["id"] for t in unchanged], [1])

    def test_ids_are_matched_as_strings_not_ints(self):
        """State comes back from JSON, where keys are always strings."""
        state = {"seen": {"27564": {"requester_updated_at": "2026-08-03T12:00:00Z"}}}
        _, _, unchanged = triage.partition_by_state(
            [ticket(27564, updated_at="2026-08-03T12:00:00Z")], state
        )
        self.assertEqual(len(unchanged), 1)


class TestRequesterActivity(unittest.TestCase):
    """requester_updated_at comes from the ticket's metric set, sideloaded rather than
    fetched per ticket."""

    def metrics(self, *pairs):
        return FakeResponse({"metric_sets": [
            {"ticket_id": i, "requester_updated_at": stamp} for i, stamp in pairs]})

    def test_it_attaches_the_requester_timestamp(self):
        tickets = [ticket(1), ticket(2)]
        session = FakeSession([self.metrics((1, "A"), (2, "B"))])
        self.assertEqual(triage.hydrate_requester_activity(session, "acme", tickets), 2)
        self.assertEqual([t["requester_updated_at"] for t in tickets], ["A", "B"])

    def test_it_sideloads_rather_than_fetching_each_ticket(self):
        session = FakeSession([self.metrics((1, "A"))])
        triage.hydrate_requester_activity(session, "acme", [ticket(1)])
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("show_many.json", url)
        self.assertEqual(kwargs["params"]["include"], "metric_sets")

    def test_it_batches_at_a_hundred_ids(self):
        """show_many caps at 100 ids, so 150 tickets must be two requests."""
        tickets = [ticket(i) for i in range(150)]
        session = FakeSession([self.metrics(), self.metrics()])
        triage.hydrate_requester_activity(session, "acme", tickets)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(session.calls[0][2]["params"]["ids"].split(",")), 100)
        self.assertEqual(len(session.calls[1][2]["params"]["ids"].split(",")), 50)

    def test_a_failed_sideload_leaves_the_ticket_alone(self):
        """activity_key then falls back to updated_at: noisy, never silent."""
        tickets = [ticket(1, updated_at="X")]
        for response in (FakeResponse({}, status_code=500), NonJsonResponse(),
                         requests.ConnectionError("unreachable")):
            with self.subTest(response=type(response).__name__):
                with NoSleep():
                    triage.hydrate_requester_activity(
                        FakeSession([response] * 2), "acme", tickets)
                self.assertNotIn("requester_updated_at", tickets[0])
                self.assertEqual(triage.activity_key(tickets[0]), "X")

    def test_a_ticket_the_requester_never_touched_falls_back(self):
        """A null requester_updated_at must not read as "no activity ever" — that would
        pin the ticket unchanged forever."""
        tickets = [ticket(1, updated_at="X")]
        session = FakeSession([self.metrics((1, None))])
        triage.hydrate_requester_activity(session, "acme", tickets)
        self.assertEqual(triage.activity_key(tickets[0]), "X")


class TestStateRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "nested", "seen.json")
        self.addCleanup(self.dir.cleanup)

    def test_save_then_load_recovers_reported_tickets(self):
        triage.save_state(self.path, triage.empty_state(), [ticket(1), ticket(2)], 30)
        state = triage.load_state(self.path)
        self.assertEqual(sorted(state["seen"]), ["1", "2"])
        self.assertEqual(state["seen"]["1"]["requester_updated_at"], "2026-08-03T12:00:00Z")
        self.assertEqual(state["version"], triage.STATE_VERSION)

    def test_save_creates_missing_parent_directories(self):
        triage.save_state(self.path, triage.empty_state(), [ticket(1)], 30)
        self.assertTrue(os.path.exists(self.path))

    def test_save_leaves_no_temp_file_behind(self):
        triage.save_state(self.path, triage.empty_state(), [ticket(1)], 30)
        siblings = os.listdir(os.path.dirname(self.path))
        self.assertEqual(siblings, ["seen.json"])

    def test_resaving_updates_an_existing_entry(self):
        triage.save_state(self.path, triage.empty_state(), [ticket(1, updated_at="A")], 30)
        state = triage.load_state(self.path)
        triage.save_state(self.path, state, [ticket(1, updated_at="B")], 30)
        self.assertEqual(
            triage.load_state(self.path)["seen"]["1"]["requester_updated_at"], "B")

    def test_entries_past_retention_are_pruned(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(STAMP)
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(STAMP)
        state = {
            "version": triage.STATE_VERSION,
            "seen": {
                "1": {"requester_updated_at": "A", "last_reported": old},
                "2": {"requester_updated_at": "B", "last_reported": recent},
            },
        }
        kept, pruned = triage.save_state(self.path, state, [], 30)
        self.assertEqual((kept, pruned), (1, 1))
        self.assertEqual(list(triage.load_state(self.path)["seen"]), ["2"])

    def test_entries_with_unparseable_timestamps_are_dropped(self):
        state = {"version": triage.STATE_VERSION,
                 "seen": {"1": {"requester_updated_at": "A", "last_reported": "nonsense"}}}
        kept, pruned = triage.save_state(self.path, state, [], 30)
        self.assertEqual((kept, pruned), (0, 1))

    def test_a_ticket_reported_now_survives_pruning(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(STAMP)
        state = {"version": triage.STATE_VERSION,
                 "seen": {"1": {"requester_updated_at": "A", "last_reported": old}}}
        kept, _ = triage.save_state(self.path, state, [ticket(1, updated_at="B")], 30)
        self.assertEqual(kept, 1)


class TestStateDegradation(unittest.TestCase):
    """A missing or damaged cache must never crash the run — worst case it
    re-reports the window once."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_missing_file(self):
        path = os.path.join(self.dir.name, "absent.json")
        self.assertEqual(triage.load_state(path), triage.empty_state())

    def test_unparseable_json(self):
        self.assertEqual(triage.load_state(self._write("c.json", "{{{")), triage.empty_state())

    def test_json_that_is_not_an_object(self):
        self.assertEqual(triage.load_state(self._write("l.json", "[]")), triage.empty_state())

    def test_object_without_a_seen_map(self):
        self.assertEqual(
            triage.load_state(self._write("n.json", '{"version": 1}')), triage.empty_state()
        )

    def test_seen_of_the_wrong_type(self):
        self.assertEqual(
            triage.load_state(self._write("w.json", '{"seen": []}')), triage.empty_state()
        )

    def test_absent_version_is_a_cache_miss(self):
        """Without a version we can't know the fields mean what we think."""
        path = self._write("v.json", '{"seen": {"1": {"updated_at": "A"}}}')
        self.assertEqual(triage.load_state(path), triage.empty_state())

    def test_unknown_version_is_a_cache_miss(self):
        path = self._write("v2.json", '{"version": 99, "seen": {"1": {"updated_at": "A"}}}')
        self.assertEqual(triage.load_state(path), triage.empty_state())

    def test_matching_version_loads_normally(self):
        path = self._write(
            "ok.json",
            json.dumps({"version": triage.STATE_VERSION, "seen": {"1": {"updated_at": "A"}}}),
        )
        self.assertEqual(list(triage.load_state(path)["seen"]), ["1"])

    def test_state_written_by_save_state_round_trips_the_version(self):
        """Guards against save_state and load_state disagreeing on the version."""
        path = os.path.join(self.dir.name, "rt.json")
        triage.save_state(path, triage.empty_state(), [ticket(1)], 30)
        self.assertEqual(list(triage.load_state(path)["seen"]), ["1"])


# ---- Discord rendering -----------------------------------------------------


class TestHeader(unittest.TestCase):
    def header(self, findings, stats):
        highlights = [f for f in findings if f.get("worth_looking_into")]
        return triage.build_header(findings, highlights, stats)

    def test_reports_analyzed_against_matched(self):
        text = self.header([finding(1)], {"matched": 47})
        self.assertIn("analyzed **1** of **47** tickets in the window", text)

    def test_names_the_window(self):
        text = self.header([finding(1)], {"matched": 5, "scope": "updated in the past 3 days"})
        self.assertIn("(updated in the past 3 days)", text)

    def test_reports_skipped_unchanged_tickets(self):
        text = self.header([finding(1)], {"matched": 47, "skipped_unchanged": 45})
        self.assertIn("Skipped **45** already reported and unchanged", text)

    def test_omits_the_skip_line_when_nothing_was_skipped(self):
        self.assertNotIn("Skipped", self.header([finding(1)], {"skipped_unchanged": 0}))

    def test_reports_the_backlog_excluding_store_reviews(self):
        """The unqualified number is ~13x the queue that needs a human, because 92%
        of unsolved tickets are AppFollow reviews."""
        text = self.header([finding(1)], {"total_unsolved": 5680,
                                          "total_unsolved_non_review": 428})
        self.assertIn("Backlog: **428** unsolved excluding app-store reviews", text)
        self.assertIn("**5,252** more are reviews", text)

    def test_falls_back_to_the_total_when_the_review_count_is_unavailable(self):
        """Both counts are best-effort; losing one must not lose the whole line."""
        text = self.header([finding(1)], {"total_unsolved": 5609,
                                          "total_unsolved_non_review": None})
        self.assertIn("Backlog: **5,609** unsolved tickets in total", text)

    def test_omits_the_backlog_line_when_the_count_is_unavailable(self):
        self.assertNotIn("Backlog", self.header([finding(1)], {"total_unsolved": None}))

    def test_flags_how_many_were_re_reports(self):
        text = self.header([finding(1)], {"updated_count": 3})
        self.assertIn("🔄 **3** changed since last reported", text)

    def test_counts_crash_and_data_loss_as_serious(self):
        findings = [finding(1, severity="crash"), finding(2, severity="data_loss")]
        self.assertIn("**2** crash/data-loss", self.header(findings, {}))

    def test_works_with_no_stats_at_all(self):
        text = self.header([finding(1)], None)
        self.assertIn("analyzed **1**", text)
        self.assertNotIn("of **", text)

    def test_tallies_categories_by_emoji(self):
        findings = [finding(1), finding(2), finding(3, category="question")]
        text = self.header(findings, {})
        self.assertIn(f"{triage.CATEGORY_EMOJI['bug_report']} **2**", text)
        self.assertIn(f"{triage.CATEGORY_EMOJI['question']} **1**", text)

    def test_groups_repeated_clusters(self):
        findings = [finding(1, cluster="push"), finding(2, cluster="push"), finding(3, cluster="solo")]
        text = triage.build_header(findings, findings, {})
        self.assertIn("Likely duplicates:", text)
        self.assertIn("push", text)
        self.assertNotIn("solo", text)  # a single ticket is not a cluster


class TestTicketLine(unittest.TestCase):
    def test_reads_as_markers_then_id_then_summary(self):
        line = triage.build_ticket_line(
            finding(27605, severity="crash", summary="Notifications only appear after opening"),
            "acme",
        )
        self.assertTrue(line.startswith(f"🔥 | {triage.CATEGORY_EMOJI['bug_report']} | "))
        self.assertIn("[#27605](https://acme.zendesk.com/agent/tickets/27605)", line)
        self.assertIn("· Notifications only appear after opening", line)
        self.assertIn("| Likely cause: cause", line)

    def test_update_marker_only_appears_for_re_reports(self):
        fresh = triage.build_ticket_line(finding(1), "acme", is_update=False)
        repeat = triage.build_ticket_line(finding(1), "acme", is_update=True)
        self.assertNotIn("🔄", fresh)
        self.assertIn("🔄 [#1]", repeat)

    def test_omits_the_cause_segment_when_there_is_none(self):
        line = triage.build_ticket_line(finding(1, likely_root_cause=""), "acme")
        self.assertNotIn("Likely cause", line)

    def test_carries_a_reported_account_when_the_classification_names_one(self):
        """Abuse reports themselves reach build_collapsed_line, not here, but any
        other ticket naming an account keeps it on its line."""
        line = triage.build_ticket_line(
            finding(1, category="security_report", reported_session_id="05" + "a" * 64), "acme"
        )
        self.assertIn("Reported: `05" + "a" * 64 + "`", line)

    def test_an_urgent_category_outranks_a_benign_severity(self):
        line = triage.build_ticket_line(
            finding(1, category="legal_or_data_request", severity="not_applicable"), "acme"
        )
        self.assertTrue(line.startswith(triage.URGENT_MARKER))

    def test_a_long_summary_is_clipped(self):
        line = triage.build_ticket_line(finding(1, summary="x" * 500), "acme")
        self.assertIn("…", line)
        self.assertLess(len(line), 500)


class TestBuildMessages(unittest.TestCase):
    def test_only_tickets_worth_looking_into_get_a_line(self):
        findings = [finding(1), finding(2, worth_looking_into=False)]
        text = digest_text(build_messages(findings, "acme"))
        self.assertEqual(sum(1 for line in text.splitlines() if "[#" in line), 1)
        self.assertIn("[#1]", text)

    def test_highlights_are_ordered_by_priority_rank(self):
        findings = [finding(1, priority_rank=3), finding(2, priority_rank=1)]
        lines = [l for l in digest_text(build_messages(findings, "acme")).splitlines() if "[#" in l]
        self.assertIn("[#2]", lines[0])
        self.assertIn("[#1]", lines[1])

    def test_updated_ids_mark_the_right_line(self):
        findings = [finding(1), finding(2)]
        text = digest_text(build_messages(findings, "acme", {}, updated_ids={2}))
        self.assertIn("🔄 [#2]", text)
        self.assertNotIn("🔄 [#1]", text)

    def test_the_header_leads_the_first_message(self):
        messages = build_messages([finding(1)], "acme", {"matched": 3})
        self.assertTrue(messages[0]["content"].startswith("🗂️ **Zendesk triage**"))

    def test_every_highlight_reaches_a_message(self):
        findings = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS)]
        text = digest_text(build_messages(findings, "acme"))
        for i in range(triage.MAX_HIGHLIGHTS):
            self.assertIn(f"[#{i}]", text)

    def test_highlights_beyond_the_cap_are_dropped_but_announced(self):
        over = triage.MAX_HIGHLIGHTS + 5
        findings = [finding(i, priority_rank=i) for i in range(over)]
        text = digest_text(build_messages(findings, "acme"))
        self.assertIn(f"top **{triage.MAX_HIGHLIGHTS}** of **{over}**", text)

    def test_no_truncation_notice_when_nothing_was_dropped(self):
        self.assertNotIn("Showing the top", digest_text(build_messages([finding(1)], "acme")))

    def test_messages_are_plain_content(self):
        for message in build_messages([finding(1)], "acme"):
            self.assertEqual(set(message), {"content"})


class TestCollapsedAbuseReports(unittest.TestCase):
    """Session is metadata-free, so a reported Session ID is not actionable by
    anyone. These are ~11% of non-review tickets: still worth seeing, never worth a
    line each, and never worth a highlight slot."""

    def abuse(self, ticket_id, **extra):
        return finding(ticket_id, category="abuse_report",
                       severity="not_applicable", **extra)

    def test_one_line_replaces_every_abuse_report(self):
        findings = [self.abuse(i) for i in range(10, 18)]
        text = digest_text(build_messages(findings, "acme"))
        collapsed = [l for l in text.splitlines() if "abuse report" in l]
        self.assertEqual(len(collapsed), 1)
        self.assertIn("**8** abuse reports", collapsed[0])

    def test_the_line_links_every_ticket_it_counts(self):
        findings = [self.abuse(i) for i in range(10, 18)]
        text = digest_text(build_messages(findings, "acme"))
        for i in range(10, 18):
            self.assertIn(f"[#{i}](https://acme.zendesk.com/agent/tickets/{i})", text)

    def test_it_carries_the_muted_marker_not_the_urgent_one(self):
        line = triage.build_collapsed_line([self.abuse(1)], "acme")
        self.assertTrue(line.startswith(triage.CATEGORY_EMOJI["abuse_report"]))
        self.assertNotIn(triage.URGENT_MARKER, line)

    def test_a_single_report_reads_singular(self):
        self.assertIn("**1** abuse report —", triage.build_collapsed_line([self.abuse(1)], "acme"))

    def test_it_sorts_below_the_individual_tickets(self):
        """Lowest position for the lowest priority."""
        findings = [self.abuse(10), finding(1, priority_rank=9)]
        lines = [l for l in digest_text(build_messages(findings, "acme")).splitlines()
                 if "[#" in l]
        self.assertIn("[#1]", lines[0])
        self.assertIn("abuse report", lines[-1])

    def test_it_is_emitted_even_when_the_model_flagged_them(self):
        """The whole point is that these are low-value whatever the model said."""
        findings = [self.abuse(10, worth_looking_into=True, priority_rank=1)]
        self.assertIn("**1** abuse report", digest_text(build_messages(findings, "acme")))

    def test_they_never_get_a_ticket_line_of_their_own(self):
        findings = [self.abuse(10, worth_looking_into=True)]
        text = digest_text(build_messages(findings, "acme"))
        self.assertNotIn("| [#10]", text)

    def test_they_do_not_count_as_worth_looking_into(self):
        findings = [self.abuse(10, worth_looking_into=True), finding(1)]
        text = digest_text(build_messages(findings, "acme"))
        self.assertIn("**1** worth looking into", text)

    def test_they_never_take_a_highlight_slot(self):
        findings = ([self.abuse(1000 + i, worth_looking_into=True, priority_rank=1)
                     for i in range(10)]
                    + [finding(i, priority_rank=i + 2) for i in range(triage.MAX_HIGHLIGHTS)])
        text = digest_text(build_messages(findings, "acme"))
        self.assertNotIn("Showing the top", text)
        for i in range(triage.MAX_HIGHLIGHTS):
            self.assertIn(f"[#{i}]", text)

    def test_the_tally_still_counts_them(self):
        findings = [self.abuse(10), self.abuse(11), finding(1)]
        text = digest_text(build_messages(findings, "acme"))
        self.assertIn("analyzed **3**", text)
        self.assertIn(f"{triage.CATEGORY_EMOJI['abuse_report']} **2**", text)

    def test_no_line_at_all_when_there_are_none(self):
        self.assertNotIn("abuse report", digest_text(build_messages([finding(1)], "acme")))

    def test_the_other_urgent_categories_keep_their_own_lines(self):
        """These two are genuinely actionable, so they stay urgent and per-ticket."""
        findings = [finding(1, category="security_report", severity="not_applicable"),
                    finding(2, category="legal_or_data_request", severity="not_applicable")]
        text = digest_text(build_messages(findings, "acme"))
        self.assertEqual(text.count(triage.URGENT_MARKER), 2)
        self.assertIn("| [#1]", text)
        self.assertIn("| [#2]", text)

    def test_a_long_run_of_links_is_clipped_rather_than_overflowing(self):
        findings = [self.abuse(27000 + i) for i in range(200)]
        line = triage.build_collapsed_line(findings, "acme")
        self.assertLess(len(line), triage.MAX_MESSAGE_CHARS)
        self.assertIn("**200** abuse reports", line)
        self.assertRegex(line, r"\+\d+ more\)$")

    def test_nothing_is_clipped_when_it_fits(self):
        line = triage.build_collapsed_line([self.abuse(i) for i in range(10, 15)], "acme")
        self.assertNotIn("more", line)


# ---- Parsing helpers -------------------------------------------------------


class TestTaxonomyIsDerived(unittest.TestCase):
    """CATEGORY_SPECS is the single source of truth. These caught a real bug: a stale
    hardcoded CATEGORY_LABEL further down the module was shadowing the derived one,
    so seven new categories silently rendered as raw enum values."""

    def test_every_category_has_a_discord_label(self):
        missing = [c for c in triage.CATEGORIES if c not in triage.CATEGORY_LABEL]
        self.assertEqual(missing, [])

    def test_every_category_is_explained_in_the_system_prompt(self):
        missing = [c for c in triage.CATEGORIES if c not in triage.SYSTEM_PROMPT]
        self.assertEqual(missing, [])

    def test_schema_enum_matches_the_category_list(self):
        item = triage.SCHEMA["properties"]["tickets"]["items"]
        self.assertEqual(item["properties"]["category"]["enum"], triage.CATEGORIES)

    def test_schema_requires_every_property(self):
        """Structured outputs reject a schema whose fields aren't all required."""
        item = triage.SCHEMA["properties"]["tickets"]["items"]
        self.assertEqual(sorted(item["required"]), sorted(item["properties"]))

    def test_category_names_are_unique(self):
        self.assertEqual(len(triage.CATEGORIES), len(set(triage.CATEGORIES)))

    def test_urgent_categories_are_real_categories(self):
        self.assertTrue(triage.URGENT_CATEGORIES.issubset(set(triage.CATEGORIES)))

    def test_the_collapsed_category_is_a_real_category(self):
        self.assertIn(triage.COLLAPSED_CATEGORY, triage.CATEGORIES)

    def test_the_collapsed_category_is_not_urgent(self):
        """Collapsing an urgent category would hide it behind a count."""
        self.assertNotIn(triage.COLLAPSED_CATEGORY, triage.URGENT_CATEGORIES)

    def test_no_two_markers_share_an_emoji(self):
        """The marker columns only work if each emoji means one thing: a category
        emoji that doubles as a severity or platform icon reads as the wrong column."""
        groups = [set(triage.CATEGORY_EMOJI.values()),
                  set(triage.SEVERITY_EMOJI.values()),
                  set(triage.PLATFORM_EMOJI.values()),
                  {triage.URGENT_MARKER, "🔄", "🗂️"}]
        self.assertEqual(len(set(triage.CATEGORY_EMOJI.values())),
                         len(triage.CATEGORY_EMOJI))
        for i, group in enumerate(groups):
            for other in groups[i + 1:]:
                self.assertEqual(group & other, set())

    def test_platform_enum_is_wired_into_the_schema(self):
        item = triage.SCHEMA["properties"]["tickets"]["items"]
        self.assertEqual(item["properties"]["platform"]["enum"], triage.PLATFORMS)


class TestUrgency(unittest.TestCase):
    def test_urgent_category_beats_a_benign_severity(self):
        """A legal request is not a bug, so the model rates it not_applicable — the
        calmest marker on the most serious ticket in the digest is backwards."""
        legal = finding(1, category="legal_or_data_request", severity="not_applicable")
        self.assertEqual(triage.severity_marker(legal), triage.URGENT_MARKER)
        self.assertNotEqual(triage.severity_marker(legal),
                            triage.SEVERITY_EMOJI["not_applicable"])

    def test_an_abuse_report_is_not_urgent(self):
        """Session is metadata-free: a reported Session ID is not something anyone
        can act on, so 🚨 was telling the reader to do something impossible."""
        abuse = finding(1, category="abuse_report", severity="not_applicable")
        self.assertFalse(triage.is_urgent(abuse))
        self.assertNotEqual(triage.severity_marker(abuse), triage.URGENT_MARKER)

    def test_non_urgent_category_still_uses_severity(self):
        self.assertEqual(
            triage.severity_marker(finding(1, category="bug_report", severity="crash")),
            triage.SEVERITY_EMOJI["crash"])

    def test_unknown_severity_falls_back_to_a_neutral_marker(self):
        self.assertEqual(triage.severity_marker({"category": "other", "severity": "???"}), "▫️")

    def test_every_severity_has_a_marker(self):
        missing = [sev for sev in triage.SEVERITIES if sev not in triage.SEVERITY_EMOJI]
        self.assertEqual(missing, [])


class TestReviewFiltering(unittest.TestCase):
    def review(self, ticket_id, stars, channel="any_channel"):
        return ticket(ticket_id, subject="★" * stars + "☆" * (5 - stars) + " \n\tGreat app",
                      via={"channel": channel})

    def test_star_count_is_read_from_the_subject(self):
        self.assertEqual(triage.review_stars(self.review(1, 5)), 5)
        self.assertEqual(triage.review_stars(self.review(2, 1)), 1)

    def test_non_review_subject_has_no_stars(self):
        self.assertIsNone(triage.review_stars(ticket(1, subject="Notifications broken")))

    def test_channel_identifies_a_review_without_stars_in_the_subject(self):
        """The channel is the reliable signal: only 287 of 2,656 sampled reviews
        carried the app-store tag, so tag-based filtering would miss most."""
        self.assertTrue(triage.is_store_review(
            ticket(1, subject="no stars here", via={"channel": "any_channel"})))

    def test_web_tickets_are_not_reviews(self):
        self.assertFalse(triage.is_store_review(ticket(1, via={"channel": "web"})))

    def test_positive_reviews_are_skipped(self):
        keep, skipped = triage.partition_reviews(
            [self.review(1, 5), self.review(2, 4)], star_floor=3)
        self.assertEqual(keep, [])
        self.assertEqual(len(skipped), 2)

    def test_low_star_reviews_are_kept_because_they_hide_bugs(self):
        keep, skipped = triage.partition_reviews(
            [self.review(1, 1), self.review(2, 2), self.review(3, 3)], star_floor=3)
        self.assertEqual(len(keep), 3)
        self.assertEqual(skipped, [])

    def test_real_support_tickets_are_never_skipped(self):
        support = ticket(1, subject="Cannot send messages", via={"channel": "web"})
        keep, skipped = triage.partition_reviews([support], star_floor=3)
        self.assertEqual(keep, [support])
        self.assertEqual(skipped, [])

    def test_a_review_with_an_unparseable_rating_is_kept(self):
        """Better to spend a few tokens than silently drop a real complaint."""
        odd = ticket(1, subject="loved it", via={"channel": "any_channel"})
        keep, skipped = triage.partition_reviews([odd], star_floor=3)
        self.assertEqual(keep, [odd])
        self.assertEqual(skipped, [])

    def test_the_floor_is_configurable(self):
        keep, skipped = triage.partition_reviews([self.review(1, 4)], star_floor=4)
        self.assertEqual(len(keep), 1)
        self.assertEqual(skipped, [])


class TestReviewPlatform(unittest.TestCase):
    """The two integration shapes seen on real review tickets, verbatim."""

    def review(self, ticket_id, service_info=None, stars=1):
        source = {"from": {"service_info": service_info} if service_info else {},
                  "to": {}, "rel": None}
        return ticket(ticket_id, subject="★" * stars + "☆" * (5 - stars) + " \n\tno good",
                      via={"channel": "any_channel", "source": source})

    def google_play(self, ticket_id):
        return self.review(ticket_id, {
            "supports_channelback": True,
            "supports_clickthrough": True,
            "registered_integration_service_name": "Google Play",
            "registered_integration_service_external_id": "2900483",
            "integration_service_instance_name": "Session",
        })

    def app_store(self, ticket_id):
        return self.review(ticket_id, {
            "supports_channelback": True,
            "supports_clickthrough": True,
            "registered_integration_service_name": "AppFollow: Review Monitor",
            "registered_integration_service_external_id": "xxxxx",
            "integration_service_instance_name":
                "AppFollow (Session - Private Messenger, App Store)",
        })

    def test_google_play_is_android(self):
        self.assertEqual(triage.review_platform(self.google_play(1)), "android")

    def test_the_app_store_is_ios(self):
        """Its registered name is the generic 'AppFollow: Review Monitor'; only the
        instance name says which store, so both names have to be searched."""
        self.assertEqual(triage.review_platform(self.app_store(1)), "ios")

    def test_a_review_naming_no_store_stays_unresolved(self):
        self.assertIsNone(triage.review_platform(self.review(1)))
        self.assertIsNone(triage.review_platform(
            self.review(2, {"registered_integration_service_name": "Some Other Importer"})))

    def test_non_review_tickets_are_not_a_source_of_platform(self):
        email = ticket(1, via={"channel": "email",
                               "source": {"from": {"address": "a@b.c", "name": "A"}}})
        self.assertIsNone(triage.review_platform(email))
        self.assertIsNone(triage.review_platform(ticket(2, via={"channel": "web"})))

    def test_the_ticket_overrides_the_models_guess(self):
        findings = [finding(1, platform="ios"), finding(2, platform="unknown")]
        corrected = triage.apply_review_platform(
            findings, [self.google_play(1), self.app_store(2)])
        self.assertEqual([f["platform"] for f in findings], ["android", "ios"])
        self.assertEqual(corrected, 2)

    def test_an_unresolved_source_leaves_the_guess_alone(self):
        findings = [finding(1, platform="desktop_linux"), finding(2, platform="ios")]
        corrected = triage.apply_review_platform(
            findings, [self.review(1), ticket(2, via={"channel": "web"})])
        self.assertEqual([f["platform"] for f in findings], ["desktop_linux", "ios"])
        self.assertEqual(corrected, 0)

    def test_an_agreeing_guess_is_not_counted_as_a_correction(self):
        findings = [finding(1, platform="android")]
        self.assertEqual(triage.apply_review_platform(findings, [self.google_play(1)]), 0)
        self.assertEqual(findings[0]["platform"], "android")

    def test_findings_without_a_fetched_ticket_are_untouched(self):
        """The --findings path has no tickets to read a source from."""
        findings = [finding(1, platform="unknown")]
        self.assertEqual(triage.apply_review_platform(findings, []), 0)
        self.assertEqual(findings[0]["platform"], "unknown")

    def test_the_digest_line_shows_the_store_the_review_came_from(self):
        findings = [finding(1, category="low_star_review", platform="unknown")]
        triage.apply_review_platform(findings, [self.google_play(1)])
        line = triage.build_ticket_line(findings[0], "acme")
        self.assertIn(triage.PLATFORM_EMOJI["android"], line)
        self.assertNotIn(triage.PLATFORM_EMOJI["unknown"], line)

    def test_every_resolvable_source_maps_to_a_known_platform(self):
        for _, platform in triage.REVIEW_SOURCE_PLATFORMS:
            self.assertIn(platform, triage.PLATFORMS)


class TestContentFreeTickets(unittest.TestCase):
    """Twitter DM tickets arrive with subject and body both 'Conversation with
    <handle>' — 15% of non-review tickets, unclassifiable as fetched."""

    def test_detects_a_description_that_repeats_the_subject(self):
        self.assertTrue(triage.is_content_free(
            ticket(1, subject="Conversation with x", description="Conversation with x")))

    def test_tolerates_whitespace_differences(self):
        self.assertTrue(triage.is_content_free(
            ticket(1, subject="Conversation  with x", description="Conversation with x\n")))

    def test_a_real_description_is_not_content_free(self):
        self.assertFalse(triage.is_content_free(
            ticket(1, subject="Notifications", description="I get no notifications")))

    def test_hydration_pulls_the_first_informative_comment(self):
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([FakeResponse({"comments": [
            {"body": "Conversation with x"},
            {"body": "My messages will not send since the update"},
        ]})])
        self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 1)
        self.assertIn("will not send", row["description"])

    def test_hydration_skips_tickets_that_already_have_content(self):
        row = ticket(1, subject="Notifications", description="No notifications at all")
        session = FakeSession([])
        self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 0)
        self.assertEqual(session.calls, [])  # no wasted API call

    def test_hydration_survives_an_api_failure(self):
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([FakeResponse({}, status_code=404)])
        self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 0)
        self.assertEqual(row["description"], "Conversation with x")  # left as-is

    def test_hydration_survives_a_transport_failure(self):
        """An unreachable comments endpoint must not abort the whole digest."""
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([requests.ConnectionError("unreachable")] * 2)
        with NoSleep():
            self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 0)
        self.assertEqual(row["description"], "Conversation with x")

    def test_hydration_survives_a_non_json_body(self):
        """A 200 carrying an HTML error page is as harmless as an HTTP error here."""
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([NonJsonResponse()])
        self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 0)
        self.assertEqual(row["description"], "Conversation with x")  # left as-is

    def test_one_unreachable_ticket_does_not_block_the_next(self):
        rows = [
            ticket(1, subject="Conversation with a", description="Conversation with a"),
            ticket(2, subject="Conversation with b", description="Conversation with b"),
        ]
        session = FakeSession([
            requests.ConnectionError("unreachable"), requests.ConnectionError("unreachable"),
            FakeResponse({"comments": [{"body": "Cannot log in since the update"}]}),
        ])
        with NoSleep():
            self.assertEqual(triage.hydrate_descriptions(session, "acme", rows), 1)
        self.assertIn("Cannot log in", rows[1]["description"])

    def test_hydration_joins_every_informative_comment(self):
        """It joins all bodies differing from the subject within the fetched page,
        not just the first — later replies often carry the detail."""
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([FakeResponse({"comments": [
            {"body": "Conversation with x"},
            {"body": "first detail"},
            {"body": "second detail"},
        ]})])
        triage.hydrate_descriptions(session, "acme", [row])
        self.assertIn("first detail", row["description"])
        self.assertIn("second detail", row["description"])

    def test_hydration_requests_a_bounded_page_of_comments(self):
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([FakeResponse({"comments": [{"body": "detail"}]})])
        triage.hydrate_descriptions(session, "acme", [row])
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"], {"per_page": 10})

    def test_hydration_leaves_the_ticket_alone_when_no_comment_adds_anything(self):
        row = ticket(1, subject="Conversation with x", description="Conversation with x")
        session = FakeSession([FakeResponse({"comments": [{"body": "Conversation with x"}]})])
        self.assertEqual(triage.hydrate_descriptions(session, "acme", [row]), 0)


class TestMessageCharLimit(unittest.TestCase):
    """Discord caps one message's content at 2,000 characters. Every line is
    pre-clipped, and chunking has to account for the newlines that join them."""

    def fat(self, ticket_id):
        return finding(ticket_id, summary="s" * 400, likely_root_cause="r" * 400)

    def test_every_message_stays_within_the_limit(self):
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        messages = build_messages(findings, "acme")
        for message in messages:
            self.assertLessEqual(len(message["content"]), triage.MAX_MESSAGE_CHARS)
        self.assertGreater(len(messages), 1)  # fat lines must actually split

    def test_no_line_is_dropped_while_chunking(self):
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        text = digest_text(build_messages(findings, "acme"))
        for i in range(triage.MAX_HIGHLIGHTS):
            self.assertIn(f"[#{i}]", text)

    def test_lean_lines_are_not_split_early(self):
        findings = [finding(i, summary="s", likely_root_cause="") for i in range(5)]
        self.assertEqual(len(build_messages(findings, "acme")), 1)

    def test_chunking_counts_the_joining_newlines(self):
        """Two 1,000-char lines are 2,001 joined — over the cap only if the newline
        counts, which is the off-by-one this guards."""
        entries = [("x" * 1000, {1}), ("y" * 1000, {2})]
        self.assertEqual(len(triage.chunk_entries(entries)), 2)

    def test_an_oversized_entry_still_gets_a_message(self):
        chunks = triage.chunk_entries([("x" * (triage.MAX_MESSAGE_CHARS + 50), {1})])
        self.assertEqual(len(chunks), 1)

    def test_a_full_digest_of_fat_lines_and_abuse_reports_stays_within_the_limit(self):
        """Worst case: the cap's worth of maximal ticket lines plus a day of nothing
        but abuse reports, whose links are the one unbounded list on the digest."""
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        findings += [finding(27000 + i, category="abuse_report") for i in range(300)]
        for message in build_messages(findings, "a" * 60):
            self.assertLessEqual(len(message["content"]), triage.MAX_MESSAGE_CHARS)


class TestCoverage(unittest.TestCase):
    """coverage[i] is what message i accounts for, so a partial post failure records
    exactly the tickets that reached Discord."""

    def test_summary_message_covers_non_highlighted_tickets(self):
        findings = [finding(1), finding(2, worth_looking_into=False)]
        _, coverage = triage.build_messages(findings, "acme")
        self.assertIn(2, coverage[0])   # counted by the summary
        self.assertIn(1, coverage[0])   # its own embed is in the same message

    def test_highlights_omitted_by_the_cap_are_covered_by_nothing(self):
        over = triage.MAX_HIGHLIGHTS + 3
        findings = [finding(i, priority_rank=i) for i in range(over)]
        _, coverage = triage.build_messages(findings, "acme")
        covered = set().union(*coverage)
        shown, omitted = triage.select_highlights(findings)
        self.assertEqual(len(omitted), 3)
        for f in omitted:
            self.assertNotIn(f["id"], covered)
        for f in shown:
            self.assertIn(f["id"], covered)

    def test_coverage_has_one_entry_per_message(self):
        findings = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS)]
        messages, coverage = triage.build_messages(findings, "acme")
        self.assertEqual(len(messages), len(coverage))

    def test_no_ticket_is_covered_twice(self):
        findings = [finding(i, priority_rank=i) for i in range(20)]
        findings += [finding(100 + i, category="abuse_report") for i in range(5)]
        _, coverage = triage.build_messages(findings, "acme")
        flat = [tid for ids in coverage for tid in ids]
        self.assertEqual(len(flat), len(set(flat)))

    def test_the_collapsed_line_covers_the_abuse_reports_it_accounts_for(self):
        """Covered by the message carrying that line — not by the header, which no
        longer counts them, and not by nothing, which would re-report them forever."""
        findings = [finding(i, category="abuse_report") for i in range(10, 18)]
        messages, coverage = triage.build_messages(findings, "acme")
        for message, covered in zip(messages, coverage):
            if "abuse report" in message["content"]:
                self.assertEqual(covered, set(range(10, 18)))
                break
        else:
            self.fail("no message carried the collapsed line")

    def test_abuse_reports_dropped_from_the_link_list_are_still_covered(self):
        """The count covers them even when the character budget drops their link, so
        marking them reported is honest — the digest did account for them."""
        findings = [finding(27000 + i, category="abuse_report") for i in range(200)]
        messages, coverage = triage.build_messages(findings, "acme")
        covered = set().union(*coverage)
        self.assertEqual(covered, {f["id"] for f in findings})
        self.assertIn("more", digest_text(messages))


class TestPostToDiscord(unittest.TestCase):
    """Returns the accepted count rather than exiting, so main can record exactly the
    tickets that landed before signalling the failure."""

    def test_all_accepted(self):
        session = FakeSession([FakeResponse({}, status_code=204)] * 3)
        self.assertEqual(triage.post_to_discord(session, "https://hook", [{}, {}, {}]), 3)
        self.assertEqual(len(session.calls), 3)

    def test_stops_at_the_first_failure_and_reports_the_prefix(self):
        session = FakeSession([
            FakeResponse({}, status_code=204),
            FakeResponse({}, status_code=400),
        ])
        self.assertEqual(triage.post_to_discord(session, "https://hook", [{}, {}, {}]), 1)

    def test_does_not_post_after_a_failure(self):
        session = FakeSession([FakeResponse({}, status_code=404)])
        triage.post_to_discord(session, "https://hook", [{}, {}, {}])
        self.assertEqual(len(session.calls), 1)

    def test_first_message_failing_reports_zero(self):
        session = FakeSession([FakeResponse({}, status_code=500)] * 6)
        with NoSleep():
            self.assertEqual(triage.post_to_discord(session, "https://hook", [{}]), 0)

    def test_no_messages_is_zero(self):
        self.assertEqual(triage.post_to_discord(FakeSession([]), "https://hook", []), 0)


class TestSelectHighlights(unittest.TestCase):
    def test_splits_at_the_display_cap(self):
        findings = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS + 4)]
        shown, omitted = triage.select_highlights(findings)
        self.assertEqual(len(shown), triage.MAX_HIGHLIGHTS)
        self.assertEqual(len(omitted), 4)

    def test_orders_by_priority_rank(self):
        shown, _ = triage.select_highlights(
            [finding(1, priority_rank=5), finding(2, priority_rank=1)]
        )
        self.assertEqual([f["id"] for f in shown], [2, 1])

    def test_ignores_tickets_not_worth_looking_into(self):
        shown, omitted = triage.select_highlights([finding(1, worth_looking_into=False)])
        self.assertEqual((shown, omitted), ([], []))


class TestTicketsFromPayload(unittest.TestCase):
    def test_returns_the_list(self):
        payload = {"tickets": [finding(1)]}
        self.assertEqual(triage.tickets_from_payload(payload, "x"), [finding(1)])

    def test_missing_key_exits_instead_of_raising_keyerror(self):
        with self.assertRaises(SystemExit):
            triage.tickets_from_payload({"results": []}, "x")

    def test_wrong_type_exits(self):
        for payload in ({"tickets": {}}, [], "nope", None):
            with self.assertRaises(SystemExit):
                triage.tickets_from_payload(payload, "x")

    def test_an_empty_list_is_valid(self):
        self.assertEqual(triage.tickets_from_payload({"tickets": []}, "x"), [])

    def test_a_finding_missing_renderer_keys_exits(self):
        """A hand-edited --findings list has nothing enforcing its shape,
        so an entry without category/severity would KeyError in build_summary_embed."""
        for entry in ({"id": 1}, {"id": 1, "category": "bug_report"},
                      {"category": "bug_report", "severity": "major"}):
            with self.assertRaises(SystemExit):
                triage.tickets_from_payload({"tickets": [entry]}, "x")

    def test_a_non_object_entry_exits(self):
        with self.assertRaises(SystemExit):
            triage.tickets_from_payload({"tickets": [["not", "an", "object"]]}, "x")


class TestResolveApiModel(unittest.TestCase):
    """The CLI resolves aliases itself; the API takes ids, so only that path maps."""

    def test_every_alias_maps_to_an_id(self):
        for alias, model_id in triage.API_MODEL_ALIASES.items():
            self.assertEqual(triage.resolve_api_model(alias), model_id)
            self.assertTrue(model_id.startswith("claude-"), model_id)

    def test_the_default_model_resolves_to_an_api_id(self):
        """The API 404s on a bare shorthand, so whatever DEFAULT_MODEL is —
        a pinned id today, an alias if that ever changes — it has to resolve to one."""
        resolved = triage.resolve_api_model(triage.DEFAULT_MODEL)
        self.assertNotIn(resolved, triage.API_MODEL_ALIASES)
        self.assertTrue(resolved.startswith("claude-"), resolved)

    def test_a_full_id_passes_through(self):
        self.assertEqual(triage.resolve_api_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_an_unknown_value_passes_through(self):
        """A model newer than this table should reach the API rather than be rewritten."""
        self.assertEqual(triage.resolve_api_model("claude-future-9"), "claude-future-9")


class TestAnalyzeInChunks(unittest.TestCase):
    """A batch of 2000 would need ~204K output tokens, past the 128K ceiling, so
    oversized batches must split rather than truncate."""

    def setUp(self):
        self.seen = []

    def analyzer(self, chunk):
        self.seen.append(len(chunk))
        return [finding(t["id"]) for t in chunk]

    def tickets(self, count):
        return [{"id": i} for i in range(count)]

    def test_a_batch_within_the_limit_is_one_request(self):
        result = triage.analyze_in_chunks(self.analyzer, self.tickets(45), 400)
        self.assertEqual(self.seen, [45])
        self.assertEqual(len(result), 45)

    def test_a_batch_exactly_at_the_limit_is_not_split(self):
        triage.analyze_in_chunks(self.analyzer, self.tickets(400), 400)
        self.assertEqual(self.seen, [400])

    def test_an_oversized_batch_is_split(self):
        triage.analyze_in_chunks(self.analyzer, self.tickets(1000), 400)
        self.assertEqual(self.seen, [400, 400, 200])

    def test_every_ticket_appears_exactly_once_across_chunks(self):
        result = triage.analyze_in_chunks(self.analyzer, self.tickets(2000), 400)
        self.assertEqual(len(result), 2000)
        self.assertEqual(sorted(f["id"] for f in result), list(range(2000)))

    def test_no_chunk_exceeds_the_batch_size(self):
        triage.analyze_in_chunks(self.analyzer, self.tickets(2000), 400)
        self.assertTrue(all(size <= 400 for size in self.seen))

    def test_an_empty_batch_makes_a_single_no_op_request(self):
        self.assertEqual(triage.analyze_in_chunks(self.analyzer, [], 400), [])

    def test_a_failing_chunk_propagates_rather_than_reporting_partial_results(self):
        def exploding(chunk):
            if len(self.seen) == 1:
                raise RuntimeError("api error")
            self.seen.append(len(chunk))
            return []

        with self.assertRaises(RuntimeError):
            triage.analyze_in_chunks(exploding, self.tickets(1000), 400)


class TestLoadFindings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _write(self, content):
        path = os.path.join(self.dir.name, "f.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    MINIMAL = {"id": 1, "category": "bug_report", "severity": "major"}

    def test_accepts_an_object_with_a_tickets_list(self):
        payload = json.dumps({"tickets": [self.MINIMAL]})
        self.assertEqual(triage.load_findings(self._write(payload)), [self.MINIMAL])

    def test_accepts_a_bare_list(self):
        payload = json.dumps([self.MINIMAL])
        self.assertEqual(triage.load_findings(self._write(payload)), [self.MINIMAL])

    def test_rejects_anything_else(self):
        with self.assertRaises(SystemExit):
            triage.load_findings(self._write('{"nope": 1}'))

    def test_rejects_a_finding_missing_keys_the_renderer_indexes(self):
        """build_summary_embed does f["category"] / f["severity"] directly."""
        for payload in ('[{"id": 1}]',
                        '[{"id": 1, "category": "bug_report"}]',
                        '[{"category": "bug_report", "severity": "major"}]'):
            with self.assertRaises(SystemExit):
                triage.load_findings(self._write(payload))

    def test_rejects_a_non_object_entry(self):
        with self.assertRaises(SystemExit):
            triage.load_findings(self._write('[["not", "an", "object"]]'))

    def test_an_empty_list_is_valid(self):
        self.assertEqual(triage.load_findings(self._write("[]")), [])


class TestCompactTicket(unittest.TestCase):
    def test_long_descriptions_are_truncated_and_marked(self):
        compact = triage.compact_ticket(ticket(1, description="x" * 5000))
        self.assertIn("…[truncated]", compact["description"])
        self.assertLess(len(compact["description"]), 5000)

    def test_short_descriptions_are_left_alone(self):
        self.assertEqual(triage.compact_ticket(ticket(1, description="hi"))["description"], "hi")

    def test_missing_description_becomes_empty_string(self):
        self.assertEqual(triage.compact_ticket(ticket(1, description=None))["description"], "")

    def test_satisfaction_score_is_lifted_out_of_the_nested_object(self):
        compact = triage.compact_ticket(ticket(1, satisfaction_rating={"score": "bad"}))
        self.assertEqual(compact["satisfaction_rating"], "bad")

    def test_absent_satisfaction_rating_is_none(self):
        self.assertIsNone(triage.compact_ticket(ticket(1))["satisfaction_rating"])

    def test_updated_at_is_not_sent_to_the_model(self):
        """It is only needed for dedup state, so it stays out of the prompt."""
        self.assertNotIn("updated_at", triage.compact_ticket(ticket(1)))


# ---- Fetching --------------------------------------------------------------


class TestFetchTickets(unittest.TestCase):
    def test_returns_the_total_match_count_alongside_the_batch(self):
        session = FakeSession([FakeResponse({"count": 47, "results": [ticket(1), ticket(2)]})])
        tickets, total = triage.fetch_tickets(session, "acme", "q", 100)
        self.assertEqual(len(tickets), 2)
        self.assertEqual(total, 47)

    def test_follows_pagination(self):
        session = FakeSession([
            FakeResponse({"count": 3, "results": [ticket(1)], "next_page": "https://n/2"}),
            FakeResponse({"count": 3, "results": [ticket(2), ticket(3)]}),
        ])
        tickets, total = triage.fetch_tickets(session, "acme", "q", 100)
        self.assertEqual([t["id"] for t in tickets], [1, 2, 3])
        self.assertEqual(total, 3)

    def test_max_tickets_caps_the_batch_but_not_the_reported_total(self):
        session = FakeSession([
            FakeResponse({"count": 500, "results": [ticket(i) for i in range(10)]}),
        ])
        tickets, total = triage.fetch_tickets(session, "acme", "q", 4)
        self.assertEqual(len(tickets), 4)
        self.assertEqual(total, 500)  # the gap is what the digest surfaces

    def test_non_ticket_search_results_are_ignored(self):
        session = FakeSession([
            FakeResponse({"count": 2, "results": [ticket(1), {"result_type": "user", "id": 9}]}),
        ])
        tickets, _ = triage.fetch_tickets(session, "acme", "q", 100)
        self.assertEqual([t["id"] for t in tickets], [1])

    def test_total_is_none_when_zendesk_omits_the_count(self):
        session = FakeSession([FakeResponse({"results": [ticket(1)]})])
        _, total = triage.fetch_tickets(session, "acme", "q", 100)
        self.assertIsNone(total)

    def test_forbidden_response_exits_with_a_hint(self):
        session = FakeSession([FakeResponse({}, status_code=403)])
        with self.assertRaises(SystemExit):
            triage.fetch_tickets(session, "acme", "q", 100)

    def test_pagination_stops_at_the_zendesk_result_limit(self):
        """Past 1000 results the search API 422s, so we never ask for that page.

        A caller asking for more gets the limit, not an error: one page beyond the
        cap is queued here and must go unrequested.
        """
        limit = triage.SEARCH_RESULT_LIMIT
        pages = [
            FakeResponse({
                "count": 5000,
                "results": [ticket(i) for i in range(offset, offset + 100)],
                "next_page": "https://n/next",
            })
            for offset in range(0, limit + 100, 100)
        ]
        session = FakeSession(pages)
        tickets, total = triage.fetch_tickets(session, "acme", "q", 5000)
        self.assertEqual(len(tickets), limit)
        self.assertEqual(total, 5000)  # the digest still reports the real backlog
        self.assertEqual(len(session.calls), limit // 100)

    def test_max_tickets_below_the_limit_still_wins(self):
        session = FakeSession([
            FakeResponse({"count": 500, "results": [ticket(i) for i in range(100)]}),
        ])
        tickets, _ = triage.fetch_tickets(session, "acme", "q", 7)
        self.assertEqual(len(tickets), 7)

    def test_an_unexpected_422_keeps_the_tickets_already_fetched(self):
        """Belt and braces: a lower-than-documented limit truncates, not crashes."""
        session = FakeSession([
            FakeResponse({"count": 900, "results": [ticket(1)], "next_page": "https://n/2"}),
            FakeResponse({"error": "invalid"}, status_code=422),
        ])
        tickets, total = triage.fetch_tickets(session, "acme", "q", 900)
        self.assertEqual([t["id"] for t in tickets], [1])
        self.assertEqual(total, 900)

    def test_a_422_on_the_first_page_still_exits(self):
        """Nothing fetched means nothing to salvage — that's a real failure."""
        session = FakeSession([FakeResponse({"error": "invalid"}, status_code=422)])
        with self.assertRaises(SystemExit):
            triage.fetch_tickets(session, "acme", "q", 100)


class TestBacklogQueries(unittest.TestCase):
    def test_the_non_review_query_is_the_backlog_minus_the_review_channel(self):
        self.assertTrue(triage.BACKLOG_NON_REVIEW_QUERY.startswith(triage.BACKLOG_QUERY))
        self.assertIn(f"-via:{triage.REVIEW_CHANNEL}", triage.BACKLOG_NON_REVIEW_QUERY)

    def test_the_counter_honours_the_query_it_is_given(self):
        session = FakeSession([FakeResponse({"count": 428})])
        count = triage.fetch_total_unsolved(session, "acme", triage.BACKLOG_NON_REVIEW_QUERY)
        self.assertEqual(count, 428)
        self.assertEqual(session.calls[0][2]["params"]["query"],
                         triage.BACKLOG_NON_REVIEW_QUERY)


class TestFetchTotalUnsolved(unittest.TestCase):
    def test_returns_the_count(self):
        session = FakeSession([FakeResponse({"count": 5609})])
        self.assertEqual(triage.fetch_total_unsolved(session, "acme"), 5609)

    def test_failure_is_non_fatal(self):
        """The backlog number is context, not a reason to abort the digest."""
        session = FakeSession([FakeResponse({}, status_code=500)] * 2)
        self.assertIsNone(triage.fetch_total_unsolved(session, "acme"))

    def test_uses_a_short_retry_budget(self):
        """A full 6-attempt backoff would stall the digest ~60s for optional data."""
        session = FakeSession([FakeResponse({}, status_code=500)] * 6)
        triage.fetch_total_unsolved(session, "acme")
        self.assertEqual(len(session.calls), 2)

    def test_a_transport_failure_is_non_fatal_too(self):
        """request_with_retry re-raises once its budget is spent; None is documented."""
        session = FakeSession([requests.ConnectionError("no route")] * 2)
        with NoSleep():
            self.assertIsNone(triage.fetch_total_unsolved(session, "acme"))

    def test_a_non_json_body_is_non_fatal(self):
        """A 200 with an HTML error page (proxy, maintenance) must not abort the run."""
        session = FakeSession([NonJsonResponse()])
        self.assertIsNone(triage.fetch_total_unsolved(session, "acme"))


class TestRequestWithRetry(unittest.TestCase):
    def test_returns_the_first_success_without_retrying(self):
        session = FakeSession([FakeResponse({"ok": True})])
        resp = triage.request_with_retry(session, "GET", "https://x")
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 1)

    def test_retries_a_server_error_then_succeeds(self):
        session = FakeSession([
            FakeResponse({}, status_code=500),
            FakeResponse({"ok": True}),
        ])
        resp = triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 2)

    def test_does_not_retry_a_client_error(self):
        session = FakeSession([FakeResponse({}, status_code=404)])
        resp = triage.request_with_retry(session, "GET", "https://x")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(session.calls), 1)

    def test_gives_up_after_the_attempt_budget(self):
        session = FakeSession([FakeResponse({}, status_code=503)] * 3)
        resp = triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(len(session.calls), 3)

    def test_transport_failures_retry_then_raise_when_exhausted(self):
        session = FakeSession([requests.ConnectionError("boom")] * 3)
        with NoSleep():
            with self.assertRaises(requests.ConnectionError):
                triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(len(session.calls), 3)

    def test_a_transport_failure_can_recover_on_a_later_attempt(self):
        session = FakeSession([requests.Timeout("slow"), FakeResponse({"ok": True})])
        with NoSleep():
            resp = triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 2)

    def test_no_sleep_after_the_final_attempt(self):
        """Sleeping after the last try only delays the caller — nothing follows it."""
        session = FakeSession([FakeResponse({}, status_code=503)] * 3)
        with NoSleep() as clock:
            triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(len(clock.slept), 2)  # 3 attempts, 2 gaps

    def test_numeric_retry_after_is_honoured(self):
        session = FakeSession([
            FakeResponse({}, status_code=429, retry_after="7"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(clock.slept, [7.0])

    def test_retry_after_is_capped(self):
        session = FakeSession([
            FakeResponse({}, status_code=429, retry_after="9999"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(clock.slept, [60])

    def test_http_date_retry_after_falls_back_instead_of_crashing(self):
        """RFC 9110 allows an HTTP-date here; float() on it used to raise ValueError."""
        session = FakeSession([
            FakeResponse({}, status_code=503, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            resp = triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(clock.slept, [1.0])  # fell back to the backoff delay

    def test_zero_attempts_is_rejected_rather_than_unbound(self):
        with self.assertRaises(ValueError):
            triage.request_with_retry(FakeSession([]), "GET", "https://x", attempts=0)


class TestRetryAfterSeconds(unittest.TestCase):
    def test_missing_header_uses_the_default(self):
        self.assertEqual(triage.retry_after_seconds(FakeResponse({}, retry_after=None), 4.0), 4.0)

    def test_numeric_header_wins(self):
        self.assertEqual(triage.retry_after_seconds(FakeResponse({}, retry_after="12"), 4.0), 12.0)

    def test_unparseable_header_uses_the_default(self):
        for raw in ("Wed, 21 Oct 2026 07:28:00 GMT", "", "soon", "12s"):
            self.assertEqual(triage.retry_after_seconds(FakeResponse({}, retry_after=raw), 4.0), 4.0)

    def test_negative_and_non_finite_values_use_the_default(self):
        """time.sleep() rejects a negative or NaN duration, so passing one through
        would crash the run on a hostile or buggy Retry-After header."""
        for raw in ("-30", "-0.5", "nan", "inf", "-inf"):
            self.assertEqual(triage.retry_after_seconds(FakeResponse({}, retry_after=raw), 4.0),
                             4.0, msg=f"retry-after={raw!r}")

    def test_zero_is_honoured_rather_than_replaced(self):
        """Zero is a valid instruction to retry immediately, not a missing value."""
        self.assertEqual(triage.retry_after_seconds(FakeResponse({}, retry_after="0"), 4.0), 0.0)

    def test_a_negative_retry_after_does_not_crash_a_real_retry_loop(self):
        session = FakeSession([
            FakeResponse({}, status_code=503, retry_after="-30"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            resp = triage.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertTrue(all(s >= 0 for s in clock.slept), clock.slept)


# ---- Workflow wiring -------------------------------------------------------


class TestFailureNotificationWiring(unittest.TestCase):
    """The failure notifier matches on workflow *name*, so a rename silently
    unsubscribes the triage job. The README promises failures get reported; this
    keeps that promise checkable without running Actions.
    """

    WORKFLOWS = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows"
    )

    def read(self, filename):
        with open(os.path.join(self.WORKFLOWS, filename), encoding="utf-8") as fh:
            return fh.read()

    def test_notify_failure_watches_the_triage_workflow_by_its_current_name(self):
        triage_yml = self.read("zendesk_triage.yml")
        match = re.search(r"^name:\s*(.+?)\s*$", triage_yml, re.MULTILINE)
        self.assertIsNotNone(match, "zendesk_triage.yml has no top-level name")
        name = match.group(1).strip("\"'")
        self.assertIn(f'"{name}"', self.read("notify_failure.yml"))


class TestResolveChainWiring(unittest.TestCase):
    """The digest is only correct if the positive-review resolver ran first: solved
    reviews leave the triage's `status<solved` query, so running second would have the
    digest re-count reviews the other job had just closed. The ordering lives entirely in
    YAML, and a moved or renamed reusable workflow would otherwise surface as a failed
    run at 10am on a weekday.
    """

    WORKFLOWS = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows"
    )
    RESOLVE = "./.github/workflows/zendesk_resolve_reviews.yml"

    def read(self, filename):
        with open(os.path.join(self.WORKFLOWS, filename), encoding="utf-8") as fh:
            return "\n".join(l for l in fh.read().splitlines()
                             if not l.lstrip().startswith("#"))

    def test_the_triage_calls_the_resolver(self):
        self.assertIn(f"uses: {self.RESOLVE}", self.read("zendesk_triage.yml"))

    def test_the_resolver_is_callable(self):
        """`uses:` against a workflow that only has `schedule`/`workflow_dispatch` is a
        run-time error, not a parse error, so assert the trigger is actually there."""
        self.assertIn("workflow_call:", self.read("zendesk_resolve_reviews.yml"))

    def test_the_digest_waits_for_it(self):
        """`uses:` alone runs the two jobs concurrently; `needs:` is what orders them."""
        self.assertIn("needs: resolve", self.read("zendesk_triage.yml"))

    def test_a_failed_resolve_does_not_cost_the_digest(self):
        """Resolve is an optimisation for the digest, not a precondition — and the same
        always() is what lets the digest run when a manual dispatch skips resolve."""
        self.assertIn("if: always()", self.read("zendesk_triage.yml"))


class TestNoDiscordWiring(unittest.TestCase):
    """The dispatch button offers a way to run without posting. It has to be
    --no-discord and never --dry-run: Actions logs on this public repo would
    otherwise carry the whole digest, ticket content included.
    """

    WORKFLOW = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".github", "workflows", "zendesk_triage.yml",
    )

    @classmethod
    def setUpClass(cls):
        with open(cls.WORKFLOW, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        # Comments explain why --dry-run is not used here; only what the job runs
        # should be matched against.
        cls.yml = "\n".join(l for l in lines if not l.lstrip().startswith("#"))

    def test_the_workflow_never_passes_dry_run(self):
        self.assertNotIn("--dry-run", self.yml)

    def test_the_no_discord_input_reaches_the_script(self):
        self.assertIn("no_discord:", self.yml)
        self.assertIn("--no-discord", self.yml)

    def test_every_flag_the_workflow_passes_is_one_the_script_defines(self):
        """The workflow builds the command as a string, so a flag that no longer
        exists surfaces as a failed scheduled run rather than anything local."""
        defined = set(re.findall(r'add_argument\("(--[a-z-]+)"',
                                 inspect.getsource(triage.main)))
        for flag in set(re.findall(r"(--[a-z-]+)", self.yml)):
            self.assertIn(flag, defined, msg=f"{flag} is not a triage.py flag")


if __name__ == "__main__":
    unittest.main()
