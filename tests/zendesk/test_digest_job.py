"""
    uv run python -m unittest tests.zendesk.test_digest_job

The weekday run's order and failure handling. These only ever run on a timer, so a
wiring mistake would surface as a failed 10am run rather than at review time.
"""
import inspect
import re
import unittest
from unittest import mock

from session_ops.ops import registry
from session_ops.zendesk import digest_job, resolve_reviews, triage


def defined_flags(function):
    return set(re.findall(r'add_argument\("(--[a-z-]+)"', inspect.getsource(function)))


class TestDigestJob(unittest.TestCase):
    def run_job(self, argv, resolver=None, digest=None):
        calls = []

        def fake(name, behaviour):
            def call(argv):
                calls.append((name, argv))
                if behaviour:
                    raise behaviour
            return call

        with mock.patch.object(resolve_reviews, "main", fake("resolve", resolver)), \
                mock.patch.object(triage, "main", fake("triage", digest)):
            outcome = digest_job.main(argv)
        return outcome, calls

    def test_the_resolver_runs_before_the_digest(self):
        """Solved reviews leave the digest's `status<pending` query, so running second
        would have the digest re-count reviews just closed."""
        _, calls = self.run_job(["--state", "/s/seen.json"])
        self.assertEqual([name for name, _ in calls], ["resolve", "triage"])
        self.assertEqual(calls[0][1], ["--apply"])
        self.assertIn("--state", calls[1][1])

    def test_a_failed_resolve_is_reported_and_does_not_cost_the_digest(self):
        outcome, calls = self.run_job([], resolver=SystemExit("Zendesk returned 500"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome.failures(), {"resolve reviews": "Zendesk returned 500"})
        self.assertFalse(outcome.fatal(), "the digest still ran, so the run succeeds")

    def test_a_failed_digest_fails_the_run(self):
        outcome, _ = self.run_job([], digest=RuntimeError("boom"))
        self.assertTrue(outcome.fatal())

    def test_a_dry_run_solves_nothing_and_posts_nothing(self):
        _, calls = self.run_job(["--dry-run"])
        self.assertNotIn("--apply", calls[0][1])
        self.assertIn("--no-discord", calls[0][1])
        self.assertIn("--dry-run", calls[1][1])

    def test_every_flag_passed_on_is_one_the_script_defines(self):
        _, calls = self.run_job(["--state", "/s", "--dry-run"])
        _, real = self.run_job(["--state", "/s"])
        for (name, argv) in calls + real:
            function = resolve_reviews.main if name == "resolve" else triage.main
            for flag in (a for a in argv if a.startswith("--")):
                self.assertIn(flag, defined_flags(function), f"{flag} is not a {name} flag")


class TestRegistryEntry(unittest.TestCase):
    job = registry.get("zendesk-digest")

    def test_the_timer_never_passes_dry_run(self):
        """A dry run posts nothing and records nothing: a digest silently missing
        while every run looks green."""
        self.assertNotIn("--dry-run", self.job.argv())

    def test_the_digest_keeps_its_state_somewhere_persistent(self):
        argv = self.job.argv()
        self.assertEqual(argv[argv.index("--state") + 1],
                         "/var/lib/session-ops/zendesk-digest/seen.json")

    def test_every_flag_the_registry_passes_is_real(self):
        for flag in (a for a in self.job.argv(dry_run=True) if a.startswith("--")):
            self.assertIn(flag, defined_flags(digest_job.main))


if __name__ == "__main__":
    unittest.main()
