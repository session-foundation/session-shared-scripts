"""
    python -m unittest discover        # from github_prs/
    UPDATE_GOLDENS=1 python -m unittest test_golden   # accept a new payload
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digest  # noqa: E402
from shared.testing import (GOLDENS_DIR, RecordedSession, assert_golden,  # noqa: E402
                            frozen_datetime, load_golden_json)


class TestDigestGolden(unittest.TestCase):
    """The dry-run output of a recorded live run over a 720-hour window."""

    def run_digest(self, *extra):
        recording = load_golden_json("digest/responses.json")
        session = RecordedSession(recording["exchanges"])
        out = io.StringIO()
        with mock.patch.object(digest, "github_session", lambda token: session), \
                mock.patch.object(digest, "datetime",
                                  frozen_datetime(datetime.fromisoformat(recording["now"]))), \
                mock.patch.object(sys, "argv", ["digest.py", *recording["argv"], *extra]), \
                mock.patch.dict(os.environ, {"GITHUB_PRS_TOKEN": "t"}), \
                contextlib.redirect_stdout(out):
            digest.main()
        return out.getvalue()

    def test_dry_run_matches_the_recorded_run(self):
        assert_golden(self, "digest/dry-run.txt", self.run_digest())

    def test_state_splits_the_window_into_new_changed_and_unchanged(self):
        state = os.path.join(GOLDENS_DIR, "digest", "state.json")
        assert_golden(self, "digest/dry-run-with-state.txt", self.run_digest("--state", state))


if __name__ == "__main__":
    unittest.main()
