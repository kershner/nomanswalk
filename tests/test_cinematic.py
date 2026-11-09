"""Cinematic bot controls, camera geometry, and terrain clearance tests."""
import ast
import copy
import ctypes as C
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import random
import re
import tempfile
import threading
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
        (isinstance(node, ast.ClassDef) and node.name in {"Pose", "Overlay", "Director", "TerrainProbeCache", "ProcessMemory"}) or
        (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets))]
# Only geometry/settings helpers are called; native memory helpers remain unused.
camera = dict(math=math, random=random, dataclass=dataclass, queue=queue, time=time, json=json, C=C, threading=threading, re=re)
exec(compile(ast.Module(body=pure, type_ignores=[]), "cinematic_geometry", "exec"), camera)
Director = camera["Director"]


class NativeSignatureTests(unittest.TestCase):
    def test_relative_address_and_function_position_can_change(self):
        find = camera["find_unique_code"]
        signature = "48 8B 05 ? ? ? ? 2E 5B"
        for prefix, displacement in ((b"", b"\x01\x02\x03\x04"),
                                     (b"padding", b"\x0a\xff\x00\x80")):
            data = prefix + b"\x48\x8b\x05" + displacement + b".[" + b"suffix"
            self.assertEqual(find(data, signature, "test"), len(prefix))

    def test_missing_or_ambiguous_code_is_not_used_as_a_hook(self):
        for data in (b"\x90", b"\x48\x8b\x48\x8b"):
            with self.assertRaisesRegex(RuntimeError, "expected one code match"):
                camera["find_unique_code"](data, "48 8B", "test")


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


class ProcessMemoryTests(unittest.TestCase):
    def setUp(self):
        self.target = C.create_string_buffer(b"abcd")
        self.partial = False

        def read_native(process, address, buffer, size, count):
            C.memmove(buffer, address, size)
            C.cast(count, C.POINTER(C.c_size_t)).contents.value = size - int(self.partial)
            return 1

        def write_native(process, address, buffer, size, count):
            C.memmove(address, buffer, size)
            C.cast(count, C.POINTER(C.c_size_t)).contents.value = size - int(self.partial)
            return 1

        self.access = camera["ProcessMemory"](
            SimpleNamespace(ReadProcessMemory=read_native, WriteProcessMemory=write_native), 1)
        self.address = C.addressof(self.target)

    def test_reused_buffers_preserve_snapshots_and_exact_write_length(self):
        first = self.access.read(self.address, 4)
        buffer = self.access._state()["buffers"][4][0]
        self.access.write(self.address, b"wxyz")
        self.assertEqual(self.access.read(self.address, 4), b"wxyz")
        self.assertEqual(first, b"abcd")
        self.assertEqual(self.target.raw, b"wxyz\x00")
        self.assertIs(self.access._state()["buffers"][4][0], buffer)


    def test_partial_transfers_and_null_addresses_still_raise(self):
        self.partial = True
        with self.assertRaises(RuntimeError):
            self.access.read(self.address, 4)
        with self.assertRaises(RuntimeError):
            self.access.write(self.address, b"abcd")
        with self.assertRaises(RuntimeError):
            self.access.read(0, 4)
        with self.assertRaises(RuntimeError):
            self.access.write(0, b"abcd")
        self.partial = False
        self.assertEqual(self.access.read(self.address, 4), b"abcd")

    def test_buffers_are_isolated_between_threads(self):
        self.access.read(self.address, 4)
        worker_result = []

        def worker():
            self.access.read(self.address, 4)
            worker_result.append(self.access._state()["buffers"][4][0])

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(len(worker_result), 1)
        self.assertIsNot(worker_result[0], self.access._state()["buffers"][4][0])

    @unittest.skipUnless(os.name == "nt", "Windows process-memory API")
    def test_real_windows_api_round_trip_with_embedded_null_bytes(self):
        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = C.c_void_p
        for name in ("ReadProcessMemory", "WriteProcessMemory"):
            func = getattr(kernel, name)
            func.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            func.restype = C.c_int
        access = camera["ProcessMemory"](kernel, kernel.GetCurrentProcess())
        original = access.read(self.address, 4)
        access.write(self.address, b"x\x00yz")
        self.assertEqual(access.read(self.address, 4), b"x\x00yz")
        self.assertEqual(original, b"abcd")
        self.assertEqual(self.target.raw[-1:], b"\x00")


class MovementHookTests(unittest.TestCase):
    def setUp(self):
        self.bytes_at_camera = b"cinematic"
        self.overlay = camera["Overlay"]()
        self.overlay.pending = (200, b"original", b"cinematic", 100)
        self.mod = SimpleNamespace(enabled=True, overlay=self.overlay, _movement_camera=None,
                                   _report_error=Mock())

        def write_native(address, data):
            self.bytes_at_camera = data

        namespace = dict(time=time,
                         PLAYER_OFFSET=50, data_address=lambda: 100, get_addressof=lambda this: this,
                         read=lambda address, size: self.bytes_at_camera, write=write_native)
        mod_class = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "CinematicCamera")
        for name in ("before_movement", "after_movement"):
            method = copy.deepcopy(next(n for n in mod_class.body if isinstance(n, ast.FunctionDef) and n.name == name))
            method.decorator_list = []
            exec(compile(ast.Module(body=[method], type_ignores=[]), "movement_hook", "exec"), namespace)
        self.hooks = namespace

    def test_hooks_restore_native_movement_then_cinematic_view(self):
        self.hooks["before_movement"](self.mod, 150, 1 / 30)
        self.assertEqual(self.bytes_at_camera, b"original")
        self.assertIsNone(self.overlay.pending)
        self.hooks["after_movement"](self.mod, 150, 1 / 30)
        self.assertEqual(self.bytes_at_camera, b"cinematic")
        self.assertIsNotNone(self.overlay.pending)
        self.mod._report_error.assert_not_called()

    def test_external_camera_change_is_preserved(self):
        self.hooks["before_movement"](self.mod, 150, 1 / 30)
        self.bytes_at_camera = b"external"
        self.hooks["after_movement"](self.mod, 150, 1 / 30)
        self.assertEqual(self.bytes_at_camera, b"external")
        self.assertIsNone(self.overlay.pending)


class TerrainProbeCacheTests(unittest.TestCase):
    def test_ten_checks_per_second_at_thirty_and_sixty_fps(self):
        for fps in (30, 60):
            cache = camera["TerrainProbeCache"]()
            probe = Mock(return_value=None)
            for frame in range(fps):
                point = cache.place((0, 8, 0), (0, 1, 0), 1, 1 / fps,
                                    False, frame / fps, probe)
                self.assertEqual(point, (0, 8, 0))
            # Floating point boundaries can delay a check by one frame.
            self.assertGreaterEqual(probe.call_count, 8)
            self.assertLessEqual(probe.call_count, 10)

    def test_cut_and_owner_change_check_immediately(self):
        cache = camera["TerrainProbeCache"]()
        probe = Mock(return_value=(0, 10, 0))
        cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0, probe)
        probe.return_value = None
        point = cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, True, 0.01, probe)
        self.assertEqual(point, (0, 8, 0))
        cache.place((0, 8, 0), (0, 1, 0), 2, 0.01, False, 0.02, probe)
        self.assertEqual(probe.call_count, 3)

    def test_cached_ground_prevents_descent_through_ground(self):
        cache = camera["TerrainProbeCache"]()
        probe = Mock(return_value=(0, 10, 0))
        cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0, probe)
        point = cache.place((0, 6, 0), (0, 1, 0), 1, 0.01, False, 0.01, probe)
        self.assertEqual(point, (0, 10.25, 0))
        self.assertEqual(probe.call_count, 1)

    def test_ground_drop_eases_every_frame_between_checks(self):
        cache = camera["TerrainProbeCache"]()
        probe = Mock(return_value=(0, 10, 0))
        cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0, probe)
        probe.return_value = None
        first = cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0.11, probe)
        second = cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0.12, probe)
        self.assertGreater(first[1], second[1])
        self.assertGreater(second[1], 8)
        self.assertEqual(probe.call_count, 2)

    def test_failed_probe_is_retried_without_reusing_stale_ground(self):
        cache = camera["TerrainProbeCache"]()
        probe = Mock(side_effect=[ValueError("unavailable"), None])
        with self.assertRaises(ValueError):
            cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0, probe)
        cache.place((0, 8, 0), (0, 1, 0), 1, 0.01, False, 0.01, probe)
        self.assertEqual(probe.call_count, 2)


class RequestTests(unittest.TestCase):
    def test_file_worker_consumes_each_fresh_request_once(self):
        mod_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "CinematicCamera")
        service = next(node for node in mod_class.body if isinstance(node, ast.FunctionDef) and node.name == "_service_files")
        for age, expected in [(0, ["off"]), (30, [])]:
            with self.subTest(age=age), tempfile.TemporaryDirectory() as directory:
                request_file = Path(directory) / "request.json"
                request_file.write_text(json.dumps({"id": "test", "action": "off", "timestamp": time.time() - age}), encoding="utf-8")
                namespace = dict(json=json, time=time, REQUEST_FILE=request_file, LOG=Mock())
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
