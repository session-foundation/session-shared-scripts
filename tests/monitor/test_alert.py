"""
    uv run python -m unittest tests.monitor.test_alert
"""
import subprocess
import unittest
from unittest import mock

from session_ops.monitor import alert

# What the digest itself left in the journal on 2026-09-23.
JOURNAL = """\
Fetched 13 of 13 matching tickets (query: 'type:ticket status<pending').
Loaded state for 242 previously reported tickets.
4 new, 1 changed since last reported, 8 unchanged (skipped).
claude exited 1 on a batch of 5 tickets: Failed to authenticate: OAuth session \
expired and could not be refreshed (api_error)

"""
CLI = "claude exited 1 on a batch of 5 tickets: "


class TestLastJobLine(unittest.TestCase):
    def test_the_last_non_blank_line_is_the_excerpt(self):
        self.assertTrue(alert.last_job_line(JOURNAL).startswith(CLI))
        self.assertIn("OAuth session expired", alert.last_job_line(JOURNAL))

    def test_a_unit_that_said_nothing_leaves_it_empty(self):
        self.assertEqual(alert.last_job_line("   \n\n"), "")
        self.assertEqual(alert.last_job_line(""), "")

    def test_the_excerpt_is_clipped(self):
        line = alert.last_job_line("x" * 5000)
        self.assertEqual(len(line), alert.EXCERPT_CHARS)


class TestAuthDetection(unittest.TestCase):
    def test_the_host_failure_is_recognised(self):
        self.assertTrue(alert.is_auth_failure(alert.last_job_line(JOURNAL)))

    def test_the_cli_rewordings_are_covered(self):
        for line in ("Invalid API key · Please run /login",
                     "OAuth token expired",
                     "Request failed: unauthorized",
                     "Credit balance is too low",
                     "it printed nothing, which is what a login it can no longer use "
                     "looks like; check that `claude` is still signed in."):
            with self.subTest(line=line):
                self.assertTrue(alert.is_auth_failure(CLI + line))

    def test_an_ordinary_failure_is_not_a_login_problem(self):
        for line in ("Zendesk 500 on /api/v2/search.json",
                     "claude did not finish a batch of 5 tickets within 1800s.",
                     ""):
            with self.subTest(line=line):
                self.assertFalse(alert.is_auth_failure(line))

    def test_a_dead_zendesk_token_is_not_a_dead_claude_login(self):
        """Zendesk's 401 body says "authenticate" too, and re-logging the CLI in
        would fix nothing."""
        for line in ('Zendesk search failed (401): {"error":"Couldn\'t authenticate you"}',
                     'update_many failed (401): {"error":"Couldn\'t authenticate you"}'):
            with self.subTest(line=line):
                self.assertFalse(alert.is_auth_failure(line))


class TestMessage(unittest.TestCase):
    def build(self, detail="", journal_unit=None):
        return alert.build_message("zendesk-digest.service", "angus",
                                   journal_unit, detail=detail)

    def test_a_login_failure_says_so_and_says_what_to_run(self):
        message = self.build(alert.last_job_line(JOURNAL))
        self.assertIn("OAuth session expired", message)
        self.assertIn("no longer logged in", message)
        self.assertIn("/login", message)
        self.assertIn("runuser -u zendesk", message)

    def test_an_ordinary_failure_is_not_told_to_log_in(self):
        message = self.build("Zendesk 500 on /api/v2/search.json")
        self.assertIn("Zendesk 500", message)
        self.assertNotIn("logged in", message)

    def test_the_journalctl_line_survives_an_excerpt(self):
        """One line is rarely the whole story, so the excerpt adds to the pointer at
        the journal rather than replacing it."""
        for detail in ("", "something broke", alert.last_job_line(JOURNAL)):
            with self.subTest(detail=detail):
                self.assertIn("journalctl -u zendesk-digest.service",
                              self.build(detail))

    def test_a_step_that_is_not_a_unit_points_at_the_unit_that_ran_it(self):
        message = alert.build_message("resolve_reviews.py", "angus",
                                      "zendesk-digest.service", detail="")
        self.assertIn("as part of zendesk-digest.service", message)
        self.assertIn("journalctl -u zendesk-digest.service", message)

    def test_an_unreadable_journal_leaves_the_message_it_always_sent(self):
        message = self.build("")
        self.assertIn("**zendesk-digest.service** failed on `angus`", message)
        self.assertNotIn(">", message)

    def test_it_fits_a_discord_message(self):
        message = self.build("x" * alert.EXCERPT_CHARS)
        self.assertLess(len(message), 2000)


class TestJournalTail(unittest.TestCase):
    def run_tail(self, **kwargs):
        with mock.patch.object(alert.subprocess, "run", **kwargs) as run:
            self.run_call = run
            return alert.journal_tail("zendesk-digest.service")

    def test_only_the_units_own_output_is_asked_for(self):
        """-u would add systemd's lines about the unit, and the last of those would
        become the excerpt."""
        self.run_tail(return_value=mock.Mock(returncode=0, stdout=JOURNAL))
        command = self.run_call.call_args[0][0]
        self.assertIn("_SYSTEMD_UNIT=zendesk-digest.service", command)
        self.assertNotIn("-u", command)
        self.assertIn("--no-pager", command)

    def test_output_comes_back_whole(self):
        self.assertEqual(
            self.run_tail(return_value=mock.Mock(returncode=0, stdout=JOURNAL)),
            JOURNAL)

    def test_a_journal_it_may_not_read_costs_the_excerpt_and_nothing_else(self):
        """Without SupplementaryGroups=systemd-journal the unit gets a permission
        error, and an alert that raised there would report nothing at all."""
        self.assertEqual(
            self.run_tail(return_value=mock.Mock(returncode=1, stdout="")), "")

    def test_a_missing_or_wedged_journalctl_costs_the_excerpt_too(self):
        for error in (FileNotFoundError(), PermissionError(),
                      subprocess.TimeoutExpired("journalctl", 15)):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.run_tail(side_effect=error), "")


class TestBackstopContext(unittest.TestCase):
    def test_only_the_failed_invocation_is_quoted(self):
        """A run killed before it logged would otherwise get the previous run's line."""
        with mock.patch.object(alert.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="", stderr="")) as run:
            alert.journal_tail("session-ops@x.service", invocation="abc")
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc", run.call_args.args[0])

    def test_the_result_says_how_the_unit_failed(self):
        message = alert.build_message("session-ops@x.service", "box", result="timeout")
        self.assertIn("failed on `box` (timed out).", message)


class TestAlreadyAlerted(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "demo"))
        with open(os.path.join(self.root, "demo", "alerted"), "w", encoding="utf-8") as fh:
            fh.write("inv-1")

    def invocation(self, value):
        return mock.patch.object(alert.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=f"{value}\n", stderr=""))

    def test_the_invocation_the_run_reported_is_skipped(self):
        with self.invocation("inv-1"):
            self.assertTrue(alert.already_alerted("session-ops@demo.service", self.root))

    def test_a_later_failure_of_the_same_job_is_not(self):
        with self.invocation("inv-2"):
            self.assertFalse(alert.already_alerted("session-ops@demo.service", self.root))

    def test_a_unit_that_is_not_a_job_always_alerts(self):
        self.assertFalse(alert.already_alerted("zendesk-relay.service", self.root))

    def test_a_job_that_never_alerted_is_reported(self):
        with self.invocation("inv-1"):
            self.assertFalse(alert.already_alerted("session-ops@other.service", self.root))


if __name__ == "__main__":
    unittest.main()
