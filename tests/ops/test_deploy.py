"""
    uv run python -m unittest tests.ops.test_deploy

The units and the registry only ever meet on the host, so a mismatch surfaces as a
failed timer rather than at review time.
"""
import glob
import importlib
import os
import re
import stat
import subprocess
import tempfile
import tomllib
import unittest

from session_ops.ops import discord_commands, registry, units
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
            if name == "session-ops-alert@.service":
                continue
            with self.subTest(unit=name):
                self.assertIn("OnFailure=session-ops-alert@%n.service", unit_text(name))

    def test_a_scheduled_job_gets_a_timer_and_an_unscheduled_one_does_not(self):
        files = units.dropins(registry.load(), registry.load_queue())
        for job in registry.load():
            with self.subTest(job=job.name):
                self.assertEqual(f"session-ops@{job.name}.timer.d/schedule.conf" in files,
                                 bool(job.schedule))

    def test_a_watched_job_gets_a_path_unit_and_moves_its_files_out_of_the_watch(self):
        files = units.dropins(registry.load(), registry.load_queue())
        for job in registry.load():
            with self.subTest(job=job.name):
                watch = files.get(f"session-ops@{job.name}.path.d/watch.conf")
                self.assertEqual(watch is not None, bool(job.watch))
                if job.watch:
                    self.assertIn(f"\nPathExistsGlob={job.watch_glob}\n", watch)
        self.assertIn("Unit=session-ops@%i.service\n", unit_text("session-ops@.path"))

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

    def test_the_generated_dropins(self):
        files = units.dropins(registry.load(), registry.load_queue())
        text = "".join(f"==> {path} <==\n{files[path]}\n" for path in sorted(files))
        assert_golden(self, "units/dropins.txt", text)

    def test_the_generated_polkit_rule(self):
        assert_golden(self, "units/polkit.rules", units.polkit_rule(registry.load()))

    def test_the_polkit_rule_names_the_account_the_discord_relay_runs_as(self):
        self.assertIn(f"\nUser={units.RELAY_USER}\n", unit_text("session-ops-discord.service"))
        self.assertIn(f'subject.user == "{units.RELAY_USER}"', units.polkit_rule([]))

    def test_the_discord_relay_can_write_mau_s_inbox_and_nothing_else(self):
        inbox = os.path.dirname(registry.get(discord_commands.MAU_JOB).watch_glob)
        writable = re.findall(r"^ReadWritePaths=-?(.*)$",
                              unit_text("session-ops-discord.service"), re.MULTILINE)
        self.assertEqual(writable, [inbox])
        with open(os.path.join(ROOT, "deploy", "install.sh"), encoding="utf-8") as handle:
            self.assertIn(f'if [ "$job" = {discord_commands.MAU_JOB} ]; then\n'
                          f'        install -d -o "$user" -g {units.RELAY_USER} -m 770',
                          handle.read())


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


if __name__ == "__main__":
    unittest.main()
