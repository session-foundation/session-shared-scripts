"""
    cd shared && python -m unittest discover
"""
import os
import sys
import time
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import retry  # noqa: E402
from shared.testing import FakeResponse, FakeSession, NoSleep  # noqa: E402


class TestRequestWithRetry(unittest.TestCase):
    def test_returns_the_first_success_without_retrying(self):
        session = FakeSession([FakeResponse({"ok": True})])
        resp = retry.request_with_retry(session, "GET", "https://x")
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 1)

    def test_retries_a_server_error_then_succeeds(self):
        session = FakeSession([
            FakeResponse({}, status_code=500),
            FakeResponse({"ok": True}),
        ])
        resp = retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 2)

    def test_does_not_retry_a_client_error(self):
        session = FakeSession([FakeResponse({}, status_code=404)])
        resp = retry.request_with_retry(session, "GET", "https://x")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(session.calls), 1)

    def test_gives_up_after_the_attempt_budget(self):
        session = FakeSession([FakeResponse({}, status_code=503)] * 3)
        resp = retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(len(session.calls), 3)

    def test_transport_failures_retry_then_raise_when_exhausted(self):
        session = FakeSession([requests.ConnectionError("boom")] * 3)
        with NoSleep():
            with self.assertRaises(requests.ConnectionError):
                retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(len(session.calls), 3)

    def test_a_transport_failure_can_recover_on_a_later_attempt(self):
        session = FakeSession([requests.Timeout("slow"), FakeResponse({"ok": True})])
        with NoSleep():
            resp = retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(session.calls), 2)

    def test_no_sleep_after_the_final_attempt(self):
        """Sleeping after the last try only delays the caller — nothing follows it."""
        session = FakeSession([FakeResponse({}, status_code=503)] * 3)
        with NoSleep() as clock:
            retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(len(clock.slept), 2)  # 3 attempts, 2 gaps

    def test_numeric_retry_after_is_honoured(self):
        session = FakeSession([
            FakeResponse({}, status_code=429, retry_after="7"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(clock.slept, [7.0])

    def test_retry_after_is_capped(self):
        session = FakeSession([
            FakeResponse({}, status_code=429, retry_after="9999"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(clock.slept, [60])

    def test_http_date_retry_after_falls_back_instead_of_crashing(self):
        """RFC 9110 allows an HTTP-date here; float() on it used to raise ValueError."""
        session = FakeSession([
            FakeResponse({}, status_code=503, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            resp = retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(clock.slept, [1.0])  # fell back to the backoff delay

    def test_zero_attempts_is_rejected_rather_than_unbound(self):
        with self.assertRaises(ValueError):
            retry.request_with_retry(FakeSession([]), "GET", "https://x", attempts=0)

    def test_a_negative_retry_after_does_not_crash_a_real_retry_loop(self):
        session = FakeSession([
            FakeResponse({}, status_code=503, retry_after="-30"),
            FakeResponse({"ok": True}),
        ])
        with NoSleep() as clock:
            resp = retry.request_with_retry(session, "GET", "https://x", attempts=3)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertTrue(all(s >= 0 for s in clock.slept), clock.slept)


class TestRetryAfterSeconds(unittest.TestCase):
    def seconds(self, headers, default=4.0):
        resp = FakeResponse({}, retry_after=None)
        resp.headers = headers
        return retry.retry_after_seconds(resp, default)

    def test_missing_header_uses_the_default(self):
        self.assertEqual(self.seconds({}), 4.0)

    def test_numeric_header_wins(self):
        self.assertEqual(self.seconds({"retry-after": "12"}), 12.0)

    def test_unparseable_header_uses_the_default(self):
        for raw in ("Wed, 21 Oct 2026 07:28:00 GMT", "", "soon", "12s"):
            self.assertEqual(self.seconds({"retry-after": raw}), 4.0)

    def test_negative_and_non_finite_values_use_the_default(self):
        """time.sleep() rejects a negative or NaN duration, so passing one through
        would crash the run on a hostile or buggy Retry-After header."""
        for raw in ("-30", "-0.5", "nan", "inf", "-inf"):
            self.assertEqual(self.seconds({"retry-after": raw}), 4.0, msg=f"retry-after={raw!r}")

    def test_zero_is_honoured_rather_than_replaced(self):
        """Zero is a valid instruction to retry immediately, not a missing value."""
        self.assertEqual(self.seconds({"retry-after": "0"}), 0.0)

    def test_a_primary_rate_limit_falls_back_to_the_reset_epoch(self):
        """GitHub's primary limit sends x-ratelimit-reset and no Retry-After."""
        reset = str(int(time.time()) + 30)
        seconds = self.seconds({"x-ratelimit-reset": reset})
        self.assertTrue(25 <= seconds <= 31, seconds)

    def test_a_reset_epoch_in_the_past_means_no_wait(self):
        self.assertEqual(self.seconds({"x-ratelimit-reset": "1"}), 0.0)

    def test_retry_after_beats_the_reset_epoch(self):
        reset = str(int(time.time()) + 3000)
        self.assertEqual(self.seconds({"retry-after": "5", "x-ratelimit-reset": reset}), 5.0)

    def test_an_unparseable_reset_epoch_uses_the_default(self):
        self.assertEqual(self.seconds({"x-ratelimit-reset": "?"}), 4.0)


if __name__ == "__main__":
    unittest.main()
