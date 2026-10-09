"""
    uv run python -m unittest tests.test_mau
"""
import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from session_ops.platforms import mau
from session_ops.shared.testing import FakeResponse, FakeSession


class GzipResponse(FakeResponse):
    def __init__(self, text):
        super().__init__({})
        self._content = gzip.compress(text.encode())

    @property
    def content(self):
        return self._content

PLAY_HEADER = f'Date,"{mau.PLAY_COLUMN}",Notes\n'


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

ANDROID = [release("1.33.6", asset("session-1.33.6-arm64-v8a-play-release.apk", 9),
                   prerelease=True),
           release("1.33.5", asset("app-play-release.aab", 500),
                   asset("session-1.33.5-arm64-v8a-play-release.apk", 60),
                   asset("session-1.33.5-universal-huawei-release.apk", 4),
                   asset("signature.asc", 500), published="2026-07-13")]

FLATHUB = {"installs_per_day": {"2026-07-11": 10, "2026-07-09": 1000, "2026-07-10": 5}}


def play(*rows):
    return PLAY_HEADER + "".join(f'"{day}","{value}",{note}\n' for day, value, note in rows)


def apple(*rows, column=mau.APPLE_COLUMN):
    return ('Name,"Session - Private Messenger"\nStart Date,8/31/26\nEnd Date,9/30/26\n\n'
            f"Date,{column}\n" + "".join(f"{day},{value}\n" for day, value in rows))


# LATEST's downloads with FLATHUB's 15 installs since its release: 30 + 34 + 100.
DOWNLOADS = (LATEST, {"linux": 30, "macos": 34, "windows": 100})
ANDROID_SEPTEMBER = play(("Aug 31, 2026", "100,000", ""),
                        ("Sep 29, 2026", "104,500", "Rollout of release: 1.32.1 at 5%."),
                        ("Sep 30, 2026", "105,000", ""))
IOS_SEPTEMBER = apple(("8/31/26", "40000.0"), ("9/29/26", "41500.0"), ("9/30/26", "42000.0"))
HISTORY = {"android": {"2026-08-31": 100000, "2026-09-30": 105000},
           "ios": {"2026-08-31": 40000, "2026-09-30": 42000}}
# iOS: 42,000 opted in at 1 in 4 is 168,000; August's 40,000 at 1 in 5 is 200,000.
# F-Droid: 10% of 105,000 + 168,000 + 64 + 164.
SOURCES = {"desktop": DOWNLOADS, "apks": (ANDROID[1], 64),
           "opt_in": ((400, 100), (500, 100))}


class ParseTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as handle:
            handle.write(text)
        self.addCleanup(os.remove, handle.name)
        return mau.parse_export(handle.name)

    def test_a_play_export_is_android_without_thousands_separators(self):
        self.assertEqual(self.parse(ANDROID_SEPTEMBER),
                         ("android", {"2026-08-31": 100000, "2026-09-29": 104500,
                                      "2026-09-30": 105000}))

    def test_an_app_store_connect_export_is_ios_past_its_preamble(self):
        self.assertEqual(self.parse(IOS_SEPTEMBER),
                         ("ios", {"2026-08-31": 40000, "2026-09-29": 41500, "2026-09-30": 42000}))

    def test_a_day_apple_is_still_counting_is_rounded(self):
        self.assertEqual(self.parse(apple(("10/7/26", "18973.094")))[1], {"2026-10-07": 18973})

    def test_apples_daily_active_devices_is_refused_for_the_30_day_metric(self):
        with self.assertRaisesRegex(mau.Rejected, "of Active Devices: export Active Last 30 Days"):
            self.parse(apple(("9/30/26", "25000.0"), column="Active Devices"))

    def test_a_blank_play_figure_is_skipped(self):
        self.assertEqual(self.parse(play(("Sep 30, 2026", "", ""), ("Oct 1, 2026", "1,000", "")))[1],
                         {"2026-10-01": 1000})

    def test_another_report_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "neither a Play Console MAU export"):
            self.parse('Date,"Daily active users (DAU): All countries / regions"\n'
                       '"Sep 30, 2026","1"\n')

    def test_a_console_in_another_language_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "line 2"):
            self.parse(play(("30 sept. 2026", "608 002", "")))

    def test_a_cut_off_last_row_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "line 4"):
            self.parse(ANDROID_SEPTEMBER.rsplit("\n", 2)[0] + "\nSep 3")
        with self.assertRaisesRegex(mau.Rejected, "line 9"):
            self.parse(IOS_SEPTEMBER + "9/3")

    def test_a_figure_cut_off_mid_number_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "not a CSV export"):
            self.parse(ANDROID_SEPTEMBER.rsplit("\n", 2)[0] + '\n"Sep 30, 2026","105,0')

    def test_a_non_figure_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "for a figure"):
            self.parse(play(("Sep 30, 2026", "n/a", "")))


class DesktopTest(unittest.TestCase):
    def test_latest_skips_prereleases(self):
        self.assertIs(mau.latest(DESKTOP), LATEST)

    def test_counts_installers_only_and_adds_flathub_to_linux(self):
        self.assertEqual(mau.platform_totals(LATEST, 15),
                         {"linux": 30, "macos": 34, "windows": 100})

    def test_flathub_counts_from_the_release_day_on(self):
        self.assertEqual(mau.flathub_installs_since(FLATHUB, "2026-07-10"), 15)

    def test_flathub_refuses_a_release_older_than_its_window(self):
        with self.assertRaisesRegex(RuntimeError, "from 2026-07-09 only"):
            mau.flathub_installs_since(FLATHUB, "2026-07-01")

    def test_an_error_status_fails(self):
        session = FakeSession([FakeResponse({"message": "nope"}, status_code=404)])
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            mau.desktop_downloads(session)


class AppleTest(unittest.TestCase):
    def request(self, rid, access, stopped=False):
        return {"id": rid, "attributes": {"accessType": access,
                                          "stoppedDueToInactivity": stopped}}

    def instance(self, iid, processed):
        return {"id": iid, "attributes": {"granularity": "DAILY", "processingDate": processed}}

    def page(self, *data):
        return FakeResponse({"data": list(data), "links": {}})

    def segment(self, *rows):
        return GzipResponse("Date\tApp Name\tApp Apple Identifier\tDownloading Users\t"
                            "Users Opting-In\n" + "".join(f"{d}\tSession\t1\t{n}\t{o}\n"
                                                         for d, n, o in rows))

    def test_the_latest_processed_instance_wins_and_older_ones_are_skipped(self):
        asc = FakeSession([
            self.page(self.request("snap", "ONE_TIME_SNAPSHOT"), self.request("on", "ONGOING")),
            self.page({"id": "r-snap"}),
            self.page(self.instance("old", "2026-07-01"), self.instance("s", "2026-10-06")),
            self.page({"id": "r-on"}),
            self.page(self.instance("o", "2026-10-07")),
            self.page({"attributes": {"url": "https://s3/s"}}),
            self.page({"attributes": {"url": "https://s3/o"}}),
        ])
        downloads = FakeSession([self.segment(("2026-09-30", 100, 25), ("2026-10-05", 10, 1)),
                                 self.segment(("2026-10-05", 12, 3), ("2026-10-06", 8, 2))])
        days = mau.opt_in_days(asc, downloads, since="2026-08-01")
        self.assertEqual(days, {"2026-09-30": (100, 25), "2026-10-05": (12, 3),
                                "2026-10-06": (8, 2)})
        self.assertEqual([url for _, url, _ in downloads.calls], ["https://s3/s", "https://s3/o"])

    def test_a_stopped_ongoing_request_fails(self):
        asc = FakeSession([self.page(self.request("on", "ONGOING", stopped=True))])
        with self.assertRaisesRegex(RuntimeError, "stopped .* for inactivity"):
            mau.opt_in_days(asc, FakeSession([]), since="2026-08-01")

    def test_the_rate_sums_the_month_only(self):
        days = {"2026-08-31": (1000, 1000), "2026-09-01": (300, 60), "2026-09-30": (100, 40)}
        self.assertEqual(mau.opt_in(days, date(2026, 9, 30)), (400, 100))
        self.assertIsNone(mau.opt_in(days, date(2026, 7, 31)))
        self.assertEqual(mau.ios_estimate(42000, (400, 100)), 168000)

    def test_apks_of_the_latest_stable_android_release_only(self):
        session = FakeSession([FakeResponse(ANDROID)])
        self.assertEqual(mau.apk_downloads(session), (ANDROID[1], 64))


class MergeTest(unittest.TestCase):
    def test_a_later_export_wins_per_platform_and_its_changes_are_returned(self):
        history = {"android": {"2026-09-29": 104000}, "ios": {"2026-09-29": 7}}
        revisions = mau.merge(history, [("a", "android", {"2026-09-29": 104500, "2026-09-30": 1}),
                                        ("b", "android", {"2026-09-30": 1}),
                                        ("c", "ios", {"2026-09-29": 7})])
        self.assertEqual(history, {"android": {"2026-09-29": 104500, "2026-09-30": 1},
                                   "ios": {"2026-09-29": 7}})
        self.assertEqual(revisions, [("android", "2026-09-29", 104000, 104500)])


class MessageTest(unittest.TestCase):
    def report(self, history=HISTORY, revisions=(), sources=SOURCES):
        return mau.report_message(date(2026, 9, 30), history, list(revisions), sources)

    def test_each_store_gives_its_month_end_and_the_change_on_the_month_before(self):
        message = self.report()
        self.assertIn("**Monthly active users, September 2026**", message)
        self.assertIn("Android, Play: **105,000** (+5,000, +5.0% on August)", message)
        self.assertIn("iOS: **168,000** (−32,000, -16.0% on August), estimated", message)

    def test_ios_names_the_opt_in_report_and_its_counts(self):
        self.assertIn("42,000 active in the 30 days to 30 September, scaled by September's "
                      "opt-in rate from Apple's App Opt In report: 25.0%, 100 of 400 "
                      "first-time downloaders", self.report())

    def test_apks_and_the_fdroid_estimate_share_a_line_and_everything_adds_up(self):
        message = self.report()
        self.assertIn("Android, outside Play: **27,387** (GitHub APKs 64 · F-Droid 27,323, "
                      "estimated)", message)
        self.assertIn("Desktop: **164** (Linux 30 · macOS 34 · Windows 100)", message)
        self.assertIn("**Total: 300,551**", message)
        self.assertIn("estimated as 10% of the other figures", message)
        self.assertIn("downloads of 1.33.5's APKs since its release on 13 July", message)
        self.assertIn("downloads of v1.18.1 since its release on 10 July", message)

    def test_no_change_without_the_month_befores_rate(self):
        message = self.report(sources={**SOURCES, "opt_in": ((400, 100), None)})
        self.assertIn("iOS: **168,000**, estimated", message)

    def test_no_opt_in_rate_fails_rather_than_guess(self):
        with self.assertRaisesRegex(RuntimeError, "no opt-in rate for September 2026"):
            self.report(sources={**SOURCES, "opt_in": (None, None)})

    def test_a_drop_is_signed(self):
        history = {**HISTORY, "android": {"2026-08-31": 107000, "2026-09-30": 105000}}
        self.assertIn("(−2,000, -1.9% on August)", self.report(history))

    def test_revisions_are_listed(self):
        message = self.report(revisions=[("ios", "2026-09-28", 41200, 41300)])
        self.assertIn("revised iOS 28 Sep 41,200 → 41,300", message)

    def test_the_reminder_names_each_missing_platform_and_the_upload_command(self):
        message = mau.reminder_message(date(2026, 9, 30), ["android", "ios"])
        self.assertIn("Android and iOS active users for September 2026 are missing", message)
        self.assertIn("- iOS: App Store Connect → Analytics", message)
        self.assertIn("covering 30 September", message)
        self.assertIn("`/mau-upload`", message)

    def test_the_reminder_for_one_platform_is_singular(self):
        message = mau.reminder_message(date(2026, 9, 30), ["ios"])
        self.assertIn("iOS active users for September 2026 is missing", message)
        self.assertNotIn("Android", message)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state))
        os.makedirs(os.path.join(self.state, "inbox"))
        patcher = mock.patch.dict(os.environ, {"MAU_DISCORD_WEBHOOK_URL": "https://hook"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def drop(self, name, text):
        with open(os.path.join(self.state, "inbox", name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_on(self, day, *args, responses=(FakeResponse({}),)):
        session = FakeSession(list(responses))
        now = datetime(*day, 12, tzinfo=mau.ZONE)
        with mock.patch.object(mau, "datetime", wraps=datetime) as clock, \
                mock.patch.object(mau.http, "Session", return_value=session), \
                mock.patch.object(mau, "read_sources", return_value=SOURCES), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            clock.now.return_value = now
            mau.main(["--state", self.state, *args])
        return session, out.getvalue()

    def posted(self, session):
        return [kwargs["json"]["content"] for method, _, kwargs in session.calls
                if method == "POST"]

    def history(self):
        with open(os.path.join(self.state, mau.HISTORY), encoding="utf-8") as handle:
            return json.load(handle)

    def listing(self, folder):
        return os.listdir(os.path.join(self.state, folder))

    def drop_both(self):
        self.drop("All countries _ regions.csv", ANDROID_SEPTEMBER)
        self.drop("session_private_messenger-active_last_30_days.csv", IOS_SEPTEMBER)

    def test_both_exports_post_once_and_are_filed_away(self):
        self.drop_both()
        session, _ = self.run_on((2026, 10, 9))
        (message,) = self.posted(session)
        self.assertIn("Android, Play: **105,000**", message)
        self.assertIn("iOS: **168,000**", message)
        self.assertEqual(self.listing("inbox"), [])
        self.assertEqual(len(self.listing("done")), 2)
        self.assertEqual(self.history()["posted"], ["2026-09"])

        session, out = self.run_on((2026, 10, 10))
        self.assertEqual(session.calls, [])
        self.assertIn("2026-09 already posted", out)

    def test_one_platform_alone_waits_then_is_reminded_of_the_other(self):
        self.drop("a.csv", ANDROID_SEPTEMBER)
        session, out = self.run_on((2026, 10, 9))
        self.assertEqual(session.calls, [])
        self.assertIn("Waiting for 2026-09-30 from ios", out)

        session, _ = self.run_on((2026, 10, 10))
        (message,) = self.posted(session)
        self.assertIn("iOS active users for September 2026 is missing", message)
        self.assertEqual(self.history()["posted"], [])

        self.drop("i.csv", IOS_SEPTEMBER)
        session, _ = self.run_on((2026, 10, 11))
        self.assertIn("iOS: **168,000**", self.posted(session)[0])
        self.assertEqual(self.history()["posted"], ["2026-09"])

    def test_an_export_without_the_month_end_is_kept_but_not_posted(self):
        self.drop("early.csv", play(("Sep 29, 2026", "104,500", "")))
        self.drop("i.csv", IOS_SEPTEMBER)
        session, _ = self.run_on((2026, 10, 10))
        self.assertIn("Android active users for September 2026 is missing", self.posted(session)[0])
        self.assertEqual(self.history()["android"], {"2026-09-29": 104500})

    def test_a_rejected_file_is_set_aside_and_fails_the_run_after_the_rest(self):
        self.drop_both()
        self.drop("wrong.csv", "Date,Installs\n")
        with self.assertRaisesRegex(SystemExit, "wrong.csv: neither"):
            self.run_on((2026, 10, 9))
        self.assertEqual(self.listing("inbox"), [])
        self.assertEqual(len(self.listing("rejected")), 1)
        self.assertEqual(self.history()["posted"], ["2026-09"])

    def test_a_dotfile_is_left_for_rsync_to_finish(self):
        self.drop(".export.csv.Ab12Cd", ANDROID_SEPTEMBER)
        session, _ = self.run_on((2026, 10, 9))
        self.assertEqual(session.calls, [])
        self.assertEqual(self.listing("inbox"), [".export.csv.Ab12Cd"])

    def test_a_refused_post_is_not_recorded_and_fails_the_run(self):
        self.drop_both()
        with self.assertRaisesRegex(RuntimeError, "Discord did not accept"):
            self.run_on((2026, 10, 9), responses=[FakeResponse({}, status_code=400)])
        self.assertEqual(self.history()["posted"], [])
        self.assertIn("2026-09-30", self.history()["ios"])

    def test_a_dry_run_moves_and_writes_nothing(self):
        self.drop_both()
        session, out = self.run_on((2026, 10, 9), "--dry-run")
        self.assertEqual(self.posted(session), [])
        self.assertIn("iOS: **168,000**", out)
        self.assertEqual(len(self.listing("inbox")), 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, mau.HISTORY)))

    def test_an_unreadable_history_stops_the_run_instead_of_starting_over(self):
        with open(os.path.join(self.state, mau.HISTORY), "w", encoding="utf-8") as handle:
            handle.write("{")
        self.drop("a.csv", ANDROID_SEPTEMBER)
        with self.assertRaises(ValueError):
            self.run_on((2026, 10, 9))
        self.assertEqual(self.listing("inbox"), ["a.csv"])
