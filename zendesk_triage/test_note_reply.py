#!/usr/bin/env python3
"""Tests for driving a Zendesk reply from private notes on the ticket.

Stdlib unittest, offline, same stubs as the rest. Run from anywhere:

    python -m unittest discover -s zendesk_triage -v

This publishes comments that email a customer, so the tests are about the guards
rather than the happy path. Four of them exist because getting them wrong is how this
sends the wrong thing to a real person:

  * a draft note must not read as a command, or Claude drafts against itself forever
  * `reply` must send the newest draft, not the oldest — the comment feed is newest
    first and reversing it is an easy and silent mistake
  * a replayed webhook must not send twice
  * `reply` must send what was reviewed, byte for byte
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import note_reply  # noqa: E402
import triage  # noqa: E402
from test_triage import FakeResponse, FakeSession, Patched  # noqa: E402

API_USER = 901790886886
AGENT = 555
def option(text="Anhänge werden 14 Tage lang gespeichert.", approach="explain and close"):
    return {"approach": approach, "reply_en": "Attachments are kept for 14 days.",
            "translated": text, "back_translation": "Attachments are stored for 14 days."}


GERMAN = {"language": "German", "language_code": "de", "is_english": False,
          "options": [option()]}
GERMAN_THREE = {"language": "German", "language_code": "de", "is_english": False,
                "options": [option("Erste Antwort", "explain and close"),
                            option("Zweite Antwort", "ask which device was online"),
                            option("Dritte Antwort", "answer and keep it open")]}


def comment(body, author=AGENT, public=False, cid=1):
    return {"id": cid, "author_id": author, "public": public,
            "body": body, "plain_body": body}


def as_zendesk_plain(markup):
    """`plain_body` as Zendesk actually returns it for a comment posted as html_body.

    Measured against the live API rather than assumed: block tags become line breaks,
    every other tag is dropped, whitespace inside <pre> survives exactly — and
    entities are NOT unescaped, which is why note_reply.comment_text unescapes. A
    fixture that skipped this passed on raw markup and would have shipped a
    find_draft that returned "</p><pre>…" to a customer.
    """
    text = re.sub(r"</(p|pre)>", "\n", markup)
    return re.sub(r"<[^>]+>", "", text)


def posted_note(markup, author=API_USER, cid=9):
    """A note this tool wrote, as it reads back off the ticket."""
    return {"id": cid, "author_id": author, "public": False,
            "body": markup, "plain_body": as_zendesk_plain(markup)}


def draft_note(*texts, cid=9, marker_for=1):
    """A draft note as it reads back, carrying one numbered option per text."""
    blocks = [note_reply.para("Claude drafted a reply.")]
    for number, text in enumerate(texts, 1):
        blocks += [note_reply.para(note_reply.begin_marker(number)),
                   note_reply.verbatim(text),
                   note_reply.para(note_reply.end_marker(number))]
    blocks.append(note_reply.para(f"{note_reply.done_marker(marker_for)} "
                                  f"{note_reply.draft_marker(marker_for)}"))
    return posted_note("".join(blocks), cid=cid)


class ParseCommand(unittest.TestCase):
    def test_reads_action_and_brief(self):
        self.assertEqual(note_reply.parse_command("claude: draft - keeps 14 days"),
                         ("draft", "keeps 14 days"))

    def test_tolerates_spacing_case_and_dashes(self):
        for text in ("claude:draft x", "Claude : DRAFT — x", "  claude: draft: x",
                     "*claude: draft* x".replace("*", "", 1)):
            with self.subTest(text=text):
                self.assertEqual(note_reply.parse_command(text)[0], "draft")

    def test_brief_runs_to_the_end_of_the_note(self):
        action, brief = note_reply.parse_command("claude: draft - one\ntwo\nthree")
        self.assertEqual((action, brief), ("draft", "one\ntwo\nthree"))

    def test_reply_takes_no_brief(self):
        self.assertEqual(note_reply.parse_command("claude: reply"), ("reply", ""))

    def test_ignores_a_note_that_is_not_a_command(self):
        self.assertIsNone(note_reply.parse_command("we should tell them 14 days"))

    def test_only_matches_at_the_start_of_a_line(self):
        """The loop guard. Claude's own draft note names both commands in its
        instructions; if those parsed, every draft would command another draft."""
        self.assertIsNone(note_reply.parse_command(
            "To send it exactly as above, add a private note — claude: reply"))

    def test_a_generated_draft_note_is_not_a_command(self):
        note = note_reply.build_draft_note(GERMAN, "keeps 14 days", 42)
        self.assertIsNone(note_reply.parse_command(
            note_reply.comment_text(posted_note(note))))


class EnglishTranscript(unittest.TestCase):
    def test_english_is_a_command(self):
        self.assertEqual(note_reply.parse_command("claude: english"), ("english", ""))

    def test_the_marker_tracks_the_newest_public_comment(self):
        """Keyed on the conversation, not the command: asking twice with nothing said
        in between must cost nothing, and asking after a reply must re-render."""
        self.assertNotEqual(note_reply.english_marker(1), note_reply.english_marker(2))

    def test_an_up_to_date_transcript_is_not_re_rendered(self):
        """The expensive half is the model call. A repeat ask with no new comment
        must not reach it."""
        called = []
        prior = comment(f"transcript\n\n{note_reply.english_marker(7)}",
                        author=API_USER, cid=8)
        comments = [comment("claude: english", cid=9),
                    dict(comment("hallo", author=42, cid=7), public=True), prior]
        with Patched(triage, conversation_turns=lambda *a: called.append(a)):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_english(session, "sub", "model", {"id": 7}, comments,
                                   {"id": 9, "author": AGENT, "action": "english",
                                    "brief": ""}, dry_run=False)
        self.assertEqual(called, [])

    def test_nothing_new_is_not_an_error(self):
        """`claude-error` is the queue of broken tickets. A working command that had
        nothing to do does not belong in it."""
        prior = comment(f"transcript\n\n{note_reply.english_marker(7)}",
                        author=API_USER, cid=8)
        comments = [comment("claude: english", cid=9),
                    dict(comment("hallo", author=42, cid=7), public=True), prior]
        session = FakeSession([FakeResponse({"ticket": {}})])
        with Patched(triage, conversation_turns=lambda *a: None):
            note_reply.run_english(session, "sub", "model", {"id": 7}, comments,
                                   {"id": 9, "author": AGENT, "action": "english",
                                    "brief": ""}, dry_run=False)
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertEqual(ticket.get("additional_tags", []), [])
        self.assertIn(note_reply.TAG_ERROR, ticket["remove_tags"])

    def test_an_english_ticket_gets_no_transcript(self):
        """A transcript of English text repeats what is already on the ticket."""
        turns = [{"index": 0, "who": "Customer", "when": "t", "body": "My app crashes"}]
        session = FakeSession([FakeResponse({"ticket": {}})])
        with Patched(triage, conversation_turns=lambda *a: turns,
                     claude_cli_json=lambda *a, **k: {
                         "turns": [{"index": 0, "english": "My app crashes"}]}):
            note_reply.run_english(session, "sub", "model", {"id": 7},
                                   [comment("claude: english", cid=9)],
                                   {"id": 9, "author": AGENT, "action": "english",
                                    "brief": ""}, dry_run=False)
        body = session.calls[0][2]["json"]["ticket"]["comment"]["html_body"]
        self.assertIn("already in English", body)
        self.assertEqual(session.calls[0][2]["json"]["ticket"].get("additional_tags", []), [])

    def test_unaccented_german_is_still_translated(self):
        """The reason this is checked after the call, not guessed before it:
        "Hallo, ich habe ein Problem" is pure ASCII, and a character test would have
        called it English and left the agent unable to read it."""
        turns = [{"index": 0, "who": "Customer", "when": "t",
                  "body": "Hallo, ich habe ein Problem"}]
        self.assertFalse(note_reply.already_english(
            turns, [{"index": 0, "english": "Hello, I have a problem"}]))

    def test_a_turn_the_model_dropped_counts_as_needing_translation(self):
        turns = [{"index": 0, "who": "Customer", "when": "t", "body": "Hallo"},
                 {"index": 1, "who": "Support", "when": "t", "body": "Hi"}]
        self.assertFalse(note_reply.already_english(
            turns, [{"index": 0, "english": "Hallo"}]))

    def test_whitespace_and_case_do_not_count_as_a_translation(self):
        turns = [{"index": 0, "who": "Customer", "when": "t", "body": "My  app\ncrashes"}]
        self.assertTrue(note_reply.already_english(
            turns, [{"index": 0, "english": "my app crashes"}]))

    def test_a_turn_with_blank_lines_stays_one_turn(self):
        """The first version split render_transcript's output on blank lines, which
        tore a single multi-paragraph message into a row of disconnected boxes."""
        turns = [{"index": 0, "who": "Customer", "when": "2026-09-03 10:00 UTC",
                  "body": "Hallo,\n\nzweiter Absatz.\n\ndritter Absatz."}]
        blocks = note_reply.transcript_blocks(turns, [])
        self.assertEqual(sum(1 for b in blocks if "<strong>" in b), 1,
                         "one speaker line per turn, not one per paragraph")
        self.assertEqual(len(blocks), 4)  # speaker line + three paragraphs

    def test_the_transcript_is_prose_not_code_blocks(self):
        """<pre> is for text that gets extracted and sent byte for byte. Nothing
        extracts a transcript, and a code box is the wrong shape for prose."""
        turns = [{"index": 0, "who": "Customer", "when": "", "body": "Hallo"}]
        self.assertNotIn("<pre>", "".join(note_reply.transcript_blocks(turns, [])))

    def test_each_turn_gets_its_speaker_line(self):
        turns = [{"index": 0, "who": "Customer", "when": "t1", "body": "a"},
                 {"index": 1, "who": "Support", "when": "t2", "body": "b"}]
        blocks = note_reply.transcript_blocks(turns, [{"index": 1, "english": "B"}])
        joined = "".join(blocks)
        self.assertIn("Customer:", joined)
        self.assertIn("Support:", joined)
        self.assertIn("B", joined)          # translated turn used
        self.assertIn("a", joined)          # untranslated turn falls back to original

    def test_the_transcript_note_is_never_public(self):
        turns = [{"index": 0, "who": "Customer", "when": "2026-09-03 10:00 UTC",
                  "body": "Hallo"}]
        session = FakeSession([FakeResponse({"ticket": {}})])
        with Patched(triage, conversation_turns=lambda *a: turns,
                     claude_cli_json=lambda *a, **k: {"turns": [{"index": 0,
                                                                "english": "Hello"}]}):
            note_reply.run_english(session, "sub", "model", {"id": 7},
                                   [dict(comment("Hallo", author=42, cid=3), public=True)],
                                   {"id": 9, "author": AGENT, "action": "english",
                                    "brief": ""}, dry_run=False)
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertIs(ticket["comment"]["public"], False)
        self.assertIn("Hello", ticket["comment"]["html_body"])
        self.assertIn(note_reply.english_marker(3), ticket["comment"]["html_body"])


BOOK = {
    "groups": [{"key": "attachments", "title": "Attachments fail", "platform_sensitive": True}],
    "cells": {
        "attachments|android": {"answer": "We usually explain the 14-day window.",
                                "steps": ["Ask for the app version"], "actions": [],
                                "caveat": "A fix was promised in Nov 2025 and never shipped.",
                                "consistency": "high", "n": 12, "examples": [111, 222]},
        "attachments|any": {"answer": "All-platform version.", "steps": [], "actions": [],
                            "caveat": "", "consistency": "medium", "n": 30, "examples": []},
    },
}


class HouseAnswers(unittest.TestCase):
    def test_absent_config_turns_the_feature_off(self):
        """Drafting must work exactly as before on a host with no knowledge file."""
        with Patched(os, environ={k: v for k, v in os.environ.items()
                                  if k != note_reply.HOUSE_ENV}):
            self.assertIsNone(note_reply.load_house())

    def test_a_corrupt_file_degrades_rather_than_fails(self):
        """A bad knowledge file must cost a thinner draft, never the ability to
        answer a customer."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
        self.assertIsNone(note_reply.load_house(handle.name))
        os.unlink(handle.name)

    def test_falls_back_to_the_all_platform_answer(self):
        """A small group has no per-platform answer, and the general one still beats
        nothing."""
        cell, covering = note_reply.house_cell(BOOK, "attachments", "ios")
        self.assertEqual(covering, "any")
        self.assertEqual(cell["n"], 30)

    def test_prefers_the_platform_specific_answer(self):
        cell, covering = note_reply.house_cell(BOOK, "attachments", "android")
        self.assertEqual((covering, cell["n"]), ("android", 12))

    def test_an_unplaced_ticket_has_no_precedent(self):
        self.assertEqual(note_reply.house_cell(BOOK, None, "android"), (None, None))

    def test_a_cached_placement_skips_the_classifier(self):
        """The tags are written on the first draft so a revision does not pay for the
        same model call twice."""
        ticket = {"id": 7, "tags": ["grp-attachments", "plat-android", "relay-test"]}
        self.assertEqual(note_reply.tagged_placement(ticket), ("attachments", "android"))

    def test_no_placement_tags_means_no_cache(self):
        self.assertEqual(note_reply.tagged_placement({"id": 7, "tags": ["relay-test"]}),
                         (None, None))

    def test_the_precedent_carries_shape_not_claims(self):
        cell, _ = note_reply.house_cell(BOOK, "attachments", "android")
        text = note_reply.render_precedent(cell, "Attachments fail", "android")
        self.assertIn("We usually explain the 14-day window.", text)
        self.assertIn("Ask for the app version", text)
        # The caveat is for the agent reviewing the draft, never for the model
        # writing it: it is a note about which past promises went stale.
        self.assertNotIn("never shipped", text)

    def test_the_brief_sits_after_the_precedent_in_the_prompt(self):
        """Precedent is background, the brief is the instruction, and what is nearest
        the end is what governs."""
        built = note_reply.build_compose_prompt("their words", "the brief",
                                                precedent="PRECEDENT BLOCK")
        self.assertLess(built.index("PRECEDENT BLOCK"), built.index("the brief"))

    def test_the_prompt_forbids_repeating_specifics_from_precedent(self):
        """The caveats say fixes were declared shipped and recurred. Repeating one
        into a live reply re-promises something nobody delivered."""
        prompt = " ".join(note_reply.COMPOSE_SYSTEM.lower().split())
        self.assertIn("take nothing specific from it", prompt)
        self.assertIn("the brief wins and the precedent is dropped", prompt)

    def test_the_draft_note_shows_the_grounding_and_the_caveat(self):
        cell, covering = note_reply.house_cell(BOOK, "attachments", "android")
        note = note_reply.build_draft_note(GERMAN, "brief", 42, False, cell,
                                           "Attachments fail", covering)
        self.assertIn("Attachments fail", note)
        self.assertIn("#111", note)
        self.assertIn("never shipped", note)
        # and it still must not be able to command itself
        self.assertIsNone(note_reply.parse_command(
            note_reply.comment_text(posted_note(note))))


class FindDraft(unittest.TestCase):
    def test_finds_the_text_between_the_delimiters(self):
        self.assertEqual(note_reply.find_draft([draft_note("Hallo")], API_USER),
                         {1: "Hallo"})

    def test_finds_every_numbered_option(self):
        found = note_reply.find_draft([draft_note("eins", "zwei", "drei")], API_USER)
        self.assertEqual(found, {1: "eins", 2: "zwei", 3: "drei"})

    def test_newest_draft_wins(self):
        """fetch_comments returns newest first, so the first match is the newest.
        An agent who rejected a draft and rewrote the brief must get the new one."""
        comments = [draft_note("second", cid=10), draft_note("first", cid=9)]
        self.assertEqual(note_reply.find_draft(comments, API_USER), {1: "second"})

    def test_ignores_a_draft_shaped_note_from_a_human(self):
        """Otherwise an agent could paste the delimiters into a note and have
        `reply` publish text nobody generated or reviewed."""
        forged = dict(posted_note(f"{note_reply.para(note_reply.begin_marker(1))}"
                                  f"{note_reply.verbatim('send me')}"
                                  f"{note_reply.para(note_reply.end_marker(1))}"),
                      author_id=AGENT)
        self.assertEqual(note_reply.find_draft([forged], API_USER), {})

    def test_ignores_public_comments(self):
        public = dict(draft_note("x"), public=True)
        self.assertEqual(note_reply.find_draft([public], API_USER), {})

    def test_no_draft_at_all(self):
        self.assertEqual(note_reply.find_draft([comment("claude: reply")], API_USER), {})

    def test_preserves_the_reviewed_text_exactly(self):
        """What was reviewed is what goes out. The note is HTML, so the draft has to
        survive escaping, Zendesk's tag stripping and unescaping and come back
        identical — blank lines, indentation, ampersands and angle brackets included.
        Anything less and the customer receives something nobody read."""
        body = ('Hallo,\n\nAnhänge werden 14 Tage gespeichert & danach gelöscht.\n'
                'Zeile mit <spitzen Klammern> und „Anführungszeichen".\n\n'
                '  eingerückte Zeile\n\n05ab & Grüße')
        note = draft_note(body)
        self.assertEqual(note_reply.find_draft([note], API_USER), {1: body})

    def test_the_stored_markup_escapes_what_the_draft_contains(self):
        """The other half: unescaping on read is only safe because writing escapes."""
        note = draft_note("a & b <c>")
        self.assertIn("a &amp; b &lt;c&gt;", note["body"])


class ChoosingAnOption(unittest.TestCase):
    """Which of the offered replies actually reaches the customer."""

    def test_a_bare_reply_sends_the_only_option(self):
        text, complaint = note_reply.choose_option({1: "only"}, None)
        self.assertEqual((text, complaint), ("only", None))

    def test_a_bare_reply_refuses_to_guess_between_options(self):
        """Picking for them would send a customer a reply nobody chose."""
        text, complaint = note_reply.choose_option({1: "a", 2: "b", 3: "c"}, None)
        self.assertIsNone(text)
        self.assertIn("3 options", complaint)

    def test_a_number_selects_that_option(self):
        self.assertEqual(note_reply.choose_option({1: "a", 2: "b"}, 2)[0], "b")

    def test_an_option_that_does_not_exist_is_refused(self):
        text, complaint = note_reply.choose_option({1: "a", 2: "b"}, 7)
        self.assertIsNone(text)
        self.assertIn("no option 7", complaint)

    def test_reads_the_number_off_the_command(self):
        for text, want in [("2", 2), (" 3 ", 3), ("#2", 2), ("", None),
                           ("please send", None), ("2 but nicer", 2)]:
            with self.subTest(text=text):
                self.assertEqual(note_reply.asked_option(text), want)

    def test_reply_two_sends_the_second_option_verbatim(self):
        session = FakeSession([FakeResponse({"user": {"id": AGENT, "name": "Audric"}}),
                               FakeResponse({"ticket": {}}), FakeResponse({"ticket": {}})])
        comments = [comment("claude: reply 2", cid=3),
                    draft_note("erste", "zweite", "dritte", cid=2)]
        note_reply.run_reply(session, "sub", {"id": 7}, comments,
                             {"id": 3, "author": AGENT, "action": "reply", "brief": "2"},
                             API_USER, dry_run=False)
        puts = [c for c in session.calls if c[0] == "PUT"]
        self.assertEqual(puts[0][2]["json"]["ticket"]["comment"],
                         {"body": "zweite", "public": True})

    def test_an_ambiguous_reply_writes_no_public_comment(self):
        session = FakeSession([FakeResponse({"ticket": {}})])
        comments = [comment("claude: reply", cid=3),
                    draft_note("erste", "zweite", cid=2)]
        note_reply.run_reply(session, "sub", {"id": 7}, comments,
                             {"id": 3, "author": AGENT, "action": "reply", "brief": ""},
                             API_USER, dry_run=False)
        for _, _, kwargs in session.calls:
            self.assertIs(kwargs["json"]["ticket"]["comment"]["public"], False)

    def test_being_asked_to_choose_is_not_an_error(self):
        """`claude-error` is the queue of broken tickets, not of ordinary prompts."""
        session = FakeSession([FakeResponse({"ticket": {}})])
        note_reply.run_reply(session, "sub", {"id": 7},
                             [comment("claude: reply", cid=3),
                              draft_note("a", "b", cid=2)],
                             {"id": 3, "author": AGENT, "action": "reply", "brief": ""},
                             API_USER, dry_run=False)
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertEqual(ticket.get("additional_tags", []), [])

    def test_the_prompt_asks_for_genuinely_different_options(self):
        prompt = " ".join(note_reply.COMPOSE_SYSTEM.lower().split())
        self.assertIn("genuinely different", prompt)
        self.assertIn("if the brief only supports one honest reply, return one", prompt)


class QueueTag(unittest.TestCase):
    """`claude-queued` means "a webhook fired and nobody serviced it". Anything else
    left in it turns the dropped-job view into noise."""

    def test_a_run_with_nothing_to_do_still_clears_the_tag(self):
        """Claude's own notes name the commands, so posting one re-fires the trigger.
        That second run finds only its own note — and must not leave the tag behind."""
        session = FakeSession([FakeResponse({"ticket": {}})])
        note_reply.clear_queued(session, "sub", 7)
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertEqual(ticket["remove_tags"], [note_reply.TAG_QUEUED])
        self.assertNotIn("comment", ticket)

    def test_a_dry_run_clears_nothing(self):
        session = FakeSession([])
        note_reply.clear_queued(session, "sub", 7, dry_run=True)
        self.assertEqual(session.calls, [])

    def test_a_failure_to_clear_is_not_fatal(self):
        """The tag is a dashboard light, not the work."""
        session = FakeSession([FakeResponse({}, status_code=500),
                               FakeResponse({}, status_code=500),
                               FakeResponse({}, status_code=500),
                               FakeResponse({}, status_code=500),
                               FakeResponse({}, status_code=500),
                               FakeResponse({}, status_code=500)])
        note_reply.clear_queued(session, "sub", 7)   # must not raise


class Explain(unittest.TestCase):
    """The read-only verb that surfaces known fixes, which `draft` refuses to assert."""

    def test_explain_is_a_command(self):
        self.assertEqual(note_reply.parse_command("claude: explain"), ("explain", ""))

    def test_it_shows_what_was_actually_done(self):
        cell = dict(BOOK["cells"]["attachments|android"],
                    actions=["Fix shipped in v2.15.3 (case 27896)"])
        book = {"groups": BOOK["groups"],
                "cells": dict(BOOK["cells"], **{"attachments|android": cell})}
        session = FakeSession([FakeResponse({"ticket": {}})])
        with Patched(note_reply, load_house=lambda *a: book):
            note_reply.run_explain(
                session, "sub", "model",
                {"id": 7, "tags": ["grp-attachments", "plat-android"]}, [],
                {"id": 9, "author": AGENT, "action": "explain", "brief": ""},
                dry_run=False)
        body = session.calls[0][2]["json"]["ticket"]["comment"]["html_body"]
        self.assertIn("v2.15.3", body)
        self.assertIn("never shipped", body)      # the caveat travels with it
        self.assertIs(session.calls[0][2]["json"]["ticket"]["comment"]["public"], False)

    def test_it_writes_nothing_when_the_ticket_matches_nothing(self):
        with Patched(note_reply, load_house=lambda *a: BOOK,
                     place_ticket=lambda *a: (None, "android")):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_explain(session, "sub", "model", {"id": 7, "tags": []}, [],
                                   {"id": 9, "author": AGENT, "action": "explain",
                                    "brief": ""}, dry_run=False)
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertIn("no precedent", ticket["comment"]["html_body"])
        self.assertEqual(ticket.get("additional_tags", []), [])

    def test_no_house_answers_configured_is_said_plainly(self):
        with Patched(note_reply, load_house=lambda *a: None):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_explain(session, "sub", "model", {"id": 7, "tags": []}, [],
                                   {"id": 9, "author": AGENT, "action": "explain",
                                    "brief": ""}, dry_run=False)
        self.assertIn("No house answers",
                      session.calls[0][2]["json"]["ticket"]["comment"]["html_body"])


class Authorisation(unittest.TestCase):
    def test_agents_and_admins_may_command(self):
        for role in ("agent", "admin"):
            self.assertTrue(note_reply.may_command({"id": AGENT, "role": role}))

    def test_end_users_may_not(self):
        self.assertFalse(note_reply.may_command({"id": AGENT, "role": "end-user"}))

    def test_allowlist_narrows_further(self):
        with Patched(os, environ={**os.environ, "ZENDESK_NOTE_AUTHORS": "1,2"}):
            self.assertFalse(note_reply.may_command({"id": AGENT, "role": "agent"}))
            self.assertTrue(note_reply.may_command({"id": 2, "role": "agent"}))

    def test_allowlist_does_not_override_the_role_check(self):
        with Patched(os, environ={**os.environ, "ZENDESK_NOTE_AUTHORS": str(AGENT)}):
            self.assertFalse(note_reply.may_command({"id": AGENT, "role": "end-user"}))


class LatestCommand(unittest.TestCase):
    def session_for(self, role="agent"):
        return FakeSession([FakeResponse({"user": {"id": AGENT, "role": role}})])

    def test_takes_the_newest_command(self):
        comments = [comment("claude: reply", cid=3), comment("claude: draft - x", cid=2)]
        found = note_reply.latest_command(comments, API_USER, self.session_for(), "sub")
        self.assertEqual((found["action"], found["id"]), ("reply", 3))

    def test_skips_notes_written_by_the_api_user(self):
        """The in-code half of the loop guard, independent of the trigger's config."""
        comments = [comment("claude: draft - mine", author=API_USER, cid=4),
                    comment("claude: draft - theirs", cid=2)]
        found = note_reply.latest_command(comments, API_USER, self.session_for(), "sub")
        self.assertEqual(found["id"], 2)

    def test_skips_public_comments(self):
        """A customer can type the prefix into a public reply."""
        comments = [comment("claude: reply", author=42, public=True, cid=5),
                    comment("claude: draft - x", cid=2)]
        found = note_reply.latest_command(comments, API_USER, self.session_for(), "sub")
        self.assertEqual(found["id"], 2)

    def test_an_unauthorised_author_stops_the_search(self):
        comments = [comment("claude: reply", cid=3), comment("claude: draft - x", cid=2)]
        found = note_reply.latest_command(comments, API_USER,
                                          self.session_for("end-user"), "sub")
        self.assertIsNone(found)

    def test_no_command_present(self):
        self.assertIsNone(note_reply.latest_command(
            [comment("just a note")], API_USER, FakeSession([]), "sub"))


class Idempotency(unittest.TestCase):
    def test_the_done_marker_is_keyed_on_the_command(self):
        """Not on the ticket: two briefs on one ticket are two commands, and the
        second must not be swallowed by the first one's marker."""
        self.assertNotEqual(note_reply.done_marker(1), note_reply.done_marker(2))

    def test_a_handled_command_is_recognised(self):
        import reply
        note = note_reply.build_draft_note(GERMAN, "x", 42)
        self.assertTrue(reply.already_replied([comment(note)], note_reply.done_marker(42)))
        self.assertFalse(reply.already_replied([comment(note)], note_reply.done_marker(43)))


class Writes(unittest.TestCase):
    def test_reply_sends_the_draft_verbatim_and_sets_pending(self):
        session = FakeSession([FakeResponse({"user": {"id": AGENT, "name": "Audric"}}),
                               FakeResponse({"ticket": {}}), FakeResponse({"ticket": {}})])
        comments = [comment("claude: reply", cid=3), draft_note("Hallo Welt", cid=2)]
        note_reply.run_reply(session, "sub", {"id": 7}, comments,
                             {"id": 3, "author": AGENT, "action": "reply", "brief": ""},
                             API_USER, dry_run=False)
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 2)
        public = puts[0][2]["json"]["ticket"]
        self.assertEqual(public["comment"], {"body": "Hallo Welt", "public": True})
        self.assertEqual(public["status"], note_reply.REPLIED_STATUS)
        self.assertIs(puts[1][2]["json"]["ticket"]["comment"]["public"], False)

    def test_reply_without_a_draft_writes_no_public_comment(self):
        session = FakeSession([FakeResponse({"ticket": {}})])
        note_reply.run_reply(session, "sub", {"id": 7}, [comment("claude: reply", cid=3)],
                             {"id": 3, "author": AGENT, "action": "reply", "brief": ""},
                             API_USER, dry_run=False)
        for _, _, kwargs in session.calls:
            self.assertIs(kwargs["json"]["ticket"]["comment"]["public"], False)

    def test_a_dry_run_writes_nothing(self):
        session = FakeSession([FakeResponse({"user": {"id": AGENT, "name": "Audric"}})])
        note_reply.run_reply(session, "sub", {"id": 7},
                             [comment("claude: reply", cid=3), draft_note("Hallo", cid=2)],
                             {"id": 3, "author": AGENT, "action": "reply", "brief": ""},
                             API_USER, dry_run=True)
        self.assertEqual([call for call in session.calls if call[0] == "PUT"], [])

    def test_an_empty_brief_is_refused_without_calling_claude(self):
        called = []
        with Patched(note_reply, compose=lambda *a: called.append(a)):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_draft(session, "sub", "model", {"id": 7}, [],
                                 {"id": 3, "author": AGENT, "action": "draft", "brief": ""},
                                 API_USER, dry_run=False)
        self.assertEqual(called, [])
        self.assertIs(session.calls[0][2]["json"]["ticket"]["comment"]["public"], False)

    def test_tags_move_the_ticket_out_of_the_queue(self):
        session = FakeSession([FakeResponse({"ticket": {}})])
        note_reply.write_to_ticket(session, "sub", 7, "note", public=False,
                                   add_tags=[note_reply.TAG_DRAFTED],
                                   drop_tags=[note_reply.TAG_QUEUED])
        ticket = session.calls[0][2]["json"]["ticket"]
        self.assertEqual(ticket["additional_tags"], [note_reply.TAG_DRAFTED])
        self.assertEqual(ticket["remove_tags"], [note_reply.TAG_QUEUED])


class DraftNote(unittest.TestCase):
    def test_carries_the_back_translation_for_a_foreign_ticket(self):
        note = note_reply.build_draft_note(GERMAN, "keeps 14 days", 42)
        self.assertIn(GERMAN["options"][0]["back_translation"], note)

    def test_omits_the_back_translation_for_an_english_ticket(self):
        text = "Attachments are kept for 14 days."
        english = {"language": "English", "language_code": "en", "is_english": True,
                   "options": [{"approach": "explain", "reply_en": text,
                                "translated": text, "back_translation": ""}]}
        note = note_reply.build_draft_note(english, "keeps 14 days", 42)
        self.assertNotIn("back in English", note)
        self.assertEqual(note_reply.find_draft([posted_note(note)], API_USER), {1: text})

    def test_numbers_every_option_and_names_its_approach(self):
        """The approach line is how the agent picks without reading all three."""
        note = note_reply.build_draft_note(GERMAN_THREE, "keeps 14 days", 42)
        for number in (1, 2, 3):
            self.assertIn(f"Option {number}", note)
        self.assertIn("ask which device was online", note)
        self.assertEqual(
            note_reply.find_draft([posted_note(note)], API_USER),
            {1: "Erste Antwort", 2: "Zweite Antwort", 3: "Dritte Antwort"})

    def test_the_sent_note_quotes_what_went_out(self):
        note = note_reply.build_sent_note("Audric", 42, "Hallo Welt\nzweite Zeile")
        self.assertIn("Hallo Welt", note)
        self.assertNotIn("zweite Zeile", note)


class Composition(unittest.TestCase):
    def test_an_empty_translation_is_refused(self):
        with self.assertRaises(SystemExit):
            note_reply.validate_composition(dict(GERMAN, options=[option("  ")]))

    def test_empty_options_are_dropped_not_offered(self):
        """An empty block in the note is a blank the agent could send."""
        result = note_reply.validate_composition(
            dict(GERMAN, options=[option("keep me"), option("   "), option("also me")]))
        self.assertEqual([o["translated"] for o in result["options"]],
                         ["keep me", "also me"])

    def test_more_options_than_the_cap_are_trimmed(self):
        result = note_reply.validate_composition(
            dict(GERMAN, options=[option(f"n{i}") for i in range(6)]))
        self.assertEqual(len(result["options"]), note_reply.MAX_OPTIONS)

    def test_a_missing_field_is_refused(self):
        partial = {key: value for key, value in GERMAN.items() if key != "options"}
        with self.assertRaises(SystemExit):
            note_reply.validate_composition(partial)

    @staticmethod
    def prompt():
        """The system prompt as one line, so a probe cannot fail merely because the
        text was re-wrapped."""
        return " ".join(note_reply.COMPOSE_SYSTEM.lower().split())

    def test_the_prompt_forbids_inventing_facts_and_actions(self):
        """The two rules that matter most, asserted so a prompt edit cannot quietly
        drop them: 183 solved tickets say an account was banned, and a reply
        claiming an action nobody took is the worst output this can produce."""
        prompt = self.prompt()
        self.assertIn("never state a fact neither of them contains", prompt)
        self.assertIn("never claim an action was taken", prompt)

    def test_a_second_draft_amends_the_first(self):
        """An agent writing "also mention X" wants the draft they just read plus X.
        Regenerating from the new brief alone throws away wording they kept."""
        seen = {}
        def fake(model, sample, brief, previous=None, precedent=None):
            seen.update(brief=brief, previous=previous, precedent=precedent)
            return GERMAN
        with Patched(note_reply, compose=fake):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_draft(
                session, "sub", "model", {"id": 7, "requester_id": 42},
                [comment("claude: draft - also mention X", cid=11),
                 draft_note("Erster Entwurf", cid=10)],
                {"id": 11, "author": AGENT, "action": "draft",
                 "brief": "also mention X"}, API_USER, dry_run=False)
        self.assertIn("Erster Entwurf", seen["previous"])
        self.assertIn("revised",
                      session.calls[0][2]["json"]["ticket"]["comment"]["html_body"])

    def test_a_first_draft_has_nothing_to_amend(self):
        seen = {}
        def fake(model, sample, brief, previous=None, precedent=None):
            seen.update(previous=previous, precedent=precedent)
            return GERMAN
        with Patched(note_reply, compose=fake):
            session = FakeSession([FakeResponse({"ticket": {}})])
            note_reply.run_draft(
                session, "sub", "model", {"id": 7, "requester_id": 42},
                [comment("claude: draft - x", cid=11)],
                {"id": 11, "author": AGENT, "action": "draft", "brief": "x"},
                API_USER, dry_run=False)
        self.assertIsNone(seen["previous"])

    def test_the_amend_prompt_puts_the_brief_after_the_draft(self):
        """The brief reads as an instruction about the draft above it, which is how
        the agent meant it."""
        built = note_reply.build_compose_prompt("their words", "add X", "old draft")
        self.assertLess(built.index("old draft"), built.index("add X"))

    def test_the_prompt_still_forbids_workarounds_and_placeholder_names(self):
        """Loosening the tone must not loosen the facts. A confident wrong
        instruction costs more than a short answer, and 23 macros on this account
        already send "(User name)" literally."""
        prompt = self.prompt()
        self.assertIn("never offer a workaround", prompt)
        self.assertIn("(user name)", prompt)

    def test_the_prompt_grants_tone_latitude(self):
        """The other half: a reply that is correct and cold is a worse reply."""
        self.assertIn("correct and cold is a worse reply", self.prompt())

    def test_the_brief_is_bounded(self):
        action, brief = note_reply.parse_command("claude: draft - " + "x" * 5000)
        self.assertLessEqual(len(brief), note_reply.BRIEF_CHARS)


if __name__ == "__main__":
    unittest.main()
