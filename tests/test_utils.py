import unittest
from unittest import mock

import utils


class InfoTextTests(unittest.TestCase):
    def test_planet_details_are_localized_and_description_token_is_expanded(self):
        state = {
            "state": "ON_FOOT",
            "universe_address": {
                "voxel_x": 1082,
                "voxel_y": -89,
                "voxel_z": 1951,
                "solar_system_index": 367,
                "planet_index": 0,
            },
            "planet": {
                "name": "Onoeleto Prime",
                "description": "Airless %PLANETCLASS%",
                "planet_type": "Planet",
                "weather_label": "Perfectly Clear",
                "flora_label": "Barren",
                "fauna_label": "Absent",
                "resources_label": "Intermittent",
                "sentinel_label": "Intermittent",
            },
        }
        stats = {
            "distance_walked": 0,
            "planets_visited": 0,
            "walkers": 0,
            "commands": 0,
        }

        with (
            mock.patch.object(utils, "_get_status_state", return_value=(state, stats)),
            mock.patch.object(utils, "_get_galaxy_name", return_value=None),
        ):
            text = utils.get_info_text(include_planet_details=True, include_portal=True)

        self.assertEqual(
            text,
            "Walking across Onoeleto Prime (Airless Planet)"
            " • Portal: 116FA779F43A"
            " • Weather: Perfectly Clear • Flora: Barren • Fauna: Absent",
        )

    def test_location_uses_descriptive_environment_fields(self):
        state = {
            "state": "ON_FOOT",
            "universe_address": {
                "voxel_x": 1082,
                "voxel_y": -89,
                "voxel_z": 1951,
                "solar_system_index": 367,
                "planet_index": 0,
            },
            "planet": {
                "name": "Onoeleto Prime",
                "description": "Airless %PLANETCLASS%",
                "planet_type": "Planet",
                "weather_label": "Perfectly Clear",
                "flora_label": "Barren",
                "fauna_label": "Absent",
                "resources_label": "Intermittent",
                "sentinel_label": "Intermittent",
            },
        }

        with (
            mock.patch.object(utils, "_get_status_state", return_value=(state, {})),
            mock.patch.object(utils, "_get_galaxy_name", return_value=None),
        ):
            text = utils.get_location_text(include_portal=True)

        self.assertIn("Airless Planet", text)
        self.assertIn("Portal: 116FA779F43A", text)
        self.assertIn("Weather: Perfectly Clear", text)
        self.assertIn("Flora: Barren", text)
        self.assertIn("Fauna: Absent", text)
        self.assertIn("Resources: Intermittent", text)
        self.assertIn("Sentinels: Intermittent", text)
        self.assertNotIn("Gravity:", text)
        self.assertNotIn("Storming:", text)
        self.assertNotIn("Time:", text)

    def test_portal_is_not_shown_off_planet(self):
        state = {
            "state": "SPACE_STATION",
            "universe_address": {
                "voxel_x": 1082,
                "voxel_y": -89,
                "voxel_z": 1951,
                "solar_system_index": 367,
                "planet_index": 0,
            },
        }
        stats = {
            "distance_walked": 0,
            "planets_visited": 0,
            "walkers": 0,
            "commands": 0,
        }

        with (
            mock.patch.object(utils, "_get_status_state", return_value=(state, stats)),
            mock.patch.object(utils, "_get_galaxy_name", return_value="Dimonimba"),
        ):
            self.assertNotIn("Portal:", utils.get_info_text())
            self.assertNotIn("Portal:", utils.get_location_text())


if __name__ == "__main__":
    unittest.main()
