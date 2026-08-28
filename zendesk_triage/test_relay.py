#!/usr/bin/env python3
"""Tests for the Discord interactions endpoint.

Stdlib unittest, same as the others. Everything is offline: Zendesk runs against a
stub session, reply.py is never actually executed, and requests go through FastAPI's
TestClient rather than a socket. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

This is the front door to something that writes public comments to customer tickets,
so the tests are about the gates: that an unsigned or tampered request is refused,
that an unlisted person is refused, that a dialog still opens when Zendesk is
unreachable, and that an attachment URL never reaches a channel-visible message.
"""
import json
import os
import sys
import time
import unittest

from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402
import triage  # noqa: E402
from test_triage import FakeResponse, FakeSession, Patched  # noqa: E402


SIGNING_KEY = SigningKey.generate()
PUBLIC_KEY = SIGNING_KEY.verify_key.encode().hex()
USER_ID = "648644489891676160"
GUILD_ID = "1539868117302247444"

BASE_ENV = {
    "DISCORD_PUBLIC_KEY": PUBLIC_KEY,
    "ALLOWED_USER_IDS": USER_ID,
    "ALLOWED_ROLE_IDS": "",
    "DISCORD_GUILD_ID": GUILD_ID,
    "ZENDESK_SUBDOMAIN": "acme",
    "ZENDESK_EMAIL": "support@acme.test",
    "ZENDESK_API_TOKEN": "token",
}


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


def interaction(kind, *, custom_id=None, name=None, ticket=None, user_id=USER_ID,
                roles=(), guild_id=GUILD_ID, components=None, message=None,
                embeds=None):
    """A Discord-shaped interaction, only the fields relay.py reads."""
    data = {}
    if name:
        data["name"] = name
        data["options"] = [{"name": "ticket", "value": ticket}]
    if custom_id:
        data["custom_id"] = custom_id
    if components is not None:
        data["components"] = components
    row = {
        "id": "1400000000000000001",
        "application_id": "1300000000000000002",
        "token": "interaction-token",
        "type": kind,
        "guild_id": guild_id,
        "member": {"nick": "Audric", "roles": list(roles),
                   "user": {"id": user_id, "username": "audric"}},
        "data": data,
    }
    if message is not None or embeds is not None:
        row["message"] = {"components": message or [], "embeds": embeds or []}
    return row


def post(payload, *, sign=True, tamper=False, age=None):
    """Send a signed interaction through the real endpoint."""
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()) if age is None else int(time.time()) - age)
    headers = {"Content-Type": "application/json"}
    if sign:
        signed = SIGNING_KEY.sign(timestamp.encode() + raw).signature.hex()
        headers["x-signature-ed25519"] = signed
        headers["x-signature-timestamp"] = timestamp
    if tamper:
        raw = raw.replace(b'"type"', b'"typo"')
    with TestClient(relay.app) as client:
        return client.post("/discord/interactions", content=raw, headers=headers)


def comment(body, author_id=42, attachments=()):
    return {"id": 1, "author_id": author_id, "body": body,
            "attachments": list(attachments)}


ATTACHMENT_URL = "https://acme.zendesk.com/attachments/token/abc/"


def attachment(name="report.pdf", size=2048, url=ATTACHMENT_URL):
    return {"file_name": name, "size": size, "content_url": url,
            "content_type": "application/pdf", "inline": False}


# ---- The signature gate ----------------------------------------------------


class TestSignature(unittest.TestCase):
    """Discord signs every request. Nothing past this gate is trustworthy without
    it, so an unsigned request must never reach a handler."""

    def test_a_signed_ping_is_answered(self):
        with Env():
            resp = post({"type": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"type": relay.RESPONSE_PONG})

    def test_an_unsigned_request_is_refused(self):
        with Env():
            self.assertEqual(post({"type": 1}, sign=False).status_code, 401)

    def test_a_tampered_body_is_refused(self):
        with Env():
            self.assertEqual(post({"type": 1}, tamper=True).status_code, 401)

    def test_a_stale_signature_is_refused(self):
        """The signature covers the timestamp, so this is not about forgery — it
        bounds how long a genuinely signed request stays replayable."""
        with Env():
            self.assertEqual(
                post({"type": 1},
                     age=relay.MAX_SIGNATURE_AGE_SECONDS + 60).status_code, 401)

    def test_a_fresh_signature_inside_the_window_is_accepted(self):
        with Env():
            self.assertEqual(post({"type": 1}, age=10).status_code, 200)

    def test_a_clock_a_little_behind_discords_still_works(self):
        """A negative age is a timestamp from this host's future. Refusing those made
        the endpoint's health a matter of NTP: a host a second behind Discord refused
        every interaction, and the PING that registers the URL along with them."""
        with Env():
            self.assertEqual(post({"type": 1}, age=-10).status_code, 200)

    def test_a_timestamp_far_in_the_future_is_still_refused(self):
        with Env():
            self.assertEqual(
                post({"type": 1},
                     age=-(relay.MAX_CLOCK_SKEW_SECONDS + 60)).status_code, 401)

    def test_another_keys_signature_is_refused(self):
        with Env(DISCORD_PUBLIC_KEY=SigningKey.generate().verify_key.encode().hex()):
            self.assertEqual(post({"type": 1}).status_code, 401)

    def test_a_missing_public_key_refuses_rather_than_accepts(self):
        with Env(DISCORD_PUBLIC_KEY=None):
            self.assertEqual(post({"type": 1}).status_code, 401)

    def test_a_malformed_key_does_not_crash_the_endpoint(self):
        with Env(DISCORD_PUBLIC_KEY="not-hex"):
            self.assertEqual(post({"type": 1}).status_code, 401)


# ---- The allowlist --------------------------------------------------------


class TestAllowlist(unittest.TestCase):
    def test_a_listed_user_is_allowed(self):
        with Env():
            self.assertIsNone(relay.refusal(interaction(2)))

    def test_an_unlisted_user_is_refused(self):
        with Env():
            self.assertIsNotNone(relay.refusal(interaction(2, user_id="999")))

    def test_a_listed_role_is_allowed(self):
        with Env(ALLOWED_USER_IDS="", ALLOWED_ROLE_IDS="777"):
            self.assertIsNone(relay.refusal(interaction(2, user_id="999",
                                                        roles=["777"])))

    def test_no_allowlist_refuses_everybody(self):
        """An unconfigured relay must not be an open one — this writes to customers."""
        with Env(ALLOWED_USER_IDS="", ALLOWED_ROLE_IDS=""):
            self.assertIsNotNone(relay.refusal(interaction(2)))

    def test_another_server_is_refused(self):
        with Env():
            self.assertIsNotNone(relay.refusal(interaction(2, guild_id="other")))

    def test_a_dm_has_no_member_and_is_refused(self):
        """No member means no roles, so neither list can grant. DISCORD_GUILD_ID is
        unset here on purpose: with it set the guild check answers first and this
        never reaches the allowlist branch it is meant to cover."""
        bare = {"id": "1", "application_id": "2", "token": "t", "type": 2,
                "user": {"id": "999", "username": "someone"}, "data": {}}
        with Env(DISCORD_GUILD_ID=None):
            self.assertIsNotNone(relay.refusal(bare))


# ---- The dialog -----------------------------------------------------------


REQUESTER_ID = 42


def ticket_row(requester_id=REQUESTER_ID, status="open"):
    return {"id": 27896, "requester_id": requester_id, "status": status}


class TestTicketContext(unittest.TestCase):
    """Rendering only: ticket_context is handed what read_ticket fetched, so the
    filtering can be asserted without a session in the way."""

    def context(self, comments, ticket=...):
        """`ticket=None` is the real case of the ticket call having failed, so the
        default has to be something else."""
        return relay.ticket_context(ticket_row() if ticket is ... else ticket, comments)

    def test_the_requesters_words_are_shown(self):
        got = self.context([
            comment("Es geht nicht mehr."),
            comment("Thanks for reaching out!", author_id=7),
            comment("Immer noch kaputt."),
        ])
        self.assertIn("Es geht nicht mehr.", got)
        self.assertIn("Immer noch kaputt.", got)

    def test_an_agents_reply_is_not_mistaken_for_the_requesters(self):
        """An agent's earlier English reply is text on the ticket too, and letting it
        in drags the dialog towards English on the tickets this feature exists for."""
        got = self.context([
            comment("Es geht nicht mehr."),
            comment("Thanks for reaching out!", author_id=7),
        ])
        self.assertNotIn("Thanks for reaching out", got)

    def test_the_requester_is_the_ticket_field_not_whoever_opened_it(self):
        """An agent taking a phone call is the author of comment 0 and not the
        requester. Filtering on comments[0] showed the dialog that agent's English
        while reply.py, which reads requester_id, translated for the customer."""
        got = self.context([
            comment("Called in about a login problem.", author_id=7),
            comment("Ich komme nicht mehr rein."),
        ])
        self.assertIn("Ich komme nicht mehr rein.", got)
        self.assertNotIn("Called in about", got)
        self.assertIn("What they wrote", got)

    def test_an_unreadable_ticket_shows_every_comment_without_claiming_whose(self):
        """Half the fetch can fail. An empty dialog is worse than an unattributed
        one, but a heading that names the requester would be a claim we cannot make."""
        got = self.context([comment("Es geht nicht mehr."),
                            comment("Thanks!", author_id=7)], ticket=None)
        self.assertIn("Es geht nicht mehr.", got)
        self.assertIn("Thanks!", got)
        self.assertIn("On the ticket", got)
        self.assertNotIn("What they wrote", got)

    def test_the_english_rendering_replaces_the_original_when_the_field_is_set(self):
        """The dialog exists so an agent who cannot read German can answer a German
        ticket. The digest writes this to Zendesk before the card carrying the button
        is posted, which is what makes it there to read."""
        ticket = ticket_row()
        ticket["custom_fields"] = [{"id": 42, "value": "It stopped working."}]
        with Patched(relay.os, environ={"ZENDESK_ENGLISH_FIELD_ID": "42"}):
            got = self.context([comment("Es geht nicht mehr.")], ticket=ticket)
        self.assertIn("It stopped working.", got)
        self.assertIn("translated", got)
        self.assertNotIn("Es geht nicht mehr.", got)

    def test_without_the_field_configured_nothing_changes(self):
        """The field does not exist in Zendesk yet. Until it does, the dialog has to
        behave exactly as it did before — the customer's own words, no heading that
        claims a translation nobody made."""
        ticket = ticket_row()
        ticket["custom_fields"] = [{"id": 42, "value": "It stopped working."}]
        with Patched(relay.os, environ={}):
            got = self.context([comment("Es geht nicht mehr.")], ticket=ticket)
        self.assertIn("Es geht nicht mehr.", got)
        self.assertNotIn("It stopped working.", got)
        self.assertNotIn("translated", got)

    def test_an_empty_field_falls_back_rather_than_showing_a_blank_dialog(self):
        """Zendesk returns the field on every ticket once it exists, with a null
        value on the ones nothing was written to — an English ticket, or one the
        digest could not render. That must read as absent, not as empty."""
        for value in (None, "", "   "):
            ticket = ticket_row()
            ticket["custom_fields"] = [{"id": 42, "value": value}]
            with Patched(relay.os, environ={"ZENDESK_ENGLISH_FIELD_ID": "42"}):
                got = self.context([comment("Es geht nicht mehr.")], ticket=ticket)
            self.assertIn("Es geht nicht mehr.", got, f"value={value!r}")
            self.assertNotIn("translated", got, f"value={value!r}")

    def test_another_custom_field_is_not_mistaken_for_the_rendering(self):
        """Tickets carry many custom fields. Matching anything but the configured id
        would put an unrelated field's value in front of the agent as the customer's
        words."""
        ticket = ticket_row()
        ticket["custom_fields"] = [{"id": 7, "value": "android"},
                                   {"id": 42, "value": "It stopped working."}]
        with Patched(relay.os, environ={"ZENDESK_ENGLISH_FIELD_ID": "42"}):
            got = self.context([comment("Es geht nicht mehr.")], ticket=ticket)
        self.assertIn("It stopped working.", got)
        self.assertNotIn("android", got)

    def test_attachments_are_linked_with_name_and_size(self):
        got = self.context([comment("Siehe Anhang.", attachments=[attachment()])])
        self.assertIn("report.pdf", got)
        self.assertIn("https://acme.zendesk.com/attachments/token/abc/", got)
        self.assertIn("2 KB", got)

    def test_a_flood_of_attachments_is_capped_and_announced(self):
        many = [attachment(name=f"f{i}.pdf") for i in range(relay.MAX_ATTACHMENTS + 4)]
        got = self.context([comment("x", attachments=many)])
        self.assertEqual(got.count("📎"), relay.MAX_ATTACHMENTS)
        self.assertIn("4 more", got)

    def test_a_long_body_is_clipped(self):
        got = self.context([comment("ä" * 9000)])
        self.assertLess(len(got), relay.BODY_CHARS + 200)

    def test_nothing_to_show_is_none_rather_than_an_empty_heading(self):
        self.assertIsNone(self.context([]))
        self.assertIsNone(self.context([comment("Ich bin es nicht", author_id=7)]))


class TestZendeskRead(unittest.TestCase):
    """The fetching half. Every failure costs the dialog its context and never its
    ability to open, so every path returns None rather than raising."""

    def read(self, responses, **env):
        with Env(**env), Patched(relay.triage,
                                 zendesk_session=lambda *a: FakeSession(responses)):
            return relay.zendesk_read(27896, "its comments", "/comments.json",
                                      per_page=100)

    def test_a_readable_response_is_parsed(self):
        self.assertEqual(self.read([FakeResponse({"comments": [1]})]),
                         {"comments": [1]})

    def test_an_unreachable_zendesk_costs_context_not_the_dialog(self):
        # Two responses: request_with_retry(attempts=2) retries a 500, and a queue
        # that runs dry would exercise the exception path instead of this one.
        self.assertIsNone(self.read([FakeResponse({}, status_code=500)] * 2))
        self.assertIsNone(self.read([triage.requests.RequestException("down")] * 2))

    def test_an_unreadable_payload_costs_context_not_the_dialog(self):
        from test_triage import NonJsonResponse
        self.assertIsNone(self.read([NonJsonResponse()]))

    def test_missing_zendesk_config_is_not_an_error(self):
        self.assertIsNone(self.read([], ZENDESK_API_TOKEN=None))


class TestReadTicket(unittest.IsolatedAsyncioTestCase):
    async def gathered(self, responses):
        """read_ticket over a session shared by both calls, so the queue records the
        two requests it makes."""
        session = FakeSession(responses)
        with Env(), Patched(relay.triage, zendesk_session=lambda *a: session):
            return await relay.read_ticket(27896), session

    async def test_the_ticket_and_its_comments_are_both_fetched(self):
        (ticket, comments), session = await self.gathered([
            FakeResponse({"ticket": ticket_row()}),
            FakeResponse({"comments": [comment("hallo")]}),
        ])
        self.assertEqual(ticket["requester_id"], REQUESTER_ID)
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(session.calls), 2)

    async def test_the_comments_are_asked_for_oldest_first(self):
        """The requester's words read as the story they told. Spelled out rather than
        left to whatever the endpoint defaults to."""
        _, session = await self.gathered([
            FakeResponse({"ticket": ticket_row()}),
            FakeResponse({"comments": []}),
        ])
        params = [kwargs["params"] for _, url, kwargs in session.calls
                  if url.endswith("/comments.json")]
        self.assertEqual(params, [{"per_page": 100, "sort_order": "asc"}])

    async def test_one_half_failing_does_not_take_the_other_with_it(self):
        (ticket, comments), _ = await self.gathered([
            FakeResponse({}, status_code=404),
            FakeResponse({"comments": [comment("hallo")]}),
        ])
        self.assertIsNone(ticket)
        self.assertEqual(len(comments), 1)


class TestModal(unittest.TestCase):
    def test_the_dialog_links_the_ticket_and_offers_the_box(self):
        with Env():
            modal = relay.reply_modal("27896", "**What they wrote**\nEs geht nicht.")
        blocks = modal["data"]["components"]
        self.assertEqual(modal["type"], relay.RESPONSE_MODAL)
        self.assertEqual(blocks[0]["type"], relay.TEXT_DISPLAY)
        self.assertIn("acme.zendesk.com/agent/tickets/27896", blocks[0]["content"])
        self.assertIn("Es geht nicht.", blocks[0]["content"])
        self.assertEqual(blocks[1]["type"], relay.LABEL)
        self.assertEqual(blocks[1]["component"]["custom_id"], "body")
        self.assertEqual(blocks[1]["component"]["max_length"], relay.MAX_REPLY_CHARS)

    def test_the_dialog_opens_without_context(self):
        with Env():
            modal = relay.reply_modal("27896")
        self.assertIn("27896", modal["data"]["components"][0]["content"])

    def test_the_title_fits_discords_limit(self):
        with Env():
            self.assertLessEqual(len(relay.reply_modal("27896")["data"]["title"]), 45)


class TestSubmittedValue(unittest.TestCase):
    """By custom_id, never by position: reading components[0] found the dialog's own
    link text and reported every reply as empty."""

    def test_found_through_a_label(self):
        components = [
            {"type": relay.TEXT_DISPLAY, "content": "a link"},
            {"type": relay.LABEL, "component": {"custom_id": "body", "value": "hello"}},
        ]
        self.assertEqual(relay.submitted_value(components, "body"), "hello")

    def test_found_through_a_legacy_action_row(self):
        components = [{"type": 1, "components": [{"custom_id": "body", "value": "hi"}]}]
        self.assertEqual(relay.submitted_value(components, "body"), "hi")

    def test_absent_is_none_rather_than_the_wrong_field(self):
        components = [{"type": relay.TEXT_DISPLAY, "content": "a link"}]
        self.assertIsNone(relay.submitted_value(components, "body"))

    def test_an_empty_box_is_a_value_not_a_miss(self):
        """An empty box submits the empty string. Falling through that would keep
        searching and report the modal's shape as wrong when the shape was fine."""
        components = [
            {"type": relay.LABEL, "component": {"custom_id": "body", "value": ""}},
        ]
        self.assertEqual(relay.submitted_value(components, "body"), "")


# ---- Routing --------------------------------------------------------------


class TestRouting(unittest.TestCase):
    # zendesk_read builds its own session per call, so each of read_ticket's two
    # calls gets a fresh queue of this rather than sharing one. A single response
    # carrying both keys is what lets one entry serve either caller.
    BOTH_READS = (FakeResponse({"ticket": ticket_row(), "comments": []}),)

    def send(self, payload, responses=BOTH_READS):
        with Env(), Patched(relay.triage,
                            zendesk_session=lambda *a: FakeSession(list(responses))), \
             Patched(relay, run_reply=lambda payload: None):
            return post(payload).json()

    def test_the_comment_button_opens_the_dialog(self):
        body = self.send(interaction(3, custom_id="comment:27896"))
        self.assertEqual(body["type"], relay.RESPONSE_MODAL)
        self.assertIn("27896", body["data"]["title"])

    def test_a_closed_ticket_is_refused_before_the_box_opens(self):
        """Zendesk takes no comment on a closed ticket and closing cannot be undone,
        so the reply was refused only after somebody had typed the whole thing."""
        body = self.send(interaction(3, custom_id="comment:27896"),
                         responses=(FakeResponse(
                             {"ticket": ticket_row(status="closed")}),))
        self.assertEqual(body["type"], relay.RESPONSE_MESSAGE)
        self.assertIn("closed", body["data"]["content"])

    def test_an_unreadable_ticket_still_opens_the_box(self):
        """The closed check must cost the dialog its context, not its existence: an
        unreachable Zendesk is not a closed ticket, and reply.py checks again anyway."""
        body = self.send(interaction(3, custom_id="comment:27896"),
                         responses=(FakeResponse({}, status_code=500),) * 2)
        self.assertEqual(body["type"], relay.RESPONSE_MODAL)

    def test_the_comment_button_falls_back_to_the_card_when_zendesk_is_down(self):
        card = [{"type": 17, "components": [{
            "type": 9,
            "components": [{"type": relay.TEXT_DISPLAY, "content": "🟠 | #27896 · summary"}],
            "accessory": {"custom_id": "comment:27896"}}]}]
        body = self.send(interaction(3, custom_id="comment:27896", message=card),
                         responses=(FakeResponse({}, status_code=500),) * 2)
        self.assertIn("summary", body["data"]["components"][0]["content"])

    def test_send_strips_the_buttons_in_the_same_response(self):
        """A second click must have nothing left to press."""
        body = self.send(interaction(3, custom_id="send:27896", embeds=[{"x": 1}]))
        self.assertEqual(body["type"], relay.RESPONSE_UPDATE_MESSAGE)
        self.assertEqual(body["data"]["components"], [])
        self.assertIn("27896", body["data"]["content"])

    def test_cancel_writes_nothing(self):
        body = self.send(interaction(3, custom_id="cancel"))
        self.assertEqual(body["data"]["components"], [])
        self.assertIn("Cancelled", body["data"]["content"])

    def test_an_unknown_button_is_refused(self):
        body = self.send(interaction(3, custom_id="explode:1"))
        self.assertIn("Unknown button", body["data"]["content"])

    def test_a_modal_submit_defers_ephemerally(self):
        body = self.send(interaction(
            5, custom_id="reply:27896",
            components=[{"type": relay.LABEL,
                         "component": {"custom_id": "body", "value": "We fixed it."}}]))
        self.assertEqual(body["type"], relay.RESPONSE_DEFERRED)
        self.assertEqual(body["data"]["flags"], relay.EPHEMERAL)

    def test_an_empty_submit_is_refused(self):
        body = self.send(interaction(
            5, custom_id="reply:27896",
            components=[{"type": relay.LABEL,
                         "component": {"custom_id": "body", "value": "   "}}]))
        self.assertIn("empty", body["data"]["content"])

    def test_a_non_numeric_ticket_never_reaches_a_subprocess(self):
        for bad in ("../../etc/passwd", "12/34", "27896abc", "0"):
            with self.subTest(bad):
                body = self.send(interaction(3, custom_id=f"comment:{bad}"))
                self.assertIn("not a ticket number", body["data"]["content"])


class TestNoLeakToTheChannel(unittest.TestCase):
    """The digest is channel-visible and attachment URLs are bearer capabilities, so
    the only place either may appear is a modal or an ephemeral message."""

    def test_the_send_response_carries_no_ticket_content(self):
        with Env(), Patched(relay, run_reply=lambda payload: None):
            body = post(interaction(3, custom_id="send:27896",
                                    embeds=[{"description": "secret"}])).json()
        rendered = json.dumps(body)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("attachments/token", rendered)


# ---- Handing off to reply.py ---------------------------------------------


class TestRunReply(unittest.TestCase):
    def captured(self, **env):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with Env(**env), Patched(relay.subprocess, run=fake_run):
            relay.run_reply({"action": "draft", "ticket_id": 27896})
        return calls[0]

    def test_the_payload_travels_by_environment_not_argv(self):
        """It carries the reply text; a process list is readable by other users."""
        command, kwargs = self.captured()
        self.assertTrue(command[1].endswith("reply.py"))
        self.assertNotIn("27896", " ".join(command))
        self.assertEqual(json.loads(kwargs["env"]["DISCORD_PAYLOAD"])["ticket_id"],
                         27896)

    def test_dry_run_is_opt_in(self):
        self.assertNotIn("--dry-run", self.captured()[0])
        self.assertIn("--dry-run", self.captured(RELAY_DRY_RUN="1")[0])

    def test_a_dry_run_switch_fails_towards_writing_nothing(self):
        """systemd keeps an inline # as part of the value, so an equality check read
        `1  # …` as off — the opposite of what whoever wrote it meant."""
        for value in ("1", "1  # run the whole path, write nothing", " true ", "yes"):
            with self.subTest(value):
                self.assertIn("--dry-run", self.captured(RELAY_DRY_RUN=value)[0])
        for value in ("", "0", "false", "off"):
            with self.subTest(value):
                self.assertNotIn("--dry-run", self.captured(RELAY_DRY_RUN=value)[0])

    def test_a_timeout_is_survived(self):
        def explode(command, **kwargs):
            raise relay.subprocess.TimeoutExpired(command, 1)

        with Env(), Patched(relay.subprocess, run=explode):
            relay.run_reply({"action": "draft", "ticket_id": 1})  # must not raise


class TestFailuresReachTheAgent(unittest.TestCase):
    """reply.py reports its own refusals and then exits. A crash, a non-zero exit and
    the timeout kill are the ones it cannot report, and each used to leave the agent's
    message on "⏳ Sending…" — which reads as *still working* for a write that may
    already have emailed the customer."""

    def report(self, outcome, payload=None):
        """Run reply.py with `outcome` and return what Discord was asked to show."""
        patched = {}
        if isinstance(outcome, BaseException):
            def run(command, **kwargs):
                raise outcome
            patched["run"] = run
        else:
            patched["run"] = lambda command, **kwargs: outcome
        session = FakeSession([FakeResponse({})])
        with Env(), Patched(relay.subprocess, **patched), \
                Patched(relay, requests=type("M", (), {
                    "Session": staticmethod(lambda: session)})):
            relay.run_reply(payload if payload is not None else {
                "action": "send", "ticket_id": 27896,
                "application_id": "1300000000000000002",
                "interaction_token": "interaction-token"})
        return session.calls

    def done(self, returncode, stderr=""):
        return type("Done", (), {"returncode": returncode, "stdout": "",
                                 "stderr": stderr})()

    def test_a_timeout_tells_the_agent_to_check_the_ticket(self):
        calls = self.report(relay.subprocess.TimeoutExpired("reply.py", 1))
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, "PATCH")
        self.assertIn("interaction-token", url)
        self.assertIn("@original", url)
        self.assertIn("Zendesk", kwargs["json"]["content"])

    def test_a_failed_run_tells_the_agent_too(self):
        calls = self.report(self.done(1, stderr="Traceback…"))
        self.assertEqual(len(calls), 1)
        self.assertIn("❌", calls[0][2]["json"]["content"])

    def test_the_outcome_says_the_reply_may_have_gone_out(self):
        """Not "nothing was sent". A run killed after the private note may have got
        as far as the reply itself, and only the ticket knows which."""
        for outcome in (relay.subprocess.TimeoutExpired("reply.py", 1), self.done(1)):
            with self.subTest(outcome=type(outcome).__name__):
                content = self.report(outcome)[0][2]["json"]["content"]
                self.assertIn("may have gone out", content)

    def test_the_live_send_button_goes_with_it(self):
        """Leaving the components behind would keep a Send button under a message
        saying the reply failed, and pressing it would write a second time."""
        kwargs = self.report(self.done(1))[0][2]
        self.assertEqual(kwargs["json"]["components"], [])
        self.assertEqual(kwargs["json"]["embeds"], [])

    def test_a_successful_run_reports_nothing_of_its_own(self):
        """reply.py has already told the agent what happened; a second edit would
        overwrite its outcome with a worse one."""
        self.assertEqual(self.report(self.done(0)), [])

    def test_reporting_a_failure_cannot_add_one(self):
        """run_reply promises never to raise, and this is the one part of it that
        indexes rather than gets: a payload without a token must still be survivable."""
        self.assertEqual(
            self.report(self.done(1), payload={"action": "send", "ticket_id": 1}), [])


if __name__ == "__main__":
    unittest.main()
