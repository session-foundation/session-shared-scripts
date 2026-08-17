#!/usr/bin/env python3
"""Tests for the positive-review resolver.

Stdlib unittest, same as test_triage.py. Everything is offline — the Zendesk calls
run against a stub session. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

This script writes to Zendesk, so the tests lean on the guards rather than the happy
path: that a dry run cannot PUT, that only positive app-store reviews are selected,
and that an asynchronous job's failures are surfaced instead of swallowed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve_reviews  # noqa: E402
import triage  # noqa: E402
from test_triage import FakeResponse, FakeSession, NoSleep  # noqa: E402


def review(ticket_id, stars=5, channel="any_channel", subject=None):
    """An AppFollow-shaped review ticket: the rating lives in the subject."""
    if subject is None:
        subject = ("★" * stars) + ("☆" * (5 - stars)) + " Great app"
    return {
        "id": ticket_id,
        "result_type": "ticket",
        "subject": subject,
        "description": "d",
        "tags": ["appfollow"],
        "status": "new",
        "via": {"channel": channel},
    }


def human_ticket(ticket_id):
    return {
        "id": ticket_id,
        "result_type": "ticket",
        "subject": "Cannot log in after update",
        "description": "d",
        "tags": [],
        "status": "new",
        "via": {"channel": "email"},
    }


class TestQuery(unittest.TestCase):
    def test_only_untouched_reviews_are_eligible(self):
        """`new`, not `status<solved`: every open review sampled had an assignee, a
        group and an updated_at past its created_at, so something already handled it."""
        query = resolve_reviews.build_query()
        self.assertIn("status:new", query)
        self.assertNotIn("status<solved", query)

    def test_filters_to_the_appfollow_channel(self):
        self.assertIn("via:any_channel", resolve_reviews.build_query())


class TestSelection(unittest.TestCase):
    def select(self, tickets, min_stars=resolve_reviews.MIN_STARS):
        return resolve_reviews.select_resolvable(tickets, min_stars)

    def test_selects_four_and_five_star_reviews(self):
        resolvable, _ = self.select([review(1, stars=5), review(2, stars=4)])
        self.assertEqual([t["id"] for t in resolvable], [1, 2])

    def test_leaves_low_star_reviews_alone(self):
        """3★ and below are the ones that hide real bugs — the triage wants them."""
        resolvable, skipped = self.select([review(1, stars=3), review(2, stars=1)])
        self.assertEqual(resolvable, [])
        self.assertEqual(len(skipped), 2)
        self.assertIn("below the 4★ floor", skipped[0][1])

    def test_never_touches_a_non_review(self):
        resolvable, skipped = self.select([human_ticket(1)])
        self.assertEqual(resolvable, [])
        self.assertIn("not an app-store review", skipped[0][1])

    def test_skips_a_review_whose_stars_cannot_be_parsed(self):
        """Detected as a review by channel, but no rating to compare — leave it."""
        resolvable, skipped = self.select([review(1, subject="Conversation with x")])
        self.assertEqual(resolvable, [])
        self.assertIn("no star rating", skipped[0][1])

    def test_the_floor_is_fixed_at_four_stars(self):
        """Not a flag: lowering it would close the reviews the triage most wants."""
        self.assertEqual(resolve_reviews.MIN_STARS, 4)
        resolvable, _ = self.select([review(1, stars=4), review(2, stars=3)])
        self.assertEqual([t["id"] for t in resolvable], [1])

    def test_a_star_subject_counts_even_off_channel(self):
        """triage.is_store_review accepts either signal; the rating still decides."""
        resolvable, _ = self.select([review(1, stars=5, channel="email")])
        self.assertEqual([t["id"] for t in resolvable], [1])


class TestBatching(unittest.TestCase):
    def test_splits_at_the_update_many_limit(self):
        ids = list(range(250))
        sizes = [len(b) for b in resolve_reviews.batches(ids)]
        self.assertEqual(sizes, [100, 100, 50])

    def test_an_exact_multiple_produces_no_empty_batch(self):
        self.assertEqual([len(b) for b in resolve_reviews.batches(list(range(200)))],
                         [100, 100])

    def test_no_batches_for_nothing(self):
        self.assertEqual(list(resolve_reviews.batches([])), [])


class TestSolveBatch(unittest.TestCase):
    def session(self, payload=None, status_code=200):
        return FakeSession([FakeResponse(payload or {"job_status": {"id": "job1"}},
                                        status_code=status_code)])

    def test_puts_the_ids_as_a_query_parameter(self):
        session = self.session()
        resolve_reviews.solve_batch(session, "acme", [1, 2, 3], "t", None)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "PUT")
        self.assertIn("/tickets/update_many.json", url)
        self.assertEqual(kwargs["params"], {"ids": "1,2,3"})

    def test_adds_tags_without_replacing_the_ticket_s_own(self):
        """`tags` would overwrite; `additional_tags` is the documented way to add."""
        session = self.session()
        resolve_reviews.solve_batch(session, "acme", [1], "auto-resolved-review", None)
        body = session.calls[0][2]["json"]["ticket"]
        self.assertEqual(body["additional_tags"], ["auto-resolved-review"])
        self.assertNotIn("tags", body)

    def test_solves_rather_than_closes(self):
        """closed is irreversible; solved can be reopened."""
        session = self.session()
        resolve_reviews.solve_batch(session, "acme", [1], "t", None)
        self.assertEqual(session.calls[0][2]["json"]["ticket"]["status"], "solved")

    def test_the_note_is_private(self):
        """A public comment would email the person who wrote the review."""
        session = self.session()
        resolve_reviews.solve_batch(session, "acme", [1], "t", "closed automatically")
        comment = session.calls[0][2]["json"]["ticket"]["comment"]
        self.assertFalse(comment["public"])
        self.assertEqual(comment["body"], "closed automatically")

    def test_no_comment_key_when_there_is_no_note(self):
        session = self.session()
        resolve_reviews.solve_batch(session, "acme", [1], "t", None)
        self.assertNotIn("comment", session.calls[0][2]["json"]["ticket"])

    def test_an_http_error_exits(self):
        with self.assertRaises(SystemExit):
            resolve_reviews.solve_batch(self.session({}, status_code=422), "acme", [1], "t", None)

    def test_a_response_without_a_job_id_exits(self):
        """A 200 that queued nothing must not read as success."""
        with self.assertRaises(SystemExit):
            resolve_reviews.solve_batch(self.session({"job_status": {}}), "acme", [1], "t", None)


class TestWaitForJob(unittest.TestCase):
    def job(self, status, results=None):
        return FakeResponse({"job_status": {"id": "job1", "status": status,
                                            "results": results or []}})

    def test_polls_until_the_job_completes(self):
        session = FakeSession([
            self.job("queued"),
            self.job("working"),
            self.job("completed", [{"id": 1, "status": "Updated"}]),
        ])
        with NoSleep():
            solved, failures = resolve_reviews.wait_for_job(session, "acme", "job1")
        self.assertEqual((solved, failures), ([1], []))
        self.assertEqual(len(session.calls), 3)

    def test_per_ticket_failures_are_surfaced(self):
        """update_many is asynchronous: a 200 on the PUT says nothing about outcomes."""
        session = FakeSession([self.job("completed", [
            {"id": 1, "status": "Updated"},
            {"id": 2, "success": False, "error": "RecordInvalid"},
        ])])
        with NoSleep():
            solved, failures = resolve_reviews.wait_for_job(session, "acme", "job1")
        self.assertEqual(solved, [1])
        self.assertEqual([f["id"] for f in failures], [2])

    def test_a_failed_job_still_reports_its_results(self):
        session = FakeSession([self.job("failed", [{"id": 1, "error": "boom"}])])
        with NoSleep():
            solved, failures = resolve_reviews.wait_for_job(session, "acme", "job1")
        self.assertEqual((solved, len(failures)), ([], 1))

    def test_a_result_without_an_id_is_not_counted_as_solved(self):
        """It can't be attributed to a ticket, so it can't be tallied by rating —
        undercounting beats a summary that claims tickets it can't name."""
        session = FakeSession([self.job("completed", [
            {"id": 1, "status": "Updated"},
            {"status": "Updated"},
        ])])
        with NoSleep():
            solved, failures = resolve_reviews.wait_for_job(session, "acme", "job1")
        self.assertEqual((solved, failures), ([1], []))

    def test_a_job_that_never_finishes_exits(self):
        session = FakeSession([self.job("working") for _ in range(50)])
        with NoSleep():
            with self.assertRaises(SystemExit):
                resolve_reviews.wait_for_job(session, "acme", "job1", timeout=0)

    def test_an_http_error_exits(self):
        session = FakeSession([FakeResponse({}, status_code=500)] * 8)
        with NoSleep():
            with self.assertRaises(SystemExit):
                resolve_reviews.wait_for_job(session, "acme", "job1")


class TestSummaryMessage(unittest.TestCase):
    def test_tally_splits_by_rating(self):
        counts = resolve_reviews.tally_by_stars(
            [review(1, stars=5), review(2, stars=4), review(3, stars=5)])
        self.assertEqual(counts, {5: 2, 4: 1})

    def test_ratings_read_in_ascending_order(self):
        self.assertEqual(resolve_reviews.format_tally({5: 31, 4: 12}),
                         "**12** 4★ and **31** 5★")

    def test_a_single_rating_needs_no_conjunction(self):
        self.assertEqual(resolve_reviews.format_tally({5: 3}), "**3** 5★")

    def test_the_message_says_what_was_marked_resolved(self):
        message = resolve_reviews.build_message({4: 12, 5: 31})
        self.assertEqual(
            message,
            "✅ Marked **12** 4★ and **31** 5★ app-store reviews as solved in Zendesk.")

    def test_one_review_is_not_reviews(self):
        self.assertIn("**1** 5★ app-store review as solved",
                      resolve_reviews.build_message({5: 1}))

    def test_large_counts_are_grouped_for_reading(self):
        self.assertIn("**1,204** 5★", resolve_reviews.build_message({5: 1204}))

    def test_a_run_that_solved_nothing_says_nothing(self):
        """A weekly "0 reviews" post is noise the channel learns to skip."""
        self.assertIsNone(resolve_reviews.build_message({}))

    def test_failures_are_reported_next_to_the_count_they_contradict(self):
        message = resolve_reviews.build_message({5: 2}, failures=3)
        self.assertIn("**2** 5★", message)
        self.assertIn("**3** tickets failed to update", message)

    def test_failures_alone_still_post(self):
        message = resolve_reviews.build_message({}, failures=1)
        self.assertIn("**1** ticket failed to update", message)
        self.assertNotIn("Marked", message)

    def test_the_leftover_line_appears_only_when_there_is_a_leftover(self):
        self.assertNotIn("📥", resolve_reviews.build_message({5: 2}))
        self.assertIn("**4,769** more tickets match",
                      resolve_reviews.build_message({5: 2}, remaining=4769))

    def test_a_dry_run_message_is_hypothetical(self):
        message = resolve_reviews.build_message({5: 2}, dry_run=True)
        self.assertIn("Would mark", message)
        self.assertNotIn("Marked", message)

    def test_the_message_fits_one_discord_post(self):
        """No chunking here, unlike the triage — a tally can't grow into a second
        message, and this proves the worst case stays inside the cap."""
        message = resolve_reviews.build_message({4: 999999, 5: 999999},
                                                remaining=999999, failures=999999)
        self.assertLess(len(message), triage.MAX_MESSAGE_CHARS)

    def test_the_summary_does_not_reuse_the_zendesk_session(self):
        """That session carries the API-token auth header; Discord must not see it."""
        posted = []
        original = triage.post_to_discord
        triage.post_to_discord = lambda session, url, messages: (
            posted.append((session, url, messages)) or len(messages))
        try:
            self.assertTrue(resolve_reviews.post_summary("https://hook", "hi"))
        finally:
            triage.post_to_discord = original
        session, url, messages = posted[0]
        self.assertIsNone(session.auth)
        self.assertEqual((url, messages), ("https://hook", [{"content": "hi"}]))


class TestSharedDetectionIsNotReimplemented(unittest.TestCase):
    """The review detection is subtle — via.channel or a leading ★ run, no false
    positives in a 3,662-ticket sample. A second copy here would drift from the
    triage's, so both scripts must call the same functions."""

    def test_detection_comes_from_triage(self):
        self.assertIs(resolve_reviews.triage.is_store_review, triage.is_store_review)
        self.assertIs(resolve_reviews.triage.review_stars, triage.review_stars)

    def test_the_star_floor_matches_what_the_triage_skips(self):
        """The triage counts reviews above its floor without classifying them; this
        job solves exactly that set, so the two numbers cannot disagree."""
        self.assertEqual(resolve_reviews.MIN_STARS,
                         triage.DEFAULT_REVIEW_STAR_FLOOR + 1)


if __name__ == "__main__":
    unittest.main()
