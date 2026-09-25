import email.utils
import math
import time
from datetime import datetime, timezone

import requests


def retry_after_seconds(resp, default):
    """Seconds to wait per the response's rate-limit headers, else `default`.

    Retry-After first, in either form RFC 9110 allows: a delay in seconds or an
    HTTP-date. GitHub answers a primary rate limit with x-ratelimit-reset as an
    epoch second and no Retry-After at all, so that is read when the header is
    absent.

    Anything unparseable falls back rather than crashing the run. Negative, NaN
    and infinite values fall back too: time.sleep() rejects the first two
    outright, so a hostile or buggy proxy sending `Retry-After: -30` would
    otherwise take the run down with a ValueError.
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is None:
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


def _seconds_until_http_date(value):
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def request_with_retry(session, method, url, attempts=6, timeout=30, **kwargs):
    """GET/POST with backoff on 429 and 5xx. Returns the response, whatever its status.

    Lower `attempts` for calls whose result is nice-to-have: the full budget can
    burn ~60s of backoff, which is not worth spending on optional data.
    """
    if attempts <= 0:
        raise ValueError("attempts must be at least 1")

    delay = 1.0
    last_exc = None
    resp = None
    for attempt in range(attempts):
        final = attempt == attempts - 1
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if final:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if final:
                break
            time.sleep(min(retry_after_seconds(resp, delay), 60))
            delay = min(delay * 2, 30)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp
