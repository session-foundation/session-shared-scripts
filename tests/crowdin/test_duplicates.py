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

from fastapi.testclient import TestClient

from session_ops.crowdin import duplicates, reconcile, relay, sdk
from session_ops.shared.testing import FakeResponse, FakeSession, RecordedSession
from tests.golden import assert_golden, load_golden_json

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
        opened, _ = duplicates.apply(state, [finding(1)], {"1:de": 10, "2:de": 10}, 10, False)
        self.assertEqual([s["stringId"] for s in opened], [1])
        opened, resolved = duplicates.apply(state, [finding(2)], {"1:de": 20, "2:de": 20},
                                            20, False)
        self.assertEqual(([s["stringId"] for s in opened], [s["stringId"] for s in resolved]),
                         ([2], [1]))
        self.assertEqual(list(state["slots"]), ["2:de:"])

    def test_an_open_slot_seen_again_is_not_news(self):
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1)], {"1:de": 10}, 10, False)
        self.assertEqual(duplicates.apply(state, [finding(1, count=3)], {"1:de": 20}, 20, False),
                         ([], []))
        self.assertEqual(state["slots"]["1:de:"]["count"], 3)

    def test_only_the_checked_scopes_are_judged(self):
        """A relay check of one string must not resolve every other open slot."""
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1), finding(2)], {"1:de": 10, "2:de": 10}, 10, False)
        _, resolved = duplicates.apply(state, [], {"2:de": 20}, 20, True)
        self.assertEqual([s["stringId"] for s in resolved], [2])
        self.assertIn("1:de:", state["slots"])

    def test_each_plural_category_is_its_own_slot(self):
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1, cat="one"), finding(1, cat="few")],
                         {"1:de": 10}, 10, False)
        _, resolved = duplicates.apply(state, [finding(1, cat="one")], {"1:de": 20}, 20, False)
        self.assertEqual([s["pluralCategory"] for s in resolved], ["few"])

    def test_a_scan_older_than_an_event_check_leaves_that_scope_alone(self):
        """The scan fetched before the suggestion landed; the event saw it."""
        state = duplicates.empty_state()
        duplicates.apply(state, [finding(1)], {"1:de": 50}, 50, remember=True)
        opened, resolved = duplicates.apply(state, [], {"1:de": 40, "2:de": 40}, 60, False)
        self.assertEqual((opened, resolved), ([], []))
        self.assertIn("1:de:", state["slots"])

    def test_forgetting_keeps_only_checks_newer_than_the_scan(self):
        state = duplicates.empty_state()
        state["checked"] = {"1:de": 10, "2:de": 30}
        duplicates.forget_checks_before(state, 20)
        self.assertEqual(state["checked"], {"2:de": 30})


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

    def test_a_long_locale_splits_across_embeds_and_messages(self):
        opened = [finding(i, identifier="x" * 80) for i in range(400)]
        messages = duplicates.build_messages(opened, [], 400, PROJECT)
        embeds = [e for m in messages for e in m["embeds"]]
        self.assertTrue(all(len(e["description"]) <= duplicates.MAX_DESC_CHARS
                            for e in embeds if "description" in e))
        self.assertTrue(all(sum(duplicates.embed_len(e) for e in m["embeds"])
                            <= duplicates.MAX_MESSAGE_CHARS for m in messages))
        self.assertEqual(sum(e["description"].count("\n• ") + 1 for e in embeds[1:]), 400)


def recording(changes=None):
    """The report's recording, with some strings' German translations replaced."""
    exchanges = copy.deepcopy(load_golden_json("report/responses.json")["exchanges"])
    for ex in exchanges:
        sid = (ex.get("params") or {}).get("stringId")
        if sid in (changes or {}):
            ex["response"]["json"]["data"] = [{"data": t} for t in changes[sid]]
    return exchanges


def translation(tid, text, cat=None):
    return {"id": tid, "text": text, "pluralCategoryName": cat, "user": None,
            "createdAt": "2026-09-10T00:00:00+00:00"}


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "duplicates.json")

    def run_reconcile(self, exchanges, *flags):
        session = RecordedSession(exchanges)
        out = io.StringIO()
        with mock.patch.object(reconcile, "crowdin_client",
                               lambda token, pid: sdk.client(token, pid, session=session)), \
                mock.patch.dict(os.environ, {"CROWDIN_API_TOKEN": "t"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            reconcile.main(["--state", self.state, "--locales", "de", *flags])
        return out.getvalue()

    def slots(self):
        with open(self.state, encoding="utf-8") as handle:
            return sorted(json.load(handle)["slots"])

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
        assert_golden(self, "reconcile/dry-run.json", self.run_reconcile(changed, "--dry-run"))
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
            reconcile.main(["--state", self.state, "--locales", "de", "--croql", "--seed"])
        checked = {params.get("stringId") for method, url, params, _ in session.calls
                   if url.endswith("/translations")}
        self.assertEqual(checked, {104, 105})
        self.assertEqual(self.slots(), ["104:de:other", "105:de:one", "105:de:other"])

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


class TestRelay(unittest.TestCase):
    SECRET = "s3cret"

    def setUp(self):
        self.client = TestClient(relay.app)
        self.checked = []
        patcher = mock.patch.object(relay, "check", lambda sid, lang: self.checked.append((sid, lang)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, secret, payload, env_secret=SECRET):
        with mock.patch.dict(os.environ, {"CROWDIN_WEBHOOK_SECRET": env_secret}):
            return self.client.post(f"/crowdin/suggestions/{secret}", json=payload)

    def event(self, sid=12, lang="uk", name="suggestion.added"):
        return {"event": name, "translation": {"sourceString": {"id": str(sid)},
                                               "targetLanguage": {"id": lang}}}

    def test_the_right_secret_acks_and_checks_the_slot(self):
        resp = self.post(self.SECRET, self.event())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.checked, [(12, "uk")])

    def test_a_wrong_secret_is_indistinguishable_from_no_route(self):
        self.assertEqual(self.post("guess", self.event()).status_code, 404)
        self.assertEqual(self.checked, [])

    def test_an_unset_secret_refuses_everything(self):
        self.assertEqual(self.post("", self.event(), env_secret="").status_code, 404)
        self.assertEqual(self.post("x", self.event(), env_secret="").status_code, 404)

    def test_a_batched_delivery_checks_each_scope_once(self):
        payload = {"events": [self.event(12, "uk"), self.event(12, "uk", "suggestion.deleted"),
                              self.event(13, "de"), {"event": "file.added"}]}
        self.post(self.SECRET, payload)
        self.assertEqual(sorted(self.checked), [(12, "uk"), (13, "de")])

    def test_the_older_string_key_is_read_too(self):
        event = {"event": "suggestion.updated",
                 "translation": {"string": {"id": 7}, "targetLanguage": {"id": "fr"}}}
        self.assertEqual(relay.scopes_from(event), {(7, "fr")})

    def test_an_unplaceable_event_is_acked_and_left_to_reconciliation(self):
        resp = self.post(self.SECRET, {"event": "suggestion.added", "translation": {}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.checked, [])


class TestRelayCheck(unittest.TestCase):
    def test_a_check_posts_what_changed_and_records_it(self):
        directory = tempfile.mkdtemp()
        state_path = os.path.join(directory, "duplicates.json")
        duplicates.save(state_path, duplicates.empty_state())
        api = FakeSession([
            FakeResponse({"data": {"id": 12, "identifier": "greeting", "text": "Hi"}}),
            FakeResponse({"data": [{"data": translation(1, "Hallo")},
                                   {"data": translation(2, "Servus")}]}),
            FakeResponse({"data": []}),
        ])
        webhook = FakeSession([FakeResponse({}, status_code=204)])
        env = {"CROWDIN_DUPLICATES_STATE": state_path, "CROWDIN_DISCORD_WEBHOOK_URL": "https://hook"}
        with mock.patch.object(relay, "crowdin",
                               lambda: (sdk.client("t", 1, session=api), PROJECT)), \
                mock.patch.object(relay.http, "Session", lambda: webhook), \
                mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            relay.check(12, "de")
        self.assertIn("greeting", webhook.calls[0][2]["json"]["embeds"][1]["description"])
        self.assertEqual(list(duplicates.load(state_path)["slots"]), ["12:de:"])
        self.assertIn("12:de", duplicates.load(state_path)["checked"])

    def test_an_unseeded_state_makes_the_relay_do_nothing(self):
        env = {"CROWDIN_DUPLICATES_STATE": "/nonexistent/duplicates.json"}
        with mock.patch.object(relay, "crowdin") as crowdin, mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            relay.check(12, "de")
        crowdin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
