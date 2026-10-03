"""crowdin-api-client on this repo's transport.

The SDK never retries a 429 (its should_retry is false for 300-499) and retries 5xx
with a fixed 100 ms sleep, while Crowdin throttles near 40 requests a second. So the
SDK supplies the endpoints, parameter names and error types, and shared.http the
retries and pacing, by standing in for the session its requester talks to. The SDK's
own loop is switched off with max_retries=1.
"""
import json
import threading

import crowdin_api.requester
from crowdin_api import CrowdinClient
from crowdin_api.exceptions import APIException

from session_ops.shared import http

# The SDK decodes every ISO timestamp in a response into a datetime, which json.dump
# cannot write and which does not sort against a missing one. Responses stay JSON.
crowdin_api.requester.loads = json.loads

# Shared by all of a client's threads, and under the ~40/s Crowdin throttles at, so a
# fan-out spends its budget on work rather than on 429s.
REQUESTS_PER_SECOND = 30

__all__ = ["APIException", "client", "error_message", "fetch_all"]


class _PerThreadSession:
    """One http.Session per thread behind the SDK's single session: the SDK shares
    it across every caller, and requests.Session is not guaranteed thread-safe."""

    def __init__(self, headers, attempts, timeout, limiter):
        self.headers = dict(headers)
        self._make = lambda: http.Session(attempts=attempts, timeout=timeout, limiter=limiter)
        self._local = threading.local()

    def request(self, method, url, **kwargs):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._make()
            session.headers.update(self.headers)
        return session.request(method, url, **kwargs)

    def close(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()


def client(token, project_id, attempts=10, timeout=60, rate=REQUESTS_PER_SECOND,
           session=None):
    """A CrowdinClient whose requests go through shared.http.

    `session` replaces the transport outright, which is how a test hands in a fake;
    it receives the SDK's auth headers like any other.
    """
    crowdin = CrowdinClient(token=token, project_id=int(project_id), timeout=timeout,
                            max_retries=1)
    requester = crowdin.get_api_requestor()
    headers = requester.session.headers
    requester.session.close()
    if session is None:
        session = _PerThreadSession(headers, attempts, timeout,
                                    http.TokenBucket(rate) if rate else None)
    else:
        session.headers.update(headers)
    requester._session = session
    return crowdin


def error_message(exc):
    """Crowdin's error message, or the start of the body when it is not that envelope."""
    body = response_text(exc)
    try:
        return json.loads(body).get("error", {}).get("message", "Unknown error")
    except (ValueError, AttributeError):
        return body[:200] or "Unknown error"


def response_text(exc):
    body = exc.context or b""
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)


def fetch_all(resource, method, **params):
    """Every item of a paginated list endpoint, unwrapped from the SDK's envelopes.

    `resource` must be fresh from the client (client.source_strings, say): the SDK
    keeps the fetch-all flag on the resource object, so a shared one races.
    """
    listing = getattr(resource.with_fetch_all(), method)
    return [row["data"] for row in listing(**params)["data"]]
