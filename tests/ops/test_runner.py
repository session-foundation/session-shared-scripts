"""
    uv run python -m unittest tests.ops.test_runner
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from session_ops.ops import registry, runner
from tests.ops import fake_jobs

HOOK = "https://discord.com/api/webhooks/123/abcdef"


def job(entry, **extra):
    return registry.Job(name="demo", entry=f"tests.ops.fake_jobs:{entry}", description="d",
                        user="u", env_files=("/etc/x",), **extra)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.posted = []
        env = {"STATE_DIRECTORY": self.state, "INVOCATION_ID": "inv-1",
               "ALERT_DISCORD_WEBHOOK_URL": HOOK, "DEMO_TOKEN": "s3cr3t-value"}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(runner.discord, "post_to_discord",
                                          side_effect=self.post),
                        mock.patch.object(runner.socket, "gethostname", return_value="box")):
            patcher.start()
            self.addCleanup(patcher.stop)
        fake_jobs.calls.clear()

    def post(self, session, url, messages):
        self.posted.append((url, messages))
        return len(messages)

    def run_job(self, the_job, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.run(the_job, **kwargs)

    def marker(self):
        path = os.path.join(self.state, "alerted")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_success_is_quiet(self):
        self.assertEqual(self.run_job(job("succeeds", args=("--state", "{state}/s"))), 0)
        self.assertEqual(fake_jobs.calls, [["--state", f"{self.state}/s"]])
        self.assertEqual(self.posted, [])

    def test_a_failure_names_the_job_host_step_and_commands_and_is_marked(self):
        self.assertEqual(self.run_job(job("exits")), 1)
        content = self.posted[0][1][0]["content"]
        self.assertIn("❌ **demo** failed on `box` during **posting**.", content)
        self.assertIn("> Posted 0 of 2 messages.", content)
        self.assertIn("journalctl -u session-ops@demo -n 50", content)
        self.assertIn("systemctl start session-ops@demo.service", content)
        self.assertEqual(self.marker(), "inv-1")

    def test_an_exception_is_one_sentence_with_secrets_scrubbed(self):
        self.run_job(job("raises"), extra=["s3cr3t-value"])
        content = self.posted[0][1][0]["content"]
        self.assertIn("> ValueError: bad token <DEMO_TOKEN> second line", content)
        self.assertNotIn("s3cr3t-value", content)

    def test_a_job_that_names_no_step_is_not_said_to_fail_during_one(self):
        self.run_job(job("exits_with_a_pretty_printed_body"))
        self.assertIn("❌ **demo** failed on `box`.\n", self.posted[0][1][0]["content"])

    def test_a_crowdin_error_reads_as_its_status_and_message(self):
        from crowdin_api.exceptions import AuthenticationFailed
        error = AuthenticationFailed(http_status=401, context=b'{"error":{"message":"Unauthorized"}}')
        self.assertEqual(runner.describe(error), "Crowdin 401: Unauthorized")

    def test_a_webhook_url_never_appears_in_an_alert(self):
        self.assertNotIn("abcdef", runner.scrub(f"posting to {HOOK} failed", environ={}))

    def test_missing_environment_is_reported_without_running_the_job(self):
        the_job = job("succeeds", env=("DEMO_MISSING",))
        self.assertEqual(self.run_job(the_job), 1)
        self.assertEqual(fake_jobs.calls, [])
        self.assertIn("missing DEMO_MISSING", self.posted[0][1][0]["content"])

    def test_each_target_is_listed_and_a_failed_one_fails_the_run(self):
        self.assertEqual(self.run_job(job("partial")), 1)
        content = self.posted[0][1][0]["content"]
        self.assertIn("❌ **demo** on `box`: 1 of 2 targets failed.", content)
        self.assertIn("✅ **ios**", content)
        self.assertIn("❌ **android**: push rejected", content)

    def test_an_optional_target_alerts_but_the_run_succeeds_unmarked(self):
        """A success leaves nothing for the backstop to skip."""
        self.assertEqual(self.run_job(job("optional_failure")), 0)
        self.assertIn("⚠️ **resolver**: 500", self.posted[0][1][0]["content"])
        self.assertIsNone(self.marker())

    def test_a_dead_claude_login_says_so_and_says_how_to_log_in_instead_of_re_running(self):
        for entry in ("digest_login_expired", "exits_login_expired"):
            with self.subTest(entry=entry):
                self.posted.clear()
                self.assertEqual(self.run_job(job(entry)), 1)
                content = self.posted[0][1][0]["content"]
                self.assertIn("OAuth token has expired", content)
                self.assertIn("no longer logged in", content)
                self.assertIn("runuser -u zendesk", content)
                self.assertIn("journalctl -u session-ops@demo -n 50", content)
                self.assertNotIn("re-run", content)

    def test_an_optional_failure_whose_alert_cannot_be_posted_fails_the_run(self):
        """OnFailure= is then the only thing left that can report it."""
        with mock.patch.object(runner.discord, "post_to_discord", return_value=0):
            self.assertEqual(self.run_job(job("optional_failure")), 1)
        self.assertIsNone(self.marker())

    def test_a_dry_run_prints_the_alert_instead(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            runner.run(job("exits"), dry_run=True)
        self.assertEqual(self.posted, [])
        self.assertIn("failed on `box`", out.getvalue())

    def test_no_webhook_leaves_it_to_the_backstop(self):
        with mock.patch.dict(os.environ, {"ALERT_DISCORD_WEBHOOK_URL": ""}):
            self.assertEqual(self.run_job(job("exits")), 1)
        self.assertEqual(self.posted, [])
        self.assertIsNone(self.marker())

    def test_the_jobs_own_channel_is_used_without_an_alert_channel(self):
        with mock.patch.dict(os.environ, {"ALERT_DISCORD_WEBHOOK_URL": "",
                                          "DEMO_HOOK": "https://hook/demo"}):
            self.run_job(job("exits", channel_env="DEMO_HOOK"))
        self.assertEqual(self.posted[0][0], "https://hook/demo")

    def test_an_alert_mentions_nobody(self):
        self.run_job(job("exits"))
        payload = self.posted[0][1][0]
        self.assertTrue(payload["content"].startswith("❌ "))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


class TestCommandLine(unittest.TestCase):
    def test_dry_run_is_the_runners_and_what_follows_dashes_is_the_jobs(self):
        with mock.patch.object(runner, "run", return_value=0) as run, \
                self.assertRaises(SystemExit):
            runner.main(["run", "zendesk-digest", "--dry-run", "--", "--window-hours", "24"])
        _, dry_run, extra = run.call_args.args
        self.assertEqual((dry_run, extra), (True, ["--window-hours", "24"]))


if __name__ == "__main__":
    unittest.main()
