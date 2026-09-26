"""The one HTTP transport every job uses: a requests.Session that retries, paces and
times out.

Retries happen in urllib3, below the session, so a caller sees only the final
answer. A 429, a 5xx or a rate-limited 403 that outlasts the budget comes back as a
response, whatever its status, and at once when the server's wait already exceeds
what the budget could sleep; a transport failure that outlasts it raises. Every
method is retried, POST included, which is what the callers here have always relied
on.

    session = http.Session(attempts=10, timeout=60, rate=20)
    session.get(url)                       # the session's budget
    session.request("GET", url, attempts=2)  # a shorter one for optional data
"""
import contextvars
import email.utils
import math
import threading
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, ResponseError
from urllib3.util.retry import Retry

DEFAULT_ATTEMPTS = 6
DEFAULT_TIMEOUT = 30
MAX_BACKOFF = 30
MAX_RETRY_AFTER = 60
# 403 only when it is a rate limit: see _Retry.increment.
RETRY_STATUSES = frozenset({403, 429, *range(500, 600)})

_attempts = contextvars.ContextVar("attempts", default=None)


def retry_after_seconds(resp, default):
    """Seconds to wait per the response's rate-limit headers, else `default`.

    Retry-After first, in either form RFC 9110 allows: a delay in seconds or an
    HTTP-date. GitHub answers an exhausted rate limit with x-ratelimit-reset as an
    epoch second and no Retry-After at all, so that is read when the header is
    absent and x-ratelimit-remaining is 0. GitHub sends the reset on every response,
    so without that check a 5xx would wait for the whole rate-limit window.

    Anything unparseable falls back rather than crashing the run. Negative, NaN
    and infinite values fall back too: time.sleep() rejects the first two
    outright, so a hostile or buggy proxy sending `Retry-After: -30` would
    otherwise take the run down with a ValueError.
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is None or resp.headers.get("x-ratelimit-remaining") != "0":
            return default
        try:
            return max(0.0, float(reset) - time.time())
        except (TypeError, ValueError):
            return default
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = _seconds_until_http_date(raw)
        if seconds is None:
            return default
    if not math.isfinite(seconds) or seconds < 0:
        return default
    return seconds


def is_rate_limited(resp):
    """GitHub signals a rate limit with a 403 as often as a 429; only these headers
    tell it from a 403 for a missing permission."""
    return (resp.headers.get("retry-after") is not None
            or resp.headers.get("x-ratelimit-remaining") == "0")


def _seconds_until_http_date(value):
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


class _Retry(Retry):
    """urllib3's Retry with this repo's waits: 1 s doubling to 30 s, or what the
    server asked for, capped at a minute.

    A server wait the attempts left cannot cover even at a minute each is not waited
    out at all: every retry would hit the same spent quota, and the sleeps alone can
    outlast the unit's timeout, so systemd kills the job before it can alert.
    """

    def increment(self, method=None, url=None, response=None, error=None, _pool=None,
                  _stacktrace=None):
        # is_retry() sees only the status. With raise_on_status off, MaxRetryError
        # makes urllib3 hand the response back unretried.
        if response is not None and response.status == 403 and not is_rate_limited(response):
            raise MaxRetryError(_pool, url, ResponseError("403 without rate-limit headers"))
        if response is not None and isinstance(self.total, int):
            wait = retry_after_seconds(response, None)
            if wait is not None and wait > MAX_RETRY_AFTER * self.total:
                raise MaxRetryError(_pool, url, ResponseError(
                    f"{response.status} asks for {wait:.0f} s, past what "
                    f"{self.total} retries can wait"))
        return super().increment(method, url, response, error, _pool, _stacktrace)

    def get_backoff_time(self):
        return min(2.0 ** (len(self.history) - 1), MAX_BACKOFF) if self.history else 0

    def get_retry_after(self, response):
        seconds = retry_after_seconds(response, None)
        return None if seconds is None else min(seconds, MAX_RETRY_AFTER)

    def sleep_for_retry(self, response):
        # The base class treats Retry-After: 0 as absent and backs off instead.
        seconds = self.get_retry_after(response)
        if seconds is None:
            return False
        time.sleep(seconds)
        return True


def _retry(attempts):
    if attempts <= 0:
        raise ValueError("attempts must be at least 1")
    return _Retry(total=attempts - 1, status_forcelist=RETRY_STATUSES, allowed_methods=None,
                  raise_on_status=False, respect_retry_after_header=True)


class _Adapter(HTTPAdapter):
    """Reads a per-call budget from the context HTTPAdapter.send() runs in, since
    requests gives no way to pass one down."""

    @property
    def max_retries(self):
        attempts = _attempts.get()
        return self._max_retries if attempts is None else _retry(attempts)

    @max_retries.setter
    def max_retries(self, value):
        self._max_retries = value


class TokenBucket:
    """At most `rate` requests per second on average, in bursts of up to `rate`."""

    def __init__(self, rate):
        self.rate = float(rate)
        self.tokens = self.rate
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            wait = 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate
            self.tokens -= 1
        if wait:
            time.sleep(wait)


class Session(requests.Session):
    """A session that retries 429, 5xx and a rate-limited 403, times out, and
    optionally paces itself.

    `limiter` may be shared between sessions, so that per-thread sessions against one
    API still draw on one budget. It paces each call, not each retry inside it.
    """

    def __init__(self, attempts=DEFAULT_ATTEMPTS, timeout=DEFAULT_TIMEOUT, rate=None,
                 limiter=None):
        super().__init__()
        self.timeout = timeout
        self.limiter = limiter or (TokenBucket(rate) if rate else None)
        adapter = _Adapter(max_retries=_retry(attempts))
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def request(self, method, url, *args, attempts=None, **kwargs):
        if attempts is not None and attempts <= 0:
            raise ValueError("attempts must be at least 1")
        kwargs.setdefault("timeout", self.timeout)
        if self.limiter:
            self.limiter.acquire()
        token = _attempts.set(attempts)
        try:
            return super().request(method, url, *args, **kwargs)
        finally:
            _attempts.reset(token)
