#!/usr/bin/env python3
"""Tests for replying to a Zendesk ticket from Discord.

Stdlib unittest, same as the other two. Everything is offline — Zendesk runs against
a stub session, and Claude and Discord are patched out. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

This script writes public comments that email a customer, so the tests lean on the
guards rather than the happy path: that a draft survives the round trip through
Discord byte for byte, that an English reply reaches the customer as typed and not as
the model echoed it, that a re-run cannot write twice, and that a dry run cannot PUT.
"""
import ast
import contextlib
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reply  # noqa: E402
import triage  # noqa: E402
from test_triage import FakeResponse, FakeSession, Patched  # noqa: E402


GERMAN = {
    "language": "German",
    "language_code": "de",
    "is_english": False,
    "translated": "Wir haben das in 1.2.3 behoben.",
    "back_translation": "We have fixed that in 1.2.3.",
}
ENGLISH = {
    "language": "English",
    "language_code": "en",
    "is_english": True,
    "translated": "We fixed this in 1.2.3.",
    "back_translation": "We fixed this in 1.2.3.",
}
REPLY_EN = "We fixed this in 1.2.3."


def payload(action="draft", **extra):
    row = {
        "action": action,
        "ticket_id": 27603,
        "user": "Audric (@audric)",
        "interaction_id": "1400000000000000001",
        "application_id": "1300000000000000002",
        "interaction_token": "tok",
    }
    if action == "draft":
        row["body_en"] = REPLY_EN
    row.update(extra)
    return row


def ticket(status="open", requester_id=42, description="Es geht nicht mehr."):
    return {"id": 27603, "status": status, "requester_id": requester_id,
            "subject": "Problem", "description": description}


def comment(body, author_id=42):
    return {"id": 1, "author_id": author_id, "public": True, "body": body}


class Recorder:
    """Stands in for reply.respond, keeping what would reach Discord."""

    def __init__(self):
        self.messages = []

    def __call__(self, _payload, data):
        self.messages.append(data)

    @property
    def last(self):
        return self.messages[-1]

    def text(self):
        return " ".join(message.get("content", "") for message in self.messages)


def embed_chars(payload):
    """Everything Discord counts towards the 6,000: descriptions, titles, footers."""
    total = 0
    for block in payload["embeds"]:
        total += len(block.get("description") or "")
        total += len(block.get("title") or "")
        total += len((block.get("footer") or {}).get("text") or "")
    return total


def writes(session):
    """The PUTs a run made, as (url, ticket-fields) pairs."""
    return [(url, kwargs["json"]["ticket"])
            for method, url, kwargs in session.calls if method == "PUT"]


# ---- The draft, carried in the preview message ------------------------------


class TestPreviewRoundTrip(unittest.TestCase):
    """The preview's embeds are the draft's only storage between the modal and the
    Send button, so what parse_preview reads back has to be exactly what
    build_preview put in — anything else means sending a customer text nobody saw.
    """

    def test_the_text_survives_the_round_trip_byte_for_byte(self):
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        translated, body_en = reply.parse_preview(preview["embeds"], 27603)
        self.assertEqual(translated, GERMAN["translated"])
        self.assertEqual(body_en, REPLY_EN)

    def test_text_that_an_embed_would_truncate_is_refused_not_clipped(self):
        long_reply = "x" * (reply.MAX_EMBED_CHARS + 1)
        self.assertIsNotNone(reply.round_trip_error(
            dict(GERMAN, translated=long_reply), REPLY_EN, 27603))
        self.assertIsNotNone(reply.round_trip_error(GERMAN, long_reply, 27603))
        self.assertIsNone(reply.round_trip_error(GERMAN, REPLY_EN, 27603))

    def test_a_long_back_translation_is_clipped_rather_than_refused(self):
        """It never leaves Discord, so it is the part that can give ground."""
        result = dict(GERMAN, back_translation="y" * 9000)
        self.assertIsNone(reply.round_trip_error(result, REPLY_EN, 27603))
        preview = reply.build_preview("acme", 27603, result, REPLY_EN)
        rendered = preview["embeds"][reply.EMBED_BACK_TRANSLATION]["description"]
        self.assertLessEqual(len(rendered), reply.MAX_EMBED_CHARS)
        # And the two texts that must survive verbatim still did.
        self.assertEqual(reply.parse_preview(preview["embeds"], 27603),
                         (result["translated"], REPLY_EN))

    def test_a_draft_leaving_no_room_to_check_it_is_refused(self):
        half = "z" * (reply.MAX_EMBEDS_TOTAL_CHARS // 2)
        self.assertIsNotNone(reply.round_trip_error(
            dict(GERMAN, translated=half), half, 27603))

    def test_the_longest_label_and_ticket_id_still_fit_the_budget(self):
        """The chrome is measured rather than assumed, so the aggregate has to hold
        at the extremes the code accepts: a language label longer than its own cap,
        and a ticket id far longer than Zendesk will ever issue."""
        result = dict(GERMAN,
                      language="L" * (reply.LANGUAGE_LABEL_CHARS * 2),
                      translated="t" * 2000,
                      back_translation="y" * 9000)
        body_en, ticket_id = "x" * 1200, int("9" * 20)
        self.assertIsNone(reply.round_trip_error(result, body_en, ticket_id))
        preview = reply.build_preview("acme", ticket_id, result, body_en)
        self.assertLessEqual(embed_chars(preview), reply.MAX_EMBEDS_TOTAL_CHARS)

    def test_a_longer_title_comes_out_of_the_budget_automatically(self):
        """The whole point of measuring: editing a title must not silently eat into
        what the aggregate assumed was free.

        The texts are long enough that the 6,000 aggregate binds rather than the
        per-embed cap — with a short draft the min() absorbs the difference and the
        title's cost is invisible either way.
        """
        result = dict(GERMAN, translated="t" * 1500, back_translation="y" * 9000)
        body_en = "x" * 1500
        before = reply.back_translation_budget(result, body_en, 27603)
        self.assertLess(before, reply.MAX_EMBED_CHARS)  # the aggregate is binding
        with Patched(reply, TITLE_ORIGINAL=reply.TITLE_ORIGINAL + " this reply"):
            after = reply.back_translation_budget(result, body_en, 27603)
        self.assertEqual(before - after, len(" this reply"))

    def test_the_embeds_stay_within_discords_total_budget(self):
        """Discord counts every embed's title and footer towards the 6,000, not just
        the descriptions — so the assertion has to count them too."""
        result = dict(GERMAN, back_translation="y" * 9000)
        preview = reply.build_preview("acme", 27603, result, "x" * 1200)
        self.assertLessEqual(embed_chars(preview), reply.MAX_EMBEDS_TOTAL_CHARS)


class TestPreviewGuards(unittest.TestCase):
    def test_a_preview_for_another_ticket_is_refused(self):
        """The footer is what stops a Send pressed on some other message from
        posting this text to the wrong ticket."""
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        with self.assertRaises(SystemExit):
            reply.parse_preview(preview["embeds"], 27604)

    def test_a_message_that_is_not_a_preview_is_refused(self):
        for embeds in ([], None, [{}, {}], "nope"):
            with self.assertRaises(SystemExit):
                reply.parse_preview(embeds, 27603)

    def test_an_empty_translation_is_never_sent(self):
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        preview["embeds"][reply.EMBED_TRANSLATED]["description"] = ""
        with self.assertRaises(SystemExit):
            reply.parse_preview(preview["embeds"], 27603)

    def test_the_preview_offers_send_and_cancel(self):
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        buttons = preview["components"][0]["components"]
        self.assertEqual([b["custom_id"] for b in buttons], ["send:27603", "cancel"])

    def test_an_outcome_clears_the_buttons(self):
        """Otherwise the preview keeps a live Send under a message saying it was
        already sent."""
        message = reply.outcome_message("acme", 27603, "✅ Sent")
        self.assertEqual(message["components"], [])
        self.assertEqual(message["embeds"], [])
        self.assertIn("27603", message["content"])


# ---- The private note ------------------------------------------------------


class TestNote(unittest.TestCase):
    def test_a_translated_reply_records_the_english_original(self):
        note = reply.build_note("Audric (@audric)", "99", REPLY_EN,
                                GERMAN["translated"])
        self.assertIn("Audric (@audric)", note)
        self.assertIn(REPLY_EN, note)
        self.assertIn("[discord:99]", note)

    def test_an_english_reply_does_not_repeat_itself(self):
        """The public comment already is the English original; a note echoing it one
        line below is noise in a ticket someone reads later."""
        note = reply.build_note("Audric (@audric)", "99", REPLY_EN, REPLY_EN)
        self.assertNotIn("Original (English)", note)
        self.assertEqual(note.count(REPLY_EN), 0)

    def test_attribution_and_marker_are_on_both_paths(self):
        for translated in (REPLY_EN, GERMAN["translated"]):
            note = reply.build_note("Audric (@audric)", "99", REPLY_EN, translated)
            self.assertIn("Audric (@audric)", note)
            self.assertIn(reply.sent_marker("99"), note)


# ---- Zendesk writes --------------------------------------------------------


class TestComments(unittest.TestCase):
    def test_a_reply_is_public_and_moves_the_ticket_to_the_customer(self):
        session = FakeSession([FakeResponse({"ticket": {}})])
        reply.post_comment(session, "acme", 27603, "hallo", public=True,
                           status=reply.REPLIED_STATUS)
        (url, fields), = writes(session)
        self.assertIn("/api/v2/tickets/27603.json", url)
        self.assertEqual(fields["comment"], {"body": "hallo", "public": True})
        self.assertEqual(fields["status"], "pending")

    def test_a_note_is_private_and_does_not_touch_the_status(self):
        session = FakeSession([FakeResponse({"ticket": {}})])
        reply.post_comment(session, "acme", 27603, "note", public=False)
        (_, fields), = writes(session)
        self.assertFalse(fields["comment"]["public"])
        self.assertNotIn("status", fields)

    def test_a_rejected_write_stops_the_run(self):
        session = FakeSession([FakeResponse({"error": "nope"}, status_code=422)])
        with self.assertRaises(SystemExit) as caught:
            reply.post_comment(session, "acme", 27603, "hallo", public=True)
        # A 422 echoes the submitted comment back, and this repo's logs are public.
        self.assertNotIn("nope", str(caught.exception))


class TestLanguageSample(unittest.TestCase):
    def test_only_the_requesters_own_words_are_sampled(self):
        """An agent's earlier English reply is still text on the ticket, and would
        drag detection towards English on exactly the tickets this exists for."""
        sample = reply.customer_text(
            ticket(),
            [comment("Thanks for reaching out!", author_id=7),
             comment("Immer noch kaputt.", author_id=42)])
        self.assertIn("Es geht nicht mehr.", sample)
        self.assertIn("Immer noch kaputt.", sample)
        self.assertNotIn("Thanks for reaching out", sample)

    def test_a_ticket_with_no_text_still_yields_a_sample(self):
        sample = reply.customer_text(ticket(description=""), [])
        self.assertTrue(sample.strip())

    def test_the_sample_is_bounded(self):
        sample = reply.customer_text(ticket(description="ä" * 9000), [])
        self.assertLessEqual(len(sample), reply.CUSTOMER_SAMPLE_CHARS)


# ---- Actions ---------------------------------------------------------------


class TestEnglishPath(unittest.TestCase):
    def test_an_english_ticket_is_answered_without_a_confirmation_step(self):
        session = FakeSession([
            FakeResponse({"ticket": ticket(description="It broke.")}),
            FakeResponse({"comments": []}),
            FakeResponse({"ticket": {}}),
            FakeResponse({"ticket": {}}),
        ])
        respond = Recorder()
        with Patched(reply, respond=respond,
                     translate=lambda *args: ENGLISH):
            reply.run_draft(session, "acme", "model", payload(), dry_run=False)
        note, public = writes(session)
        self.assertFalse(note[1]["comment"]["public"])
        self.assertTrue(public[1]["comment"]["public"])
        self.assertIn("Sent", respond.text())

    def test_what_goes_out_is_what_the_agent_typed_not_the_models_echo(self):
        """An English reply must reach the customer exactly as written — the model
        is asked to repeat it back, and that echo is never what gets posted."""
        session = FakeSession([
            FakeResponse({"ticket": ticket(description="It broke.")}),
            FakeResponse({"comments": []}),
            FakeResponse({"ticket": {}}),
            FakeResponse({"ticket": {}}),
        ])
        rewritten = dict(ENGLISH, translated="We have resolved your issue. Thanks!")
        with Patched(reply, respond=Recorder(),
                     translate=lambda *args: rewritten):
            reply.run_draft(session, "acme", "model", payload(), dry_run=False)
        _, public = writes(session)
        self.assertEqual(public[1]["comment"]["body"], REPLY_EN)


class TestTranslatedPath(unittest.TestCase):
    def test_a_translated_reply_is_previewed_and_nothing_is_written(self):
        session = FakeSession([
            FakeResponse({"ticket": ticket()}),
            FakeResponse({"comments": [comment("Es geht nicht mehr.")]}),
        ])
        respond = Recorder()
        with Patched(reply, respond=respond,
                     translate=lambda *args: GERMAN):
            reply.run_draft(session, "acme", "model", payload(), dry_run=False)
        self.assertEqual(writes(session), [])
        self.assertEqual([b["custom_id"]
                          for b in respond.last["components"][0]["components"]],
                         ["send:27603", "cancel"])

    def test_send_writes_the_text_the_preview_showed(self):
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        session = FakeSession([
            FakeResponse({"ticket": ticket()}),
            FakeResponse({"comments": []}),
            FakeResponse({"ticket": {}}),
            FakeResponse({"ticket": {}}),
        ])
        with Patched(reply, respond=Recorder()):
            reply.run_send(session, "acme",
                           payload("send", embeds=preview["embeds"]), dry_run=False)
        note, public = writes(session)
        self.assertEqual(public[1]["comment"]["body"], GERMAN["translated"])
        self.assertIn(REPLY_EN, note[1]["comment"]["body"])

    def test_the_marker_is_written_before_the_customer_is_emailed(self):
        """Public-first would leave a reply with no marker if the note then failed,
        and a re-run would email the customer twice. The note is private, so writing
        it first cannot reach anybody on its own."""
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        sent = payload("send")
        session = FakeSession([
            FakeResponse({"ticket": ticket()}),
            FakeResponse({"comments": []}),
            FakeResponse({"ticket": {}}),
            FakeResponse({"ticket": {}}),
        ])
        with Patched(reply, respond=Recorder()):
            reply.run_send(session, "acme", dict(sent, embeds=preview["embeds"]),
                           dry_run=False)
        first, second = writes(session)
        self.assertFalse(first[1]["comment"]["public"])
        self.assertIn(reply.sent_marker(sent["interaction_id"]),
                      first[1]["comment"]["body"])
        self.assertTrue(second[1]["comment"]["public"])


class TestGuards(unittest.TestCase):
    def test_a_closed_ticket_is_refused_before_any_write(self):
        session = FakeSession([FakeResponse({"ticket": ticket(status="closed")})])
        respond = Recorder()
        with Patched(reply, respond=respond,
                     translate=lambda *args: GERMAN):
            reply.run_draft(session, "acme", "model", payload(), dry_run=False)
        self.assertEqual(writes(session), [])
        self.assertIn("Closed", respond.text())

    def test_a_ticket_closed_while_the_preview_waited_is_refused_too(self):
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        session = FakeSession([FakeResponse({"ticket": ticket(status="closed")})])
        with Patched(reply, respond=Recorder()):
            reply.run_send(session, "acme",
                           payload("send", embeds=preview["embeds"]), dry_run=False)
        self.assertEqual(writes(session), [])

    def test_a_rerun_of_an_interaction_that_already_wrote_writes_nothing(self):
        """Discord's own guard is the stripped buttons; this is the one it cannot
        cover — a workflow re-run of a dispatch that already landed."""
        sent = payload("send")
        already = comment(f"Reply sent from Discord by x.\n"
                          f"{reply.sent_marker(sent['interaction_id'])}")
        preview = reply.build_preview("acme", 27603, GERMAN, REPLY_EN)
        session = FakeSession([
            FakeResponse({"ticket": ticket()}),
            FakeResponse({"comments": [already]}),
        ])
        respond = Recorder()
        with Patched(reply, respond=respond):
            reply.run_send(session, "acme", dict(sent, embeds=preview["embeds"]),
                           dry_run=False)
        self.assertEqual(writes(session), [])
        self.assertIn("Already sent", respond.text())

    def test_a_dry_run_cannot_write(self):
        session = FakeSession([
            FakeResponse({"ticket": ticket(description="It broke.")}),
            FakeResponse({"comments": []}),
        ])
        respond = Recorder()
        with Patched(reply, respond=respond,
                     translate=lambda *args: ENGLISH):
            reply.run_draft(session, "acme", "model", payload(), dry_run=True)
        self.assertEqual(writes(session), [])
        self.assertIn("Dry run", respond.text())

    def test_an_expired_interaction_fails_the_run(self):
        """The ticket is already correct, but an agent who never saw an outcome will
        assume the reply did not go out."""
        with Patched(reply, requests=type("M", (), {"Session": staticmethod(
                lambda: FakeSession([FakeResponse({}, status_code=404)]))})):
            with self.assertRaises(SystemExit):
                reply.respond(payload(), {"content": "hi"})


class TestPayload(unittest.TestCase):
    def test_a_well_formed_payload_parses(self):
        parsed = reply.load_payload(triage.json.dumps(payload()))
        self.assertEqual(parsed["ticket_id"], 27603)

    def test_what_is_rejected(self):
        cases = {
            "not json": "{",
            "not an object": "[]",
            "missing keys": '{"action": "draft"}',
            "unknown action": triage.json.dumps(payload(action="delete")),
            "unparseable ticket": triage.json.dumps(payload(ticket_id="abc")),
            "empty draft": triage.json.dumps(payload(body_en="   ")),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                with self.assertRaises(SystemExit):
                    reply.load_payload(raw)

    def test_a_ticket_number_arriving_as_a_string_is_accepted(self):
        """The relay strips it to digits and hands it over as a string."""
        parsed = reply.load_payload(triage.json.dumps(payload(ticket_id="27603")))
        self.assertEqual(parsed["ticket_id"], 27603)


# ---- Wiring ----------------------------------------------------------------


class TestRelayWiring(unittest.TestCase):
    """The digest, the relay and the deployment agree on things nothing checks at
    runtime. Each of these was a real break once, or would be a silent one.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_relay_answers_the_button_the_digest_renders(self):
        """triage.py hangs a Comment button off every ticket card and relay.py opens
        the dialog for it. The custom_id prefix is the whole contract between them,
        and getting it wrong is a button that reports "Unknown button" to whoever
        presses it — visible only in production, in front of the team."""
        rendered = triage.build_ticket_section("line", 27603)["accessory"]["custom_id"]
        self.assertTrue(rendered.startswith("comment:"))
        self.assertIn('startswith("comment:")',
                      self.read("zendesk_triage", "relay.py"))

    def test_every_setting_the_relay_reads_is_written_down(self):
        """The relay's configuration lives in an EnvironmentFile that nothing
        enumerates. A setting the code reads and no document mentions is one nobody
        knows to set — and for the allowlists that means silently refusing everybody.
        """
        source = self.read("zendesk_triage", "relay.py")
        # The module docstring and the deployment guide, never the source itself:
        # including the source made every name it reads match by construction, which
        # is how this test came to pass while documenting nothing.
        documented = (self.read("deploy", "README.md")
                      + (ast.get_docstring(ast.parse(source)) or ""))
        for name in sorted(set(re.findall(r'env\("([A-Z][A-Z0-9_]+)"', source))):
            with self.subTest(name):
                self.assertIn(name, documented)

    def test_both_scheduled_units_report_their_own_failures(self):
        """notify_failure.yml watched workflows by name, so a rename unsubscribed the
        job silently. OnFailure= is set by the failing unit itself and cannot drift —
        but only if it is actually there."""
        for unit in ("zendesk-relay.service", "zendesk-digest.service"):
            with self.subTest(unit):
                self.assertIn("OnFailure=zendesk-alert@",
                              self.read("deploy", unit))

    def test_the_digest_cannot_be_posted_by_a_webhook(self):
        """A plain incoming webhook silently drops components, so a digest sent that
        way would arrive with no buttons at all."""
        self.assertIn("DISCORD_BOT_TOKEN", self.read("deploy", "README.md"))


class TestLocalMode(unittest.TestCase):
    def test_local_prints_instead_of_reaching_discord(self):
        """A hand-written payload has no real interaction token, so the PATCH would
        404 and take the run down with it."""
        # Empty queue: any request at all would raise rather than pass quietly.
        session = FakeSession([])
        printed = io.StringIO()
        with Patched(reply, requests=type("M", (), {
                "Session": staticmethod(lambda: session)})):
            with contextlib.redirect_stdout(printed):
                reply.respond(dict(payload(), local=True), {"content": "hallo"})
        self.assertIn("hallo", printed.getvalue())
        self.assertEqual(session.calls, [])

    def main(self, argv):
        """Why reply.main() gave up on this command line.

        Both channels: argparse writes its complaint to stderr and exits 2, while
        get_env passes its message as the exit code itself.
        """
        with Patched(reply.sys, argv=["reply.py"] + argv), \
                contextlib.redirect_stderr(io.StringIO()) as complaint:
            with self.assertRaises(SystemExit) as caught:
                reply.main()
        return f"{complaint.getvalue()}\n{caught.exception}"

    def test_local_alone_refuses_rather_than_writing(self):
        """--local only redirects what goes back to Discord. On its own it still posts
        a public comment, which is the opposite of what a hand-written payload is for
        — and the mistake costs a real customer a real email."""
        self.assertIn("--dry-run", self.main(["--local"]))

    def test_local_with_dry_run_gets_past_the_gate(self):
        """It must fail on the missing payload, not on the flags."""
        self.assertIn("DISCORD_PAYLOAD", self.main(["--local", "--dry-run"]))


if __name__ == "__main__":
    unittest.main()
