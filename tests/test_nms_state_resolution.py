import unittest

from nms_bot import (
    NMSState,
    STATE_MAX_AGE_SECONDS,
    _game_reports_autowalking,
    _is_in_cave,
    get_coarse_player_state,
    is_state_snapshot_fresh,
)


class CoarsePlayerStateTests(unittest.TestCase):
    def test_valid_on_foot_location_recovers_from_stale_unknown_state(self):
        data = {
            "state": "UNKNOWN",
            "environment": {
                "location_stable_raw": 3,
                "location_stable": "PlanetOnFoot",
            },
        }

        self.assertEqual(get_coarse_player_state(data), "ON_FOOT")

    def test_unknown_without_valid_location_fails_closed(self):
        data = {
            "state": "UNKNOWN",
            "environment": {"location_stable_raw": 0, "location_stable": "None_"},
        }

        self.assertEqual(get_coarse_player_state(data), "NOT_ON_FOOT")

    def test_explicit_cockpit_state_wins_over_on_foot_location(self):
        data = {
            "state": "IN_COCKPIT",
            "environment": {"location_stable": "PlanetOnFoot"},
        }

        self.assertEqual(get_coarse_player_state(data), "NOT_ON_FOOT")


class StateSnapshotFreshnessTests(unittest.TestCase):
    def test_recent_snapshot_is_fresh(self):
        self.assertTrue(is_state_snapshot_fresh({"timestamp": 100.0}, now=105.0))

    def test_frozen_snapshot_is_stale(self):
        self.assertFalse(
            is_state_snapshot_fresh(
                {"timestamp": 100.0},
                now=100.0 + STATE_MAX_AGE_SECONDS + 0.001,
            )
        )

    def test_missing_or_invalid_timestamp_is_stale(self):
        self.assertFalse(is_state_snapshot_fresh({}, now=100.0))
        self.assertFalse(is_state_snapshot_fresh({"timestamp": "bad"}, now=100.0))


class CaveStateTests(unittest.TestCase):
    def _set_environment(self, environment):
        NMSState.update("ON_FOOT", 1.0, {"environment": environment})

    def test_independent_cave_signal_wins_over_planet_on_foot_location(self):
        self._set_environment(
            {
                "location": "PlanetOnFoot",
                "location_stable": "PlanetOnFoot",
                "is_in_cave": True,
            }
        )

        self.assertTrue(_is_in_cave())

    def test_stable_cave_location_is_accepted(self):
        self._set_environment(
            {
                "location": "PlanetOnFoot",
                "location_stable": "Cave",
                "is_in_cave": False,
            }
        )

        self.assertTrue(_is_in_cave())

    def test_surface_location_is_not_a_cave(self):
        self._set_environment(
            {
                "location": "PlanetOnFoot",
                "location_stable": "PlanetOnFoot",
                "is_in_cave": False,
            }
        )

        self.assertFalse(_is_in_cave())


class AutowalkConfirmationTests(unittest.TestCase):
    def test_game_autowalk_flag(self):
        self.assertFalse(
            _game_reports_autowalking({"movement": {"is_auto_walking": False}})
        )
        self.assertTrue(
            _game_reports_autowalking({"movement": {"is_auto_walking": True}})
        )

if __name__ == "__main__":
    unittest.main()
