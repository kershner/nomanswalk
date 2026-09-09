import unittest

from nms_bot import get_coarse_player_state


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


if __name__ == "__main__":
    unittest.main()
