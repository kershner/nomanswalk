import unittest
from unittest import mock
import tempfile
from pathlib import Path

import nms_bot
import planet_reports


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
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(planet_reports, "OUTBOX_FILE", Path(directory.name) / "outbox.json")
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_unknown_rings_are_omitted(self):
        data = dict(SAMPLE_STATE, planet=dict(SAMPLE_STATE["planet"]))
        del data["planet"]["has_rings"]
        self.assertNotIn("has_rings", nms_bot._planet_report_item(data))

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
        report = {"planets": [nms_bot._planet_report_item(SAMPLE_STATE)]}
        with (
            mock.patch.object(nms_bot, "get_daily_planets_payload", return_value=report),
            mock.patch.object(planet_reports.requests, "post") as post,
        ):
            post.return_value.status_code = 200
            post.return_value.text = '{"planets_created":1,"planets_updated":0,"visits_created":1}'
            payload = nms_bot.post_daily_planets("https://example.test/planets", "secret")

        self.assertEqual(payload, report)
        post.assert_called_once_with(
            "https://example.test/planets",
            json=report,
            headers={"Authorization": "Bearer secret"},
            timeout=15,
            allow_redirects=False,
        )

    @mock.patch.object(nms_bot, "_save_daily_stats")
    def test_retry_after_rollover_and_reload_preserves_first_visit(self, _save):
        nms_bot.update_daily_movement("ON_FOOT", SAMPLE_STATE)
        original = nms_bot.get_daily_planets_payload()
        with mock.patch.object(planet_reports.requests, "post", side_effect=TimeoutError("offline")):
            with self.assertRaises(TimeoutError):
                nms_bot.post_daily_planets("https://example.test", "secret")
        with mock.patch.object(nms_bot, "_today", return_value="2099-01-01"):
            nms_bot._ensure_daily_stats()
            with mock.patch.object(nms_bot, "_read_runtime_state", return_value={"daily": nms_bot._daily_stats}):
                nms_bot._load_daily_stats()
            with mock.patch.object(planet_reports.requests, "post") as post:
                post.return_value.status_code = 200
                post.return_value.text = '{}'
                nms_bot.post_daily_planets("https://example.test", "secret")
        self.assertEqual(post.call_args.kwargs["json"], original)


if __name__ == "__main__":
    unittest.main()
