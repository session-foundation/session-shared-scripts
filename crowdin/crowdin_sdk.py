"""crowdin-api-client with this repo's retry underneath it.

The SDK's own retry is a fixed 100 ms sleep, five times, on 5xx only: a 429 raises
at once whatever Retry-After says, and Crowdin throttles at ~40 req/s. So the SDK
supplies the endpoints, parameter names and error mapping, and shared.retry the
transport, by standing in for the requests.Session its requester talks to.
"""
import functools
import json
import threading

import requests
from crowdin_api import CrowdinClient
from crowdin_api.requester import APIRequester

from shared.retry import request_with_retry


class RetryingSession:
    """The SDK's session, routed through shared.retry.

    One requests.Session per thread: the SDK shares its session across callers, and
    requests.Session is not guaranteed thread-safe. `fixed` is one session for every
    thread instead, which is how a test hands in a fake.
    """

    def __init__(self, attempts):
        self.attempts = attempts
        self.headers = {}
        self.fixed = None
        self._local = threading.local()

    def _session(self):
        if self.fixed is not None:
            return self.fixed
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            session.headers.update(self.headers)
        return session

    def request(self, method, url, **kwargs):
        return request_with_retry(self._session(), method, url, attempts=self.attempts,
                                  **kwargs)

    def close(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()


class RetryingRequester(APIRequester):
    def __init__(self, *args, attempts, **kwargs):
        super().__init__(*args, **kwargs)
        # super().__init__ has already put the auth headers on the session it created.
        plain, self._session = self._session, RetryingSession(attempts)
        self._session.headers.update(plain.headers)
        plain.close()


def client(token, project_id, attempts, timeout):
    """A CrowdinClient retrying like shared.retry: `attempts` tries, backing off on
    429 and 5xx. max_retries=1 is the SDK's own loop switched off."""
    crowdin = CrowdinClient(token=token, project_id=int(project_id), timeout=timeout,
                            max_retries=1)
    crowdin.API_REQUESTER_CLASS = functools.partial(RetryingRequester, attempts=attempts)
    return crowdin


def use_session(crowdin, session):
    """Answer every request from `session` (a test fake) instead of the network."""
    crowdin.get_api_requestor().session.fixed = session


def error_message(exc):
    """Crowdin's error message, or the start of the body when it is not that envelope."""
    body = exc.context or b""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        return json.loads(body).get("error", {}).get("message", "Unknown error")
    except (ValueError, AttributeError):
        return body[:200] or "Unknown error"


def fetch_all(resource, method, **params):
    """Every item of a paginated list endpoint, unwrapped from the SDK's envelopes."""
    listing = getattr(resource.with_fetch_all(), method)
    return [row["data"] for row in listing(**params)["data"]]
