import contextlib
import io
import json
import os
import tempfile
import unittest

from session_ops.shared import state as dedup

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

    def test_a_state_file_that_is_not_utf8_is_a_cache_miss(self):
        path = os.path.join(self.directory.name, "binary.json")
        with open(path, "wb") as handle:
            handle.write(b'{"version": 7, "seen": {"\xff": {}}}')
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

    def test_a_record_that_is_not_an_object_is_dropped_rather_than_failing_the_save(self):
        """The save runs after the post, so failing it would repost next run."""
        state = {"version": VERSION, "seen": {"1": "stray", "2": None, "3": [1]}}
        self.assertEqual(self.save({"4": {}}, state=state), (1, 3))
        self.assertEqual(list(self.load()["seen"]), ["4"])

    def test_without_extra_fields_the_file_holds_only_the_version_the_stamp_and_seen(self):
        self.save({"1": {}})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(sorted(json.load(handle)), ["seen", "updated_at", "version"])

    def test_extra_fields_are_written_beside_seen_and_read_back(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dedup.save_state(self.path, dedup.empty_state(VERSION), {"1": {}}, 30, VERSION,
                             extra={"covered_until": "2026-09-24T00:00:00Z"})
        state = self.load()
        self.assertEqual(state["covered_until"], "2026-09-24T00:00:00Z")
        self.assertEqual(list(state["seen"]), ["1"])

    def test_the_directory_is_created_and_the_write_is_atomic(self):
        self.save({"1": {}})
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["seen.json"])


if __name__ == "__main__":
    unittest.main()
