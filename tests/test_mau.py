"""
    uv run python -m unittest tests.test_mau
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from session_ops.platforms import mau
from session_ops.shared.testing import FakeResponse, FakeSession

HEADER = f'Date,"{mau.MAU_COLUMN}",Notes\n'


def export(*rows):
    return HEADER + "".join(f'"{day}","{value}",{note}\n' for day, value, note in rows)


SEPTEMBER = export(("Aug 31, 2026", "100,000", ""),
                   ("Sep 29, 2026", "104,500", "Rollout of release: 1.32.1 at 5%."),
                   ("Sep 30, 2026", "105,000", ""))


class ParseTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as handle:
            handle.write(text)
        self.addCleanup(os.remove, handle.name)
        return mau.parse_export(handle.name)

    def test_reads_each_day_without_thousands_separators(self):
        self.assertEqual(self.parse(SEPTEMBER), {"2026-08-31": 100000, "2026-09-29": 104500,
                                                 "2026-09-30": 105000})

    def test_a_blank_figure_is_skipped(self):
        self.assertEqual(self.parse(export(("Sep 30, 2026", "", ""),
                                           ("Oct 1, 2026", "1,000", ""))),
                         {"2026-10-01": 1000})

    def test_another_report_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "saved MAU report"):
            self.parse('Date,"Daily active users (DAU): All countries / regions"\n'
                       '"Sep 30, 2026","1"\n')

    def test_a_console_in_another_language_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "line 2"):
            self.parse(export(("30 sept. 2026", "608 002", "")))

    def test_a_cut_off_last_row_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "line 4"):
            self.parse(SEPTEMBER.rsplit("\n", 2)[0] + "\nSep 3")

    def test_a_figure_cut_off_mid_number_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "not a CSV export"):
            self.parse(SEPTEMBER.rsplit("\n", 2)[0] + '\n"Sep 30, 2026","105,0')

    def test_a_non_figure_is_refused(self):
        with self.assertRaisesRegex(mau.Rejected, "for a figure"):
            self.parse(export(("Sep 30, 2026", "n/a", "")))


class MergeTest(unittest.TestCase):
    def test_a_later_export_wins_and_its_changes_are_returned(self):
        history = {"2026-09-29": 104000}
        revisions = mau.merge(history, [("a", {"2026-09-29": 104500, "2026-09-30": 1}),
                                        ("b", {"2026-09-30": 1})])
        self.assertEqual(history, {"2026-09-29": 104500, "2026-09-30": 1})
        self.assertEqual(revisions, [("2026-09-29", 104000, 104500)])


class MessageTest(unittest.TestCase):
    def test_the_report_gives_the_month_end_and_the_change_on_the_month_before(self):
        android = {"2026-08-31": 100000, "2026-09-30": 105000}
        message = mau.report_message(date(2026, 9, 30), android, [])
        self.assertIn("**Monthly active users, September 2026**", message)
        self.assertIn("Android: **105,000** (+5,000, +5.0% on August)", message)

    def test_a_drop_is_signed(self):
        message = mau.report_message(date(2026, 9, 30),
                                     {"2026-08-31": 107000, "2026-09-30": 105000}, [])
        self.assertIn("(−2,000, -1.9% on August)", message)

    def test_revisions_are_listed(self):
        message = mau.report_message(date(2026, 9, 30), {"2026-09-30": 105000},
                                     [("2026-09-28", 104200, 104300)])
        self.assertIn("revised 28 Sep 104,200 → 104,300", message)

    def test_the_reminder_names_the_day_and_where_to_copy_the_export(self):
        with mock.patch.dict(os.environ, {"MAU_INBOX_HOST": "root@ops.example.org"}):
            message = mau.reminder_message(date(2026, 9, 30), "/var/lib/session-ops/mau/inbox")
        self.assertIn("Android MAU for September 2026 is missing", message)
        self.assertIn("covers 30 September", message)
        self.assertIn("root@ops.example.org:/var/lib/session-ops/mau/inbox/", message)


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
                contextlib.redirect_stdout(io.StringIO()) as out:
            clock.now.return_value = now
            mau.main(["--state", self.state, *args])
        return session, out.getvalue()

    def posted(self, session):
        return [kwargs["json"]["content"] for _, _, kwargs in session.calls]

    def history(self):
        with open(os.path.join(self.state, mau.HISTORY), encoding="utf-8") as handle:
            return json.load(handle)

    def listing(self, folder):
        return os.listdir(os.path.join(self.state, folder))

    def test_an_export_with_the_month_end_posts_once_and_is_filed_away(self):
        self.drop("All countries _ regions.csv", SEPTEMBER)
        session, _ = self.run_on((2026, 10, 9))
        self.assertIn("Android: **105,000**", self.posted(session)[0])
        self.assertEqual(self.listing("inbox"), [])
        (filed,) = self.listing("done")
        self.assertTrue(filed.endswith("-All countries _ regions.csv"))
        self.assertEqual(self.history()["posted"], ["2026-09"])

        session, out = self.run_on((2026, 10, 10))
        self.assertEqual(session.calls, [])
        self.assertIn("2026-09 already posted", out)

    def test_without_the_month_end_it_waits_then_reminds_from_the_tenth(self):
        self.drop("early.csv", export(("Sep 29, 2026", "104,500", "")))
        session, out = self.run_on((2026, 10, 9))
        self.assertEqual(session.calls, [])
        self.assertIn("Waiting for 2026-09-30", out)

        session, _ = self.run_on((2026, 10, 10))
        self.assertIn("is missing", self.posted(session)[0])
        self.assertEqual(self.history()["posted"], [])
        self.assertEqual(self.history()["android"], {"2026-09-29": 104500})

    def test_a_rejected_file_is_set_aside_and_fails_the_run_after_the_rest(self):
        self.drop("good.csv", SEPTEMBER)
        self.drop("wrong.csv", "Date,Installs\n")
        with self.assertRaisesRegex(SystemExit, "wrong.csv: no Date and"):
            session, _ = self.run_on((2026, 10, 9))
        self.assertEqual(self.listing("inbox"), [])
        self.assertEqual(len(self.listing("rejected")), 1)
        self.assertEqual(self.history()["posted"], ["2026-09"])

    def test_a_dotfile_is_left_for_rsync_to_finish(self):
        self.drop(".export.csv.Ab12Cd", SEPTEMBER)
        session, _ = self.run_on((2026, 10, 9))
        self.assertEqual(session.calls, [])
        self.assertEqual(self.listing("inbox"), [".export.csv.Ab12Cd"])

    def test_a_refused_post_is_not_recorded_and_fails_the_run(self):
        self.drop("a.csv", SEPTEMBER)
        with self.assertRaisesRegex(RuntimeError, "Discord did not accept"):
            self.run_on((2026, 10, 9), responses=[FakeResponse({}, status_code=400)])
        self.assertEqual(self.history()["posted"], [])
        self.assertIn("2026-09-30", self.history()["android"])

    def test_a_dry_run_moves_and_writes_nothing(self):
        self.drop("a.csv", SEPTEMBER)
        session, out = self.run_on((2026, 10, 9), "--dry-run")
        self.assertEqual(session.calls, [])
        self.assertIn("Android: **105,000**", out)
        self.assertEqual(self.listing("inbox"), ["a.csv"])
        self.assertFalse(os.path.exists(os.path.join(self.state, mau.HISTORY)))

    def test_an_unreadable_history_stops_the_run_instead_of_starting_over(self):
        with open(os.path.join(self.state, mau.HISTORY), "w", encoding="utf-8") as handle:
            handle.write("{")
        self.drop("a.csv", SEPTEMBER)
        with self.assertRaises(ValueError):
            self.run_on((2026, 10, 9))
        self.assertEqual(self.listing("inbox"), ["a.csv"])
