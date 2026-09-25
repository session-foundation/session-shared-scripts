#!/usr/bin/env python3
"""
Webhook endpoint for acting on `claude:` private notes in Zendesk.

Serves POST /zendesk/notes — vendor-namespaced so the same host and certificate can
carry other integrations later. Run it behind a TLS terminator; see
deploy/nginx-webhooks.conf.

    ⚠️  This is the front door to something that writes public comments to customer
        tickets. Every request is gated by the HMAC signature Zendesk issues, and it
        fails closed: with no ZENDESK_WEBHOOK_SECRET set, nothing is accepted.

note_reply.py is invoked as a subprocess, not imported and called. It is a CLI and
exits on every error path, and SystemExit derives from BaseException, so importing it
into a long-running service would mean one bad Zendesk response could take the
endpoint down. A subprocess turns each of those into an exit code, isolates a crash,
and keeps its own test suite testing exactly what runs in production.

Nothing is stored. A ticket is read when its webhook fires and forgotten; the draft
awaiting review lives in a private note on the ticket, so a restart mid-review loses
nothing.

This used to serve a second route, POST /discord/interactions, behind a Comment button
on each digest card. Replies are written on the ticket now — the digest is read-only,
and note_reply.py is the only way in.

Config (env vars):
    ZENDESK_SUBDOMAIN     e.g. "mycompany"
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token
    ZENDESK_WEBHOOK_SECRET  shared secret Zendesk signs its webhooks with. Unset
                          refuses every request
    RELAY_DRY_RUN         (optional) "1" passes --dry-run to note_reply.py, so the
                          whole path runs and nothing is written to Zendesk

A request is rejected unless its signature is valid and its timestamp is within
MAX_SIGNATURE_AGE_SECONDS, so a captured request cannot be replayed later. A timestamp
slightly in the future is accepted, within MAX_CLOCK_SKEW_SECONDS: the alternative is
an endpoint that refuses everything whenever this host's clock trails Zendesk's.

Usage:
    uvicorn relay:app --host 127.0.0.1 --port 8080
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time

from fastapi import BackgroundTasks, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

NOTE_MODULE = "session_ops.zendesk.note_reply"

# How stale a signed request may be. The signature covers the timestamp, so this
# cannot be forged — it bounds *replay* of a request that was genuinely signed.
MAX_SIGNATURE_AGE_SECONDS = 300
# Skew allowed in the other direction. The signature still covers the timestamp, so
# this widens nothing an attacker controls; without it a host whose clock is a second
# behind Zendesk's refuses every request with a 401 that says nothing about clocks.
MAX_CLOCK_SKEW_SECONDS = 60
# How long one note_reply.py run may take. Generous, because nothing is waiting on it:
# Zendesk gets its 200 immediately and the outcome is written to the ticket whenever
# it lands.
NOTE_TIMEOUT_SECONDS = 420
# Zendesk signs a webhook over the timestamp followed by the raw body.
ZENDESK_SIGNATURE_HEADER = "x-zendesk-webhook-signature"
ZENDESK_TIMESTAMP_HEADER = "x-zendesk-webhook-signature-timestamp"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def env(name, default=None):
    return os.environ.get(name, default)


def ticket_number(value):
    """The value if it is a plain positive ticket number, else None.

    Validated whole rather than filtered: stripping non-digits would quietly turn
    `12/34` into `1234` and hand a different ticket to a subprocess that writes to
    customers. isascii() as well as isdigit(), because isdigit() accepts Arabic-Indic
    and other unicode digits that int() parses but a URL should not carry.
    """
    text = str(value if value is not None else "").strip()
    if not (text.isascii() and text.isdigit()):
        return None
    return text if int(text) > 0 else None


def dry_run_requested():
    """Whether to pass --dry-run to note_reply.py.

    Anything set counts as on, bar an explicit 0/false/no/off. A switch whose job is
    "write nothing" has to fail towards writing nothing: systemd keeps an inline `#`
    as part of the value, so `RELAY_DRY_RUN=1  # …` is not the string "1", and an
    equality check would silently have read that as *off* — the opposite of what
    whoever wrote it meant.
    """
    value = (env("RELAY_DRY_RUN") or "").strip().lower()
    return bool(value) and value.split()[0] not in ("0", "false", "no", "off")


def run_note_reply(ticket_id):
    """Run note_reply.py over one ticket. Never raises.

    A subprocess because its error paths are sys.exit
    calls, and SystemExit would otherwise escape into a long-running service.

    There is nobody to report a failure to here — the agent is looking at a Zendesk
    ticket, not at an open dialog — so the outcome goes to the journal and, where
    note_reply.py got far enough to write one, to the ticket itself.
    """
    command = [sys.executable, "-m", NOTE_MODULE, "--ticket", str(ticket_id)]
    if dry_run_requested():
        command.append("--dry-run")
    try:
        done = subprocess.run(command, check=False, timeout=NOTE_TIMEOUT_SECONDS,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"note_reply.py timed out on #{ticket_id}", flush=True)
        return
    for line in (done.stdout or "").splitlines():
        print(line, flush=True)
    if done.returncode != 0:
        print(f"note_reply.py exited {done.returncode} on #{ticket_id}: "
              f"{(done.stderr or '').strip()[:300]}", flush=True)


def zendesk_signature_ok(raw, signature, timestamp, secret):
    """Whether Zendesk signed this body, recently.

    HMAC-SHA256 over the timestamp followed by the raw body, base64 encoded. The
    timestamp is ISO 8601, and it is covered by the signature — so the age check
    bounds replay of a request that was genuinely signed.

    A timestamp carrying no offset is read as UTC, which is what Zendesk sends.
    Left naive, `.timestamp()` would read it as this host's local time, so the age
    would be wrong by the UTC offset — quietly accepting a stale request, or refusing
    every fresh one, on any box not set to UTC.
    """
    if not (signature and timestamp and secret):
        return False
    try:
        when = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    age = time.time() - when.timestamp()
    if age < -MAX_CLOCK_SKEW_SECONDS or age > MAX_SIGNATURE_AGE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + raw, hashlib.sha256).digest()
    try:
        given = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, given)


@app.get("/healthz")
def healthz():
    """For the reverse proxy and for `systemctl status` to have something to say."""
    return {"ok": True}


@app.post("/zendesk/notes")
async def zendesk_notes(request: Request, background: BackgroundTasks):
    """A private note on a ticket asked Claude to do something.

    The Zendesk trigger that calls this is the first gate — it fires only on private
    comments, and must exclude the API user so a draft cannot trigger another draft.
    This is the second: without ZENDESK_WEBHOOK_SECRET set, nothing is accepted, so
    an unconfigured relay refuses rather than trusting anything that reaches the URL.
    Which Zendesk user may command it is note_reply.py's decision, on the note.

    Answers immediately and works in the background. Zendesk retries a webhook that
    does not answer quickly, and a retry that arrives mid-run would be a second reply
    to the customer — note_reply.py's done marker covers that, but not needing the
    cover is better.
    """
    raw = await request.body()
    if not zendesk_signature_ok(raw, request.headers.get(ZENDESK_SIGNATURE_HEADER),
                                request.headers.get(ZENDESK_TIMESTAMP_HEADER),
                                env("ZENDESK_WEBHOOK_SECRET")):
        return Response("bad signature", status_code=401)
    # A body that is valid JSON but not an object — a bare number, a string, a list —
    # has no .get, and the AttributeError that follows would surface as a 500 rather
    # than the 400 malformed input earns.
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    ticket_id = (ticket_number(body.get("ticket_id"))
                 if isinstance(body, dict) else None)
    if not ticket_id:
        return Response("no ticket id", status_code=400)
    background.add_task(run_in_threadpool, run_note_reply, ticket_id)
    return {"ok": True}


