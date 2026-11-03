"""
    uv run python -m unittest tests.ops.test_deploy

The units and the registry only ever meet on the host, so a mismatch surfaces as a
failed timer rather than at review time.
"""
import glob
import importlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
import unittest

from session_ops.ops import registry, units
from tests.golden import assert_golden

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_BIN = "/opt/session-ops/.venv/bin/"


def unit_text(name):
    with open(os.path.join(ROOT, "deploy", name), encoding="utf-8") as handle:
        return handle.read()


class TestDeploy(unittest.TestCase):
    def test_every_venv_command_a_unit_runs_is_a_declared_entry_point(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
            declared = set(tomllib.load(handle)["project"]["scripts"]) | {"uvicorn"}
        used = set()
        for unit in glob.glob(os.path.join(ROOT, "deploy", "*.service")):
            with open(unit, encoding="utf-8") as handle:
                used |= set(re.findall(re.escape(VENV_BIN) + r"([\w-]+)", handle.read()))
        self.assertIn("session-ops", used)
        self.assertEqual(used - declared, set())

    def test_every_entry_in_the_registry_can_be_called(self):
        for job in registry.load():
            module, function = job.entry.split(":")
            with self.subTest(job=job.name):
                self.assertTrue(callable(getattr(importlib.import_module(module), function)))

    def test_the_template_stamps_each_success_for_the_silence_checker(self):
        self.assertIn("ExecStartPost=+/usr/bin/touch /var/lib/session-ops/stamps/%i\n",
                      unit_text("session-ops@.service"))

    def test_every_unit_reports_its_failure(self):
        for path in glob.glob(os.path.join(ROOT, "deploy", "*.service")):
            name = os.path.basename(path)
            if name in ("session-ops-alert@.service", "session-ops-alert-fallback@.service"):
                continue
            with self.subTest(unit=name):
                self.assertIn("OnFailure=session-ops-alert@%n.service", unit_text(name))

    def test_a_failed_alert_is_reported_by_a_unit_that_runs_nothing_from_the_venv(self):
        self.assertIn("\nOnFailure=session-ops-alert-fallback@%n.service\n",
                      unit_text("session-ops-alert@.service"))
        fallback = unit_text("session-ops-alert-fallback@.service")
        self.assertNotIn(VENV_BIN, fallback)
        self.assertNotIn("OnFailure=", fallback)
        self.assertIn("\nExecStart=/opt/session-ops/deploy/session-ops-alert-fallback.sh %i\n",
                      fallback)
        script = os.path.join(ROOT, "deploy", "session-ops-alert-fallback.sh")
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
        with open(script, encoding="utf-8") as handle:
            self.assertNotIn("python", handle.read())

    def test_a_scheduled_job_gets_a_timer_and_an_unscheduled_one_does_not(self):
        files = units.dropins(registry.load(), registry.load_queue())
        for job in registry.load():
            with self.subTest(job=job.name):
                self.assertEqual(f"session-ops@{job.name}.timer.d/schedule.conf" in files,
                                 bool(job.schedule))

    def test_a_discord_job_starts_on_a_write_to_its_request_and_never_removes_it(self):
        files = units.dropins(registry.load(), registry.load_queue())
        for job in registry.load():
            with self.subTest(job=job.name):
                watch = files.get(f"session-ops@{job.name}.path.d/request.conf", "")
                request = os.path.join(registry.REQUESTS_DIR, job.name)
                self.assertEqual(f"\nPathChanged={request}\n" in watch, job.discord)
                self.assertNotIn(request, files[f"session-ops@{job.name}.service.d/job.conf"])

    def test_each_queued_job_runs_after_every_one_before_it(self):
        queue = registry.load_queue()
        files = units.dropins(registry.load(), queue)
        self.assertIn(f"OnCalendar={queue.schedule}\n",
                      files["session-ops-queue.timer.d/schedule.conf"])
        for i, job in enumerate(queue.jobs[1:], start=1):
            with self.subTest(job=job):
                ahead = " ".join(f"session-ops@{name}.service" for name in queue.jobs[:i])
                self.assertIn(f"\nAfter={ahead}\n",
                              files[f"session-ops@{job}.service.d/job.conf"])
        self.assertNotIn("After=", files[f"session-ops@{queue.jobs[0]}.service.d/job.conf"])

    def test_install_enables_timers_only_for_jobs_with_a_schedule_of_their_own(self):
        with open(os.path.join(ROOT, "deploy", "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertEqual(re.findall(r'systemctl enable --now "session-ops@\$job\.timer"', script),
                         ['systemctl enable --now "session-ops@$job.timer"'])
        self.assertIn('for job in $TIMED; do\n    systemctl enable --now "session-ops@$job.timer"',
                      script)

    def test_the_generated_dropins(self):
        files = units.dropins(registry.load(), registry.load_queue())
        text = "".join(f"==> {path} <==\n{files[path]}\n" for path in sorted(files))
        assert_golden(self, "units/dropins.txt", text)

    def test_only_the_discord_relay_s_account_can_write_requests(self):
        self.assertIn(f"\nUser={units.RELAY_USER}\n", unit_text("session-ops-discord.service"))
        with open(os.path.join(ROOT, "deploy", "session-ops.tmpfiles"), encoding="utf-8") as handle:
            self.assertIn(f"\nd {registry.REQUESTS_DIR} 0770 root {units.RELAY_USER} -\n",
                          handle.read())

    def test_the_discord_relay_can_write_requests_and_nothing_else(self):
        writable = re.findall(r"^ReadWritePaths=-?(.*)$",
                              unit_text("session-ops-discord.service"), re.MULTILINE)
        self.assertEqual(writable, [registry.REQUESTS_DIR])


class TestInstallScript(unittest.TestCase):
    @unittest.skipIf(ROOT == "/opt/session-ops", "this checkout is the one it installs from")
    def test_a_clone_anywhere_but_opt_session_ops_is_refused_before_anything_runs(self):
        """Every unit runs /opt/session-ops/.venv, so units installed from another
        clone would all fail to start."""
        stubs = tempfile.mkdtemp()
        # Should the guard go, the root check stops the script rather than this test.
        stub = os.path.join(stubs, "id")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 1000\n")
        os.chmod(stub, stat.S_IRWXU)
        done = subprocess.run(["sh", os.path.join(ROOT, "deploy", "install.sh")],
                              capture_output=True, text=True, check=False,
                              env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn(f"run the clone at /opt/session-ops, not {ROOT}", done.stderr)


class TestAlertFallback(unittest.TestCase):
    UNIT = "session-ops-alert@session-ops@store-versions.service.service"

    def setUp(self):
        self.stubs = tempfile.mkdtemp()
        self.state = tempfile.mkdtemp()
        self.posted = os.path.join(self.stubs, "posted")
        self.stub("curl", f'cat > "{self.posted}"\nexit "${{CURL_EXIT:-0}}"\n')
        self.stub("journalctl", "echo \"ModuleNotFoundError: No module named 'session_ops'\"\n")
        self.stub("hostname", "echo angus\n")

    def stub(self, name, body):
        path = os.path.join(self.stubs, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, stat.S_IRWXU)

    def run_script(self, **env):
        if os.path.exists(self.posted):
            os.remove(self.posted)
        return subprocess.run(
            ["sh", os.path.join(ROOT, "deploy", "session-ops-alert-fallback.sh"), self.UNIT],
            capture_output=True, text=True, check=False,
            env={"PATH": f"{self.stubs}:{os.environ['PATH']}", "STATE_DIRECTORY": self.state,
                 "ALERT_DISCORD_WEBHOOK_URL": "https://discord.invalid/hook", **env})

    def test_it_posts_the_unit_host_and_last_line_as_json(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(self.posted, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn(f"**{self.UNIT}** failed on `angus`", payload["content"])
        self.assertIn("\n> ModuleNotFoundError: No module named 'session_ops'\n",
                      payload["content"])

    def test_it_posts_once_per_window(self):
        self.assertEqual(self.run_script().returncode, 0)
        again = self.run_script()
        self.assertEqual(again.returncode, 0)
        self.assertFalse(os.path.exists(self.posted))
        self.assertEqual(self.run_script(WINDOW_SECONDS="0").returncode, 0)
        self.assertTrue(os.path.exists(self.posted))

    def test_a_failed_post_fails_the_unit_and_is_retried_next_time(self):
        self.assertEqual(self.run_script(CURL_EXIT="22").returncode, 1)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(os.path.exists(self.posted))

    def test_quotes_and_backslashes_in_the_line_stay_valid_json(self):
        self.stub("journalctl", "printf '%s\\n' 'said \"no\" at C:\\path'\n")
        self.assertEqual(self.run_script().returncode, 0)
        with open(self.posted, encoding="utf-8") as handle:
            self.assertIn('> said "no" at C:\\path', json.load(handle)["content"])

    def test_without_a_webhook_it_says_so_and_succeeds(self):
        done = self.run_script(ALERT_DISCORD_WEBHOOK_URL="")
        self.assertEqual(done.returncode, 0)
        self.assertIn("No ALERT_DISCORD_WEBHOOK_URL", done.stdout)
        self.assertFalse(os.path.exists(self.posted))


if __name__ == "__main__":
    unittest.main()
