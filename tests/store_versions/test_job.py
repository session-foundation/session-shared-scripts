"""
    uv run python -m unittest tests.store_versions.test_job
"""
import json
import os
import tempfile
import unittest

from session_ops.shared import discord
from session_ops.shared.testing import Patched
from session_ops.store_versions import job, sources
from session_ops.store_versions.sources import Release

PLATFORMS = (job.Platform("ios", "Session iOS", "session-ios",
                          {"kind": "appstore", "bundle_id": "b"}),
             job.Platform("desktop", "Session Desktop", "session-desktop",
                          {"kind": "deb", "base_url": "u", "package": "p"}, strict_sync=True))
WEBHOOK = "https://discord.example/api/webhooks/1/x"


class Run:
    """job.main against fixed versions, recording what it posts; `accept` caps how
    many messages Discord takes."""

    def __init__(self, github, stores, accept=None, tags=""):
        self.github, self.stores, self.accept, self.posted = github, stores, accept, []
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")
        self.tags = os.path.join(self.dir, "tags.toml")
        with open(self.tags, "w", encoding="utf-8") as handle:
            handle.write(tags)

    def fetch_github(self, session, repo):
        return Release(self.github[repo], f"https://github.com/{repo}")

    def fetch_store(self, session, store):
        value = self.stores[store["kind"]]
        if isinstance(value, Exception):
            raise value
        return Release(value)

    def post(self, session, url, messages):
        taken = messages if self.accept is None else messages[:self.accept]
        if self.accept is not None:
            self.accept -= len(taken)
        self.posted += taken
        return len(taken)

    def __call__(self, *extra, now=1000.0):
        with Patched(job, PLATFORMS=PLATFORMS), \
                Patched(job.time, time=lambda: now), \
                Patched(sources, fetch_github=self.fetch_github, fetch_store=self.fetch_store), \
                Patched(discord, post_to_discord=self.post):
            job.main(["--state", self.state, "--tags", self.tags, "--webhook", WEBHOOK, *extra])

    def saved(self):
        with open(self.state, encoding="utf-8") as handle:
            return json.load(handle)


def titles(messages):
    return [message["embeds"][0]["title"] for message in messages]


class TestJob(unittest.TestCase):
    def test_first_run_in_sync_posts_nothing_and_records_both(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.5", "deb": "1.18.2"})
        run()
        self.assertEqual(run.posted, [])
        self.assertEqual(set(run.saved()["platforms"]), {"ios", "desktop"})

    def test_warning_tags_the_platforms_people_only(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.6", "deb": "1.18.2"}, tags="[tag]\nios = [42]\n")
        run()
        [message] = run.posted
        self.assertEqual(message["content"], "<@42>")
        self.assertEqual(message["allowed_mentions"], {"users": ["42"]})
        self.assertIn("App Store is ahead of GitHub (first check)", titles(run.posted)[0])

    def test_info_mentions_nobody(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.5", "deb": "1.18.2"}, tags="[tag]\nios = [42]\n")
        run()
        run.github["session-ios"] = "2.15.6"
        run()
        [message] = run.posted
        self.assertNotIn("content", message)
        self.assertEqual(message["allowed_mentions"], {"parse": []})

    def test_unposted_warning_is_retried_next_run(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.3"},
                  {"appstore": "2.15.6", "deb": "1.18.2"}, accept=1)
        with self.assertRaises(SystemExit):
            run()
        self.assertEqual(set(run.saved()["platforms"]), {"ios"})
        run.accept = None
        run()
        self.assertEqual(len(run.posted), 2)
        self.assertIn("APT repo is behind GitHub", titles(run.posted)[1])

    def test_a_refused_platform_does_not_hold_back_the_others(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.3"},
                  {"appstore": "2.15.6", "deb": "1.18.2"})
        accept = run.post

        def refuse_the_app_store(session, url, messages):
            if "App Store" in messages[0]["embeds"][0]["title"]:
                return 0
            return accept(session, url, messages)

        run.post = refuse_the_app_store
        with self.assertRaises(SystemExit):
            run()
        self.assertEqual(set(run.saved()["platforms"]), {"desktop"})
        self.assertIn("APT repo is behind GitHub", titles(run.posted)[0])

    def test_a_platforms_events_go_as_one_message(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.5", "deb": "1.18.2"})
        run()
        run.github["session-ios"], run.stores["appstore"] = "2.15.6", "2.15.6"
        run()
        [message] = run.posted
        self.assertEqual([embed["title"] for embed in message["embeds"]],
                         ["ℹ️ Session iOS: new GitHub release", "ℹ️ Session iOS: App Store updated"])

    def test_an_all_clear_says_what_is_true(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.6", "deb": "1.18.2"})
        run()
        run.github["session-ios"] = "2.15.7"
        run()
        self.assertEqual(titles(run.posted)[1], "✅ Session iOS: no longer ahead of GitHub")

    def test_a_refused_post_fails_with_the_same_text_however_many_wait(self):
        failures = []
        for stores in ({"appstore": "2.15.6", "deb": "1.18.2"},
                       {"appstore": "2.15.6", "deb": "1.18.1"}):
            run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"}, stores, accept=0)
            with self.assertRaises(SystemExit) as raised:
                run()
            failures.append(str(raised.exception))
        self.assertEqual(failures[0], failures[1])

    def test_past_the_deadline_messages_wait_for_the_next_run(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.3"},
                  {"appstore": "2.15.6", "deb": "1.18.2"})
        clock = iter([0.0, job.POST_DEADLINE_SECONDS + 1])
        with Patched(job.time, monotonic=lambda: next(clock, 10_000.0)):
            run()
        self.assertEqual(run.posted, [])
        self.assertEqual(set(run.saved()["platforms"]), set())
        run()
        self.assertEqual(len(run.posted), 2)

    def test_failing_source_is_posted_after_an_hour_then_recovery(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": RuntimeError("boom"), "deb": "1.18.2"})
        run(now=0.0)
        run(now=3600.0)
        self.assertEqual(titles(run.posted), ["⚠️ App Store (ios): failing for ~1h"])
        self.assertNotIn("ios", run.saved()["platforms"])
        run.stores["appstore"] = "2.15.5"
        run(now=4000.0)
        self.assertEqual(titles(run.posted)[1], "✅ App Store (ios): readable again")
        self.assertEqual(run.saved()["sources"], {})

    def test_dry_run_posts_and_writes_nothing(self):
        run = Run({"session-ios": "2.15.5", "session-desktop": "1.18.2"},
                  {"appstore": "2.15.6", "deb": "1.18.2"})
        run("--dry-run")
        self.assertEqual(run.posted, [])
        self.assertFalse(os.path.exists(run.state))

    def test_unreadable_tags_fail_the_run_saying_why(self):
        run = Run({}, {})
        os.chmod(run.tags, 0)
        if os.access(run.tags, os.R_OK):
            self.skipTest("running as root")
        with self.assertRaises(SystemExit) as raised:
            job.load_tags(run.tags)
        self.assertIn("mode 644", str(raised.exception))

    def test_tags_must_name_a_platform(self):
        run = Run({}, {}, tags="[tag]\nandroind = [1]\n")
        with self.assertRaises(SystemExit):
            job.load_tags(run.tags)
