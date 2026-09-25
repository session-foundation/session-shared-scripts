"""
    python -m unittest discover        # from deploy/
"""
import os
import tempfile
import unittest

import silence

HOUR = 3600
NOW = 1_790_000_000.0
JOB = {"name": "github-prs-digest", "max_age_hours": 80}


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.stamps = tempfile.mkdtemp()

    def stamp(self, name, age_hours):
        path = os.path.join(self.stamps, name)
        open(path, "w").close()
        os.utime(path, (NOW - age_hours * HOUR, NOW - age_hours * HOUR))

    def test_a_stamp_within_its_max_age_is_quiet(self):
        self.stamp(JOB["name"], 79)
        self.assertEqual(silence.evaluate([JOB], self.stamps, {}, NOW), ([], []))

    def test_a_stale_stamp_is_due(self):
        self.stamp(JOB["name"], 81)
        silent, due = silence.evaluate([JOB], self.stamps, {}, NOW)
        self.assertEqual(due, silent)
        self.assertEqual(due[0][1], NOW - 81 * HOUR)

    def test_an_alerted_job_is_repeated_once_a_day_not_every_hour(self):
        self.stamp(JOB["name"], 100)
        state = {JOB["name"]: {"alerted_at": NOW - 23 * HOUR}}
        silent, due = silence.evaluate([JOB], self.stamps, state, NOW)
        self.assertEqual((len(silent), due), (1, []))
        state[JOB["name"]]["alerted_at"] = NOW - 24 * HOUR
        self.assertEqual(len(silence.evaluate([JOB], self.stamps, state, NOW)[1]), 1)

    def test_recovery_clears_the_alert_so_the_next_silence_alerts_at_once(self):
        self.stamp(JOB["name"], 1)
        state = {JOB["name"]: {"alerted_at": NOW - HOUR}}
        silence.evaluate([JOB], self.stamps, state, NOW)
        self.assertNotIn("alerted_at", state[JOB["name"]])

    def test_a_missing_stamp_is_timed_from_when_it_was_first_found_missing(self):
        """Installing the checker must not alert on jobs that have not run since."""
        state = {}
        self.assertEqual(silence.evaluate([JOB], self.stamps, state, NOW), ([], []))
        self.assertEqual(state[JOB["name"]]["missing_since"], NOW)
        later = NOW + 81 * HOUR
        silent, due = silence.evaluate([JOB], self.stamps, state, later)
        self.assertEqual(due, [(JOB, None, NOW)])

    def test_the_first_success_clears_the_missing_clock(self):
        state = {JOB["name"]: {"missing_since": NOW - 10 * HOUR}}
        self.stamp(JOB["name"], 0)
        silence.evaluate([JOB], self.stamps, state, NOW)
        self.assertEqual(state[JOB["name"]], {})

    def test_a_job_dropped_from_the_registry_is_forgotten(self):
        state = {"retired-job": {"missing_since": NOW}}
        silence.evaluate([JOB], self.stamps, state, NOW)
        self.assertNotIn("retired-job", state)


class TestMessage(unittest.TestCase):
    def test_names_the_job_its_silence_and_where_to_look(self):
        message = silence.build_message([(JOB, NOW - 100 * HOUR, NOW - 100 * HOUR)],
                                        "box", NOW)
        self.assertIn("`box`", message)
        self.assertIn("**github-prs-digest**: silent for **4d 4h** (allowed 80h)", message)
        self.assertIn("last success 2026-09-", message)
        self.assertIn("systemctl list-timers github-prs-digest.timer", message)
        self.assertIn("journalctl -u github-prs-digest.service", message)

    def test_a_job_that_never_succeeded_says_since_when_it_was_watched(self):
        message = silence.build_message([(JOB, None, NOW - 90 * HOUR)], "box", NOW)
        self.assertIn("no success recorded since", message)


class TestRegistry(unittest.TestCase):
    def test_the_shipped_registry_matches_the_shipped_units(self):
        """A job listed without a unit, or a timer missing from the list, is silence
        nobody would detect."""
        here = os.path.dirname(os.path.abspath(__file__))
        names = {job["name"] for job in silence.load_jobs(silence.REGISTRY)}
        timers = {f[:-len(".timer")] for f in os.listdir(here)
                  if f.endswith(".timer") and f != "session-ops-silence.timer"}
        self.assertEqual(names, timers)
        for name in names:
            with open(os.path.join(here, f"{name}.service"), encoding="utf-8") as unit:
                self.assertIn(f"ExecStartPost=+/usr/bin/touch {silence.STAMPS_DIR}/{name}\n",
                              unit.read())


if __name__ == "__main__":
    unittest.main()
