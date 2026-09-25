"""
    uv run python -m unittest tests.test_release_stats

The expected CSVs are what the TypeScript script this replaces wrote from the same
releases, on 2026-09-25.
"""
import contextlib
import glob
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from session_ops.platforms import release_stats
from session_ops.shared.testing import RecordedSession, frozen_datetime
from tests.golden import GOLDENS_DIR, load_golden_json


class TestReleaseStats(unittest.TestCase):
    def test_the_csvs_match_the_typescript_originals(self):
        recording = load_golden_json("release-stats/responses.json")
        session = RecordedSession(recording["exchanges"])
        snapshot = datetime.fromisoformat(recording["snapshot"]).replace(tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(release_stats.http, "Session", lambda: session), \
                mock.patch.object(release_stats, "datetime", frozen_datetime(snapshot)), \
                contextlib.redirect_stdout(io.StringIO()):
            release_stats.main(["--out", out])
            for repo in ("session-desktop", "session-android"):
                with self.subTest(repo=repo):
                    written, = glob.glob(os.path.join(out, "*", f"{repo}-release-stats.csv"))
                    with open(written, encoding="utf-8") as mine, \
                            open(os.path.join(GOLDENS_DIR, "release-stats", f"{repo}.csv"),
                                 encoding="utf-8") as theirs:
                        self.assertEqual(mine.read(), theirs.read())


if __name__ == "__main__":
    unittest.main()
