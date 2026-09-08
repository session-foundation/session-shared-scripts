#!/usr/bin/env python3
"""Tests for the Zendesk note webhook endpoint.

Stdlib unittest, same as the others. Everything is offline: note_reply.py is never
actually executed, and requests go through FastAPI's TestClient rather than a socket.
Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

This is the front door to something that writes public comments to customer tickets,
so the tests are about the gate: that an unsigned, tampered, stale or wrongly-signed
request is refused, and that an unconfigured relay refuses everything rather than
trusting whatever reaches the URL.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import sys
import unittest

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402
from test_triage import Patched  # noqa: E402

# The relay only ever compares this against what it computes, so its value is
# arbitrary — but it must not be empty, since an unset secret refuses everything.
ZENDESK_SECRET = "s3cret"

BASE_ENV = {"ZENDESK_SUBDOMAIN": "acme", "ZENDESK_EMAIL": "agent@acme.test",
            "ZENDESK_API_TOKEN": "tok", "RELAY_DRY_RUN": None}


class Env:
    """Replace the process environment for the duration of a block."""

    def __init__(self, **overrides):
        self.overrides = {**BASE_ENV, **overrides}
        self.saved = None

    def __enter__(self):
        self.saved = dict(os.environ)
        for key, value in self.overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)
        return False


def zendesk_post(body=None, *, secret=ZENDESK_SECRET, sign=True, age=0, tamper=False):
    """Send a signed Zendesk webhook through the real endpoint."""
    raw = json.dumps({"ticket_id": "27603"} if body is None else body).encode()
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Content-Type": "application/json"}
    if sign:
        digest = hmac.new(secret.encode(), stamp.encode() + raw, hashlib.sha256).digest()
        headers[relay.ZENDESK_SIGNATURE_HEADER] = base64.b64encode(digest).decode()
        headers[relay.ZENDESK_TIMESTAMP_HEADER] = stamp
    if tamper:
        raw = raw.replace(b"27603", b"27604")
    with TestClient(relay.app) as client:
        return client.post("/zendesk/notes", content=raw, headers=headers)


class TestZendeskWebhook(unittest.TestCase):
    """The second gate on a path that publishes comments to customers. The Zendesk
    trigger is the first, and note_reply.py decides who may command it."""

    def setUp(self):
        self.ran = []

    def run_with(self, **env):
        with Env(ZENDESK_WEBHOOK_SECRET=ZENDESK_SECRET, **env), \
                Patched(relay, run_note_reply=self.ran.append):
            return zendesk_post(**self.kwargs)

    def test_a_signed_webhook_queues_the_ticket(self):
        self.kwargs = {}
        response = self.run_with()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ran, ["27603"])

    def test_an_unsigned_webhook_is_refused(self):
        self.kwargs = {"sign": False}
        self.assertEqual(self.run_with().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_the_wrong_secret_is_refused(self):
        self.kwargs = {"secret": "wrong"}
        self.assertEqual(self.run_with().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_a_tampered_body_is_refused(self):
        """The signature covers the body, so swapping the ticket id invalidates it —
        otherwise anyone who captured one webhook could redirect it at any ticket."""
        self.kwargs = {"tamper": True}
        self.assertEqual(self.run_with().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_a_stale_webhook_is_refused(self):
        self.kwargs = {"age": relay.MAX_SIGNATURE_AGE_SECONDS + 60}
        self.assertEqual(self.run_with().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_a_webhook_from_the_future_is_refused(self):
        """The skew allowance is bounded in both directions: a timestamp far ahead of
        now is as much a replay signal as one far behind."""
        self.kwargs = {"age": -(relay.MAX_CLOCK_SKEW_SECONDS + 60)}
        self.assertEqual(self.run_with().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_a_small_clock_skew_is_tolerated(self):
        """Without the allowance a host a second behind Zendesk refuses everything."""
        self.kwargs = {"age": -(relay.MAX_CLOCK_SKEW_SECONDS // 2)}
        self.assertEqual(self.run_with().status_code, 200)

    def test_a_json_scalar_body_is_a_400_not_a_500(self):
        """Valid JSON that is not an object has no .get, and the AttributeError that
        followed surfaced as a 500 rather than the 400 malformed input earns."""
        # not None: that is zendesk_post's "use the default body" sentinel
        for body in (5, "just a string", ["a", "list"], True):
            with self.subTest(body=body):
                self.kwargs = {"body": body}
                self.assertEqual(self.run_with().status_code, 400)
        self.assertEqual(self.ran, [])

    def test_an_unconfigured_relay_refuses_everything(self):
        """No secret must mean no, not yes. This URL writes to customers."""
        self.kwargs = {}
        with Env(ZENDESK_WEBHOOK_SECRET=None), Patched(relay, run_note_reply=self.ran.append):
            self.assertEqual(zendesk_post().status_code, 401)
        self.assertEqual(self.ran, [])

    def test_a_missing_ticket_id_is_a_400(self):
        self.kwargs = {"body": {"nothing": "here"}}
        self.assertEqual(self.run_with().status_code, 400)
        self.assertEqual(self.ran, [])

    def test_a_bogus_ticket_id_is_refused(self):
        self.kwargs = {"body": {"ticket_id": "27603; rm -rf /"}}
        self.assertEqual(self.run_with().status_code, 400)
        self.assertEqual(self.ran, [])

    def test_a_non_json_body_is_refused_not_crashed(self):
        with Env(ZENDESK_WEBHOOK_SECRET=ZENDESK_SECRET), \
                Patched(relay, run_note_reply=self.ran.append):
            raw = b"not json"
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            digest = hmac.new(ZENDESK_SECRET.encode(), stamp.encode() + raw,
                              hashlib.sha256).digest()
            with TestClient(relay.app) as client:
                response = client.post("/zendesk/notes", content=raw, headers={
                    relay.ZENDESK_SIGNATURE_HEADER: base64.b64encode(digest).decode(),
                    relay.ZENDESK_TIMESTAMP_HEADER: stamp})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ran, [])



class TestSignatureTimestamps(unittest.TestCase):
    """The age check is only meaningful if the timestamp is read in the zone it was
    written in."""

    def signed(self, stamp, raw=b"{}"):
        digest = hmac.new(ZENDESK_SECRET.encode(), stamp.encode() + raw,
                          hashlib.sha256).digest()
        return relay.zendesk_signature_ok(
            raw, base64.b64encode(digest).decode(), stamp, ZENDESK_SECRET)

    def test_an_offsetless_timestamp_is_read_as_utc(self):
        """Left naive, .timestamp() reads it as this host's local time, so the age is
        wrong by the UTC offset — accepting a stale request, or refusing every fresh
        one, on any box not set to UTC."""
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertTrue(self.signed(now.strftime("%Y-%m-%dT%H:%M:%S")))

    def test_a_stale_offsetless_timestamp_is_still_refused(self):
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=relay.MAX_SIGNATURE_AGE_SECONDS + 60))
        self.assertFalse(self.signed(old.strftime("%Y-%m-%dT%H:%M:%S")))

    def test_an_unparseable_timestamp_is_refused(self):
        for stamp in ("yesterday", "", "2026-13-45T99:99:99Z"):
            with self.subTest(stamp=stamp):
                self.assertFalse(self.signed(stamp))


class TestTicketNumber(unittest.TestCase):
    """The id goes to a subprocess that writes to customers, so it is validated
    whole rather than filtered."""

    def test_a_plain_number_passes(self):
        self.assertEqual(relay.ticket_number("27603"), "27603")
        self.assertEqual(relay.ticket_number(27603), "27603")

    def test_anything_else_is_refused(self):
        for value in ("12/34", "27603; rm -rf /", "-1", "0", "", None, "1e3", " 12 34"):
            with self.subTest(value=value):
                self.assertIsNone(relay.ticket_number(value))

    def test_unicode_digits_are_refused(self):
        """isdigit() accepts Arabic-Indic digits that int() parses happily, and a
        URL should not carry them."""
        self.assertIsNone(relay.ticket_number("١٢٣"))


class TestDryRun(unittest.TestCase):
    """A switch whose job is "write nothing" has to fail towards writing nothing."""

    def check(self, value, expected):
        with Env(RELAY_DRY_RUN=value):
            self.assertIs(relay.dry_run_requested(), expected)

    def test_anything_set_counts_as_on(self):
        for value in ("1", "true", "yes", "on", "please"):
            with self.subTest(value=value):
                self.check(value, True)

    def test_explicit_off_words_count_as_off(self):
        for value in ("0", "false", "no", "off", "", None):
            with self.subTest(value=value):
                self.check(value, False)

    def test_a_systemd_inline_comment_still_reads_as_on(self):
        """systemd keeps an inline `#` as part of the value, so RELAY_DRY_RUN=1 with
        a trailing comment is not the string "1". An equality check read that as off,
        which is the opposite of what whoever wrote it meant."""
        self.check("1  # belt and braces", True)
