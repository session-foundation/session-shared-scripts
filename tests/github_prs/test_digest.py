"""
    uv run python -m unittest tests.github_prs.test_digest
"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from session_ops.github_prs import digest
from session_ops.shared import discord
from session_ops.shared.testing import frozen_datetime

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

    def test_a_pr_quiet_for_months_still_reads_as_updated_when_it_moves(self):
        self.save(pr(1, updated="2026-01-10T09:00:00Z"))
        with open(self.path, encoding="utf-8") as handle:
            state = json.load(handle)
        months_ago = (datetime.now(timezone.utc) - timedelta(days=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        state["seen"][digest.pr_id(pr(1))]["last_reported"] = months_ago
        with contextlib.redirect_stdout(io.StringIO()):
            digest.save_state(self.path, state, [pr(2)])
        new, changed, _ = digest.partition_by_state(
            [pr(1, updated="2026-09-24T09:00:00Z")], self.load())
        self.assertEqual((len(new), len(changed)), (0, 1))


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


class TestPosting(unittest.TestCase):
    """A real run, from the fetched PRs to the state file, with Discord faked."""

    PRS = [pr(n, repo=f"repo-{n:02}", title="y" * 80) for n in range(40)]

    def run_digest(self, accepted):
        """Returns (ids recorded, ids per message sent, exit status)."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "seen.json")
        sent = []

        def post(session, url, messages):
            sent.extend(messages)
            return min(accepted, len(messages))

        status = None
        with mock.patch.object(digest, "github_session", lambda token: None), \
                mock.patch.object(digest, "fetch_repos",
                                  lambda session, org: set(map(digest.repo_name, self.PRS))), \
                mock.patch.object(digest, "search_open_prs",
                                  lambda session, org: (self.PRS, False)), \
                mock.patch.object(digest, "datetime", frozen_datetime(NOW)), \
                mock.patch.object(discord, "post_to_discord", post), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                digest.main(["--token", "t", "--webhook", "https://discord.test/api/webhooks/1/x",
                             "--window-hours", "25", "--state", path])
            except SystemExit as exc:
                status = exc.code
        with contextlib.redirect_stdout(io.StringIO()):
            recorded = set(digest.load_state(path)["seen"])
        by_number = {str(p["number"]): digest.pr_id(p) for p in self.PRS}
        per_message = [{by_number[n] for n in re.findall(r"\[#(\d+)\]", json.dumps(m))}
                       for m in sent]
        return recorded, per_message, status

    def test_a_failure_partway_records_only_the_messages_discord_accepted(self):
        recorded, per_message, status = self.run_digest(accepted=1)
        self.assertGreater(len(per_message), 1)
        self.assertTrue(per_message[0])
        self.assertEqual(recorded, per_message[0])
        self.assertEqual(status, f"Posted 1 of {len(per_message)} messages.")

    def test_every_message_accepted_records_every_pr_shown(self):
        recorded, per_message, status = self.run_digest(accepted=99)
        self.assertEqual(recorded, {digest.pr_id(p) for p in self.PRS})
        self.assertEqual(recorded, set().union(*per_message))
        self.assertIsNone(status)

    def test_nothing_accepted_records_nothing(self):
        recorded, _, status = self.run_digest(accepted=0)
        self.assertEqual(recorded, set())
        self.assertIsNotNone(status)


MELBOURNE = ZoneInfo("Australia/Melbourne")


def local(*fields):
    """A time on the timer's clock, 09:30 Australia/Melbourne unless given."""
    return datetime(*fields, tzinfo=MELBOURNE).astimezone(timezone.utc)


def stamp(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestWindowStart(unittest.TestCase):
    NOW = local(2026, 9, 28, 9, 30)

    def start(self, covered, retention_days=365):
        state = {"seen": {}, digest.COVERED_FIELD: covered}
        return digest.window_start(state, self.NOW, 72, retention_days)

    def test_without_a_stamp_the_window_is_the_configured_one(self):
        self.assertEqual(digest.window_start(digest.empty_state(), self.NOW, 72),
                         self.NOW - timedelta(hours=72))
        self.assertEqual(self.start("not a time"), self.NOW - timedelta(hours=72))

    def test_an_older_stamp_widens_the_window_to_it(self):
        covered = self.NOW - timedelta(hours=73)
        self.assertEqual(self.start(stamp(covered)), covered)

    def test_a_recent_stamp_never_narrows_the_window(self):
        self.assertEqual(self.start(stamp(self.NOW - timedelta(hours=1))),
                         self.NOW - timedelta(hours=72))
        self.assertEqual(self.start(stamp(self.NOW + timedelta(days=3))),
                         self.NOW - timedelta(hours=72))

    def test_a_stamp_is_followed_back_no_further_than_the_retention(self):
        """Past it the state has forgotten what it reported, so nothing could be deduped."""
        self.assertEqual(self.start(stamp(self.NOW - timedelta(days=900)), retention_days=30),
                         self.NOW - timedelta(days=30))


class TestAcrossRuns(unittest.TestCase):
    """Consecutive real runs sharing one state file, with Discord faked."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "seen.json")

    def run_digest(self, at, prs, accepted=99, search_takes=timedelta(0), dry_run=False):
        """Returns the PR numbers in each message sent, and the first message's JSON."""
        clock = [at]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        def search(session, org):
            clock[0] += search_takes
            return prs, False

        sent = []

        def post(session, url, messages):
            sent.extend(messages)
            return min(accepted, len(messages))

        with mock.patch.object(digest, "github_session", lambda token: None), \
                mock.patch.object(digest, "fetch_repos",
                                  lambda session, org: set(map(digest.repo_name, prs))), \
                mock.patch.object(digest, "search_open_prs", search), \
                mock.patch.object(digest, "datetime", Clock), \
                mock.patch.object(discord, "post_to_discord", post), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                digest.main(["--token", "t", "--webhook", "https://discord.test/api/webhooks/1/x",
                             "--window-hours", "72", "--state", self.path,
                             *(["--dry-run"] if dry_run else [])])
            except SystemExit:
                pass
        shown = [set(map(int, re.findall(r"\[#(\d+)\]", json.dumps(m)))) for m in sent]
        header = json.dumps(sent[0], ensure_ascii=False) if sent else ""
        return shown, header

    def covered(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle).get(digest.COVERED_FIELD)

    def test_prs_a_failed_friday_post_left_out_are_reported_on_monday(self):
        thursday = stamp(local(2026, 9, 24, 15, 0))
        prs = [pr(n, repo=f"repo-{n:02}", title="y" * 80, created=thursday)
               for n in range(40)]
        friday, _ = self.run_digest(local(2026, 9, 25, 9, 30), prs, accepted=1)
        self.assertGreater(len(friday), 1)
        monday, _ = self.run_digest(local(2026, 9, 28, 9, 30), prs)
        self.assertEqual(set().union(*monday), set(range(40)) - friday[0])

    def test_a_dst_weekend_longer_than_the_window_loses_nothing(self):
        """April's Friday 09:30 AEDT to Monday 09:30 AEST is 73 hours."""
        friday, monday = local(2027, 4, 2, 9, 30), local(2027, 4, 5, 9, 30)
        self.assertEqual(monday - friday, timedelta(hours=73))
        self.run_digest(friday, [pr(1, created=stamp(friday - timedelta(hours=5)))])
        late = pr(2, created=stamp(friday + timedelta(minutes=1)))
        shown, header = self.run_digest(monday, [late])
        self.assertEqual(shown[0], {2})
        self.assertIn("last 73h", header)

    def test_a_run_the_host_missed_is_caught_up_by_the_next(self):
        thursday, monday = local(2026, 9, 24, 9, 30), local(2026, 9, 28, 9, 30)
        self.run_digest(thursday, [])
        moved = pr(1, created=stamp(thursday + timedelta(hours=3)))
        shown, _ = self.run_digest(monday, [moved])
        self.assertEqual(shown[0], {1})

    def test_a_pr_that_moves_during_the_search_is_in_the_next_window(self):
        friday, monday = local(2027, 4, 2, 9, 30), local(2027, 4, 5, 9, 30)
        self.run_digest(friday, [], search_takes=timedelta(minutes=2))
        self.assertEqual(self.covered(), stamp(friday))
        during = pr(1, created=stamp(friday + timedelta(minutes=1)))
        shown, _ = self.run_digest(monday, [during])
        self.assertEqual(shown[0], {1})

    def test_the_stamp_advances_only_when_every_message_landed(self):
        prs = [pr(n, repo=f"repo-{n:02}", title="y" * 80,
                  created=stamp(local(2026, 9, 21, 15, 0))) for n in range(40)]
        monday = local(2026, 9, 21, 16, 0)
        self.run_digest(monday, prs)
        self.assertEqual(self.covered(), stamp(monday))
        tuesday = monday + timedelta(days=1)
        self.run_digest(tuesday, prs, dry_run=True)
        self.assertEqual(self.covered(), stamp(monday))
        moved = [dict(p, updated_at=stamp(tuesday)) for p in prs]
        self.run_digest(tuesday, moved, accepted=1)
        self.assertEqual(self.covered(), stamp(tuesday - timedelta(hours=72)))
        self.run_digest(tuesday + timedelta(hours=1), moved, accepted=0)
        self.assertEqual(self.covered(), stamp(tuesday - timedelta(hours=72)))


class TestFetching(unittest.TestCase):
    def test_forks_archived_and_private_repos_are_left_out(self):
        page = [{"name": "session-android", "fork": False, "archived": False},
                {"name": "session-pysogs", "fork": True, "archived": False},
                {"name": "retired", "fork": False, "archived": True},
                {"name": "internal", "fork": False, "archived": False, "private": True}]
        with mock.patch.object(digest, "fetch_json", return_value=page):
            self.assertEqual(digest.fetch_repos(None, "org"), {"session-android"})

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
        pages = [{"total_count": 1500,
                  "items": [pr(page * digest.PER_PAGE + n) for n in range(digest.PER_PAGE)]}
                 for page in range(15)]
        with mock.patch.object(digest, "fetch_json", side_effect=pages) as fetch:
            items, truncated = digest.search_open_prs(None, "org")
        self.assertEqual(len(items), digest.SEARCH_RESULT_LIMIT)
        self.assertTrue(truncated)
        self.assertEqual(fetch.call_count, digest.SEARCH_RESULT_LIMIT // digest.PER_PAGE)

    def test_a_pr_repeated_across_pages_is_kept_once_and_the_counts_read_as_a_floor(self):
        first = {"total_count": 106, "items": [pr(n) for n in range(digest.PER_PAGE)]}
        shifted = {"total_count": 106,
                   "items": [pr(n) for n in range(digest.PER_PAGE - 1, 105)]}
        with mock.patch.object(digest, "fetch_json", side_effect=[first, shifted]):
            items, truncated = digest.search_open_prs(None, "org")
        self.assertEqual(len(items), 105)
        self.assertEqual(len({item["id"] for item in items}), 105)
        self.assertTrue(truncated)

    def test_a_search_github_reports_incomplete_reads_as_a_floor(self):
        payload = {"total_count": 2, "incomplete_results": True, "items": [pr(1), pr(2)]}
        with mock.patch.object(digest, "fetch_json", return_value=payload):
            items, truncated = digest.search_open_prs(None, "org")
        self.assertEqual(len(items), 2)
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
