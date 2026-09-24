import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import state as dedup  # noqa: E402

VERSION = 7


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "sub", "seen.json")

    def load(self, path=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return dedup.load_state(path or self.path, VERSION, "thing")

    def save(self, records, state=None, retention_days=30):
        with contextlib.redirect_stdout(io.StringIO()):
            return dedup.save_state(self.path, state or dedup.empty_state(VERSION),
                                    records, retention_days, VERSION)

    def test_a_record_comes_back_with_its_fields_and_a_stamp(self):
        self.save({"1": {"updated_at": "A"}})
        record = self.load()["seen"]["1"]
        self.assertEqual(record["updated_at"], "A")
        self.assertIn("last_reported", record)

    def test_a_later_save_replaces_the_record(self):
        self.save({"1": {"updated_at": "A"}})
        self.save({"1": {"updated_at": "B"}}, state=self.load())
        self.assertEqual(self.load()["seen"]["1"]["updated_at"], "B")

    def test_no_path_means_no_state_and_no_complaint(self):
        self.assertEqual(dedup.load_state(None, VERSION, "thing"), dedup.empty_state(VERSION))

    def test_a_missing_file_is_a_cache_miss(self):
        self.assertEqual(self.load(), dedup.empty_state(VERSION))

    def test_every_unreadable_state_is_a_cache_miss_not_an_error(self):
        """Losing it re-reports the window once. Failing the run instead would mean a
        corrupt cache stops the digest entirely."""
        path = os.path.join(self.directory.name, "flat.json")
        for content in ("{ not json", '{"version": %d}' % VERSION, "[]",
                        '{"version": 99, "seen": {}}', '{"seen": []}'):
            with self.subTest(content=content):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self.assertEqual(self.load(path), dedup.empty_state(VERSION))

    def test_the_right_version_is_loaded(self):
        path = os.path.join(self.directory.name, "flat.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": VERSION, "seen": {"1": {"updated_at": "A"}}}, handle)
        self.assertEqual(list(self.load(path)["seen"]), ["1"])

    def test_entries_are_pruned_past_the_retention(self):
        self.save({"1": {}})
        state = self.load()
        for record in state["seen"].values():
            record["last_reported"] = "2026-01-01T00:00:00Z"
        self.assertEqual(self.save({}, state=state), (0, 1))

    def test_a_fresh_entry_is_kept(self):
        self.assertEqual(self.save({"1": {}, "2": {}}), (2, 0))

    def test_a_malformed_entry_is_dropped_rather_than_kept_forever(self):
        state = {"version": VERSION, "seen": {"1": {"last_reported": "never"}}}
        kept, _ = self.save({}, state=state)
        self.assertEqual(kept, 0)

    def test_the_directory_is_created_and_the_write_is_atomic(self):
        self.save({"1": {}})
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["seen.json"])


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "seen.json")
        self.tracker = dedup.Tracker(
            VERSION, "thing", key_of=lambda t: str(t["id"]),
            activity_of=lambda t: t["touched"], field="touched",
            describe=lambda t: {"name": t.get("name", "")})

    def test_partition_against_what_was_reported(self):
        state = self.tracker.empty()
        with contextlib.redirect_stdout(io.StringIO()):
            self.tracker.save(self.path, state, [{"id": 1, "touched": "A"},
                                                 {"id": 2, "touched": "A"}], 30)
            state = self.tracker.load(self.path)
        new, changed, unchanged = self.tracker.partition(
            [{"id": 1, "touched": "B"}, {"id": 2, "touched": "A"}, {"id": 3, "touched": "A"}],
            state)
        self.assertEqual([t["id"] for t in new], [3])
        self.assertEqual([t["id"] for t in changed], [1])
        self.assertEqual([t["id"] for t in unchanged], [2])

    def test_describe_fields_are_written_beside_the_activity(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.tracker.save(self.path, self.tracker.empty(),
                              [{"id": 1, "touched": "A", "name": "one"}], 30)
            record = self.tracker.load(self.path)["seen"]["1"]
        self.assertEqual(record["touched"], "A")
        self.assertEqual(record["name"], "one")
        self.assertIn("last_reported", record)


if __name__ == "__main__":
    unittest.main()
