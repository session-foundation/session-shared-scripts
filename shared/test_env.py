"""Run with: cd shared && python -m unittest discover"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.env import get_env  # noqa: E402
from shared.testing import Env  # noqa: E402


class TestGetEnv(unittest.TestCase):
    def test_precedence_is_flag_then_environment_then_default(self):
        with Env(SHARED_TEST_VALUE="from-env"):
            self.assertEqual(get_env("SHARED_TEST_VALUE", "from-flag"), "from-flag")
            self.assertEqual(get_env("SHARED_TEST_VALUE", default="d"), "from-env")
        with Env(SHARED_TEST_VALUE=None):
            self.assertEqual(get_env("SHARED_TEST_VALUE", default="d"), "d")

    def test_empty_counts_as_unset(self):
        with Env(SHARED_TEST_VALUE=""):
            self.assertEqual(get_env("SHARED_TEST_VALUE", "", default="d"), "d")
            self.assertIsNone(get_env("SHARED_TEST_VALUE", required=False))

    def test_missing_and_required_exits_naming_the_variable(self):
        with Env(SHARED_TEST_VALUE=None), self.assertRaises(SystemExit) as caught:
            get_env("SHARED_TEST_VALUE")
        self.assertIn("SHARED_TEST_VALUE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
