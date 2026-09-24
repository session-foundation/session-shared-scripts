"""
    python -m unittest discover        # from github_prs/
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import digest

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(hours=25)


def pr(number=1, login="octocat", repo="session-android", created="2026-09-24T08:00:00Z",
       updated=None, title="Fix a thing", user_type="User", **extra):
    item = {
        "id": 10_000 + number,
        "number": number,
        "title": title,
        "html_url": f"https://github.com/session-foundation/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/session-foundation/{repo}",
        "user": {"login": login, "type": user_type},
        "created_at": created,
        "updated_at": updated or created,
    }
    item.update(extra)
    return item


class TestMaintainers(unittest.TestCase):
    def test_comments_and_blanks_are_ignored(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=
                "# a comment\n\nBilb\njagerman  # trailing\n")):
            self.assertEqual(digest.load_maintainers("x"), {"bilb", "jagerman"})

    def test_the_shipped_list_parses_and_is_unique(self):
        with open(digest.MAINTAINERS_FILE, encoding="utf-8") as handle:
            lines = [line.split("#", 1)[0].strip() for line in handle]
        logins = [line for line in lines if line]
        self.assertTrue(logins)
        self.assertEqual(len(logins), len(set(login.lower() for login in logins)))


class TestSelection(unittest.TestCase):
    repos = {"session-android", "session-desktop"}
    maintainers = frozenset({"bilb", "mpretty-cyro"})

    def select(self, items):
        return digest.contributor_prs(items, self.repos, self.maintainers)

    def test_maintainers_are_dropped_whatever_the_case(self):
        self.assertEqual(self.select([pr(login="BiLb")]), [])

    def test_bots_are_dropped_on_account_type(self):
        self.assertEqual(self.select([pr(login="dependabot[bot]", user_type="Bot")]), [])

    def test_repos_outside_the_allowed_set_are_dropped(self):
        self.assertEqual(self.select([pr(repo="session-pysogs")]), [])

    def test_a_contributor_pr_is_kept(self):
        self.assertEqual(len(self.select([pr(login="someone")])), 1)


class TestWindow(unittest.TestCase):
    def test_only_prs_that_moved_since_the_cutoff_are_considered(self):
        fresh = pr(1, updated="2026-09-24T09:00:00Z")
        stale = pr(2, created="2026-08-01T08:00:00Z", updated="2026-08-02T08:00:00Z")
        self.assertEqual([p["number"] for p in digest.in_window([fresh, stale], CUTOFF)],
                         [1])

    def test_the_cutoff_itself_counts_as_inside_the_window(self):
        edge = pr(1, updated=CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(len(digest.in_window([edge], CUTOFF)), 1)

    def test_an_old_pr_that_just_moved_is_in(self):
        """New means never reported, not recently opened — a year-old PR that picks up
        a comment is exactly what the digest is for."""
        old = pr(1, created="2025-01-01T00:00:00Z", updated="2026-09-24T09:00:00Z")
        self.assertEqual(len(digest.in_window([old], CUTOFF)), 1)


class TestPartitionByState(unittest.TestCase):
    def state(self, *records):
        return {"version": digest.STATE_VERSION,
                "seen": {key: {"updated_at": stamp} for key, stamp in records}}

    def test_a_pr_never_reported_is_new(self):
        new, changed, unchanged = digest.partition_by_state([pr(1)], digest.empty_state())
        self.assertEqual([p["number"] for p in new], [1])
        self.assertEqual((changed, unchanged), ([], []))

    def test_a_pr_that_moved_since_it_was_reported_is_changed(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        new, changed, unchanged = digest.partition_by_state(
            [item], self.state((digest.pr_id(item), "2026-09-20T09:00:00Z")))
        self.assertEqual([p["number"] for p in changed], [1])
        self.assertEqual((new, unchanged), ([], []))

    def test_a_pr_that_has_not_moved_is_dropped(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        new, changed, unchanged = digest.partition_by_state(
            [item], self.state((digest.pr_id(item), "2026-09-24T09:00:00Z")))
        self.assertEqual([p["number"] for p in unchanged], [1])
        self.assertEqual((new, changed), ([], []))


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "sub", "seen.json")

    def save(self, *prs, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return digest.save_state(self.path, digest.empty_state(), list(prs), **kwargs)

    def load(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return digest.load_state(self.path)

    def test_a_reported_pr_comes_back_unchanged_next_run(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        self.save(item)
        _, _, unchanged = digest.partition_by_state([item], self.load())
        self.assertEqual(len(unchanged), 1)

    def test_the_same_pr_moved_comes_back_changed(self):
        self.save(pr(1, updated="2026-09-24T09:00:00Z"))
        _, changed, _ = digest.partition_by_state(
            [pr(1, updated="2026-09-24T11:00:00Z")], self.load())
        self.assertEqual(len(changed), 1)

    def test_the_file_names_the_pr_for_whoever_opens_it(self):
        self.save(pr(1958, repo="session-desktop"))
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("session-desktop#1958", handle.read())

    def test_no_path_means_no_state_and_no_complaint(self):
        self.assertEqual(digest.load_state(None), digest.empty_state())

    def test_every_unreadable_state_is_a_cache_miss_not_an_error(self):
        """Losing it re-reports the window once. Failing the run instead would mean a
        corrupt cache stops the digest entirely."""
        for content in ("{ not json", '{"version": 1}', "[]",
                        '{"version": 99, "seen": {}}'):
            with self.subTest(content=content):
                with open(self.path.replace("sub/", ""), "w", encoding="utf-8") as fh:
                    fh.write(content)
                with contextlib.redirect_stdout(io.StringIO()):
                    state = digest.load_state(self.path.replace("sub/", ""))
                self.assertEqual(state, digest.empty_state())

    def test_a_missing_file_is_a_cache_miss(self):
        self.assertEqual(self.load(), digest.empty_state())

    def test_entries_are_pruned_past_the_retention(self):
        self.save(pr(1))
        state = self.load()
        for record in state["seen"].values():
            record["last_reported"] = "2026-01-01T00:00:00Z"
        with contextlib.redirect_stdout(io.StringIO()):
            kept, pruned = digest.save_state(self.path, state, [], retention_days=30)
        self.assertEqual((kept, pruned), (0, 1))

    def test_a_malformed_entry_is_dropped_rather_than_kept_forever(self):
        state = {"version": digest.STATE_VERSION,
                 "seen": {"1": {"updated_at": "x", "last_reported": "never"}}}
        with contextlib.redirect_stdout(io.StringIO()):
            kept, _ = digest.save_state(self.path, state, [])
        self.assertEqual(kept, 0)

    def test_nothing_is_left_behind_by_the_atomic_write(self):
        self.save(pr(1))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["seen.json"])


class TestAge(unittest.TestCase):
    def test_coarsens_as_it_grows(self):
        for delta, expected in [(timedelta(minutes=40), "40m"),
                                (timedelta(hours=6), "6h"),
                                (timedelta(hours=47), "47h"),
                                (timedelta(days=3), "3d"),
                                (timedelta(days=35), "5w")]:
            self.assertEqual(digest.age(NOW - delta, NOW), expected)

    def test_a_clock_skewed_future_timestamp_reads_as_zero(self):
        self.assertEqual(digest.age(NOW + timedelta(minutes=5), NOW), "0m")


class TestLines(unittest.TestCase):
    def test_a_new_pr_line_carries_the_link_author_and_age(self):
        line = digest.build_pr_line(pr(2151, login="octocat"), NOW, True)
        self.assertIn(digest.NEW_MARKER, line)
        self.assertIn("[#2151](https://github.com/session-foundation/session-android/pull/2151)", line)
        self.assertIn("@octocat", line)
        self.assertIn("4h", line)

    def test_an_updated_line_ages_from_the_update_not_the_creation(self):
        item = pr(7, created="2026-09-01T12:00:00Z", updated="2026-09-24T09:00:00Z")
        line = digest.build_pr_line(item, NOW, False)
        self.assertIn(digest.UPDATED_MARKER, line)
        self.assertIn("3h", line)

    def test_drafts_and_comment_counts_are_marked(self):
        line = digest.build_pr_line(pr(9, draft=True, comments=3), NOW, True)
        self.assertIn("[draft]", line)
        self.assertIn("💬3", line)

    def test_a_pr_with_no_comments_says_nothing_about_them(self):
        self.assertNotIn("💬", digest.build_pr_line(pr(9, comments=0), NOW, True))

    def test_long_titles_are_clipped(self):
        line = digest.build_pr_line(pr(9, title="x" * 200), NOW, True)
        self.assertIn("…", line)
        self.assertLess(len(line), 200)


class TestGrouping(unittest.TestCase):
    def test_the_busiest_repo_comes_first_and_new_leads_its_block(self):
        new = [pr(1, repo="session-ios"), pr(2, repo="session-android"),
               pr(3, repo="session-android")]
        updated = [pr(4, repo="session-android")]
        blocks = digest.group_by_repo(new, updated, NOW)
        self.assertTrue(blocks[0][0].startswith("**session-android**"))
        android = blocks[0][0].splitlines()
        self.assertEqual(len(android), 4)
        self.assertEqual(android.count(""), 0)
        self.assertIn(digest.NEW_MARKER, android[1])
        self.assertIn(digest.UPDATED_MARKER, android[3])

    def test_each_group_sorts_on_the_age_it_shows(self):
        new = [pr(1, created="2026-09-01T00:00:00Z", updated="2026-09-24T00:00:00Z"),
               pr(2, created="2026-09-20T00:00:00Z", updated="2026-09-21T00:00:00Z")]
        updated = [pr(3, created="2026-01-01T00:00:00Z", updated="2026-09-22T00:00:00Z"),
                   pr(4, created="2026-01-01T00:00:00Z", updated="2026-09-23T00:00:00Z")]
        lines = digest.group_by_repo(new, updated, NOW)[0][0].splitlines()
        self.assertEqual([line.split("[#")[1].split("]")[0] for line in lines[1:]],
                         ["2", "1", "4", "3"])

    def test_a_repo_appears_once_however_many_prs_it_has(self):
        blocks = digest.group_by_repo([pr(1), pr(2), pr(3)], [], NOW)
        self.assertEqual(len(blocks), 1)

    def test_a_block_accounts_for_every_pr_it_shows(self):
        """What reached Discord is recorded per message, so a partial post cannot
        suppress the PRs that never went out."""
        prs = [pr(1), pr(2)]
        _, ids = digest.group_by_repo(prs, [], NOW)[0]
        self.assertEqual(ids, {digest.pr_id(p) for p in prs})


class TestHeader(unittest.TestCase):
    def test_counts_both_kinds_and_the_backlog(self):
        header = digest.build_header([pr(1)], [pr(2), pr(3)], 12, 25, False)
        self.assertIn("**1** new", header)
        self.assertIn("**2** updated", header)
        self.assertIn("**12** open from contributors", header)
        self.assertIn("last 25h", header)

    def test_a_quiet_day_says_so(self):
        header = digest.build_header([], [], 12, 25, False)
        self.assertIn("Nothing opened or updated.", header)
        self.assertIn("**12** open", header)

    def test_a_long_window_reads_in_days(self):
        self.assertIn("last 3 days", digest.build_header([], [], 0, 72, False))
        self.assertIn("last 1 day", digest.build_header([], [], 0, 24, False))
        self.assertIn("last 25h", digest.build_header([], [], 0, 25, False))

    def test_truncation_is_declared(self):
        self.assertIn("floor", digest.build_header([], [], 1000, 25, True))


class TestChunking(unittest.TestCase):
    def test_splits_on_the_component_count(self):
        blocks = ["x"] * (digest.MAX_COMPONENTS_PER_MESSAGE + 1)
        chunks = digest.chunk_blocks(blocks)
        self.assertEqual([len(chunk) for chunk in chunks],
                         [digest.MAX_COMPONENTS_PER_MESSAGE, 1])

    def test_splits_on_the_character_budget(self):
        blocks = ["x" * 2500, "y" * 2500]
        self.assertEqual(len(digest.chunk_blocks(blocks)), 2)

    def test_the_header_is_charged_to_the_first_message_only(self):
        block = "x" * 1900
        chunks = digest.chunk_blocks([block, block], first_used=0)
        self.assertEqual(len(chunks), 1)
        chunks = digest.chunk_blocks([block, block], first_used=1000)
        self.assertEqual(len(chunks), 2)

    def test_an_oversized_block_still_gets_a_message(self):
        blocks = digest.chunk_blocks(["x" * (digest.MAX_MESSAGE_TEXT_CHARS + 100)])
        self.assertEqual(len(blocks), 1)


class TestMessages(unittest.TestCase):
    def test_a_quiet_day_still_posts_the_header(self):
        messages, coverage = digest.build_messages([], [], 4, 25, NOW)
        self.assertEqual(len(messages), 1)
        self.assertEqual(coverage, [set()])
        blocks = messages[0]["components"][0]["components"]
        self.assertEqual(len(blocks), 1)
        self.assertIn("Nothing opened", blocks[0]["content"])

    def test_the_payload_is_components_v2(self):
        message = digest.build_messages([pr(1)], [], 1, 25, NOW)[0][0]
        self.assertEqual(message["flags"], digest.COMPONENTS_V2_FLAG)
        self.assertEqual(message["components"][0]["type"], digest.CONTAINER)

    def test_only_the_first_message_carries_the_header(self):
        new = [pr(n, repo=f"repo-{n}", title="y" * 80) for n in range(40)]
        messages, coverage = digest.build_messages(new, [], 40, 25, NOW)
        self.assertGreater(len(messages), 1)
        self.assertEqual(set().union(*coverage), {digest.pr_id(p) for p in new})
        for message in messages[1:]:
            rendered = json.dumps(message, ensure_ascii=False)
            self.assertNotIn("Contributor pull requests", rendered)
            self.assertLess(len(rendered), digest.MAX_MESSAGE_TEXT_CHARS * 2)


class TestWebhookUrl(unittest.TestCase):
    def test_components_are_requested(self):
        url = digest.components_webhook_url("https://discord.com/api/webhooks/1/tok")
        self.assertIn("with_components=true", url)

    def test_an_existing_query_survives(self):
        url = digest.components_webhook_url("https://discord.com/api/webhooks/1/tok?wait=true")
        self.assertIn("wait=true", url)
        self.assertIn("with_components=true", url)


class TestFetching(unittest.TestCase):
    def test_forks_and_archived_repos_are_left_out(self):
        page = [{"name": "session-android", "fork": False, "archived": False},
                {"name": "session-pysogs", "fork": True, "archived": False},
                {"name": "session-ios-private", "fork": False, "archived": True}]
        with mock.patch.object(digest, "fetch_json", return_value=page):
            self.assertEqual(digest.fetch_repos(None, "org"), {"session-android"})

    def test_forks_can_be_asked_for(self):
        page = [{"name": "session-pysogs", "fork": True, "archived": False}]
        with mock.patch.object(digest, "fetch_json", return_value=page):
            self.assertEqual(digest.fetch_repos(None, "org", include_forks=True),
                             {"session-pysogs"})

    def test_repo_pagination_follows_full_pages(self):
        pages = [[{"name": f"r{n}", "fork": False, "archived": False}
                  for n in range(digest.PER_PAGE)],
                 [{"name": "last", "fork": False, "archived": False}]]
        with mock.patch.object(digest, "fetch_json", side_effect=pages):
            self.assertEqual(len(digest.fetch_repos(None, "org")), digest.PER_PAGE + 1)

    def test_search_stops_when_the_results_run_out(self):
        payload = {"total_count": 2, "items": [pr(1), pr(2)]}
        with mock.patch.object(digest, "fetch_json", return_value=payload) as fetch:
            items, truncated = digest.search_open_prs(None, "org")
        self.assertEqual(len(items), 2)
        self.assertFalse(truncated)
        self.assertEqual(fetch.call_count, 1)

    def test_search_stops_at_the_result_ceiling_rather_than_erroring(self):
        full = {"total_count": 1500,
                "items": [pr(n) for n in range(digest.PER_PAGE)]}
        with mock.patch.object(digest, "fetch_json", return_value=full) as fetch:
            items, truncated = digest.search_open_prs(None, "org")
        self.assertEqual(len(items), digest.SEARCH_RESULT_LIMIT)
        self.assertTrue(truncated)
        self.assertEqual(fetch.call_count, digest.SEARCH_RESULT_LIMIT // digest.PER_PAGE)


class TestRetryAfter(unittest.TestCase):
    def response(self, headers):
        return mock.Mock(headers=headers)

    def test_retry_after_seconds_is_used(self):
        self.assertEqual(digest.retry_after_seconds(self.response({"retry-after": "12"}), 1), 12)

    def test_a_primary_limit_falls_back_to_the_reset_epoch(self):
        import time as time_module
        reset = str(int(time_module.time()) + 30)
        seconds = digest.retry_after_seconds(
            self.response({"x-ratelimit-reset": reset}), 1)
        self.assertTrue(25 <= seconds <= 31, seconds)

    def test_nonsense_falls_back_to_the_caller_default(self):
        for headers in ({"retry-after": "soon"}, {"retry-after": "-30"},
                        {"retry-after": "inf"}, {"x-ratelimit-reset": "?"}, {}):
            self.assertEqual(digest.retry_after_seconds(self.response(headers), 7), 7)


class TestPosting(unittest.TestCase):
    def test_a_rejection_reports_what_landed_before_it(self):
        ok, bad = mock.Mock(status_code=204), mock.Mock(status_code=400, text="no")
        session = mock.Mock()
        session.request.side_effect = [ok, bad]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(digest.post_to_discord(session, "url", [{}, {}, {}]), 1)

    def test_all_accepted(self):
        session = mock.Mock()
        session.request.return_value = mock.Mock(status_code=204)
        self.assertEqual(digest.post_to_discord(session, "url", [{}, {}]), 2)


if __name__ == "__main__":
    unittest.main()
