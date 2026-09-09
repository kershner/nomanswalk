import unittest

from nms_bot import STATE_MAX_AGE_SECONDS, get_coarse_player_state, is_state_snapshot_fresh


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

if __name__ == "__main__":
    unittest.main()
