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
from test_triage import FakeResponse, FakeSession  # noqa: E402


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


class TestTicketContext(unittest.TestCase):
    def context(self, responses):
        with Env(), Patched(relay.triage,
                            zendesk_session=lambda *a: FakeSession(responses)):
            return relay.ticket_context(27896)

    def test_the_requesters_words_are_shown(self):
        got = self.context([FakeResponse({"comments": [
            comment("Es geht nicht mehr."),
            comment("Thanks for reaching out!", author_id=7),
            comment("Immer noch kaputt."),
        ]})])
        self.assertIn("Es geht nicht mehr.", got)
        self.assertIn("Immer noch kaputt.", got)

    def test_an_agents_reply_is_not_mistaken_for_the_requesters(self):
        """The first comment identifies the requester, so a later agent reply in
        English must not join the sample."""
        got = self.context([FakeResponse({"comments": [
            comment("Es geht nicht mehr."),
            comment("Thanks for reaching out!", author_id=7),
        ]})])
        self.assertNotIn("Thanks for reaching out", got)

    def test_attachments_are_linked_with_name_and_size(self):
        got = self.context([FakeResponse({"comments": [
            comment("Siehe Anhang.", attachments=[attachment()]),
        ]})])
        self.assertIn("report.pdf", got)
        self.assertIn("https://acme.zendesk.com/attachments/token/abc/", got)
        self.assertIn("2 KB", got)

    def test_a_flood_of_attachments_is_capped_and_announced(self):
        many = [attachment(name=f"f{i}.pdf") for i in range(relay.MAX_ATTACHMENTS + 4)]
        got = self.context([FakeResponse({"comments": [comment("x", attachments=many)]})])
        self.assertEqual(got.count("📎"), relay.MAX_ATTACHMENTS)
        self.assertIn("4 more", got)

    def test_a_long_body_is_clipped(self):
        got = self.context([FakeResponse({"comments": [comment("ä" * 9000)]})])
        self.assertLess(len(got), relay.BODY_CHARS + 200)

    def test_an_unreachable_zendesk_costs_context_not_the_dialog(self):
        # Two responses: request_with_retry(attempts=2) retries a 500, and a queue
        # that runs dry would exercise the exception path instead of this one.
        self.assertIsNone(self.context([FakeResponse({}, status_code=500)] * 2))
        self.assertIsNone(self.context([triage.requests.RequestException("down")] * 2))

    def test_an_unreadable_payload_costs_context_not_the_dialog(self):
        from test_triage import NonJsonResponse
        self.assertIsNone(self.context([NonJsonResponse()]))

    def test_missing_zendesk_config_is_not_an_error(self):
        with Env(ZENDESK_API_TOKEN=None):
            self.assertIsNone(relay.ticket_context(27896))


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


# ---- Routing --------------------------------------------------------------


class TestRouting(unittest.TestCase):
    def send(self, payload, responses=(FakeResponse({"comments": []}),)):
        with Env(), Patched(relay.triage,
                            zendesk_session=lambda *a: FakeSession(list(responses))), \
             Patched(relay, run_reply=lambda payload: None):
            return post(payload).json()

    def test_the_comment_button_opens_the_dialog(self):
        body = self.send(interaction(3, custom_id="comment:27896"))
        self.assertEqual(body["type"], relay.RESPONSE_MODAL)
        self.assertIn("27896", body["data"]["title"])

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


if __name__ == "__main__":
    unittest.main()
