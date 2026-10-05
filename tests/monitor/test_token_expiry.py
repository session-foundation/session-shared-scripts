"""
    uv run python -m unittest tests.monitor.test_token_expiry
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from session_ops.monitor import token_expiry

TODAY = date(2026, 10, 5)


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
    def collect(self, environ, seen=None, config=None, today=TODAY):
        seen = {} if seen is None else seen
        expiries = token_expiry.collect(config or token_expiry.Config({}, {}), environ,
                                        seen, None, today)
        return expiries, seen

    def test_crowdin_without_a_date_is_missing_and_unset_tokens_are_left_out(self):
        expiries, _ = self.collect({})
        self.assertEqual(expiries, {"CROWDIN_API_TOKEN": "missing"})

    def test_a_new_claude_token_expires_a_year_after_it_was_first_seen(self):
        expiries, seen = self.collect({CLAUDE: "tok-a"})
        self.assertEqual(expiries[CLAUDE], TODAY + YEAR)
        later, _ = self.collect({CLAUDE: "tok-a"}, seen, today=TODAY + timedelta(days=30))
        self.assertEqual(later[CLAUDE], TODAY + YEAR)

    def test_a_replaced_token_starts_a_new_year(self):
        _, seen = self.collect({CLAUDE: "tok-a"})
        renewed = TODAY + timedelta(days=300)
        expiries, _ = self.collect({CLAUDE: "tok-b"}, seen, today=renewed)
        self.assertEqual(expiries[CLAUDE], renewed + YEAR)

    def test_the_issued_date_applies_only_to_the_token_it_names(self):
        issued = date(2025, 11, 20)
        config = token_expiry.Config({}, {CLAUDE: (token_expiry.fingerprint("tok-a"), issued)})
        expiries, seen = self.collect({CLAUDE: "tok-a"}, config=config)
        self.assertEqual(expiries[CLAUDE], issued + YEAR)
        expiries, _ = self.collect({CLAUDE: "tok-b"}, seen, config)
        self.assertEqual(expiries[CLAUDE], TODAY + YEAR)

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

    def test_reads_the_expiry_header(self):
        response = FakeResponse(200, {token_expiry.GITHUB_EXPIRY_HEADER:
                                      "2027-10-02 03:00:00 UTC"})
        self.assertEqual(self.probe(response), date(2027, 10, 2))

    def test_no_header_is_a_token_that_never_expires(self):
        self.assertEqual(self.probe(FakeResponse(200)), "never")

    def test_a_401_is_reported_not_raised(self):
        self.assertEqual(self.probe(FakeResponse(401)), "rejected")

    def test_any_other_error_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            self.probe(FakeResponse(503))


class TestEvaluate(unittest.TestCase):
    def due_in(self, days, state=None):
        expiry = date.fromordinal(TODAY.toordinal() + days)
        return token_expiry.evaluate({"CROWDIN_API_TOKEN": expiry},
                                     {} if state is None else state, TODAY)

    def test_quiet_until_fourteen_days_out(self):
        self.assertEqual(self.due_in(15), [])
        self.assertEqual(self.due_in(14)[0][2], "14d")

    def test_each_threshold_and_expiry_alerts(self):
        self.assertEqual([self.due_in(d)[0][2] for d in (8, 7, 2, 1, 0, -3)],
                         ["14d", "7d", "7d", "1d", "1d", "expired"])

    def test_an_alerted_threshold_is_not_repeated(self):
        expiry = date(2026, 10, 15)
        state = {"CROWDIN_API_TOKEN": token_expiry.record(expiry, "14d")}
        self.assertEqual(token_expiry.evaluate({"CROWDIN_API_TOKEN": expiry}, state, TODAY), [])

    def test_a_renewed_token_clears_its_state(self):
        state = {"CROWDIN_API_TOKEN": token_expiry.record(date(2026, 10, 6), "1d")}
        self.assertEqual(self.due_in(365, state), [])
        self.assertEqual(state, {})

    def test_missing_and_rejected_alert_once(self):
        state = {}
        due = token_expiry.evaluate({"CLAUDE_CODE_OAUTH_TOKEN": "missing",
                                     "GITHUB_PRS_TOKEN": "rejected"}, state, TODAY)
        self.assertEqual([d[2] for d in due], ["missing", "rejected"])
        for name, expiry, current in due:
            state[name] = token_expiry.record(expiry, current)
        self.assertEqual(token_expiry.evaluate({"CLAUDE_CODE_OAUTH_TOKEN": "missing",
                                                "GITHUB_PRS_TOKEN": "rejected"}, state, TODAY),
                         [])

    def test_never_is_quiet(self):
        self.assertEqual(token_expiry.evaluate({"CROWDIN_API_TOKEN": "never"}, {}, TODAY), [])


class TestMessage(unittest.TestCase):
    def test_names_the_token_its_date_and_how_to_renew(self):
        message = token_expiry.build_message(
            [("CROWDIN_API_TOKEN", date(2026, 10, 12), "7d"),
             ("GITHUB_PRS_TOKEN", date(2026, 10, 6), "1d")],
            "box", TODAY, "/etc/session-ops/expiry.toml")
        self.assertIn("`box`", message)
        self.assertIn("**CROWDIN_API_TOKEN**: expires **2026-10-12**, in 7 days.", message)
        self.assertIn("crowdin.com/settings#api-key", message)
        self.assertIn("then its date in `/etc/session-ops/expiry.toml`", message)
        self.assertIn("**GITHUB_PRS_TOKEN**: expires **2026-10-06**, tomorrow.", message)

    def test_a_missing_date_names_the_file(self):
        message = token_expiry.build_message([("CLAUDE_CODE_OAUTH_TOKEN", "missing", "missing")],
                                             "box", TODAY, "/x/expiry.toml")
        self.assertIn("no expiry date in `/x/expiry.toml`", message)
        self.assertNotIn("Renew", message)


class TestMain(unittest.TestCase):
    def test_a_delivered_alert_is_recorded_and_not_repeated(self):
        state = os.path.join(tempfile.mkdtemp(), "state.json")
        path = expiry_file('[expires]\nCROWDIN_API_TOKEN = 2026-10-10\n')
        argv = ["--expiry-file", path, "--state", state, "--webhook", "https://example.invalid"]
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(token_expiry, "utc_today", return_value=TODAY), \
                mock.patch.object(token_expiry.discord, "post_to_discord",
                                  return_value=1) as post:
            token_expiry.main(argv)
            token_expiry.main(argv)
        self.assertEqual(post.call_count, 1)

    def test_a_failed_post_keeps_the_fingerprint_but_records_no_alert(self):
        state = os.path.join(tempfile.mkdtemp(), "state.json")
        path = expiry_file('[expires]\nCROWDIN_API_TOKEN = 2026-10-10\n')
        with mock.patch.dict(os.environ, {CLAUDE: "tok-a"}, clear=True), \
                mock.patch.object(token_expiry, "utc_today", return_value=TODAY), \
                mock.patch.object(token_expiry.discord, "post_to_discord", return_value=0), \
                self.assertRaises(SystemExit):
            token_expiry.main(["--expiry-file", path, "--state", state,
                               "--webhook", "https://example.invalid"])
        saved = token_expiry.load_state(state)
        self.assertEqual(saved["alerts"], {})
        self.assertEqual(saved["seen"][CLAUDE]["since"], TODAY.isoformat())


if __name__ == "__main__":
    unittest.main()
