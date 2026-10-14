import unittest
from unittest import mock

import nms_bot


SAMPLE_STATE = {
    "timestamp": 1791945000,
    "universe_address": {
        "voxel_x": 1082,
        "voxel_y": -89,
        "voxel_z": 1951,
        "solar_system_index": 367,
        "reality_index": 82,
        "galaxy_name": "Dimonimba",
        "planet_index": 1,
    },
    "environment": {"inside_atmosphere": True},
    "planet": {
        "name": "New Hoca",
        "biome": "Radioactive",
        "weather_label": "Roaring Nuclear Wind",
        "flora_label": "Generous",
        "fauna_label": "High",
        "sentinel_label": "Irregular Patrols",
        "has_rings": False,
    },
}


class DailyPlanetReportTests(unittest.TestCase):
    def setUp(self):
        self.original_stats = nms_bot._daily_stats
        self.original_position = nms_bot._daily_last_position
        self.original_planet = nms_bot._daily_last_planet
        self.original_state = nms_bot._daily_last_state
        nms_bot._daily_stats = nms_bot._new_daily_stats()
        nms_bot._daily_last_position = None
        nms_bot._daily_last_planet = None
        nms_bot._daily_last_state = None

    def tearDown(self):
        nms_bot._daily_stats = self.original_stats
        nms_bot._daily_last_position = self.original_position
        nms_bot._daily_last_planet = self.original_planet
        nms_bot._daily_last_state = self.original_state

    def test_builds_expected_portal_hex(self):
        self.assertEqual(nms_bot._planet_hex(SAMPLE_STATE), "216FA779F43A")

    @mock.patch.object(nms_bot, "_save_daily_stats")
    def test_tracks_each_planet_once_with_flat_report_shape(self, _save):
        nms_bot.update_daily_movement("ON_FOOT", SAMPLE_STATE)
        nms_bot.update_daily_movement("ON_FOOT", SAMPLE_STATE)

        self.assertEqual(
            nms_bot.get_daily_planets_payload(),
            {
                "planets": [
                    {
                        "timestamp": "2026-10-13T22:30:00-04:00",
                        "planet_hex": "216FA779F43A",
                        "name": "New Hoca",
                        "galaxy_name": "Dimonimba",
                        "galaxy_index": 83,
                        "biome": "Radioactive",
                        "weather": "Roaring Nuclear Wind",
                        "flora": "Generous",
                        "fauna": "High",
                        "sentinels": "Irregular Patrols",
                        "has_rings": False,
                    }
                ]
            },
        )

    def test_post_uses_bearer_authorization_and_json_body(self):
        report = {"planets": []}
        with (
            mock.patch.object(nms_bot, "get_daily_planets_payload", return_value=report),
            mock.patch.object(nms_bot.requests, "post") as post,
        ):
            payload = nms_bot.post_daily_planets("https://example.test/planets", "secret")

        self.assertEqual(payload, report)
        post.assert_called_once_with(
            "https://example.test/planets",
            json=report,
            headers={"Authorization": "Bearer secret"},
            timeout=15,
        )
        post.return_value.raise_for_status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
