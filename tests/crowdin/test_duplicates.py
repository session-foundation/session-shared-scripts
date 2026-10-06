"""
    uv run python -m unittest tests.crowdin.test_duplicates
"""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from session_ops.crowdin import duplicates, reconcile, sdk
from session_ops.shared import discord
from session_ops.shared.testing import RecordedSession
from tests.crowdin.recording import EXCHANGES

API = "https://api.crowdin.com/api/v2"
PROJECT = duplicates.Project({
    "identifier": "p", "sourceLanguage": {"id": "en"}, "targetLanguageIds": ["de"],
    "targetLanguages": [{"id": "de", "editorCode": "de"}]})


def finding(sid, lang="de", cat=None, count=2, identifier=None):
    return {"stringId": sid, "locale": lang, "pluralCategory": cat, "count": count,
            "identifier": identifier or f"s{sid}"}


class TestApply(unittest.TestCase):
    def test_a_new_slot_opens_and_a_gone_one_resolves(self):
        state = duplicates.empty_state()
        opened, _ = duplicates.apply(state, [finding(1)], {"1:de", "2:de"}, 10)
        self.assertEqual([s["stringId"] for s in opened], [1])
        opened, resolved = duplicates.apply(state, [finding(2)], {"1:de", "2:de"}, 20)
        self.assertEqual(([s["stringId"] for s in opened], [s["stringId"] for s in resolved]),
                         ([2], [1]))
        self.assertEqual(list(state["slots"]), ["2:de:"])

    def test_an_open_slot_seen_again_is_not_news(self):
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1)], {"1:de"}, 10)
        self.assertEqual(duplicates.apply(state, [finding(1, count=3)], {"1:de"}, 20), ([], []))
        self.assertEqual(state["slots"]["1:de:"]["count"], 3)

    def test_only_the_judged_scopes_are_touched(self):
        """A string that failed to fetch keeps its open slot rather than resolving."""
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1), finding(2)], {"1:de", "2:de"}, 10)
        _, resolved = duplicates.apply(state, [], {"2:de"}, 20)
        self.assertEqual([s["stringId"] for s in resolved], [2])
        self.assertIn("1:de:", state["slots"])

    def test_each_plural_category_is_its_own_slot(self):
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1, cat="one"), finding(1, cat="few")], {"1:de"}, 10)
        _, resolved = duplicates.apply(state, [finding(1, cat="one")], {"1:de"}, 20)
        self.assertEqual([s["pluralCategory"] for s in resolved], ["few"])


class TestMessages(unittest.TestCase):
    def test_nothing_changed_posts_nothing(self):
        self.assertEqual(duplicates.build_messages([], [], 7, PROJECT), [])

    def test_new_and_resolved_are_listed_with_editor_links(self):
        messages = duplicates.build_messages(
            [finding(1, cat="few", count=3)], [finding(2)], 4, PROJECT)
        embeds = messages[0]["embeds"]
        self.assertIn("**1** slot(s) newly holding 2+ translations, **1** resolved. "
                      "**4** open in total.", embeds[0]["description"])
        self.assertEqual(embeds[1]["title"], "de — 1 new")
        self.assertIn("[s1](https://crowdin.com/editor/p/all/en-de#1) `[few]` — **3**",
                      embeds[1]["description"])
        self.assertEqual(embeds[2]["title"], "de — 1 resolved")

    def test_a_burst_in_one_locale_is_one_embed_with_a_count(self):
        opened = [finding(i, identifier="x" * 80) for i in range(462)]
        messages = duplicates.build_messages(opened, [], 462, PROJECT)
        self.assertEqual(len(messages), 1)
        locale = messages[0]["embeds"][1]
        self.assertEqual(locale["title"], "de — 462 new")
        lines = locale["description"].split("\n")
        self.assertEqual(len(lines), duplicates.LISTED_PER_LOCALE)
        self.assertEqual(lines[-1], f"…and **{462 - duplicates.LISTED_PER_LOCALE + 1}** more: "
                                    "[open de in the editor](https://crowdin.com/editor/p/all/en-de)")

    def test_a_locale_at_the_limit_is_listed_in_full(self):
        opened = [finding(i) for i in range(duplicates.LISTED_PER_LOCALE)]
        description = duplicates.build_messages(opened, [], 9, PROJECT)[0]["embeds"][1]["description"]
        self.assertEqual(description.count("• "), duplicates.LISTED_PER_LOCALE)
        self.assertNotIn("more", description)

    def test_a_burst_across_every_locale_stays_within_discords_limits(self):
        langs = [f"l{n}" for n in range(80)]
        project = duplicates.Project({
            "identifier": "p", "sourceLanguage": {"id": "en"}, "targetLanguageIds": langs,
            "targetLanguages": [{"id": lang} for lang in langs]})
        opened = [finding(i, lang=lang, identifier="x" * 90) for lang in langs
                  for i in range(50)]
        messages = duplicates.build_messages(opened, [], len(opened), project)
        self.assertTrue(all(sum(discord.embed_len(e) for e in m["embeds"])
                            <= discord.MAX_EMBEDS_TEXT_CHARS for m in messages))
        self.assertTrue(all(len(m["embeds"]) <= discord.MAX_EMBEDS_PER_MESSAGE for m in messages))
        self.assertEqual(len([e for m in messages for e in m["embeds"]]), 81)


def recording(changes=None):
    """The recording, with some strings' German translations replaced."""
    exchanges = copy.deepcopy(EXCHANGES)
    for ex in exchanges:
        sid = (ex.get("params") or {}).get("stringId")
        if sid in (changes or {}):
            ex["response"]["json"]["data"] = [{"data": t} for t in changes[sid]]
    return exchanges


def without_string(exchanges, sid):
    for ex in exchanges:
        if ex["url"].endswith("/strings"):
            ex["response"]["json"]["data"] = [
                row for row in ex["response"]["json"]["data"] if row["data"]["id"] != sid]
    return exchanges


def only_locale(exchanges, lang):
    project = exchanges[0]["response"]["json"]["data"]
    project["targetLanguageIds"] = [lang]
    project["targetLanguages"] = [t for t in project["targetLanguages"] if t["id"] == lang]
    return exchanges


def translation(tid, text, cat=None):
    return {"id": tid, "text": text, "pluralCategoryName": cat, "user": None,
            "createdAt": "2026-09-10T00:00:00+00:00"}


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "duplicates.json")

    def run_reconcile(self, exchanges, *flags, locales=("de",)):
        session = RecordedSession(exchanges)
        out = io.StringIO()
        with mock.patch.object(reconcile, "crowdin_client",
                               lambda token, pid: sdk.client(token, pid, session=session)), \
                mock.patch.dict(os.environ, {"CROWDIN_API_TOKEN": "t"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            reconcile.main(["--state", self.state, *(["--locales", *locales] if locales else []),
                            *flags])
        return out.getvalue()

    def slots(self):
        with open(self.state, encoding="utf-8") as handle:
            return sorted(json.load(handle)["slots"])

    def add_slot(self, sid, lang):
        state = duplicates.load(self.state)
        state["slots"][duplicates.slot_key(sid, lang, None)] = duplicates.record(
            finding(sid, lang), 1)
        duplicates.save(self.state, state)

    def test_seeding_records_every_open_slot_and_posts_nothing(self):
        self.assertEqual(self.run_reconcile(recording(), "--seed"), "")
        self.assertEqual(self.slots(), ["101:de:", "104:de:other", "105:de:one",
                                        "105:de:other", "107:de:"])

    def test_a_later_run_posts_only_the_difference(self):
        self.run_reconcile(recording(), "--seed")
        changed = recording({
            101: [translation(1, "Akzeptieren")],
            102: [translation(3, "Datei konnte nicht gespeichert werden."),
                  translation(16, "Speichern fehlgeschlagen.")],
        })
        embeds = [e for m in json.loads(self.run_reconcile(changed, "--dry-run"))
                  for e in m["embeds"]]
        self.assertIn("**1** slot(s) newly holding 2+ translations, **1** resolved",
                      embeds[0]["description"])
        self.assertEqual([e["title"] for e in embeds[1:]], ["de — 1 new", "de — 1 resolved"])
        self.assertIn("[attachmentsSaveError]", embeds[1]["description"])
        self.assertIn("[accept]", embeds[2]["description"])
        self.assertIn("101:de:", self.slots(), "a dry run writes nothing")

    def test_an_unchanged_run_posts_nothing_at_all(self):
        self.run_reconcile(recording(), "--seed")
        with mock.patch.object(reconcile.discord, "post_to_discord") as post:
            with mock.patch.dict(os.environ, {"CROWDIN_DISCORD_WEBHOOK_URL": "https://hook"}):
                self.run_reconcile(recording())
        post.assert_not_called()

    def test_croql_checks_only_its_candidates_and_resolves_the_rest(self):
        self.run_reconcile(recording(), "--seed")
        exchanges = recording() + [{
            "method": "GET", "url": f"{API}/projects/618696/strings",
            "params": {"croql": reconcile.CROQL.format(lang="de"), "limit": 500, "offset": 0},
            "response": {"json": {"data": [{"data": {"id": 104}}, {"data": {"id": 105}}]}}}]
        session = RecordedSession(exchanges)
        with mock.patch.object(reconcile, "crowdin_client",
                               lambda token, pid: sdk.client(token, pid, session=session)), \
                mock.patch.dict(os.environ, {"CROWDIN_API_TOKEN": "t"}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            reconcile.main(["--state", self.state, "--locales", "de", "--croql", "--reseed"])
        checked = {params.get("stringId") for method, url, params, _ in session.calls
                   if url.endswith("/translations")}
        self.assertEqual(checked, {104, 105})
        self.assertEqual(self.slots(), ["104:de:other", "105:de:one", "105:de:other"])

    def test_a_slot_whose_string_was_deleted_resolves_in_every_locale(self):
        self.run_reconcile(recording(), "--seed")
        self.add_slot(107, "fr")
        self.run_reconcile(without_string(recording(), 107), "--reseed")
        self.assertEqual(self.slots(), ["101:de:", "104:de:other", "105:de:one",
                                        "105:de:other"])

    def test_a_slot_whose_locale_left_the_project_resolves(self):
        exchanges = only_locale(recording(), "de")
        self.run_reconcile(exchanges, "--seed", locales=None)
        self.add_slot(101, "it")
        self.run_reconcile(exchanges, "--reseed", locales=("de",))
        self.assertIn("101:it:", self.slots(), "--locales judges only the named locales")
        self.run_reconcile(exchanges, "--reseed", locales=None)
        self.assertNotIn("101:it:", self.slots())

    def test_seeding_over_an_existing_state_is_refused_without_reseed(self):
        self.run_reconcile(recording(), "--seed")
        self.add_slot(107, "fr")
        with self.assertRaises(SystemExit) as stopped:
            self.run_reconcile(recording(), "--seed")
        self.assertIn("--reseed", str(stopped.exception.code))
        self.assertIn("107:fr:", self.slots(), "the refused seed wrote nothing")

    def test_an_unseeded_state_stops_a_posting_run_before_crowdin(self):
        with mock.patch.object(reconcile.discord, "post_to_discord") as post, \
                mock.patch.dict(os.environ, {"CROWDIN_DISCORD_WEBHOOK_URL": "https://hook"}), \
                self.assertRaises(SystemExit) as stopped:
            self.run_reconcile([])
        self.assertIn("--seed", str(stopped.exception.code))
        post.assert_not_called()
        self.assertFalse(os.path.exists(self.state))

    def test_an_unseeded_state_still_allows_a_dry_run(self):
        self.assertIn("107", self.run_reconcile(recording(), "--dry-run"))

    def test_a_second_run_refuses_while_one_holds_the_state(self):
        self.run_reconcile(recording(), "--seed")
        with duplicates.only_run(self.state), self.assertRaises(SystemExit) as stopped:
            self.run_reconcile(recording(), "--dry-run")
        self.assertIn("Another reconciliation", str(stopped.exception.code))

    def test_a_failed_post_writes_no_state(self):
        self.run_reconcile(recording(), "--seed")
        with open(self.state, encoding="utf-8") as handle:
            before = handle.read()
        changed = recording({101: [translation(1, "Akzeptieren")]})
        with mock.patch.object(reconcile.discord, "post_to_discord", return_value=0), \
                mock.patch.dict(os.environ, {"CROWDIN_DISCORD_WEBHOOK_URL": "https://hook"}), \
                self.assertRaises(SystemExit):
            self.run_reconcile(changed)
        with open(self.state, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)


class TestLoad(unittest.TestCase):
    def test_a_missing_state_is_refused_unless_the_caller_allows_it(self):
        path = os.path.join(tempfile.mkdtemp(), "none.json")
        with self.assertRaises(SystemExit):
            duplicates.load(path)
        self.assertEqual(duplicates.load(path, missing_ok=True), duplicates.empty_state())

    def test_a_state_written_with_relay_check_times_loads_without_them(self):
        path = os.path.join(tempfile.mkdtemp(), "old.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": duplicates.STATE_VERSION, "slots": {"1:de:": {}},
                       "checked": {"1:de": 5}}, handle)
        self.assertEqual(duplicates.load(path), {"version": duplicates.STATE_VERSION,
                                                 "slots": {"1:de:": {}}})


if __name__ == "__main__":
    unittest.main()
