#!/usr/bin/env python3
"""Tests for the Zendesk triage script.

Stdlib unittest so the repo needs no test dependency. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

Everything here is offline — no Zendesk, Claude, or Discord calls. The fetch
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


def text_displays(node):
    """Every Text Display `content` under a Components V2 node, in order."""
    if isinstance(node, list):
        return [t for item in node for t in text_displays(item)]
    if not isinstance(node, dict):
        return []
    found = [node["content"]] if node.get("type") == triage.TEXT_DISPLAY else []
    for key in ("components", "accessory"):
        found += text_displays(node.get(key))
    return found


def digest_text(messages):
    """The digest as one string: every rendered line, in order.

    The digest is Components V2 now — a card per ticket so each can carry its own
    Comment button — so the text lives in Text Display components rather than in a
    message `content`. Flattening here keeps the assertions about what the digest
    says independent of how it is packaged."""
    return "\n".join(t for m in messages for t in text_displays(m["components"]))


def all_text(message):
    """Every character Discord counts towards a Components V2 message's ceiling.

    The button labels as well as the Text Displays: an accessory's label is text in
    the message the same way a ticket line is.
    """
    total = sum(len(t) for t in text_displays(message["components"]))

    def labels(node):
        if isinstance(node, list):
            return sum(labels(item) for item in node)
        if not isinstance(node, dict):
            return 0
        own = len(node.get("label") or "") if node.get("type") == triage.BUTTON else 0
        return own + labels(node.get("components")) + labels(node.get("accessory"))

    return total + labels(message["components"])


def buttons(message):
    """Every button custom_id in a message, in order."""
    found = []
    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("type") == triage.BUTTON:
                found.append(node["custom_id"])
            walk(node.get("components"))
            walk(node.get("accessory"))
    walk(message["components"])
    return found


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


class Patched:
    """Swap module attributes for the duration of a block, then put them back.

    Lives here rather than in each test file because test_relay and test_reply both
    need it, and two copies of a helper that restores state is two chances for one of
    them to stop doing so.
    """

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for name, value in self.attrs.items():
            try:
                self.saved[name] = getattr(self.module, name)
            except AttributeError:
                # Roll back what is already swapped. Without this, a typo'd or
                # since-removed attribute leaves earlier patches applied and
                # __exit__ never runs — every later test in the file then fails
                # against a module the failing test quietly rewrote.
                self.__exit__()
                raise
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)
        return False


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
        first = text_displays(messages[0]["components"])[0]
        self.assertTrue(first.startswith("🗂️ **Zendesk triage**"))

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

    def test_messages_are_components_v2(self):
        """content and embeds stop working once the flag is set, so a payload still
        carrying either would be silently rendered empty."""
        for message in build_messages([finding(1)], "acme"):
            self.assertEqual(message["flags"], triage.COMPONENTS_V2_FLAG)
            self.assertNotIn("content", message)
            self.assertNotIn("embeds", message)
            self.assertEqual(message["components"][0]["type"], triage.CONTAINER)

    def test_every_ticket_card_carries_its_own_comment_button(self):
        """The button is a Section accessory rather than a loose row, so which
        ticket it belongs to is unambiguous."""
        findings = [finding(1), finding(2)]
        message, = build_messages(findings, "acme")
        self.assertEqual(buttons(message), ["comment:1", "comment:2"])

    def test_a_quiet_day_still_posts_the_header(self):
        """Nothing worth looking into is a result, not a reason to say nothing."""
        messages = build_messages([finding(1, worth_looking_into=False)], "acme")
        self.assertEqual(len(messages), 1)
        self.assertIn("Zendesk triage", digest_text(messages))
        self.assertEqual(buttons(messages[0]), [])


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
    """A Components V2 message is bounded twice: 40 components, of which a ticket
    card costs three, and a character budget across all its text."""

    def fat(self, ticket_id):
        return finding(ticket_id, summary="s" * 400, likely_root_cause="r" * 400)

    def test_every_message_stays_within_the_limit(self):
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        messages = build_messages(findings, "acme")
        for message in messages:
            rendered = text_displays(message["components"])
            self.assertLessEqual(sum(len(t) for t in rendered),
                                 triage.MAX_COMPONENT_CHARS)
        self.assertGreater(len(messages), 1)  # fat lines must actually split

    def test_the_header_comes_out_of_the_first_messages_budget(self):
        """The header is prepended after chunking, so without reserving its length
        message one carried a full budget of ticket lines *plus* the header. A busy
        day's accounting lines were enough to put it over."""
        stats = {"matched": 460, "scope": "updated in the past 3 days",
                 "skipped_unchanged": 40, "skipped_reviews": 300,
                 "total_unsolved": 5680, "total_unsolved_non_review": 428,
                 "updated_count": 12}
        findings = [finding(i, priority_rank=i,
                            summary="s" * triage.SUMMARY_CHARS,
                            likely_root_cause="r" * triage.ROOT_CAUSE_CHARS,
                            cluster=f"cluster-{i % 3}")
                    for i in range(triage.MAX_SECTIONS_PER_MESSAGE)]
        messages = build_messages(findings, "acme", stats)
        for message in messages:
            rendered = text_displays(message["components"])
            self.assertLessEqual(sum(len(t) for t in rendered),
                                 triage.MAX_COMPONENT_CHARS)

    def test_no_message_exceeds_discords_component_ceiling(self):
        """40 per message, and a card is a Section plus its text plus its button."""
        findings = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS)]
        for message in build_messages(findings, "acme"):
            def count(node):
                if isinstance(node, list):
                    return sum(count(i) for i in node)
                if not isinstance(node, dict):
                    return 0
                return 1 + count(node.get("components")) + count(node.get("accessory"))
            self.assertLessEqual(count(message["components"]), 40)

    def test_no_line_is_dropped_while_chunking(self):
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        text = digest_text(build_messages(findings, "acme"))
        for i in range(triage.MAX_HIGHLIGHTS):
            self.assertIn(f"[#{i}]", text)

    def test_lean_lines_are_not_split_early(self):
        findings = [finding(i, summary="s", likely_root_cause="") for i in range(5)]
        self.assertEqual(len(build_messages(findings, "acme")), 1)

    def test_the_button_labels_are_counted_too(self):
        """Every Comment label is text in the message as much as the lines are. The
        budget used to be the ceiling less a 100-character margin nobody wrote down,
        which ten labels came within thirty characters of spending."""
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        for message in build_messages(findings, "acme"):
            self.assertLessEqual(all_text(message), triage.MAX_MESSAGE_TEXT_CHARS)

    def test_the_card_count_splits_a_busy_day(self):
        """Short lines never reach the character budget, so without the component
        limit a 27-ticket day would build one illegal message."""
        findings = [finding(i, priority_rank=i, summary="s", likely_root_cause="")
                    for i in range(triage.MAX_HIGHLIGHTS)]
        messages = build_messages(findings, "acme")
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(buttons(message)),
                                 triage.MAX_SECTIONS_PER_MESSAGE)

    def test_chunking_splits_on_whichever_limit_binds_first(self):
        entries = [("x" * 2000, {1}), ("y" * 2000, {2})]
        self.assertEqual(len(triage.chunk_entries(entries)), 2)   # characters
        lean = [("x", {i}) for i in range(triage.MAX_SECTIONS_PER_MESSAGE + 1)]
        self.assertEqual(len(triage.chunk_entries(lean)), 2)      # card count

    def test_an_oversized_entry_still_gets_a_message(self):
        chunks = triage.chunk_entries([("x" * (triage.MAX_COMPONENT_CHARS + 50), {1})])
        self.assertEqual(len(chunks), 1)

    def test_a_full_digest_of_fat_lines_and_abuse_reports_stays_within_the_limit(self):
        """Worst case: the cap's worth of maximal ticket lines plus a day of nothing
        but abuse reports, whose links are the one unbounded list on the digest."""
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        findings += [finding(27000 + i, category="abuse_report") for i in range(300)]
        for message in build_messages(findings, "a" * 60):
            self.assertLessEqual(
                sum(len(text) for text in text_displays(message["components"])),
                triage.MAX_COMPONENT_CHARS)


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

    def test_the_collapsed_line_carries_no_comment_button(self):
        """It stands for every abuse report at once, so there is no single ticket a
        reply could go to — and the merge that brought the collapsed line onto the
        Components V2 digest wrapped it in a Section like any ticket, giving it a
        button whose custom_id was an arbitrary member of the set."""
        findings = [finding(i, category="abuse_report") for i in range(10, 18)]
        messages, _ = triage.build_messages(findings, "acme")
        collapsed = [m for m in messages
                     if any("abuse report" in text
                            for text in text_displays(m["components"]))]
        self.assertTrue(collapsed)
        for message in collapsed:
            for custom_id in buttons(message):
                self.assertNotIn(custom_id.removeprefix("comment:"),
                                 {str(i) for i in range(10, 18)})

    def test_the_collapsed_line_covers_the_abuse_reports_it_accounts_for(self):
        """Covered by the message carrying that line — not by the header, which no
        longer counts them, and not by nothing, which would re-report them forever."""
        findings = [finding(i, category="abuse_report") for i in range(10, 18)]
        messages, coverage = triage.build_messages(findings, "acme")
        for message, covered in zip(messages, coverage):
            if any("abuse report" in text
                   for text in text_displays(message["components"])):
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


class TestFetchEveryTicket(unittest.TestCase):
    """Walking past the Search API's 1000-result ceiling by created_at slices."""

    def page(self, ids, count, stamp="2026-08-01T00:00:00Z"):
        return FakeResponse({"count": count, "next_page": None, "results": [
            {"id": i, "result_type": "ticket", "created_at": stamp} for i in ids]})

    def test_one_short_slice_is_a_single_query(self):
        session = FakeSession([self.page([1, 2, 3], 3)])
        tickets, total = triage.fetch_every_ticket(session, "acme", "q", 100)
        self.assertEqual([t["id"] for t in tickets], [1, 2, 3])
        self.assertEqual((total, len(session.calls)), (3, 1))

    def test_a_full_slice_is_followed_by_another(self):
        """A full 1000 means there may be more behind it, so the walk continues from
        the oldest created_at rather than stopping at Zendesk's ceiling."""
        first = list(range(triage.SEARCH_RESULT_LIMIT))
        session = FakeSession([
            self.page(first, 1036, "2026-08-02T00:00:00Z"),
            self.page(range(9000, 9036), 36, "2025-08-02T00:00:00Z"),
        ])
        tickets, total = triage.fetch_every_ticket(session, "acme", "q", 5000)
        self.assertEqual(len(tickets), triage.SEARCH_RESULT_LIMIT + 36)
        self.assertEqual(total, 1036, "total comes from the unsliced query")
        self.assertIn("created<=2026-08-02T00:00:00Z", session.calls[1][2]["params"]["query"])

    def test_the_overlapping_second_is_not_counted_twice(self):
        """created<= re-fetches everything sharing the oldest second; ids dedupe it."""
        first = list(range(triage.SEARCH_RESULT_LIMIT))
        session = FakeSession([
            self.page(first, 1002),
            self.page([998, 999, 1000, 1001], 4),
            self.page([], 0),
        ])
        tickets, _ = triage.fetch_every_ticket(session, "acme", "q", 5000)
        self.assertEqual(len(tickets), len({t["id"] for t in tickets}))
        self.assertEqual(len(tickets), triage.SEARCH_RESULT_LIMIT + 2)

    def test_a_slice_that_adds_nothing_new_ends_the_walk(self):
        """Otherwise a tie group larger than a slice would loop forever."""
        first = list(range(triage.SEARCH_RESULT_LIMIT))
        session = FakeSession([self.page(first, 99999), self.page(first, 99999)])
        tickets, _ = triage.fetch_every_ticket(session, "acme", "q", 99999)
        self.assertEqual(len(tickets), triage.SEARCH_RESULT_LIMIT)
        self.assertEqual(len(session.calls), 2)

    def test_it_never_returns_more_than_asked_for(self):
        session = FakeSession([self.page(range(10), 10)])
        tickets, _ = triage.fetch_every_ticket(session, "acme", "q", 4)
        self.assertEqual(len(tickets), 4)


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


# ---- The Claude Code CLI ---------------------------------------------------


class TestClaudeCli(unittest.TestCase):
    """Both Claude calls go through `claude --print`, so the flags are the contract.
    Nothing here runs the CLI; subprocess.run is replaced by a recorder."""

    SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}

    def run_cli(self, response=None, returncode=0, stderr="", raises=None,
                prompt="ticket text"):
        self.calls = []

        def fake_run(command, **kwargs):
            self.calls.append((command, kwargs))
            if raises is not None:
                raise raises
            payload = {"subtype": "success", "is_error": False,
                       "structured_output": {"a": "b"}}
            if response is not None:
                payload = response
            return type("Done", (), {
                "returncode": returncode,
                "stdout": json.dumps(payload) if isinstance(payload, dict) else payload,
                "stderr": stderr})()

        with Patched(triage.subprocess, run=fake_run):
            return triage.claude_cli_json("claude-opus-5", "medium", "be terse",
                                          self.SCHEMA, prompt, 60, "a batch of 3")

    def command(self, **kwargs):
        self.run_cli(**kwargs)
        return self.calls[0][0]

    def test_the_prompt_goes_over_stdin_not_argv(self):
        """Linux caps one argument at 128KB and a full batch is several times that, so
        argv would work on a normal day and die on a backfill. argv is also
        world-readable through /proc, and these prompts carry ticket text."""
        self.run_cli(prompt="ticket text")
        command, kwargs = self.calls[0]
        self.assertEqual(kwargs["input"], "ticket text")
        self.assertNotIn("ticket text", command)

    def test_nothing_outside_the_call_can_change_what_the_model_is_told(self):
        """--tools "" only removes the tools. Without --setting-sources "" a
        .claude/settings.json beside this file, or one in the service account's home,
        joins every classification and every translation — hooks included."""
        command = self.command()
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_tools_stays_last(self):
        """It is variadic, so it swallows any following argument that does not begin
        with a dash — including the next flag's value."""
        self.assertEqual(self.command()[-2:], ["--tools", ""])

    def test_the_schema_is_enforced_by_the_cli(self):
        command = self.command()
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1]),
                         self.SCHEMA)
        self.assertIn("--no-session-persistence", command)

    def test_structured_output_beats_reparsing_the_result_string(self):
        """One less decode, and immune to prose alongside the JSON."""
        self.assertEqual(
            self.run_cli(response={"subtype": "success", "is_error": False,
                                   "structured_output": {"a": "from the object"},
                                   "result": '{"a": "from the string"}'}),
            {"a": "from the object"})

    def test_a_result_string_is_parsed_when_there_is_no_object(self):
        self.assertEqual(
            self.run_cli(response={"subtype": "success", "is_error": False,
                                   "result": '{"a": "b"}'}),
            {"a": "b"})

    def test_running_out_of_output_tokens_names_the_fix(self):
        """There is no --max-tokens to raise, so a batch too large to answer comes
        back as JSON that stops mid-object. Without this the parse below reports a
        syntax error for something whose only fix is a smaller batch."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(response={"subtype": "success", "is_error": False,
                                   "stop_reason": "max_tokens",
                                   "result": '{"a": "b'})
        self.assertIn("--batch-size", str(caught.exception))

    def test_a_successful_run_is_not_mistaken_for_a_truncated_one(self):
        """Structured output reports stop_reason "tool_use" on success, because that
        is how the schema is enforced underneath."""
        self.assertEqual(
            self.run_cli(response={"subtype": "success", "is_error": False,
                                   "stop_reason": "tool_use",
                                   "structured_output": {"a": "b"}}),
            {"a": "b"})

    def test_a_reported_failure_stops_the_run(self):
        for response in ({"subtype": "error_during_execution", "is_error": False},
                         {"subtype": "success", "is_error": True}):
            with self.subTest(response=response):
                with self.assertRaises(SystemExit):
                    self.run_cli(response=response)

    def test_a_non_zero_exit_reports_stderr_not_stdout(self):
        """A non-zero exit means there is no JSON to read, and the CLI could echo the
        prompt back — which this repo's public run logs must not carry."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(returncode=1, stderr="not logged in")
        self.assertIn("not logged in", str(caught.exception))

    def test_output_that_is_not_json_is_named_as_such(self):
        with self.assertRaises(SystemExit):
            self.run_cli(response="<html>proxy error</html>")

    def test_a_missing_cli_says_what_to_install(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(raises=FileNotFoundError())
        self.assertIn("PATH", str(caught.exception))

    def test_a_wedged_cli_does_not_hang_the_run(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(raises=triage.subprocess.TimeoutExpired("claude", 60))
        self.assertIn("60s", str(caught.exception))


# ---- Workflow wiring -------------------------------------------------------


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def unit_commands(unit="zendesk-digest.service"):
    """The command lines a systemd unit actually runs, continuations joined.

    Comments are stripped first: they explain the choices, and scanning them would
    have the flag check below demanding triage.py define words from prose.
    """
    with open(os.path.join(ROOT, "deploy", unit), encoding="utf-8") as fh:
        text = "\n".join(line for line in fh.read().splitlines()
                          if not line.lstrip().startswith("#"))
    return re.findall(r"^ExecStart=(.*)$", text.replace("\\\n", " "), re.MULTILINE)


class TestDigestOrdering(unittest.TestCase):
    """The digest is only correct if the positive-review resolver ran first: solved
    reviews leave the triage's `status<solved` query, so running second would have
    the digest re-count reviews the other script had just closed.

    This used to be two chained GitHub jobs; it is now two ExecStart lines. The
    invariant is the same and still lives entirely in configuration, which is why it
    is asserted here rather than trusted.
    """

    def test_the_resolver_runs_before_the_digest(self):
        commands = unit_commands()
        self.assertEqual(len(commands), 2, "expected exactly resolve then triage")
        self.assertIn("resolve_reviews.py", commands[0])
        self.assertIn("triage.py", commands[1])

    def test_a_failed_resolve_does_not_cost_the_digest(self):
        """Type=oneshot stops at the first failing ExecStart, which would make
        resolving a precondition for the digest — and it is an optimisation for it."""
        self.assertIn("||", unit_commands()[0],
                      "the resolver's failure must not stop the digest")

    def test_a_failed_resolve_is_still_reported(self):
        """A bare `-` prefix would also keep the failure from blocking the digest, and
        would hide it completely: the unit would succeed, OnFailure would never fire,
        and a resolver broken for weeks would look like one with nothing to do."""
        resolver = unit_commands()[0]
        self.assertFalse(resolver.startswith("-"),
                         "a - prefix swallows the failure instead of reporting it")
        self.assertIn("alert.py", resolver)

    def test_the_digest_is_not_prevented_from_failing_loudly(self):
        """The reverse for triage itself: a swallowed failure there is a silent day
        with no digest and no alert."""
        self.assertFalse(unit_commands()[1].startswith("-"))


class TestDigestFlags(unittest.TestCase):
    """These jobs only ever run on a timer, so a flag the script no longer defines
    surfaces as a failed run at 10am rather than at review time."""

    def flags(self, command):
        return set(re.findall(r"(--[a-z-]+)", command))

    def defined(self, source):
        return set(re.findall(r'add_argument\("(--[a-z-]+)"', source))

    def test_every_flag_the_unit_passes_to_triage_is_real(self):
        for flag in self.flags(unit_commands()[1]):
            self.assertIn(flag, self.defined(inspect.getsource(triage.main)),
                          msg=f"{flag} is not a triage.py flag")

    def test_the_unit_never_passes_dry_run(self):
        """A dry run posts nothing and records nothing, so the digest would go
        silently missing while every run looked green."""
        for command in unit_commands():
            self.assertNotIn("--dry-run", command)

    def test_the_digest_keeps_its_state_somewhere_persistent(self):
        """Without --state every run re-reports the whole window."""
        self.assertIn("--state", unit_commands()[1])


if __name__ == "__main__":
    unittest.main()
