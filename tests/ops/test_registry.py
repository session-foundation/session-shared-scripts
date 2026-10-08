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

    def test_a_watch_is_a_glob_under_the_state_directory(self):
        job = load(VALID + 'watch = "inbox/*.csv"\n')[0]
        self.assertEqual(job.watch_glob, "/var/lib/session-ops/a/inbox/*.csv")
        self.assertIsNone(load(VALID)[0].watch_glob)

    def test_a_watch_outside_the_state_directory_is_refused(self):
        for watch in ("/tmp/*.csv", "../b/*.csv"):
            with self.subTest(watch=watch), self.assertRaises(ValueError):
                load(VALID + f'watch = "{watch}"\n')

    def test_a_missing_field_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID.replace('user = "u"\n', ""))

    def test_a_scheduled_job_needs_a_max_age(self):
        with self.assertRaises(ValueError):
            load(VALID.replace("max_age_hours = 80\n", "") + 'schedule = "daily"\n')

    def test_a_queued_job_runs_after_the_one_listed_before_it(self):
        jobs = load(VALID + VALID.replace('"a"', '"b"') + QUEUE)
        self.assertEqual([(job.queued, job.after) for job in jobs], [(True, ()), (True, ("a",))])
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

    def test_a_job_is_offered_to_discord_only_when_it_says_so(self):
        self.assertFalse(load(VALID)[0].discord)
        self.assertTrue(load(VALID + "discord = true\n")[0].discord)

    def test_a_discord_flag_that_is_not_a_boolean_is_refused(self):
        with self.assertRaises(ValueError):
            load(VALID + 'discord = "yes"\n')

    def test_more_jobs_than_discord_offers_choices_for_is_refused(self):
        many = "".join(VALID.replace('"a"', f'"j{i}"') + "discord = true\n"
                       for i in range(registry.MAX_DISCORD_JOBS + 1))
        with self.assertRaises(ValueError):
            load(many)

    def test_a_timeout_is_read_as_a_systemd_time_span(self):
        self.assertEqual(load(VALID)[0].timeout_seconds, 1800)
        self.assertEqual(load(VALID + 'timeout = "1h 30min"\n')[0].timeout_seconds, 5400)
        self.assertEqual(registry.span_seconds("90"), 90)

    def test_a_timeout_systemd_would_not_read_is_refused(self):
        for timeout in ("soon", "2x", "1h later", ""):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                load(VALID + f'timeout = "{timeout}"\n')

    def test_the_shipped_registry_loads(self):
        names = [job.name for job in registry.load()]
        self.assertIn("zendesk-digest", names)


if __name__ == "__main__":
    unittest.main()
