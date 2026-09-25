"""
    UPDATE_GOLDENS=1 uv run python -m unittest tests.crowdin.test_approve_golden

The recording is shaped like Crowdin's API responses rather than captured live.
"""
import contextlib
import io
import sys
import unittest
from unittest import mock

from session_ops.crowdin import approve_strings as approve
from session_ops.crowdin import sdk
from session_ops.shared.testing import NoSleep, RecordedSession
from tests.golden import assert_golden, load_golden_json


class TestApproveGolden(unittest.TestCase):
    def run_approve(self, name):
        recording = load_golden_json("approve/responses.json")
        session = RecordedSession(recording["exchanges"])
        out = io.StringIO()
        with mock.patch.object(approve, "get_token", lambda: "t"), \
                mock.patch.object(approve, "crowdin_client",
                                  lambda token, pid: sdk.client(token, pid, session=session)), \
                mock.patch.object(sys, "argv", ["approve", *recording["runs"][name]]), \
                NoSleep(), contextlib.redirect_stdout(out):
            approve.main()
        return out.getvalue(), [call for call in session.calls if call[0].upper() == "POST"]

    def test_approving_by_user(self):
        out, posts = self.run_approve("approve")
        self.assertEqual(len(posts), 4)
        assert_golden(self, "approve/approve.txt", out)

    def test_listing_approves_nothing(self):
        out, posts = self.run_approve("list")
        self.assertEqual(posts, [])
        assert_golden(self, "approve/list.txt", out)


if __name__ == "__main__":
    unittest.main()
