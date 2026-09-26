"""
    uv run python -m unittest tests.shared.test_http

Against a real server on localhost, so the retries under test are urllib3's own.
"""
import json
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests

from session_ops.shared import http
from session_ops.shared.testing import FakeResponse, NoSleep

DROP = "drop"


class Server:
    """Answers each request with the next scripted (status, headers) or drops the
    connection for DROP, and records what it was asked."""

    def __init__(self, *script):
        self.script, self.seen = list(script), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                length = int(self.headers.get("content-length") or 0)
                outer.seen.append((self.command, self.path, self.rfile.read(length)))
                step = outer.script.pop(0)
                if step == DROP:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, headers = step
                body = json.dumps({"status": status}).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = answer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/x"
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(**headers):
    return (200, headers)


def status(code, **headers):
    return (code, {k.replace("_", "-"): v for k, v in headers.items()})


class TestSession(unittest.TestCase):
    def serve(self, *script):
        server = Server(*script)
        self.addCleanup(server.close)
        return server

    def test_returns_the_first_success_without_retrying(self):
        server = self.serve(ok())
        self.assertEqual(http.Session().get(server.url).status_code, 200)
        self.assertEqual(len(server.seen), 1)

    def test_retries_a_server_error_then_succeeds(self):
        server = self.serve(status(500), ok())
        with NoSleep():
            self.assertEqual(http.Session(attempts=3).get(server.url).status_code, 200)
        self.assertEqual(len(server.seen), 2)

    def test_does_not_retry_a_client_error(self):
        server = self.serve(status(404))
        self.assertEqual(http.Session().get(server.url).status_code, 404)
        self.assertEqual(len(server.seen), 1)

    def test_an_exhausted_budget_returns_the_last_response(self):
        server = self.serve(*[status(503)] * 3)
        with NoSleep():
            self.assertEqual(http.Session(attempts=3).get(server.url).status_code, 503)
        self.assertEqual(len(server.seen), 3)

    def test_post_is_retried_too(self):
        server = self.serve(status(502), ok())
        with NoSleep():
            resp = http.Session(attempts=3).post(server.url, json={"a": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([body for _, _, body in server.seen], [b'{"a": 1}'] * 2)

    def test_a_per_call_budget_overrides_the_sessions(self):
        server = self.serve(*[status(503)] * 6)
        session = http.Session(attempts=6)
        with NoSleep():
            self.assertEqual(session.request("GET", server.url, attempts=2).status_code, 503)
        self.assertEqual(len(server.seen), 2)
        with NoSleep():
            session.request("GET", server.url, attempts=4)
        self.assertEqual(len(server.seen), 6)

    def test_a_dropped_connection_is_retried(self):
        server = self.serve(DROP, ok())
        with NoSleep():
            self.assertEqual(http.Session(attempts=3).get(server.url).status_code, 200)
        self.assertEqual(len(server.seen), 2)

    def test_transport_failures_raise_once_the_budget_is_spent(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with NoSleep() as clock, self.assertRaises(requests.ConnectionError):
            http.Session(attempts=3).get(f"http://127.0.0.1:{port}/")
        self.assertEqual(clock.slept, [1.0, 2.0])

    def test_backoff_doubles_from_one_second_and_never_sleeps_after_the_last_try(self):
        server = self.serve(*[status(500)] * 8)
        with NoSleep() as clock:
            http.Session(attempts=8).get(server.url)
        self.assertEqual(clock.slept, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])

    def test_numeric_retry_after_is_honoured(self):
        server = self.serve(status(429, retry_after="7"), ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertEqual(clock.slept, [7.0])

    def test_retry_after_zero_means_now_not_backoff(self):
        server = self.serve(status(429, retry_after="0"), ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertEqual(clock.slept, [0.0])

    def test_retry_after_is_capped_at_a_minute(self):
        server = self.serve(status(429, retry_after="9999"), ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertEqual(clock.slept, [60])

    def test_an_http_date_retry_after_is_honoured(self):
        soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
        server = self.serve(status(503, retry_after=soon), ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertTrue(15 <= clock.slept[0] <= 21, clock.slept)

    def test_a_negative_retry_after_falls_back_to_backoff(self):
        server = self.serve(status(503, retry_after="-30"), ok())
        with NoSleep() as clock:
            self.assertEqual(http.Session().get(server.url).status_code, 200)
        self.assertEqual(clock.slept, [1.0])

    def test_githubs_reset_epoch_stands_in_for_a_missing_retry_after(self):
        reset = str(int(time.time()) + 30)
        server = self.serve(status(429, x_ratelimit_remaining="0", x_ratelimit_reset=reset),
                            ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertTrue(25 <= clock.slept[0] <= 31, clock.slept)

    def test_a_server_error_backs_off_whatever_reset_it_carries(self):
        """GitHub sends the reset on every response; only an exhausted quota waits for it."""
        reset = str(int(time.time()) + 3000)
        server = self.serve(status(502, x_ratelimit_remaining="4999", x_ratelimit_reset=reset),
                            ok())
        with NoSleep() as clock:
            http.Session().get(server.url)
        self.assertEqual(clock.slept, [1.0])

    def test_a_403_with_the_quota_exhausted_waits_for_the_reset(self):
        reset = str(int(time.time()) + 30)
        server = self.serve(status(403, x_ratelimit_remaining="0", x_ratelimit_reset=reset),
                            ok())
        with NoSleep() as clock:
            self.assertEqual(http.Session().get(server.url).status_code, 200)
        self.assertTrue(25 <= clock.slept[0] <= 31, clock.slept)

    def test_a_403_with_retry_after_is_a_secondary_rate_limit(self):
        server = self.serve(status(403, retry_after="7"), ok())
        with NoSleep() as clock:
            self.assertEqual(http.Session().get(server.url).status_code, 200)
        self.assertEqual(clock.slept, [7.0])

    def test_a_403_for_a_missing_permission_is_not_retried(self):
        server = self.serve(status(403, x_ratelimit_remaining="4999"))
        with NoSleep() as clock:
            self.assertEqual(http.Session().get(server.url).status_code, 403)
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(clock.slept, [])

    def test_a_rate_limit_that_outlasts_the_budget_comes_back_as_its_403(self):
        server = self.serve(*[status(403, retry_after="1")] * 2)
        with NoSleep():
            self.assertEqual(http.Session(attempts=2).get(server.url).status_code, 403)
        self.assertEqual(len(server.seen), 2)

    def test_the_timeout_defaults_to_the_sessions_and_can_be_overridden(self):
        with mock.patch.object(requests.Session, "request") as sent:
            session = http.Session(timeout=45)
            session.get("https://x")
            session.get("https://x", timeout=5)
        self.assertEqual([call.kwargs["timeout"] for call in sent.call_args_list], [45, 5])

    def test_zero_attempts_is_rejected(self):
        with self.assertRaises(ValueError):
            http.Session(attempts=0)
        with self.assertRaises(ValueError):
            http.Session().request("GET", "https://x", attempts=0)

    def test_the_limiter_paces_every_call(self):
        limiter = mock.Mock()
        with mock.patch.object(requests.Session, "request"):
            session = http.Session(limiter=limiter)
            session.get("https://x")
            session.post("https://x")
        self.assertEqual(limiter.acquire.call_count, 2)


class TestTokenBucket(unittest.TestCase):
    def test_a_burst_up_to_the_rate_is_free_then_calls_wait(self):
        bucket = http.TokenBucket(4)
        with NoSleep() as clock:
            for _ in range(4):
                bucket.acquire()
            self.assertEqual(clock.slept, [])
            bucket.acquire()
        self.assertEqual(len(clock.slept), 1)
        self.assertAlmostEqual(clock.slept[0], 0.25, places=2)


class TestRetryAfterSeconds(unittest.TestCase):
    def seconds(self, headers, default=4.0):
        resp = FakeResponse({}, retry_after=None)
        resp.headers = headers
        return http.retry_after_seconds(resp, default)

    def test_missing_header_uses_the_default(self):
        self.assertEqual(self.seconds({}), 4.0)

    def test_numeric_header_wins(self):
        self.assertEqual(self.seconds({"retry-after": "12"}), 12.0)

    def test_unparseable_header_uses_the_default(self):
        for raw in ("", "soon", "12s", "Wed, 32 Oct 2026 07:28:00 GMT"):
            self.assertEqual(self.seconds({"retry-after": raw}), 4.0, msg=f"retry-after={raw!r}")

    def test_an_http_date_in_the_past_uses_the_default(self):
        """A date already gone means a negative wait, which time.sleep() rejects."""
        self.assertEqual(self.seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}), 4.0)

    def test_negative_and_non_finite_values_use_the_default(self):
        for raw in ("-30", "-0.5", "nan", "inf", "-inf"):
            self.assertEqual(self.seconds({"retry-after": raw}), 4.0, msg=f"retry-after={raw!r}")

    def test_zero_is_honoured_rather_than_replaced(self):
        self.assertEqual(self.seconds({"retry-after": "0"}), 0.0)

    def test_a_reset_epoch_in_the_past_means_no_wait(self):
        self.assertEqual(self.seconds({"x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": "1"}), 0.0)

    def test_the_reset_epoch_is_ignored_while_quota_remains(self):
        reset = str(int(time.time()) + 3000)
        self.assertEqual(self.seconds({"x-ratelimit-remaining": "12",
                                       "x-ratelimit-reset": reset}), 4.0)
        self.assertEqual(self.seconds({"x-ratelimit-reset": reset}), 4.0)

    def test_retry_after_beats_the_reset_epoch(self):
        reset = str(int(time.time()) + 3000)
        self.assertEqual(self.seconds({"retry-after": "5", "x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": reset}), 5.0)

    def test_an_unparseable_reset_epoch_uses_the_default(self):
        self.assertEqual(self.seconds({"x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": "?"}), 4.0)


if __name__ == "__main__":
    unittest.main()
