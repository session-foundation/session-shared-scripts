"""Fakes for the tests of every script that talks HTTP through `shared`."""
import json
import time
from datetime import datetime

import requests


class FakeResponse:
    # retry-after: 0 keeps the retry tests instant instead of sleeping through
    # the real backoff, and exercises the header-honoring path while it's at it.
    def __init__(self, payload, status_code=200, retry_after="0"):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"retry-after": retry_after}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class NonJsonResponse(FakeResponse):
    """A 200 whose body isn't JSON — a proxy error page, say."""

    def __init__(self):
        super().__init__({})
        self.text = "<html>maintenance</html>"

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)


class FakeSession:
    """Returns queued responses in order and records the requests made.

    A queued Exception is raised instead of returned, so transport failures can be
    exercised alongside HTTP status codes.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Patched:
    """Swap module attributes for the duration of a block, then put them back."""

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for name, value in self.attrs.items():
            try:
                self.saved[name] = getattr(self.module, name)
            except AttributeError:
                # Roll back what is already swapped. Without this, a typo'd or
                # since-removed attribute leaves earlier patches applied and
                # __exit__ never runs — every later test in the file then fails
                # against a module the failing test quietly rewrote.
                self.__exit__()
                raise
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)
        return False


class NoSleep:
    """Patch out time.sleep so retry tests assert on delays without waiting."""

    def __enter__(self):
        self.slept = []
        self._real = time.sleep
        time.sleep = self.slept.append
        return self

    def __exit__(self, *exc):
        time.sleep = self._real
        return False


def request_key(method, url, params=None, data=None, json_body=None):
    """What identifies a request in a recording: method, URL, query and body."""
    if json_body is not None:
        data = json.dumps(json_body, sort_keys=True)
    elif isinstance(data, (str, bytes)):
        try:
            data = json.dumps(json.loads(data), sort_keys=True)
        except ValueError:
            data = data.decode() if isinstance(data, bytes) else data
    query = sorted((str(k), str(v)) for k, v in (params or {}).items())
    return json.dumps([method.upper(), url, query, data])


class RecordedResponse(FakeResponse):
    def __init__(self, recorded):
        super().__init__(recorded.get("json"), recorded.get("status", 200))
        if "text" in recorded:
            self.text = recorded["text"]
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]


class RecordedSession:
    """Answers each request from a recording, matched by request rather than order.

    Order-independent so that a caller fanning requests across threads replays
    deterministically. A request missing from the recording fails the test by name.
    """

    def __init__(self, exchanges):
        self.headers = {}
        self._responses = {
            request_key(ex["method"], ex["url"], ex.get("params"), ex.get("data")):
                ex["response"]
            for ex in exchanges
        }
        self.calls = []

    def request(self, method, url, params=None, data=None, json=None, **kwargs):
        self.calls.append((method, url, params, data if json is None else json))
        key = request_key(method, url, params, data, json)
        if key not in self._responses:
            raise AssertionError(f"request not in the recording: {key}")
        return RecordedResponse(self._responses[key])

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def frozen_datetime(now):
    """A datetime class whose now() is `now`, for patching over a module's import."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    return Frozen
