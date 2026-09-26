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
            if name == "session-ops-alert@.service":
                continue
            with self.subTest(unit=name):
                self.assertIn("OnFailure=session-ops-alert@%n.service", unit_text(name))

    def test_a_scheduled_job_gets_a_timer_and_an_unscheduled_one_does_not(self):
        files = units.dropins(registry.load())
        for job in registry.load():
            with self.subTest(job=job.name):
                self.assertEqual(f"session-ops@{job.name}.timer.d/schedule.conf" in files,
                                 bool(job.schedule))

    def test_the_generated_dropins(self):
        files = units.dropins(registry.load())
        text = "".join(f"==> {path} <==\n{files[path]}\n" for path in sorted(files))
        assert_golden(self, "units/dropins.txt", text)


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
