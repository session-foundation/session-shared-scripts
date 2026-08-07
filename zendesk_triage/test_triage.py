#!/usr/bin/env python3
"""Tests for the Zendesk triage script.

Stdlib unittest so the repo needs no test dependency. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

Everything here is offline — no Zendesk, Anthropic, or Discord calls. The fetch
tests drive fetch_tickets with a stub session instead.
"""
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
        query = triage.build_window_query(48)
        cutoff = query.split("created>")[1].split(" ")[0]
        parsed = datetime.strptime(cutoff, STAMP).replace(tzinfo=timezone.utc)
        expected = datetime.now(timezone.utc) - timedelta(hours=48)
        self.assertLess(abs((parsed - expected).total_seconds()), 120)

    def test_query_keeps_unsolved_filter_and_newest_first_ordering(self):
        query = triage.build_window_query(48)
        self.assertIn("type:ticket", query)
        self.assertIn("status<solved", query)
        self.assertIn("order_by:created_at", query)
        self.assertIn("sort:desc", query)

    def test_a_longer_window_reaches_further_back(self):
        short = triage.build_window_query(48).split("created>")[1].split(" ")[0]
        long = triage.build_window_query(168).split("created>")[1].split(" ")[0]
        self.assertLess(long, short)  # ISO-8601 sorts chronologically

    def test_window_label_reads_naturally(self):
        self.assertEqual(triage.window_label(24), "created in the past 1 day")
        self.assertEqual(triage.window_label(48), "created in the past 2 days")
        self.assertEqual(triage.window_label(168), "created in the past 7 days")
        self.assertEqual(triage.window_label(36), "created in the past 36h")


# ---- Dedup state -----------------------------------------------------------


class TestPartitionByState(unittest.TestCase):
    def test_empty_state_makes_everything_new(self):
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1), ticket(2)], triage.empty_state()
        )
        self.assertEqual([t["id"] for t in new], [1, 2])
        self.assertEqual(changed, [])
        self.assertEqual(unchanged, [])

    def test_same_updated_at_is_unchanged(self):
        state = {"seen": {"1": {"updated_at": "2026-08-03T12:00:00Z"}}}
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1, updated_at="2026-08-03T12:00:00Z")], state
        )
        self.assertEqual((new, changed), ([], []))
        self.assertEqual([t["id"] for t in unchanged], [1])

    def test_moved_updated_at_is_changed(self):
        state = {"seen": {"1": {"updated_at": "2026-08-03T12:00:00Z"}}}
        new, changed, unchanged = triage.partition_by_state(
            [ticket(1, updated_at="2026-08-04T09:00:00Z")], state
        )
        self.assertEqual((new, unchanged), ([], []))
        self.assertEqual([t["id"] for t in changed], [1])

    def test_mixed_batch_splits_three_ways(self):
        state = {
            "seen": {
                "1": {"updated_at": "2026-08-03T12:00:00Z"},
                "2": {"updated_at": "2026-08-01T00:00:00Z"},
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
        state = {"seen": {"27564": {"updated_at": "2026-08-03T12:00:00Z"}}}
        _, _, unchanged = triage.partition_by_state(
            [ticket(27564, updated_at="2026-08-03T12:00:00Z")], state
        )
        self.assertEqual(len(unchanged), 1)


class TestStateRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "nested", "seen.json")
        self.addCleanup(self.dir.cleanup)

    def test_save_then_load_recovers_reported_tickets(self):
        triage.save_state(self.path, triage.empty_state(), [ticket(1), ticket(2)], 30)
        state = triage.load_state(self.path)
        self.assertEqual(sorted(state["seen"]), ["1", "2"])
        self.assertEqual(state["seen"]["1"]["updated_at"], "2026-08-03T12:00:00Z")
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
        self.assertEqual(triage.load_state(self.path)["seen"]["1"]["updated_at"], "B")

    def test_entries_past_retention_are_pruned(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(STAMP)
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(STAMP)
        state = {
            "version": 1,
            "seen": {
                "1": {"updated_at": "A", "last_reported": old},
                "2": {"updated_at": "B", "last_reported": recent},
            },
        }
        kept, pruned = triage.save_state(self.path, state, [], 30)
        self.assertEqual((kept, pruned), (1, 1))
        self.assertEqual(list(triage.load_state(self.path)["seen"]), ["2"])

    def test_entries_with_unparseable_timestamps_are_dropped(self):
        state = {"version": 1, "seen": {"1": {"updated_at": "A", "last_reported": "nonsense"}}}
        kept, pruned = triage.save_state(self.path, state, [], 30)
        self.assertEqual((kept, pruned), (0, 1))

    def test_a_ticket_reported_now_survives_pruning(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(STAMP)
        state = {"version": 1, "seen": {"1": {"updated_at": "A", "last_reported": old}}}
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


class TestSummaryEmbed(unittest.TestCase):
    def description(self, findings, stats):
        highlights = [f for f in findings if f.get("worth_looking_into")]
        return triage.build_summary_embed(findings, highlights, "acme", stats)["description"]

    def test_reports_analyzed_against_matched(self):
        text = self.description([finding(1)], {"matched": 47})
        self.assertIn("Analyzed **1** of **47** tickets in the window", text)

    def test_names_the_window(self):
        text = self.description([finding(1)], {"matched": 5, "scope": "created in the past 2 days"})
        self.assertIn("(created in the past 2 days)", text)

    def test_reports_skipped_unchanged_tickets(self):
        text = self.description([finding(1)], {"matched": 47, "skipped_unchanged": 45})
        self.assertIn("Skipped **45** already reported and unchanged", text)

    def test_omits_the_skip_line_when_nothing_was_skipped(self):
        self.assertNotIn("Skipped", self.description([finding(1)], {"skipped_unchanged": 0}))

    def test_reports_the_untriaged_backlog_with_thousands_separators(self):
        text = self.description([finding(1)], {"total_unsolved": 5609})
        self.assertIn("Backlog: **5,609** unsolved tickets in total", text)

    def test_omits_the_backlog_line_when_the_count_is_unavailable(self):
        self.assertNotIn("Backlog", self.description([finding(1)], {"total_unsolved": None}))

    def test_flags_how_many_were_re_reports(self):
        text = self.description([finding(1)], {"updated_count": 3})
        self.assertIn("🔄 **3** changed since last reported", text)

    def test_counts_crash_and_data_loss_as_serious(self):
        findings = [finding(1, severity="crash"), finding(2, severity="data_loss")]
        self.assertIn("**2** crash/data-loss", self.description(findings, {}))

    def test_works_with_no_stats_at_all(self):
        text = self.description([finding(1)], None)
        self.assertIn("Analyzed **1**", text)
        self.assertNotIn("of **", text)

    def test_groups_repeated_clusters(self):
        findings = [finding(1, cluster="push"), finding(2, cluster="push"), finding(3, cluster="solo")]
        embed = triage.build_summary_embed(findings, findings, "acme", {})
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("Likely duplicate clusters", names)
        clusters = next(f for f in embed["fields"] if f["name"] == "Likely duplicate clusters")
        self.assertIn("push", clusters["value"])
        self.assertNotIn("solo", clusters["value"])  # a single ticket is not a cluster


class TestHighlightEmbed(unittest.TestCase):
    def test_update_marker_only_appears_for_re_reports(self):
        fresh = triage.build_highlight_embed(finding(1), "acme", is_update=False)
        repeat = triage.build_highlight_embed(finding(1), "acme", is_update=True)
        self.assertFalse(fresh["title"].startswith("🔄"))
        self.assertTrue(repeat["title"].startswith("🔄"))

    def test_links_back_to_the_ticket(self):
        embed = triage.build_highlight_embed(finding(42), "acme")
        self.assertEqual(embed["url"], "https://acme.zendesk.com/agent/tickets/42")

    def test_title_stays_within_the_discord_limit(self):
        embed = triage.build_highlight_embed(finding(1, summary="x" * 500), "acme", is_update=True)
        self.assertLessEqual(len(embed["title"]), 256)


class TestBuildMessages(unittest.TestCase):
    def test_only_tickets_worth_looking_into_get_their_own_embed(self):
        findings = [finding(1), finding(2, worth_looking_into=False)]
        messages = build_messages(findings, "acme")
        self.assertEqual(len(messages[0]["embeds"]), 2)  # summary + one highlight

    def test_highlights_are_ordered_by_priority_rank(self):
        findings = [finding(1, priority_rank=3), finding(2, priority_rank=1)]
        embeds = build_messages(findings, "acme")[0]["embeds"]
        self.assertIn("#2", embeds[1]["title"])
        self.assertIn("#1", embeds[2]["title"])

    def test_updated_ids_reach_the_right_embed(self):
        findings = [finding(1), finding(2)]
        embeds = build_messages(findings, "acme", {}, updated_ids={2})[0]["embeds"]
        titles = {e["title"].lstrip("🔄 ").split(" ")[0]: e["title"] for e in embeds[1:]}
        self.assertFalse(titles["#1"].startswith("🔄"))
        self.assertTrue(titles["#2"].startswith("🔄"))

    def test_embeds_are_chunked_to_the_discord_per_message_limit(self):
        findings = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS)]
        messages = build_messages(findings, "acme")
        for message in messages:
            self.assertLessEqual(len(message["embeds"]), triage.MAX_EMBEDS_PER_MESSAGE)
        total = sum(len(m["embeds"]) for m in messages)
        self.assertEqual(total, triage.MAX_HIGHLIGHTS + 1)  # + the summary

    def test_highlights_beyond_the_cap_are_dropped_but_announced(self):
        over = triage.MAX_HIGHLIGHTS + 5
        findings = [finding(i, priority_rank=i) for i in range(over)]
        messages = build_messages(findings, "acme")
        self.assertIn(f"top {triage.MAX_HIGHLIGHTS} of {over}", messages[0]["content"])

    def test_no_content_line_when_nothing_was_dropped(self):
        messages = build_messages([finding(1)], "acme")
        self.assertNotIn("content", messages[0])


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

    def test_platform_enum_is_wired_into_the_schema(self):
        item = triage.SCHEMA["properties"]["tickets"]["items"]
        self.assertEqual(item["properties"]["platform"]["enum"], triage.PLATFORMS)


class TestUrgency(unittest.TestCase):
    def test_urgent_category_beats_a_benign_severity(self):
        """An abuse report is not a bug, so severity is not_applicable — which used to
        paint the most serious ticket in the digest the calmest colour."""
        abuse = finding(1, category="abuse_report", severity="not_applicable")
        self.assertEqual(triage.embed_color(abuse), triage.CATEGORY_COLOR["abuse_report"])
        self.assertNotEqual(triage.embed_color(abuse),
                            triage.SEVERITY_COLOR["not_applicable"])

    def test_non_urgent_category_still_uses_severity(self):
        self.assertEqual(triage.embed_color(finding(1, category="bug_report", severity="crash")),
                         triage.SEVERITY_COLOR["crash"])

    def test_unknown_severity_falls_back_to_grey(self):
        self.assertEqual(triage.embed_color({"category": "other", "severity": "???"}), 0x95A5A6)

    def test_urgent_tickets_are_highlighted_even_if_not_flagged(self):
        abuse = finding(1, category="abuse_report", worth_looking_into=False)
        shown, _ = triage.select_highlights([abuse])
        self.assertEqual([f["id"] for f in shown], [1])

    def test_urgent_tickets_sort_ahead_of_better_ranked_ordinary_ones(self):
        ordinary = finding(1, category="bug_report", priority_rank=1)
        abuse = finding(2, category="abuse_report", priority_rank=99)
        shown, _ = triage.select_highlights([ordinary, abuse])
        self.assertEqual([f["id"] for f in shown], [2, 1])

    def test_urgent_tickets_cannot_be_pushed_out_by_the_display_cap(self):
        ordinary = [finding(i, priority_rank=i) for i in range(triage.MAX_HIGHLIGHTS + 5)]
        abuse = finding(9999, category="abuse_report", priority_rank=9999)
        shown, omitted = triage.select_highlights(ordinary + [abuse])
        self.assertIn(9999, [f["id"] for f in shown])
        self.assertNotIn(9999, [f["id"] for f in omitted])


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


class TestEmbedCharLimit(unittest.TestCase):
    """Discord caps a message at 10 embeds *and* 6,000 chars across them; chunking on
    count alone can build a payload Discord rejects."""

    def fat(self, ticket_id):
        # ~1,300 chars of field text: 10 of these would be ~13,000, over the limit.
        return finding(ticket_id, summary="s" * 200, likely_root_cause="r" * 300,
                       affected_component="c" * 100, language="l" * 40)

    def test_every_message_respects_both_limits(self):
        findings = [self.fat(i) for i in range(triage.MAX_HIGHLIGHTS)]
        for message in build_messages(findings, "acme"):
            self.assertLessEqual(len(message["embeds"]), triage.MAX_EMBEDS_PER_MESSAGE)
            total = sum(triage.embed_char_count(e) for e in message["embeds"])
            self.assertLessEqual(total, triage.MAX_EMBED_CHARS_PER_MESSAGE)

    def test_char_limit_splits_where_the_count_limit_would_not(self):
        """9 fat highlights + summary = 10 embeds: within the count limit, over 6,000 chars."""
        messages = build_messages([self.fat(i) for i in range(9)], "acme")
        embeds = sum(len(m["embeds"]) for m in messages)
        self.assertLessEqual(embeds, triage.MAX_EMBEDS_PER_MESSAGE)  # count alone: 1 message
        self.assertGreater(len(messages), 1)                         # chars forced the split

    def test_lean_embeds_are_not_split_early(self):
        """The char limit must not fragment ordinary digests."""
        messages = build_messages([finding(i, priority_rank=i) for i in range(9)], "acme")
        self.assertEqual(len(messages), 1)

    def test_no_embed_is_dropped_while_chunking(self):
        findings = [self.fat(i) for i in range(15)]
        messages = build_messages(findings, "acme")
        self.assertEqual(sum(len(m["embeds"]) for m in messages), 16)  # 15 + summary

    def test_char_count_covers_titles_descriptions_and_fields(self):
        embed = {"title": "abc", "description": "de",
                 "fields": [{"name": "fg", "value": "hij"}]}
        self.assertEqual(triage.embed_char_count(embed), 3 + 2 + 2 + 3)

    def test_char_count_tolerates_missing_keys(self):
        self.assertEqual(triage.embed_char_count({}), 0)


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
        _, coverage = triage.build_messages(findings, "acme")
        flat = [tid for ids in coverage for tid in ids]
        self.assertEqual(len(flat), len(set(flat)))


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
        """A hand-edited --backend file findings list has nothing enforcing its shape,
        so an entry without category/severity would KeyError in build_summary_embed."""
        for entry in ({"id": 1}, {"id": 1, "category": "bug_report"},
                      {"category": "bug_report", "severity": "major"}):
            with self.assertRaises(SystemExit):
                triage.tickets_from_payload({"tickets": [entry]}, "x")

    def test_a_non_object_entry_exits(self):
        with self.assertRaises(SystemExit):
            triage.tickets_from_payload({"tickets": [["not", "an", "object"]]}, "x")


class TestCliIsolation(unittest.TestCase):
    """The claude-cli invocation is locked down because ticket text is untrusted and
    the runner has a checkout. Each of these fails silently if broken: a wrong deny
    list returns prose instead of findings, a missing flag loads the repo's config."""

    def test_structured_output_is_never_denied(self):
        """--json-schema is implemented as the StructuredOutput tool, so denying it —
        or passing a `*` wildcard — makes the run return prose and no findings."""
        denied = triage.CLI_DENIED_TOOLS.split()
        self.assertNotIn("StructuredOutput", denied)
        self.assertNotIn("*", denied)

    def test_tools_with_side_effects_are_denied(self):
        denied = triage.CLI_DENIED_TOOLS.split()
        for tool in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task"):
            self.assertIn(tool, denied)

    def test_the_system_prompt_travels_as_a_flag(self):
        """Not on stdin with the tickets: stdin is untrusted input, the prompt isn't."""
        self.assertIn("--system-prompt", triage.CLI_ISOLATION_ARGS)
        self.assertIn(triage.SYSTEM_PROMPT, triage.CLI_ISOLATION_ARGS)

    def test_no_setting_sources_are_loaded(self):
        """An empty value is what keeps hooks, plugins, skills and CLAUDE.md out."""
        args = triage.CLI_ISOLATION_ARGS
        self.assertEqual(args[args.index("--setting-sources") + 1], "")

    def test_mcp_config_is_strict(self):
        self.assertIn("--strict-mcp-config", triage.CLI_ISOLATION_ARGS)


class TestResolveApiModel(unittest.TestCase):
    """The CLI resolves aliases itself; the API takes ids, so only that path maps."""

    def test_every_alias_maps_to_an_id(self):
        for alias, model_id in triage.API_MODEL_ALIASES.items():
            self.assertEqual(triage.resolve_api_model(alias), model_id)
            self.assertTrue(model_id.startswith("claude-"), model_id)

    def test_the_default_model_is_mappable(self):
        """DEFAULT_MODEL is an alias, so --backend api would 404 without an entry."""
        self.assertIn(triage.DEFAULT_MODEL, triage.API_MODEL_ALIASES)

    def test_a_full_id_passes_through(self):
        self.assertEqual(triage.resolve_api_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_an_unknown_value_passes_through(self):
        """A model newer than this table should reach the API rather than be rewritten."""
        self.assertEqual(triage.resolve_api_model("claude-future-9"), "claude-future-9")


class TestFindingsFromCliEnvelope(unittest.TestCase):
    def envelope(self, **overrides):
        base = {
            "subtype": "success",
            "is_error": False,
            "structured_output": {"tickets": [finding(1)]},
        }
        base.update(overrides)
        return base

    def test_reads_structured_output(self):
        self.assertEqual(triage.findings_from_cli_envelope(self.envelope()), [finding(1)])

    def test_missing_structured_output_exits(self):
        """A CLI too old for --json-schema returns prose in `result` and no
        structured_output; without this check the digest comes out silently empty."""
        stale = self.envelope(result='{"tickets": []}')
        del stale["structured_output"]
        with self.assertRaises(SystemExit):
            triage.findings_from_cli_envelope(stale)

    def test_a_reported_cli_error_exits(self):
        for envelope in (self.envelope(is_error=True),
                         self.envelope(subtype="error_max_turns")):
            with self.assertRaises(SystemExit):
                triage.findings_from_cli_envelope(envelope)

    def test_a_cost_field_is_reported_not_fatal(self):
        result = triage.findings_from_cli_envelope(self.envelope(total_cost_usd=0.42))
        self.assertEqual(result, [finding(1)])


class TestExtractJsonObject(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(triage.extract_json_object('{"a": 1}'), {"a": 1})

    def test_object_inside_a_markdown_fence(self):
        self.assertEqual(triage.extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_object_surrounded_by_prose(self):
        self.assertEqual(
            triage.extract_json_object('Sure! Here you go:\n{"a": 1}\nHope that helps.'), {"a": 1}
        )

    def test_nested_braces_survive(self):
        self.assertEqual(
            triage.extract_json_object('{"t": [{"id": 1}, {"id": 2}]}'),
            {"t": [{"id": 1}, {"id": 2}]},
        )

    def test_no_object_exits(self):
        with self.assertRaises(SystemExit):
            triage.extract_json_object("no json here")

    def test_malformed_object_exits(self):
        with self.assertRaises(SystemExit):
            triage.extract_json_object('{"a": }')


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


if __name__ == "__main__":
    unittest.main()
