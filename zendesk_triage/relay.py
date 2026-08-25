#!/usr/bin/env python3
"""
Discord interactions endpoint for replying to Zendesk tickets.

Serves POST /discord/interactions — vendor-namespaced so the same host and
certificate can carry other integrations later.

Discord pushes every interaction to one HTTPS endpoint and wants an answer inside
three seconds. This is that endpoint: it verifies the signature, decides who is
allowed, opens the compose dialog behind a digest card's Comment button, and hands
the work that writes to Zendesk to reply.py. Run it behind a TLS terminator — see
deploy/nginx-webhooks.conf.

    ⚠️  This is the front door to something that writes public comments to customer
        tickets. Every path through it is gated by an Ed25519 signature Discord
        issues and by an explicit allowlist; both fail closed.

reply.py is invoked as a subprocess, not imported and called. It is a CLI and exits
on every error path — twenty sys.exit() calls, and SystemExit derives from
BaseException, so importing it into a long-running service would mean one bad
Zendesk response could take the endpoint down. A subprocess turns each of those into
an exit code, isolates a crash, and keeps its own test suite testing exactly what
runs in production.

Nothing is stored. The dialog's ticket body is fetched when the button is pressed and
forgotten; attachments are linked, never downloaded. A draft between the dialog and
the Send button lives in the preview message's own embeds, so a restart mid-review
loses nothing that mattered.

Config (env vars):
    DISCORD_PUBLIC_KEY    the app's public key, for signature verification
    ALLOWED_USER_IDS      comma-separated Discord user ids allowed to reply
    ALLOWED_ROLE_IDS      comma-separated role ids allowed to reply
                          (either list grants; both unset refuses everybody)
    DISCORD_GUILD_ID      (optional) refuse interactions from any other server
    ZENDESK_SUBDOMAIN     e.g. "mycompany"
    ZENDESK_EMAIL         agent email for API token auth
    ZENDESK_API_TOKEN     Zendesk API token
    RELAY_DRY_RUN         (optional) "1" passes --dry-run to reply.py, so the whole
                          path runs and nothing is written to Zendesk

Requests are rejected unless their signature is valid and their timestamp is within
MAX_SIGNATURE_AGE_SECONDS, so a captured request cannot be replayed later. A timestamp
slightly in the future is accepted, within MAX_CLOCK_SKEW_SECONDS: the alternative is
an endpoint that refuses everything whenever this host's clock trails Discord's.

Usage:
    uvicorn relay:app --host 127.0.0.1 --port 8080
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import requests
from fastapi import BackgroundTasks, FastAPI, Request, Response
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage  # noqa: E402  (needs the path insert above)

REPLY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reply.py")

# Bounds the reply so its translation and back-translation still fit Discord's
# 6,000-character budget across the preview's embeds. reply.py refuses anything that
# overshoots, because a truncated embed would send a customer less than was reviewed.
MAX_REPLY_CHARS = 1200
# What the dialog shows of the ticket. Enough to answer without opening Zendesk, not
# so much that the requester's whole history is in a text box.
BODY_CHARS = 1200
MAX_ATTACHMENTS = 8
# How stale a signed request may be. The signature covers the timestamp, so this
# cannot be forged — it bounds *replay* of a request that was genuinely signed. Five
# minutes is Discord's own suggested window and is far more than the three seconds
# they wait for an answer.
MAX_SIGNATURE_AGE_SECONDS = 300
# Skew allowed in the other direction. The signature still covers the timestamp, so
# this widens nothing an attacker controls; without it a host whose clock is a second
# behind Discord's refuses every interaction — the endpoint-registering PING included
# — with a 401 that says nothing about clocks.
MAX_CLOCK_SKEW_SECONDS = 60
# How long one reply.py run may take, and unrelated to the three seconds Discord waits
# for an answer: that deadline is met by the deferral, and the work then runs in the
# background against the interaction token's own 15-minute life. What it covers is the
# translation plus Zendesk calls whose retry budget can spend a minute of backoff
# apiece, so a legitimate run can outlast this — which is why overrunning it reports
# to the agent rather than only to the journal. See tell_discord.
REPLY_TIMEOUT_SECONDS = 300

INTERACTION_PING = 1
INTERACTION_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5

RESPONSE_PONG = 1
RESPONSE_MESSAGE = 4
RESPONSE_DEFERRED = 5
RESPONSE_UPDATE_MESSAGE = 7
RESPONSE_MODAL = 9

EPHEMERAL = 64
SECTION = 9
TEXT_DISPLAY = 10
LABEL = 18
TEXT_INPUT = 4

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def env(name, default=None):
    return os.environ.get(name, default)


# ---- Authorization ---------------------------------------------------------


def id_list(raw):
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def refusal(interaction):
    """Why this person may not use the command, or None.

    Two independent allowlists, either of which grants. Both unset refuses
    everybody — this writes to customers, so "nobody has configured it yet" must not
    mean "everybody".

    There is no DM path on purpose: a DM interaction carries no member and no
    guild_id, so neither list has anything to read.
    """
    guild = env("DISCORD_GUILD_ID")
    if guild and interaction.get("guild_id") != guild:
        return "This command does not work here."
    users = id_list(env("ALLOWED_USER_IDS"))
    roles = id_list(env("ALLOWED_ROLE_IDS"))
    if not users and not roles:
        return ("Neither ALLOWED_USER_IDS nor ALLOWED_ROLE_IDS is set on the relay, "
                "so nobody can send replies yet.")
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    if user.get("id") and user["id"] in users:
        return None
    if any(role in roles for role in (member.get("roles") or [])):
        return None
    return "You are not on the list of people who can reply to Zendesk tickets."


def author(interaction):
    """The Discord author, as it will read in the ticket's private note."""
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    handle = user.get("username") or "unknown"
    display = member.get("nick") or user.get("global_name") or handle
    return f"@{handle}" if display == handle else f"{display} (@{handle})"


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


# ---- The ticket, as the dialog shows it ------------------------------------


def attachment_lines(comments):
    """Attachment links, newest comment first, as markdown.

    Linked rather than downloaded: Zendesk's content_url is a capability URL that
    resolves without authentication, so there is nothing to fetch, nothing to store,
    and nothing copied onto Discord's CDN. It is a bearer URL, which is why it only
    ever goes into an ephemeral dialog and never into a channel message or a log.
    """
    lines = []
    for comment in reversed(comments):
        for item in comment.get("attachments") or []:
            name = triage.clip(item.get("file_name") or "attachment", 60)
            url = item.get("content_url")
            if not url:
                continue
            size = item.get("size") or 0
            lines.append(f"📎 [{name}]({url}) · {size // 1024} KB")
    if len(lines) > MAX_ATTACHMENTS:
        extra = len(lines) - MAX_ATTACHMENTS
        lines = lines[:MAX_ATTACHMENTS] + [f"…and {extra} more on the ticket."]
    return lines


def zendesk_read(ticket_id, what, suffix, **params):
    """One authenticated GET under /tickets/{id}, parsed. None on any failure.

    Returns None rather than raising, on every path: a slow or unreachable Zendesk
    should cost the dialog its context, never its ability to open.

    Its own session per call, because two of these run concurrently and
    requests.Session is not documented as thread-safe. Setting an auth header is all
    a session is here, so there is nothing worth sharing.
    """
    subdomain = env("ZENDESK_SUBDOMAIN")
    email = env("ZENDESK_EMAIL")
    token = env("ZENDESK_API_TOKEN")
    if not (subdomain and email and token):
        return None
    url = f"https://{subdomain}.zendesk.com/api/v2/tickets/{ticket_id}{suffix}"
    try:
        session = triage.zendesk_session(email, token)
        # attempts=2: the dialog is on a deadline a modal cannot defer past, and the
        # full retry budget can burn a minute of backoff on what is only enrichment.
        resp = triage.request_with_retry(session, "GET", url, attempts=2,
                                         params=params)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the dialog
        print(f"#{ticket_id}: could not read {what} ({exc})", flush=True)
        return None
    if resp.status_code >= 400:
        print(f"#{ticket_id}: {what} returned {resp.status_code}", flush=True)
        return None
    try:
        return resp.json() or {}
    except ValueError:
        print(f"#{ticket_id}: {what} was not JSON", flush=True)
        return None


async def read_ticket(ticket_id):
    """The ticket and the requester's comments, as (ticket, comments).

    Two calls rather than one, run concurrently so they cost about what one costs.
    The second exists because `requester_id` is a field on the ticket and nothing on
    the comments: inferring it from comments[0] was wrong on every ticket somebody
    other than the requester opened — an agent taking a phone call, the review
    importer — and reply.py reads the field, so the dialog showed one person's words
    while the translation was chosen from another's.

    Either half can be None; the caller renders what it has.
    """
    ticket, comments = await asyncio.gather(
        run_in_threadpool(zendesk_read, ticket_id, "the ticket", ".json"),
        # Ascending: the requester's words read as the story they told, oldest first.
        # Spelled out rather than left to Zendesk's default for the endpoint.
        run_in_threadpool(zendesk_read, ticket_id, "its comments", "/comments.json",
                          per_page=100, sort_order="asc"),
    )
    return ((ticket or {}).get("ticket") or None,
            (comments or {}).get("comments") or None)


def ticket_context(ticket, comments):
    """What the ticket says, for the dialog. Returns markdown, or None.

    The requester's own comments, found by the ticket's `requester_id` — the same
    question reply.py asks of the same ticket, answered the same way. An agent's
    earlier English reply is text on the ticket too, and letting it in would drag the
    dialog towards English on exactly the tickets this feature exists for.

    When the ticket itself could not be read there is nobody to filter on, so every
    comment goes in under a heading that does not claim whose words they are. Better
    than an empty dialog, and it says which one you are looking at.
    """
    if not comments:
        return None
    requester = (ticket or {}).get("requester_id")
    if requester is None:
        heading = "**On the ticket**"
        bodies = [c.get("body") or "" for c in comments]
    else:
        heading = "**What they wrote**"
        bodies = [c.get("body") or "" for c in comments
                  if c.get("author_id") == requester]
    blocks = []
    body = triage.clip("\n\n".join(b.strip() for b in bodies if b.strip()), BODY_CHARS)
    if body:
        blocks.append(f"{heading}\n{body}")
    links = attachment_lines(comments)
    if links:
        blocks.append("\n".join(links))
    return "\n\n".join(blocks) or None


# ---- Responses ------------------------------------------------------------


def message(content):
    return {"type": RESPONSE_MESSAGE, "data": {"content": content, "flags": EPHEMERAL}}


def reply_modal(ticket_id, context=None):
    """The compose dialog, opened by the Comment button on a digest card.

    Text Display and Label rather than an Action-Row-wrapped input: modals gained
    Text Display in September 2025, which is what lets the dialog carry a link back
    to the ticket and the ticket's own words above the box.
    """
    subdomain = env("ZENDESK_SUBDOMAIN", "")
    lines = [f"**[Open #{ticket_id} in Zendesk]"
             f"(https://{subdomain}.zendesk.com/agent/tickets/{ticket_id})**"]
    if context:
        lines.append(context)
    return {
        "type": RESPONSE_MODAL,
        "data": {
            "custom_id": f"reply:{ticket_id}",
            "title": f"Reply to ticket #{ticket_id}",
            "components": [
                {"type": TEXT_DISPLAY, "content": "\n\n".join(lines)},
                {
                    "type": LABEL,
                    "label": "Your reply, in English",
                    "description": ("Translated into the requester's language. "
                                    "You review it before it sends."),
                    "component": {
                        "type": TEXT_INPUT,
                        "custom_id": "body",
                        "style": 2,
                        "required": True,
                        "max_length": MAX_REPLY_CHARS,
                        "placeholder": "What should the requester be told?",
                    },
                },
            ],
        },
    }


def submitted_value(node, custom_id):
    """One input's value from a modal submission, wherever Discord nested it.

    By custom_id, never by position: a Label wraps its input in `component`, an
    Action Row in `components`, and a Text Display sits alongside carrying neither.
    """
    if isinstance(node, list):
        for item in node:
            found = submitted_value(item, custom_id)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    if node.get("custom_id") == custom_id and isinstance(node.get("value"), str):
        return node["value"]
    # `is not None` rather than `or`, the same test the list branch makes: an empty
    # box submits "", and treating that as a miss would keep searching and report the
    # modal's shape as wrong when the shape was fine.
    for key in ("components", "component"):
        found = submitted_value(node.get(key), custom_id)
        if found is not None:
            return found
    return None


def card_text(node, custom_id):
    """The line the digest already wrote for this ticket, from the message the button
    was attached to. Free context when Zendesk is unreachable."""
    if isinstance(node, list):
        for item in node:
            found = card_text(item, custom_id)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    accessory = node.get("accessory") or {}
    if node.get("type") == SECTION and accessory.get("custom_id") == custom_id:
        for child in node.get("components") or []:
            if child.get("type") == TEXT_DISPLAY:
                return child.get("content")
    return card_text(node.get("components"), custom_id)


# ---- Handing the writes to reply.py ---------------------------------------


def tell_discord(payload, text):
    """Replace the agent's pending message with an outcome. Never raises.

    Only for the failures reply.py could not report itself. It reports its own
    refusals and then exits, but a crash, a non-zero exit and the timeout kill all
    leave the agent's message on "⏳ Sending…" — which reads as *still working* for a
    write that may already have emailed the customer.

    The interaction token that arrived in the payload is the credential, so this
    needs no bot token. A fresh session, never a Zendesk one: that carries the API
    token, and Discord has no business receiving it.

    attempts=2 because this is the last thing in the run and its own failure has
    nowhere to go: the full retry budget would hold a worker for minutes to tell
    somebody something the journal already records.
    """
    try:
        # Inside the try: run_reply promises not to raise, and this is the one part of
        # it that indexes rather than gets.
        url = (f"https://discord.com/api/v10/webhooks/{payload['application_id']}"
               f"/{payload['interaction_token']}/messages/@original")
        triage.request_with_retry(
            requests.Session(), "PATCH", url, attempts=2,
            json={"content": text, "embeds": [], "components": []})
    except Exception as exc:  # noqa: BLE001 — reporting a failure must not add one
        print(f"could not report the failure on #{payload.get('ticket_id')} to "
              f"Discord ({exc})", flush=True)


def run_reply(payload):
    """Run reply.py over this interaction. Never raises.

    A subprocess so that reply.py's twenty sys.exit() paths stay exit codes rather
    than SystemExit escaping into the service, and so a crash cannot take the
    endpoint down with it. Its stdout is ticket ids and outcomes by design — no
    ticket content — so it goes straight to the journal.
    """
    command = [sys.executable, REPLY_SCRIPT]
    if dry_run_requested():
        command.append("--dry-run")
    try:
        done = subprocess.run(
            command, check=False, timeout=REPLY_TIMEOUT_SECONDS,
            capture_output=True, text=True,
            env={**os.environ, "DISCORD_PAYLOAD": json.dumps(payload)})
    except subprocess.TimeoutExpired:
        print(f"reply.py timed out on #{payload.get('ticket_id')}", flush=True)
        # Deliberately not "nothing was sent": a run killed this late may have got
        # as far as the private note, or the reply itself. Only the ticket knows.
        tell_discord(payload, "❌ Timed out. Check the ticket in Zendesk before "
                              "writing the reply again — it may have gone out.")
        return
    for line in (done.stdout or "").splitlines():
        print(line, flush=True)
    if done.returncode != 0:
        # The message reply.py prints is the reason, and it goes to the journal
        # rather than to Discord: on this path it is as likely to be a traceback as
        # a sentence, and a stack trace under a customer's ticket number helps
        # nobody who is only deciding whether to write the reply again.
        print(f"reply.py exited {done.returncode} on #{payload.get('ticket_id')}: "
              f"{(done.stderr or '').strip()[:300]}", flush=True)
        tell_discord(payload, "❌ The reply failed. Check the ticket in Zendesk "
                              "before writing it again — it may have gone out.")


def dry_run_requested():
    """Whether to pass --dry-run to reply.py.

    Anything set counts as on, bar an explicit 0/false/no/off. A switch whose job is
    "write nothing" has to fail towards writing nothing: systemd keeps an inline `#`
    as part of the value, so `RELAY_DRY_RUN=1  # …` is not the string "1", and an
    equality check would silently have read that as *off* — the opposite of what
    whoever wrote it meant.
    """
    value = (env("RELAY_DRY_RUN") or "").strip().lower()
    return bool(value) and value.split()[0] not in ("0", "false", "no", "off")


def interaction_payload(interaction, action, **extra):
    return {
        "action": action,
        "user": author(interaction),
        "interaction_id": interaction["id"],
        "application_id": interaction["application_id"],
        "interaction_token": interaction["token"],
        **extra,
    }


# ---- Routing --------------------------------------------------------------


async def handle_component(interaction, background):
    custom_id = (interaction.get("data") or {}).get("custom_id") or ""
    if custom_id == "cancel":
        return {"type": RESPONSE_UPDATE_MESSAGE,
                "data": {"content": "Cancelled. Nothing was sent.",
                         "embeds": [], "components": []}}

    # Comment, on a card in the triage digest. That message is channel-visible,
    # unlike the ephemeral preview Send lives on, so this is the one button anybody
    # could press — the allowlist is what actually gates it.
    if custom_id.startswith("comment:"):
        denied = refusal(interaction)
        if denied:
            return message(f"❌ {denied}")
        ticket_id = ticket_number(custom_id.split(":", 1)[1])
        if not ticket_id:
            return message("❌ That is not a ticket number.")
        ticket, comments = await read_ticket(ticket_id)
        # Refused before the box opens rather than after a reply has been typed into
        # it: Zendesk takes no comment on a closed ticket, and closing cannot be
        # undone. Only when the ticket was actually read — an unreachable Zendesk must
        # cost the dialog its context and not its existence, and reply.py checks again
        # on its own before writing either way.
        if (ticket or {}).get("status") == "closed":
            return message(f"❌ #{ticket_id} is closed, and Zendesk takes no comments "
                           f"on a closed ticket.")
        context = ticket_context(ticket, comments)
        if context is None:
            context = card_text((interaction.get("message") or {}).get("components"),
                                custom_id)
        return reply_modal(ticket_id, context)

    if not custom_id.startswith("send:"):
        return message("❌ Unknown button.")
    denied = refusal(interaction)
    if denied:
        return message(f"❌ {denied}")
    ticket_id = ticket_number(custom_id.split(":", 1)[1])
    if not ticket_id:
        return message("❌ That is not a ticket number.")
    # The buttons go in the same response that acknowledges the click, so a second
    # click has nothing left to press. reply.py's marker covers a re-run.
    background.add_task(run_reply, interaction_payload(
        interaction, "send", ticket_id=ticket_id,
        embeds=(interaction.get("message") or {}).get("embeds") or []))
    return {"type": RESPONSE_UPDATE_MESSAGE,
            "data": {"content": f"⏳ Sending to #{ticket_id}…",
                     "embeds": [], "components": []}}


async def handle_modal_submit(interaction, background):
    denied = refusal(interaction)
    if denied:
        return message(f"❌ {denied}")
    data = interaction.get("data") or {}
    ticket_id = ticket_number((data.get("custom_id") or "").split(":", 1)[-1])
    if not ticket_id:
        return message("❌ That is not a ticket number.")
    body = submitted_value(data.get("components"), "body") or ""
    if not body.strip():
        # Logged, not just refused: an empty reply is far more likely to mean the
        # modal's shape moved than that somebody submitted a blank box.
        print("modal submit carried no body for custom_id 'body'", flush=True)
        return message("❌ The reply was empty.")
    background.add_task(run_reply, interaction_payload(
        interaction, "draft", ticket_id=ticket_id, body_en=body))
    return {"type": RESPONSE_DEFERRED, "data": {"flags": EPHEMERAL}}


@app.get("/healthz")
def healthz():
    """For the reverse proxy and for `systemctl status` to have something to say."""
    return {"ok": True}


@app.post("/discord/interactions")
async def interactions(request: Request, background: BackgroundTasks):
    raw = await request.body()
    signature = request.headers.get("x-signature-ed25519")
    timestamp = request.headers.get("x-signature-timestamp")
    key = env("DISCORD_PUBLIC_KEY")
    if not (signature and timestamp and key):
        return Response("bad signature", status_code=401)
    try:
        age = time.time() - float(timestamp)
    except (TypeError, ValueError):
        return Response("bad signature", status_code=401)
    if age < -MAX_CLOCK_SKEW_SECONDS or age > MAX_SIGNATURE_AGE_SECONDS:
        return Response("stale signature", status_code=401)
    try:
        VerifyKey(bytes.fromhex(key)).verify(timestamp.encode() + raw,
                                             bytes.fromhex(signature))
    except (BadSignatureError, ValueError):
        return Response("bad signature", status_code=401)

    interaction = json.loads(raw)
    kind = interaction.get("type")
    if kind == INTERACTION_PING:
        return {"type": RESPONSE_PONG}
    if kind == INTERACTION_COMPONENT:
        return await handle_component(interaction, background)
    if kind == INTERACTION_MODAL_SUBMIT:
        return await handle_modal_submit(interaction, background)
    return Response("unsupported interaction", status_code=400)
