"""Fakes for the tests of every script that talks HTTP through `shared`."""
import json
import time

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
