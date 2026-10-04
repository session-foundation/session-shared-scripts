"""
    uv run python -m unittest tests.ops.test_registry
"""
import os
import tempfile
import unittest

from session_ops.ops import registry

VALID = '''
[[job]]
name = "a"
description = "A"
entry = "m:f"
user = "u"
env_files = ["/etc/a.env"]
args = ["--state", "{state}/s.json"]
max_age_hours = 80
'''
QUEUE = '''
[queue]
schedule = "Mon..Fri 10:00 Australia/Melbourne"
jobs = ["a", "b"]
'''


def load(text):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
        handle.write(text)
    try:
        return registry.load(handle.name)
    finally:
        os.unlink(handle.name)


class TestRegistry(unittest.TestCase):
    def test_state_is_substituted_and_dry_run_appended(self):
        job = load(VALID)[0]
        self.assertEqual(job.argv(), ["--state", "/var/lib/session-ops/a/s.json"])
        self.assertEqual(job.argv(dry_run=True, state_dir="/x"),
                         ["--state", "/x/s.json", "--dry-run"])

    def test_a_missing_field_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID.replace('user = "u"\n', ""))

    def test_a_scheduled_job_needs_a_max_age(self):
        with self.assertRaises(ValueError):
            load(VALID.replace("max_age_hours = 80\n", "") + 'schedule = "daily"\n')

    def test_a_queued_job_runs_after_the_one_listed_before_it(self):
        jobs = load(VALID + VALID.replace('"a"', '"b"') + QUEUE)
        self.assertEqual([(job.queued, job.after) for job in jobs], [(True, None), (True, "a")])
        self.assertTrue(all(job.scheduled for job in jobs))
        self.assertEqual(jobs[1].timer, "session-ops-queue.timer")

    def test_a_queued_job_needs_a_max_age(self):
        with self.assertRaises(ValueError):
            load(VALID.replace("max_age_hours = 80\n", "") + QUEUE.replace(', "b"', ""))

    def test_a_queued_job_with_its_own_schedule_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID + 'schedule = "daily"\n' + QUEUE.replace(', "b"', ""))

    def test_a_queue_naming_an_unknown_job_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID + QUEUE)

    def test_a_name_listed_twice_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID + VALID)

    def test_the_shipped_registry_loads(self):
        names = [job.name for job in registry.load()]
        self.assertIn("zendesk-digest", names)


if __name__ == "__main__":
    unittest.main()
