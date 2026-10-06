"""
    uv run python -m unittest tests.monitor.test_token_expiry
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from session_ops.monitor import token_expiry



def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# 09:00 Melbourne on 6 October, the job's schedule: still the 5th in UTC.
NOW = utc(2026, 10, 5, 22)
TODAY = NOW.date()


def expiry_file(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(text)
    handle.close()
    return handle.name


CLAUDE = "CLAUDE_CODE_OAUTH_TOKEN"
YEAR = timedelta(days=365)


class TestConfig(unittest.TestCase):
    def test_reads_expires_and_issued(self):
        config = token_expiry.load_config(expiry_file(
            '[expires]\nCROWDIN_API_TOKEN = 2027-03-01\n'
            '[issued.CLAUDE_CODE_OAUTH_TOKEN]\nfingerprint = "abc"\ndate = 2025-11-20\n'))
        self.assertEqual(config.expires, {"CROWDIN_API_TOKEN": date(2027, 3, 1)})
        self.assertEqual(config.issued, {CLAUDE: ("abc", date(2025, 11, 20))})

    def test_a_missing_file_records_nothing(self):
        self.assertEqual(token_expiry.load_config("/nonexistent/expiry.toml"),
                         token_expiry.Config({}, {}))

    def test_refuses_an_expiry_that_is_neither_a_date_nor_never(self):
        with self.assertRaises(SystemExit):
            token_expiry.load_config(expiry_file('[expires]\nCROWDIN_API_TOKEN = "March"\n'))

    def test_refuses_an_issued_entry_without_a_fingerprint(self):
        with self.assertRaises(SystemExit):
            token_expiry.load_config(expiry_file('[issued.CLAUDE_CODE_OAUTH_TOKEN]\n'
                                                 'date = 2025-11-20\n'))


class TestCollect(unittest.TestCase):
    def collect(self, environ, seen=None, config=None, now=NOW):
        seen = {} if seen is None else seen
        expiries = token_expiry.collect(config or token_expiry.Config({}, {}), environ,
                                        seen, None, now)
        return expiries, seen

    def test_crowdin_without_a_date_is_missing_and_unset_tokens_are_left_out(self):
        expiries, _ = self.collect({})
        self.assertEqual(expiries, {"CROWDIN_API_TOKEN": "missing"})

    def test_a_recorded_date_counts_from_its_start_in_utc(self):
        config = token_expiry.Config({"CROWDIN_API_TOKEN": date(2027, 3, 1)}, {})
        self.assertEqual(self.collect({}, config=config)[0]["CROWDIN_API_TOKEN"],
                         utc(2027, 3, 1))

    def test_a_new_claude_token_expires_a_year_after_it_was_first_seen(self):
        expiries, seen = self.collect({CLAUDE: "tok-a"})
        self.assertEqual(expiries[CLAUDE], utc(2027, 10, 5))
        later, _ = self.collect({CLAUDE: "tok-a"}, seen, now=NOW + timedelta(days=30))
        self.assertEqual(later[CLAUDE], utc(2027, 10, 5))

    def test_a_replaced_token_starts_a_new_year(self):
        _, seen = self.collect({CLAUDE: "tok-a"})
        expiries, _ = self.collect({CLAUDE: "tok-b"}, seen, now=utc(2027, 8, 1, 22))
        self.assertEqual(expiries[CLAUDE], utc(2028, 7, 31))

    def test_the_issued_date_applies_only_to_the_token_it_names(self):
        config = token_expiry.Config({}, {CLAUDE: (token_expiry.fingerprint("tok-a"),
                                                   date(2025, 11, 20))})
        expiries, seen = self.collect({CLAUDE: "tok-a"}, config=config)
        self.assertEqual(expiries[CLAUDE], utc(2026, 11, 20))
        expiries, _ = self.collect({CLAUDE: "tok-b"}, seen, config)
        self.assertEqual(expiries[CLAUDE], utc(2027, 10, 5))

    def test_the_state_holds_a_fingerprint_not_the_token(self):
        _, seen = self.collect({CLAUDE: "tok-a"})
        self.assertNotIn("tok-a", repr(seen))
        self.assertEqual(seen[CLAUDE]["fingerprint"], token_expiry.fingerprint("tok-a"))

    def test_an_unset_token_forgets_its_fingerprint(self):
        _, seen = self.collect({CLAUDE: "tok-a"})
        self.collect({}, seen)
        self.assertEqual(seen, {})


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class TestProbe(unittest.TestCase):
    def probe(self, response):
        session = mock.Mock()
        session.request.return_value = response
        return token_expiry.probe_github(session, "tok")

    def test_reads_the_time_from_the_expiry_header(self):
        response = FakeResponse(200, {token_expiry.GITHUB_EXPIRY_HEADER:
                                      "2027-10-02 03:00:00 UTC"})
        self.assertEqual(self.probe(response), utc(2027, 10, 2, 3))

    def test_no_header_is_a_token_that_never_expires(self):
        self.assertEqual(self.probe(FakeResponse(200)), "never")

    def test_a_401_is_reported_not_raised(self):
        self.assertEqual(self.probe(FakeResponse(401)), "rejected")

    def test_any_other_error_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            self.probe(FakeResponse(503))


class TestEvaluate(unittest.TestCase):
    def level_at(self, left, state=None):
        due = token_expiry.evaluate({"CROWDIN_API_TOKEN": NOW + left},
                                    {} if state is None else state, NOW)
        return due[0][2] if due else None

    def test_each_window_by_time_left(self):
        hours = [24 * 14 + 1, 24 * 14, 24 * 7 + 1, 24 * 7, 25, 24, 1, 0, -1]
        self.assertEqual([self.level_at(timedelta(hours=h)) for h in hours],
                         [None, "14d", "14d", "7d", "7d", "1d", "1d", "expired", "expired"])

    def test_the_last_warning_comes_within_a_day_of_an_expiry_hours_away(self):
        """09:00 Melbourne on 2 Oct is 23:00 UTC on 1 Oct; the token expires at 03:00 UTC on 2 Oct."""
        expiry, now = utc(2027, 10, 2, 3), utc(2027, 10, 1, 23)
        due = token_expiry.evaluate({"GITHUB_PRS_TOKEN": expiry}, {}, now)
        self.assertEqual(due, [("GITHUB_PRS_TOKEN", expiry, "1d")])

    def test_a_date_only_expiry_warns_the_run_before_and_reports_expired_the_run_after(self):
        expiry = utc(2027, 3, 1)
        runs = [utc(2027, 2, 27, 22), utc(2027, 2, 28, 22), utc(2027, 3, 1, 22)]
        self.assertEqual([token_expiry.level(expiry, run) for run in runs],
                         ["7d", "1d", "expired"])

    def test_an_alerted_window_is_not_repeated(self):
        expiry = NOW + timedelta(days=10)
        state = {"CROWDIN_API_TOKEN": token_expiry.record(expiry, "14d")}
        self.assertEqual(token_expiry.evaluate({"CROWDIN_API_TOKEN": expiry}, state, NOW), [])

    def test_a_renewed_token_clears_its_state(self):
        state = {"CROWDIN_API_TOKEN": token_expiry.record(NOW + timedelta(hours=5), "1d")}
        self.assertIsNone(self.level_at(timedelta(days=365), state))
        self.assertEqual(state, {})

    def test_a_missing_date_alerts_on_every_run_and_rejected_once(self):
        state = {}
        expiries = {"CROWDIN_API_TOKEN": "missing", "GITHUB_PRS_TOKEN": "rejected"}
        due = token_expiry.evaluate(expiries, state, NOW)
        self.assertEqual([d[2] for d in due], ["missing", "rejected"])
        for name, expiry, current in due:
            state[name] = token_expiry.record(expiry, current)
        self.assertEqual(token_expiry.evaluate(expiries, state, NOW + timedelta(days=1)),
                         [("CROWDIN_API_TOKEN", "missing", "missing")])

    def test_a_date_recorded_later_stops_the_missing_alert(self):
        state = {"CROWDIN_API_TOKEN": token_expiry.record("missing", "missing")}
        self.assertEqual(token_expiry.evaluate({"CROWDIN_API_TOKEN": "never"}, state, NOW), [])
        self.assertEqual(state, {})

    def test_never_is_quiet(self):
        self.assertEqual(token_expiry.evaluate({"CROWDIN_API_TOKEN": "never"}, {}, NOW), [])


class TestMessage(unittest.TestCase):
    def test_names_the_token_its_time_window_and_how_to_renew(self):
        message = token_expiry.build_message(
            [("CROWDIN_API_TOKEN", utc(2026, 10, 12), "7d"),
             ("GITHUB_PRS_TOKEN", utc(2026, 10, 6, 3), "1d")],
            "box", "/etc/session-ops/expiry.toml")
        self.assertIn("`box`", message)
        self.assertIn("**CROWDIN_API_TOKEN**: expires **2026-10-12 00:00 UTC**, "
                      "in less than 7 days.", message)
        self.assertIn("crowdin.com/settings#api-key", message)
        self.assertIn("then its date in `/etc/session-ops/expiry.toml`", message)
        self.assertIn("**GITHUB_PRS_TOKEN**: expires **2026-10-06 03:00 UTC**, "
                      "in less than 24h.", message)

    def test_an_expired_token_says_when(self):
        message = token_expiry.build_message(
            [("GITHUB_PRS_TOKEN", utc(2026, 10, 5, 3), "expired")], "box", "/x/expiry.toml")
        self.assertIn("**expired** at 2026-10-05 03:00 UTC", message)

    def test_a_missing_date_names_the_file(self):
        message = token_expiry.build_message([("CLAUDE_CODE_OAUTH_TOKEN", "missing", "missing")],
                                             "box", "/x/expiry.toml")
        self.assertIn("no expiry date in `/x/expiry.toml`", message)
        self.assertNotIn("Renew", message)


class TestMain(unittest.TestCase):
    def test_a_delivered_alert_is_recorded_and_not_repeated(self):
        state = os.path.join(tempfile.mkdtemp(), "state.json")
        path = expiry_file('[expires]\nCROWDIN_API_TOKEN = 2026-10-10\n')
        argv = ["--expiry-file", path, "--state", state, "--webhook", "https://example.invalid"]
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(token_expiry, "utc_now", return_value=NOW), \
                mock.patch.object(token_expiry.discord, "post_to_discord",
                                  return_value=1) as post:
            token_expiry.main(argv)
            token_expiry.main(argv)
        self.assertEqual(post.call_count, 1)

    def test_a_failed_post_keeps_the_fingerprint_but_records_no_alert(self):
        state = os.path.join(tempfile.mkdtemp(), "state.json")
        path = expiry_file('[expires]\nCROWDIN_API_TOKEN = 2026-10-10\n')
        with mock.patch.dict(os.environ, {CLAUDE: "tok-a"}, clear=True), \
                mock.patch.object(token_expiry, "utc_now", return_value=NOW), \
                mock.patch.object(token_expiry.discord, "post_to_discord", return_value=0), \
                self.assertRaises(SystemExit):
            token_expiry.main(["--expiry-file", path, "--state", state,
                               "--webhook", "https://example.invalid"])
        saved = token_expiry.load_state(state)
        self.assertEqual(saved["alerts"], {})
        self.assertEqual(saved["seen"][CLAUDE]["since"], TODAY.isoformat())


if __name__ == "__main__":
    unittest.main()
