import glob
import os
import re
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_BIN = "/opt/zendesk/.venv/bin/"


class TestUnits(unittest.TestCase):
    def test_every_venv_command_a_unit_runs_is_a_declared_entry_point(self):
        """A unit only runs on the host, so a renamed entry point surfaces as a failed
        timer rather than at review time."""
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
            declared = set(tomllib.load(handle)["project"]["scripts"]) | {"uvicorn"}
        used = set()
        for unit in glob.glob(os.path.join(ROOT, "deploy", "*.service")):
            with open(unit, encoding="utf-8") as handle:
                used |= set(re.findall(re.escape(VENV_BIN) + r"([\w-]+)", handle.read()))
        self.assertTrue(used)
        self.assertEqual(used - declared, set())


if __name__ == "__main__":
    unittest.main()
