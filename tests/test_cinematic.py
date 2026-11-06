"""Cinematic bot controls, camera geometry, and terrain clearance tests."""
import ast
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import random
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nms_bot
from nms_twitch_bot import Config, NMSBot, VoteState


source = ast.parse((Path(__file__).resolve().parents[1] / "nmspy_mods/cinematic_camera.py").read_text(encoding="utf-8"))
names = {"DEFAULT_SETTINGS", "SETTING_BOUNDS", "BASE_FRAMING"}
pure = [node for node in source.body if
        isinstance(node, ast.FunctionDef) or
        (isinstance(node, ast.ClassDef) and node.name in {"Pose", "Overlay", "Director"}) or
        (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets))]
# Only geometry/settings helpers are called; native memory helpers remain unused.
camera = dict(math=math, random=random, dataclass=dataclass)
exec(compile(ast.Module(body=pure, type_ignores=[]), "cinematic_geometry", "exec"), camera)
Director = camera["Director"]


class FramingTests(unittest.TestCase):
    def sample(self, director):
        director.next()
        pose = director.update(1 / 60, (0, 0, 0), (0, 1, 0), (0, 0, 1), -1)
        camera["validate_basis"](pose.right, pose.up, pose.at)
        return pose

    def test_random_range_includes_close_and_far_without_categories(self):
        director = Director({"smooth_transitions": False, "distance_max": 120, "height_max": 80}, seed=10)
        distances, heights = [], []
        for _ in range(2000):
            pose = self.sample(director)
            values = director.shot_values
            distance = values["rear_distance"]
            height = pose.position[1]
            self.assertTrue(3 <= distance <= 120)
            self.assertTrue(1.5 <= height <= 80)
            self.assertTrue(1 <= values["distance_scale"] <= 4)
            self.assertLessEqual(math.radians(values["orbit_speed_degrees"]) * distance * values["distance_scale"], 6.000001)
            distances.append(distance)
            heights.append(height)
        self.assertLess(min(distances), 4)
        self.assertGreater(max(distances), 119)
        self.assertLess(min(heights), 2)
        self.assertGreater(max(heights), 79)
        # A uniform range has its mean near the midpoint, without a close-shot bias.
        self.assertAlmostEqual(sum(distances) / len(distances), 61.5, delta=3)

    def test_low_caps_work_for_every_angle(self):
        director = Director({"distance_max": 3, "height_max": 1.5, "zoom_max": 1, "smooth_transitions": False}, seed=12)
        for _ in range(40):
            pose = self.sample(director)
            self.assertLessEqual(math.hypot(pose.position[0], pose.position[2]), 3.1)
            self.assertEqual(pose.position[1], 1.5)

    def test_smooth_transition_stays_outside_subject_and_finishes(self):
        director = Director(seed=20)
        self.sample(director)
        self.sample(director)
        start = director.position
        director.update(1 / 60, (0, 0, 0), (0, 1, 0), (0, 0, 1), -1)
        self.assertLess(camera["length"](camera["sub"](director.position, start)), 0.1)
        for _ in range(400):
            pose = director.update(1 / 60, (0, 0, 0), (0, 1, 0), (0, 0, 1), -1)
            self.assertGreater(math.hypot(pose.position[0], pose.position[2]), 2.9)
            camera["validate_basis"](pose.right, pose.up, pose.at)


class RequestTests(unittest.TestCase):
    def test_file_worker_consumes_each_fresh_request_once(self):
        mod_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "CinematicCamera")
        service = next(node for node in mod_class.body if isinstance(node, ast.FunctionDef) and node.name == "_service_files")
        for age, expected in [(0, ["off"]), (30, [])]:
            with self.subTest(age=age), tempfile.TemporaryDirectory() as directory:
                request_file = Path(directory) / "request.json"
                request_file.write_text(json.dumps({"id": "test", "action": "off", "timestamp": time.time() - age}), encoding="utf-8")
                namespace = dict(json=json, time=time, REQUEST_FILE=request_file)
                exec(compile(ast.Module(body=[service], type_ignores=[]), "cinematic_service", "exec"), namespace)
                stop = Mock()
                stop.is_set.side_effect = [False, False, True]
                mod = SimpleNamespace(_io_stop=stop, _last_request=None, actions=queue.SimpleQueue(), _publish_status=Mock())
                namespace["_service_files"](mod)
                received = []
                while not mod.actions.empty():
                    received.append(mod.actions.get_nowait())
                self.assertEqual(received, expected)
                self.assertEqual(mod._publish_status.call_count, 2)


class ClearanceTests(unittest.TestCase):
    def test_open_view_keeps_requested_position(self):
        point, lift = camera["camera_clearance"]((0, 8, 24), (0, 1, 0), None, None, 1 / 30)
        self.assertEqual(point, (0, 8, 24))
        self.assertEqual(lift, 0)

    def test_ground_penetration_lifts_without_pulling_in(self):
        point, lift = camera["camera_clearance"]((20, 4, 100), (0, 1, 0), (20, 6, 100), 0, 1 / 30)
        self.assertEqual(point, (20, 6.25, 100))
        self.assertEqual(lift, 2.25)

    def test_ground_drop_recovers_smoothly(self):
        point, lift = camera["camera_clearance"]((0, 4, 100), (0, 1, 0), None, 2.25, 1 / 30)
        self.assertGreater(lift, 2)
        self.assertLess(lift, 2.25)
        self.assertAlmostEqual(point[1], 4 + lift)

    def test_ground_correction_uses_planet_up(self):
        point, lift = camera["camera_clearance"]((1000, 4, 100), (1, 0, 0), (1002, 4, 100), 0, 1 / 30)
        self.assertEqual(point, (1002.25, 4, 100))
        self.assertEqual(lift, 2.25)

    def test_terrain_below_clearance_does_not_change_shot(self):
        point, lift = camera["camera_clearance"]((0, 4, 100), (0, 1, 0), (0, 3, 100), None, 1 / 30)
        self.assertEqual(point, (0, 4, 100))
        self.assertEqual(lift, 0)


class CinematicControlTests(unittest.TestCase):
    def test_already_requested_state_does_not_press_toggle(self):
        for enabled, action in [(True, "on"), (False, "off")]:
            with self.subTest(action=action), patch.object(nms_bot, "get_cinematic_status", return_value=enabled), patch.object(nms_bot, "send_key") as send:
                nms_bot.cinematic([action])
                send.assert_not_called()

    def test_changes_state_without_sending_game_keys(self):
        for enabled, action in [(False, "on"), (True, "off")]:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory, patch.object(nms_bot, "BASE_DIR", directory), patch.object(nms_bot, "get_cinematic_status", side_effect=[enabled, not enabled]), patch.object(nms_bot, "send_key") as send:
                os.mkdir(os.path.join(directory, "nmspy_mods"))
                nms_bot.cinematic([action])
                send.assert_not_called()
                with open(os.path.join(directory, "nmspy_mods", "cinematic_camera_request.json")) as file:
                    request = json.load(file)
                self.assertEqual(request["action"], action)
                self.assertTrue(request["id"])
                self.assertGreater(request["timestamp"], 0)

    def test_unavailable_camera_does_not_press_keys(self):
        with patch.object(nms_bot, "get_cinematic_status", return_value=None), patch.object(nms_bot, "send_key") as send:
            with self.assertRaises(RuntimeError):
                nms_bot.cinematic(["on"])
            send.assert_not_called()


class CinematicChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = object.__new__(NMSBot)
        self.bot._say = AsyncMock()
        self.bot._start_vote = AsyncMock()
        self.ctx = SimpleNamespace(author=SimpleNamespace(name="viewer"))

    async def test_bare_command_only_reports_live_state(self):
        for status, label in [(True, "On"), (False, "Off"), (None, "Unavailable")]:
            with patch("nms_twitch_bot.get_cinematic_status", return_value=status):
                await self.bot._dispatch_nms_command(self.ctx, "cinematic", [])
                self.bot._say.assert_awaited_with(self.ctx, f"Toggle cinematic mode - !cinematic [on/off].  Currently: {label}")
        self.bot._start_vote.assert_not_awaited()

    async def test_invalid_options_do_not_start_vote(self):
        for args in [["toggle"], ["on", "off"]]:
            await self.bot._dispatch_nms_command(self.ctx, "cinematic", args)
        self.bot._start_vote.assert_not_awaited()

    async def test_explicit_state_starts_vote(self):
        with patch("nms_twitch_bot.is_command_allowed", return_value=True), patch("nms_twitch_bot.is_planet_loading", return_value=False):
            await self.bot._dispatch_nms_command(self.ctx, "cinematic", ["ON"])
        self.bot._start_vote.assert_awaited_once_with(self.ctx, "cinematic", ["on"])

    async def test_passed_vote_applies_requested_state(self):
        self.bot._vote = VoteState()
        self.bot._submit_command = AsyncMock()
        with patch.object(Config, "VOTING_DURATION", 0):
            await NMSBot._start_vote(self.bot, self.ctx, "cinematic", ["off"])
            await self.bot._vote.task
        self.bot._submit_command.assert_awaited_once_with("cinematic", ["off"])
        self.assertFalse(self.bot._vote.active)
        self.assertIn("Turn cinematic mode Off.", self.bot._say.await_args_list[0].args[1])

    async def test_worker_reports_camera_errors(self):
        self.bot._chat_context = self.ctx
        with patch.object(nms_bot, "get_cinematic_status", return_value=None):
            await self.bot._run_command("cinematic", ["on"])
        self.bot._say.assert_awaited_once_with(self.ctx, "Cinematic camera is unavailable; load the camera mod first.")

    async def test_help_includes_usage_voting_and_status(self):
        await self.bot._do_help(self.ctx, ["cinematic"])
        text = self.bot._say.await_args.args[1]
        for phrase in ["!cinematic on/off", "!yes", "!no", "on foot", "status"]:
            self.assertIn(phrase, text)
        self.assertIn("cinematic", self.bot._help_commands())


if __name__ == "__main__":
    unittest.main()
