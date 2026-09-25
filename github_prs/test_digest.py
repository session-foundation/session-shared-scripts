"""
    python -m unittest discover        # from github_prs/
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from github.Issue import IssueSearchResult
from github.Requester import Requester

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digest  # noqa: E402
from shared import discord  # noqa: E402
from shared.testing import NoSleep  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(hours=25)
GH = digest.github_client("token")


def pr_item(number=1, login="octocat", repo="session-android", created="2026-09-24T08:00:00Z",
            updated=None, title="Fix a thing", user_type="User", **extra):
    """A search result as the API returns one."""
    item = {
        "id": 10_000 + number,
        "number": number,
        "title": title,
        "html_url": f"https://github.com/session-foundation/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/session-foundation/{repo}",
        "user": {"login": login, "type": user_type},
        "created_at": created,
        "updated_at": updated or created,
        "draft": False,
        "comments": 0,
    }
    item.update(extra)
    return item


def pr(*args, **kwargs):
    """A search result as PyGithub hands it to the digest."""
    return IssueSearchResult(GH.requester, {}, pr_item(*args, **kwargs), completed=True)


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
        self.assertEqual([p.number for p in digest.in_window([fresh, stale], CUTOFF)], [1])

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
        new, changed, unchanged = digest.STATE.partition([pr(1)], digest.STATE.empty())
        self.assertEqual([p.number for p in new], [1])
        self.assertEqual((changed, unchanged), ([], []))

    def test_a_pr_that_moved_since_it_was_reported_is_changed(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        new, changed, unchanged = digest.STATE.partition(
            [item], self.state((digest.pr_id(item), "2026-09-20T09:00:00Z")))
        self.assertEqual([p.number for p in changed], [1])
        self.assertEqual((new, unchanged), ([], []))

    def test_a_pr_that_has_not_moved_is_dropped(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        new, changed, unchanged = digest.STATE.partition(
            [item], self.state((digest.pr_id(item), "2026-09-24T09:00:00Z")))
        self.assertEqual([p.number for p in unchanged], [1])
        self.assertEqual((new, changed), ([], []))


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "sub", "seen.json")

    def save(self, *prs, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return digest.STATE.save(self.path, digest.STATE.empty(), list(prs), **kwargs)

    def load(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return digest.STATE.load(self.path)

    def test_a_reported_pr_comes_back_unchanged_next_run(self):
        item = pr(1, updated="2026-09-24T09:00:00Z")
        self.save(item)
        _, _, unchanged = digest.STATE.partition([item], self.load())
        self.assertEqual(len(unchanged), 1)

    def test_the_same_pr_moved_comes_back_changed(self):
        self.save(pr(1, updated="2026-09-24T09:00:00Z"))
        _, changed, _ = digest.STATE.partition(
            [pr(1, updated="2026-09-24T11:00:00Z")], self.load())
        self.assertEqual(len(changed), 1)

    def test_the_file_names_the_pr_for_whoever_opens_it(self):
        self.save(pr(1958, repo="session-desktop"))
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("session-desktop#1958", handle.read())

    def test_no_path_means_no_state_and_no_complaint(self):
        self.assertEqual(digest.STATE.load(None), digest.STATE.empty())


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

    def test_a_repo_that_outgrows_a_message_is_split_under_repeated_headings(self):
        prs = [pr(n, title="t" * 80) for n in range(60)]
        blocks = digest.group_by_repo(prs, [], NOW, max_chars=1000)
        self.assertGreater(len(blocks), 1)
        for text, ids in blocks:
            self.assertLessEqual(len(text), 1000)
            self.assertTrue(text.startswith("**session-android**\n"))
            self.assertEqual(len(text.splitlines()) - 1, len(ids))
        self.assertEqual(sum(len(ids) for _, ids in blocks), len(prs))
        self.assertEqual(set().union(*(ids for _, ids in blocks)),
                         {digest.pr_id(p) for p in prs})

    def test_a_split_repo_keeps_its_blocks_together_and_first(self):
        busy = [pr(n, repo="busy", title="t" * 80) for n in range(30)]
        quiet = [pr(100, repo="quiet")]
        headings = [text.splitlines()[0]
                    for text, _ in digest.group_by_repo(busy + quiet, [], NOW, max_chars=1000)]
        self.assertGreater(len(headings), 2)
        self.assertEqual(headings[-1], "**quiet**")
        self.assertEqual(set(headings[:-1]), {"**busy**"})

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
        self.assertEqual(message["flags"], discord.COMPONENTS_V2_FLAG)
        self.assertEqual(message["components"][0]["type"], discord.CONTAINER)

    def test_one_busy_repo_never_exceeds_the_message_budget(self):
        new = [pr(n, title="y" * 80) for n in range(120)]
        messages, coverage = digest.build_messages(new, [], 120, 72, NOW)
        self.assertGreater(len(messages), 1)
        for message in messages:
            text = sum(len(c.get("content", ""))
                       for c in message["components"][0]["components"])
            self.assertLessEqual(text, discord.MAX_MESSAGE_TEXT_CHARS)
        self.assertEqual(set().union(*coverage), {digest.pr_id(p) for p in new})

    def test_only_the_first_message_carries_the_header(self):
        new = [pr(n, repo=f"repo-{n}", title="y" * 80) for n in range(40)]
        messages, coverage = digest.build_messages(new, [], 40, 25, NOW)
        self.assertGreater(len(messages), 1)
        self.assertEqual(set().union(*coverage), {digest.pr_id(p) for p in new})
        for message in messages[1:]:
            rendered = json.dumps(message, ensure_ascii=False)
            self.assertNotIn("Contributor pull requests", rendered)
            self.assertLess(len(rendered), discord.MAX_MESSAGE_TEXT_CHARS * 2)


class FakeHttpResponse:
    def __init__(self, status, headers, payload):
        self.status, self.headers, self.payload = status, headers, payload

    def getheaders(self):
        return self.headers.items()

    def read(self):
        return json.dumps(self.payload)


class FakeConnection:
    """PyGithub's connection interface, answering from a queue and recording each request."""
    queue, calls = [], []

    def __init__(self, *args, **kwargs):
        pass

    def request(self, verb, url, input, headers, stream=False):
        self.calls.append((verb, url))

    def getresponse(self):
        return FakeHttpResponse(*self.queue.pop(0))

    def close(self):
        pass


def page(payload, next_page=None):
    headers = {"content-type": "application/json"}
    if next_page is not None:
        headers["link"] = f'<https://api.github.com/x?page={next_page}>; rel="next"'
    return 200, headers, payload


def repo(name, **flags):
    return {"name": name, "fork": False, "archived": False, "private": False, **flags}


ORG = {"login": "org", "url": "https://api.github.com/orgs/org"}


class TestFetching(unittest.TestCase):
    def setUp(self):
        FakeConnection.queue.clear()
        FakeConnection.calls.clear()
        Requester.injectConnectionClasses(FakeConnection, FakeConnection)
        self.addCleanup(Requester.resetConnectionClasses)

    def fetch(self, *responses, call):
        FakeConnection.queue.extend(responses)
        with NoSleep():
            return call(digest.github_client("token"), "org")

    def test_forks_archived_and_private_repos_are_left_out(self):
        listing = [repo("session-android"), repo("session-pysogs", fork=True),
                   repo("retired", archived=True), repo("internal", private=True)]
        self.assertEqual(self.fetch(page(ORG), page(listing), call=digest.fetch_repos),
                         {"session-android"})

    def test_repo_pagination_follows_full_pages(self):
        first = [repo(f"r{n}") for n in range(digest.PER_PAGE)]
        names = self.fetch(page(ORG), page(first, next_page=2), page([repo("last")]),
                           call=digest.fetch_repos)
        self.assertEqual(len(names), digest.PER_PAGE + 1)
        self.assertEqual(len(FakeConnection.calls), 3)

    def test_search_stops_when_the_results_run_out(self):
        payload = {"total_count": 2, "incomplete_results": False,
                   "items": [pr_item(1), pr_item(2)]}
        items, truncated = self.fetch(page(payload), call=digest.search_open_prs)
        self.assertEqual([p.number for p in items], [1, 2])
        self.assertFalse(truncated)
        self.assertEqual(len(FakeConnection.calls), 1)

    def test_search_stops_at_the_result_ceiling_rather_than_erroring(self):
        pages = digest.SEARCH_RESULT_LIMIT // digest.PER_PAGE
        full = [page({"total_count": 1500, "incomplete_results": False,
                      "items": [pr_item(n) for n in range(digest.PER_PAGE)]},
                     next_page=n + 2)
                for n in range(pages)]
        items, truncated = self.fetch(*full, call=digest.search_open_prs)
        self.assertEqual(len(items), digest.SEARCH_RESULT_LIMIT)
        self.assertTrue(truncated)
        self.assertEqual(len(FakeConnection.calls), pages)


if __name__ == "__main__":
    unittest.main()
