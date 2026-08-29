import unittest

from command_block_lists import blocked_commands_for_state, resolve_command_state


class CommandBlockListTests(unittest.TestCase):
    def test_nmspy_default_name_resolves_to_space(self):
        data = {"environment": {"location_stable": "Default"}}

        self.assertEqual(resolve_command_state(data), "Space")

    def test_raw_location_one_resolves_to_space(self):
        data = {"environment": {"location_stable_raw": 1}}

        self.assertEqual(resolve_command_state(data), "Space")

    def test_eva_movement_is_available_in_space(self):
        blocked = blocked_commands_for_state("Space")

        self.assertNotIn("jet", blocked)
        self.assertNotIn("walk", blocked)

    def test_legacy_default_name_uses_space_block_list(self):
        self.assertNotIn("jet", blocked_commands_for_state("Default"))

    def test_other_cockpit_restrictions_remain_in_space(self):
        blocked = blocked_commands_for_state("Space")

        self.assertIn("selfie", blocked)
        self.assertIn("teleport", blocked)

    def test_unset_location_remains_unknown(self):
        data = {"environment": {"location_stable": "None_"}}

        self.assertEqual(resolve_command_state(data), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
