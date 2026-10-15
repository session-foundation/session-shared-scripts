"""
    uv run python -m unittest tests.store_versions.test_transitions
"""
import unittest

from session_ops.store_versions.transitions import (
    ALL_CLEAR, INFO_GITHUB, INFO_STORE, SOURCE_FAILING, SOURCE_RECOVERED, WARNING,
    WARNING_LOWERED, WARNING_REMINDER, WARNING_UPDATE, evaluate, source_events)

DAY = 12 * 3600


def seen(github, store, warning=False, warned_at=None, store_high=None):
    return {"github": github, "store": store, "warning": warning, "warned_at": warned_at,
            "store_high": store_high}


def kinds(result):
    return [event.kind for event in result.events]


class TestFirstCheck(unittest.TestCase):
    def test_in_sync_or_github_ahead_is_quiet(self):
        for github, store in (("1.0.0", "1.0.0"), ("1.1.0", "1.0.0")):
            result = evaluate(None, github, store)
            self.assertEqual(kinds(result), [])
            self.assertFalse(result.state["warning"])

    def test_store_ahead_warns_as_first_check(self):
        result = evaluate(None, "1.0.0", "1.1.0", now=50.0)
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.events[0].initial)
        self.assertEqual(result.state["warned_at"], 50.0)

    def test_strict_sync_behind_warns(self):
        result = evaluate(None, "1.1.0", "1.0.0", strict_sync=True)
        self.assertEqual(kinds(result), [WARNING])


class TestTransitions(unittest.TestCase):
    def test_no_movement_is_quiet(self):
        self.assertEqual(kinds(evaluate(seen("1.0.0", "1.0.0"), "1.0.0", "1.0.0")), [])

    def test_store_jumps_ahead_warns(self):
        result = evaluate(seen("1.0.0", "1.0.0"), "1.0.0", "1.1.0")
        self.assertEqual(kinds(result), [WARNING])
        self.assertTrue(result.state["warning"])

    def test_store_further_ahead_while_warning_updates(self):
        result = evaluate(seen("1.0.0", "1.1.0", warning=True, warned_at=0.0), "1.0.0", "1.2.0")
        self.assertEqual(kinds(result), [WARNING_UPDATE])

    def test_github_catching_up_or_passing_clears(self):
        for github in ("1.1.0", "1.2.0"):
            result = evaluate(seen("1.0.0", "1.1.0", warning=True, warned_at=0.0), github, "1.1.0")
            self.assertEqual(kinds(result), [ALL_CLEAR])
            self.assertFalse(result.state["warning"])
            self.assertIsNone(result.state["warned_at"])

    def test_github_advancing_is_info(self):
        result = evaluate(seen("1.0.0", "1.0.0"), "1.1.0", "1.0.0")
        self.assertEqual(kinds(result), [INFO_GITHUB])

    def test_store_catching_up_is_info(self):
        result = evaluate(seen("1.2.0", "1.0.0"), "1.2.0", "1.1.0")
        self.assertEqual(kinds(result), [INFO_STORE])

    def test_both_advancing_store_ahead_only_warns(self):
        result = evaluate(seen("1.0.0", "1.0.0"), "1.1.0", "1.2.0")
        self.assertEqual(kinds(result), [WARNING])

    def test_unchanged_warning_is_not_repeated(self):
        prev = seen("1.0.0", "1.1.0", warning=True, warned_at=0.0)
        self.assertEqual(kinds(evaluate(prev, "1.0.0", "1.1.0", now=60.0)), [])


class TestStrictSync(unittest.TestCase):
    def test_behind_warns_only_under_strict_sync(self):
        self.assertEqual(kinds(evaluate(seen("1.0.0", "1.0.0"), "1.1.0", "1.0.0",
                                        strict_sync=True)), [WARNING])
        self.assertEqual(kinds(evaluate(seen("1.0.0", "1.0.0"), "1.1.0", "1.0.0")),
                         [INFO_GITHUB])

    def test_github_pulling_further_ahead_updates(self):
        prev = seen("1.1.0", "1.0.0", warning=True, warned_at=100.0)
        result = evaluate(prev, "1.2.0", "1.0.0", strict_sync=True)
        self.assertEqual(kinds(result), [WARNING_UPDATE])

    def test_catching_up_clears(self):
        prev = seen("1.1.0", "1.0.0", warning=True, warned_at=100.0)
        self.assertEqual(kinds(evaluate(prev, "1.1.0", "1.1.0", strict_sync=True)), [ALL_CLEAR])


class TestReminder(unittest.TestCase):
    def test_repeats_after_the_interval_and_resets_its_clock(self):
        prev = seen("1.0.0", "1.1.0", warning=True, warned_at=0.0)
        result = evaluate(prev, "1.0.0", "1.1.0", now=DAY, reminder_seconds=DAY)
        self.assertEqual(kinds(result), [WARNING_REMINDER])
        self.assertEqual(result.state["warned_at"], DAY)

    def test_quiet_before_the_interval(self):
        prev = seen("1.0.0", "1.1.0", warning=True, warned_at=0.0)
        result = evaluate(prev, "1.0.0", "1.1.0", now=DAY - 1, reminder_seconds=DAY)
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.state["warned_at"], 0.0)

    def test_growing_gap_resets_the_clock(self):
        prev = seen("1.0.0", "1.1.0", warning=True, warned_at=0.0)
        result = evaluate(prev, "1.0.0", "1.2.0", now=5000.0, reminder_seconds=DAY)
        self.assertEqual(kinds(result), [WARNING_UPDATE])
        self.assertEqual(result.state["warned_at"], 5000.0)


class TestStoreHigh(unittest.TestCase):
    def test_recovering_to_an_announced_version_is_quiet(self):
        result = evaluate(seen("2.15.4", "2.15.3", store_high="2.15.4"), "2.15.4", "2.15.4")
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.state["store_high"], "2.15.4")

    def test_new_high_is_announced(self):
        result = evaluate(seen("2.15.5", "2.15.4", store_high="2.15.4"), "2.15.5", "2.15.5")
        self.assertEqual(kinds(result), [INFO_STORE])

    def test_dip_is_quiet_and_keeps_the_true_version(self):
        result = evaluate(seen("2.15.4", "2.15.4", store_high="2.15.4"), "2.15.4", "2.15.3")
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.state["store"], "2.15.3")
        self.assertEqual(result.state["store_high"], "2.15.4")

    def test_flap_announces_once(self):
        state, posted = seen("2.15.4", "2.15.4"), []
        for store in ("2.15.5", "2.15.4", "2.15.5", "2.15.4", "2.15.5"):
            result = evaluate(state, "2.15.5", store)
            posted += kinds(result)
            state = result.state
        self.assertEqual(posted.count(INFO_STORE), 1)

    def test_store_flapping_ahead_warns_once(self):
        state, posted = None, []
        for store in ("2.15.6", "2.15.5", "2.15.6", "2.15.5", "2.15.6"):
            result = evaluate(state, "2.15.5", store)
            posted += kinds(result)
            state = result.state
        self.assertEqual(posted, [WARNING])

    def test_store_flapping_two_ahead_does_not_update_the_warning(self):
        state = evaluate(None, "2.15.5", "2.15.7").state
        for store in ("2.15.6", "2.15.7", "2.15.6"):
            result = evaluate(state, "2.15.5", store)
            self.assertEqual(kinds(result), [])
            state = result.state

    def test_strict_sync_dip_after_a_merge_stays_in_sync(self):
        state = evaluate(seen("1.18.1", "1.18.1"), "1.18.2", "1.18.2", strict_sync=True).state
        result = evaluate(state, "1.18.2", "1.18.1", strict_sync=True)
        self.assertEqual(kinds(result), [])


class TestWatermark(unittest.TestCase):
    HOURS = 3600

    def run_sequence(self, steps, strict_sync=False):
        state, posted = None, []
        for at_hour, github, store in steps:
            result = evaluate(state, github, store, strict_sync=strict_sync,
                              now=at_hour * self.HOURS)
            posted.append(kinds(result))
            state = result.state
        return posted, state

    def test_a_pulled_release_warns_once_the_store_has_served_lower_for_six_hours(self):
        posted, state = self.run_sequence(
            [(0, "1.15", "1.15"), (1, "1.15", "1.16"), (2, "1.15", "1.15"),
             (3, "1.16", "1.15"), (7.9, "1.16", "1.15"), (8, "1.16", "1.15")],
            strict_sync=True)
        self.assertEqual(posted, [[], [WARNING], [], [ALL_CLEAR], [], [WARNING]])
        self.assertEqual(state["store_high"], "1.15")

    def test_a_bad_read_clears_after_six_hours_back_at_the_real_version(self):
        posted, state = self.run_sequence(
            [(0, "1.20", "1.20"), (1, "1.20", "8.0"), (2, "1.20", "1.20"),
             (8, "1.20", "1.20")])
        self.assertEqual(posted, [[], [WARNING], [], [ALL_CLEAR]])
        self.assertEqual((state["store_high"], state["low_since"]), ("1.20", None))

    def test_a_short_dip_stays_quiet_and_resets_the_clock(self):
        posted, state = self.run_sequence(
            [(0, "2.15.5", "2.15.5"), (1, "2.15.5", "2.15.4"), (2, "2.15.5", "2.15.5"),
             (3, "2.15.5", "2.15.4"), (8, "2.15.5", "2.15.4")])
        self.assertEqual(posted, [[], [], [], [], []])
        self.assertEqual((state["store_high"], state["low_since"]), ("2.15.5", 3 * self.HOURS))

    def test_a_state_from_before_the_clock_loads(self):
        result = evaluate(seen("2.15.4", "2.15.4", store_high="2.15.4"), "2.15.4", "2.15.3",
                          now=100.0)
        self.assertEqual(result.state["low_since"], 100.0)


    def test_a_lowered_watermark_updates_a_warning_that_flips_direction(self):
        posted, state = [], None
        for hours, store in ((0, "1.6"), (1, "1.7"), (2, "1.5"), (8, "1.5")):
            result = evaluate(state, "1.6", store, strict_sync=True, now=hours * 3600,
                              reminder_seconds=DAY)
            posted.append(kinds(result))
            state = result.state
        self.assertEqual(posted, [[], [WARNING], [], [WARNING_LOWERED]])
        self.assertEqual((result.events[0].store, result.state["warned_at"]), ("1.5", 8 * 3600))

    def test_a_lowered_watermark_updates_a_shrinking_gap(self):
        state = evaluate(None, "1.6", "1.8", now=0).state
        state = evaluate(state, "1.6", "1.7", now=3600).state
        result = evaluate(state, "1.6", "1.7", now=7 * 3600)
        self.assertEqual(kinds(result), [WARNING_LOWERED])
        self.assertEqual(result.events[0].store, "1.7")

class TestPrereleases(unittest.TestCase):
    def test_a_store_shipping_a_prerelease_is_in_sync(self):
        result = evaluate(None, "1.32.2", "1.33.0", prereleases=["1.33.0"])
        self.assertEqual(kinds(result), [])
        self.assertEqual(result.state["github"], "1.33.0")

    def test_strict_sync_on_the_release_below_a_prerelease_is_in_sync(self):
        result = evaluate(None, "1.17.12", "1.17.12", prereleases=["1.17.13"], strict_sync=True)
        self.assertEqual(kinds(result), [])

    def test_a_store_past_every_prerelease_warns(self):
        result = evaluate(None, "1.32.2", "1.33.1", prereleases=["1.33.0"])
        self.assertEqual(kinds(result), [WARNING])
        self.assertEqual(result.events[0].github, "1.32.2")


class TestSourceEvents(unittest.TestCase):
    def call(self, prev, error, now):
        return source_events(prev, error, now=now, after_seconds=3600, reminder_seconds=DAY)

    def test_a_short_failure_and_its_recovery_are_quiet(self):
        kind, state = self.call(None, "boom", 0.0)
        self.assertIsNone(kind)
        kind, state = self.call(state, "boom", 3599.0)
        self.assertIsNone(kind)
        self.assertEqual(self.call(state, None, 3700.0), (None, None))

    def test_reported_after_an_hour_then_every_reminder(self):
        _, state = self.call(None, "boom", 0.0)
        kind, state = self.call(state, "boom", 3600.0)
        self.assertEqual(kind, SOURCE_FAILING)
        self.assertEqual(self.call(state, "boom", 3600.0 + DAY - 1)[0], None)
        self.assertEqual(self.call(state, "boom", 3600.0 + DAY)[0], SOURCE_FAILING)

    def test_recovery_after_a_report_is_posted(self):
        _, state = self.call(None, "boom", 0.0)
        _, state = self.call(state, "boom", 3600.0)
        self.assertEqual(self.call(state, None, 4000.0), (SOURCE_RECOVERED, None))
