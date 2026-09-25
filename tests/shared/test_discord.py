import contextlib
import io
import unittest
from urllib.parse import parse_qsl, urlsplit

from session_ops.shared import discord
from session_ops.shared.testing import FakeResponse, FakeSession


class TestComponentsWebhookUrl(unittest.TestCase):
    """Discord ignores `components` on a webhook post without this param, and a
    digest is nothing but components."""

    def test_adds_the_param(self):
        self.assertEqual(
            discord.components_webhook_url("https://discord.com/api/webhooks/1/tok"),
            "https://discord.com/api/webhooks/1/tok?with_components=true")

    def test_keeps_an_existing_query(self):
        url = discord.components_webhook_url(
            "https://discord.com/api/webhooks/1/tok?thread_id=42")
        self.assertEqual(dict(parse_qsl(urlsplit(url).query)),
                         {"thread_id": "42", "with_components": "true"})

    def test_does_not_duplicate_the_param(self):
        once = discord.components_webhook_url("https://discord.com/api/webhooks/1/tok")
        self.assertEqual(discord.components_webhook_url(once), once)


class TestPostToDiscord(unittest.TestCase):
    """Returns the accepted count rather than exiting, so a caller can record exactly
    what landed before signalling the failure."""

    def post(self, session, messages):
        with contextlib.redirect_stdout(io.StringIO()):
            return discord.post_to_discord(session, "https://hook", messages)

    def test_all_accepted(self):
        session = FakeSession([FakeResponse({}, status_code=204)] * 3)
        self.assertEqual(self.post(session, [{}, {}, {}]), 3)
        self.assertEqual(len(session.calls), 3)

    def test_stops_at_the_first_failure_and_reports_the_prefix(self):
        session = FakeSession([
            FakeResponse({}, status_code=204),
            FakeResponse({}, status_code=400),
        ])
        self.assertEqual(self.post(session, [{}, {}, {}]), 1)

    def test_does_not_post_after_a_failure(self):
        session = FakeSession([FakeResponse({}, status_code=404)])
        self.post(session, [{}, {}, {}])
        self.assertEqual(len(session.calls), 1)

    def test_first_message_failing_reports_zero(self):
        session = FakeSession([FakeResponse({}, status_code=500)])
        self.assertEqual(self.post(session, [{}]), 0)

    def test_an_unreachable_webhook_reports_the_prefix_rather_than_raising(self):
        import requests
        session = FakeSession([FakeResponse({}, status_code=204)]
                              + [requests.ConnectionError("down")])
        self.assertEqual(self.post(session, [{}, {}]), 1)

    def test_no_messages_is_zero(self):
        self.assertEqual(self.post(FakeSession([]), []), 0)


class TestChunkEntries(unittest.TestCase):
    def test_splits_on_the_entry_count(self):
        entries = [("x", {i}) for i in range(11)]
        self.assertEqual([len(c) for c in discord.chunk_entries(entries, 10)], [10, 1])

    def test_splits_on_the_character_budget(self):
        entries = [("x" * 2500, {1}), ("y" * 2500, {2})]
        self.assertEqual(len(discord.chunk_entries(entries, 10)), 2)

    def test_the_header_is_charged_to_the_first_message_only(self):
        entries = [("x" * 1900, {1}), ("x" * 1900, {2})]
        self.assertEqual(len(discord.chunk_entries(entries, 10, first_used=0)), 1)
        self.assertEqual(len(discord.chunk_entries(entries, 10, first_used=1000)), 2)

    def test_an_oversized_entry_still_gets_a_message(self):
        entries = [("x" * (discord.MAX_MESSAGE_TEXT_CHARS + 100), {1})]
        self.assertEqual(len(discord.chunk_entries(entries, 10)), 1)

    def test_entries_come_back_by_identity(self):
        entry = ("x", {1})
        self.assertIs(discord.chunk_entries([entry], 10)[0][0], entry)


class TestMessagesFromEntries(unittest.TestCase):
    def texts(self, message):
        return [c["content"] for c in message["components"][0]["components"]
                if c["type"] == discord.TEXT_DISPLAY]

    def test_no_entries_still_yields_the_header(self):
        messages, coverage = discord.messages_from_entries("header", [], 10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.texts(messages[0]), ["header"])
        self.assertEqual(coverage, [set()])
        self.assertEqual(len(messages[0]["components"][0]["components"]), 1)  # no separator

    def test_the_payload_is_a_components_v2_container(self):
        message = discord.messages_from_entries("h", [("a", {1})], 10)[0][0]
        self.assertEqual(message["flags"], discord.COMPONENTS_V2_FLAG)
        self.assertEqual(message["components"][0]["type"], discord.CONTAINER)
        types = [c["type"] for c in message["components"][0]["components"]]
        self.assertEqual(types, [discord.TEXT_DISPLAY, discord.SEPARATOR, discord.TEXT_DISPLAY])

    def test_only_the_first_message_carries_the_header(self):
        entries = [(f"e{i}", {i}) for i in range(3)]
        messages, coverage = discord.messages_from_entries("header", entries, 2)
        self.assertEqual(self.texts(messages[0]), ["header", "e0", "e1"])
        self.assertEqual(self.texts(messages[1]), ["e2"])
        self.assertEqual(coverage, [{0, 1}, {2}])

    def test_the_header_counts_against_the_first_budget(self):
        entries = [("x" * 2000, {1}), ("y" * 1500, {2})]
        messages, _ = discord.messages_from_entries("h" * 1000, entries, 10)
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
