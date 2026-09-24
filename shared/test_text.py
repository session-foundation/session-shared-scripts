"""Run with: cd shared && python -m unittest discover"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import text  # noqa: E402


class TestClip(unittest.TestCase):
    def test_fits_within_the_limit_with_the_ellipsis(self):
        self.assertEqual(text.clip("abcdef", 4), "abc…")
        self.assertEqual(len(text.clip("abcdef", 4)), 4)

    def test_short_text_and_none_pass_through(self):
        self.assertEqual(text.clip("  abc  ", 10), "abc")
        self.assertEqual(text.clip(None, 10), "")


class TestSquash(unittest.TestCase):
    def test_collapses_every_kind_of_whitespace(self):
        self.assertEqual(text.squash(" a\n\t b   c "), "a b c")
        self.assertEqual(text.squash(None), "")


class TestWindowLabel(unittest.TestCase):
    def test_whole_days_read_as_days(self):
        self.assertEqual(text.window_label(24), "1 day")
        self.assertEqual(text.window_label(72), "3 days")

    def test_anything_else_stays_in_hours(self):
        self.assertEqual(text.window_label(36), "36h")
        self.assertEqual(text.window_label(12), "12h")


if __name__ == "__main__":
    unittest.main()
