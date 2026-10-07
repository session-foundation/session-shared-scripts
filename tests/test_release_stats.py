"""
    uv run python -m unittest tests.test_release_stats
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from session_ops.platforms import release_stats
from session_ops.shared.testing import FakeResponse, FakeSession


def asset(name, count):
    return {"name": name, "download_count": count}


def release(tag, *assets, published="2026-07-10", prerelease=False):
    return {"tag_name": tag, "published_at": f"{published}T00:00:00Z", "draft": False,
            "prerelease": prerelease, "assets": list(assets)}


LATEST = release(
    "v1.18.1",
    asset("session-desktop-linux-amd64-1.18.1.deb", 5),
    asset("session-desktop-linux-x86_64-1.18.1.AppImage", 7),
    asset("session-desktop-linux-x86_64-1.18.1.rpm", 1),
    asset("session-desktop-linux-x64-1.18.1.freebsd", 2),
    asset("session-desktop-mac-arm64-1.18.1.dmg", 30),
    asset("session-desktop-mac-arm64-1.18.1.dmg.blockmap", 900),
    asset("session-desktop-mac-x64-1.18.1.zip", 4),
    asset("session-desktop-win-x64-1.18.1.exe", 100),
    asset("session-desktop-win-x64-1.18.1.exe.blockmap", 900),
    asset("latest.yml", 900),
    asset("latest-linux.yml", 900),
    asset("signature.asc", 900))

DESKTOP = [
    release("v1.19.0", asset("session-desktop-win-x64-1.19.0.exe", 9),
            published="2026-10-01", prerelease=True),
    LATEST,
    release("v1.18.0", asset("session-desktop-win-x64-1.18.0.exe", 50),
            published="2026-04-09"),
]

FLATHUB = {"installs_per_day": {"2026-07-11": 10, "2026-07-09": 1000, "2026-07-10": 5}}


class PlatformTotalsTest(unittest.TestCase):
    def test_latest_skips_prereleases(self):
        self.assertIs(release_stats.latest(DESKTOP), LATEST)

    def test_counts_installers_only_and_adds_flathub_to_linux(self):
        self.assertEqual(release_stats.platform_totals(LATEST, 15), {
            "linux": 30, "macos": 34, "windows": 100,
            "linux_github": 15, "linux_flathub": 15,
        })

    def test_flathub_counts_from_the_release_day_on(self):
        self.assertEqual(release_stats.flathub_installs_since(FLATHUB, "2026-07-10"), 15)

    def test_flathub_refuses_a_release_older_than_its_window(self):
        with self.assertRaisesRegex(RuntimeError, "from 2026-07-09 only"):
            release_stats.flathub_installs_since(FLATHUB, "2026-07-01")

    def test_csv_columns_follow_the_header(self):
        totals = release_stats.platform_totals(LATEST, 15)
        self.assertEqual(release_stats.platforms_csv(LATEST, totals, "2026-10-06"),
                         "version,snapshot_date,release_date,linux,macos,windows,"
                         "linux_github,linux_flathub\n"
                         "1.18.1,2026-10-06,2026-07-10,30,34,100,15,15")


class MainTest(unittest.TestCase):
    def test_writes_the_three_csvs(self):
        session = FakeSession([FakeResponse(DESKTOP), FakeResponse([]),
                               FakeResponse(FLATHUB)])
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(release_stats.http, "Session", return_value=session), \
                contextlib.redirect_stdout(io.StringIO()):
            release_stats.main(["--out", out])
            (run,) = os.listdir(out)
            files = sorted(os.listdir(os.path.join(out, run)))
            with open(os.path.join(out, run, "desktop-platform-totals.csv"),
                      encoding="utf-8") as handle:
                row = handle.read().splitlines()[1]
        self.assertEqual(files, ["desktop-platform-totals.csv",
                                 "session-android-release-stats.csv",
                                 "session-desktop-release-stats.csv"])
        self.assertEqual(row, "1.18.1," + row.split(",")[1] + ",2026-07-10,30,34,100,15,15")

    def test_an_error_status_fails_the_run(self):
        session = FakeSession([FakeResponse({"message": "nope"}, status_code=404)])
        with mock.patch.object(release_stats.http, "Session", return_value=session), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            release_stats.main(["--dry-run"])
