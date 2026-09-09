# /// script
# dependencies = ["nmspy==170671.5", "pymhf[gui]==0.2.4"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# always_on_top = false
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "Selfie Camera"
# ///

"""Apply the permanent, collision-aware selfie camera pose."""

import ctypes
import json
import math
import os
import time
import traceback
from typing import Annotated

from pymhf import Mod
from pymhf.core.hooking import Structure, function_hook, on_key_pressed, static_function_hook

import nmspy.data.basic_types as basic
from nmspy.common import gameData

from shared_state import _make_logger


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(_BASE_DIR, "selfie_camera_request.json")
STATUS_FILE = os.path.join(_BASE_DIR, "selfie_camera_status.json")
CAPTURE_FILE = os.path.join(_BASE_DIR, "selfie_camera_capture.json")
READY_FRAMES = 3

# Player-relative camera poses for each bot/server environment.
CAMERA_PROFILES = {
    "dev": {
        "position": (0.3819905381652802, 0.13626888326393963, 0.21394229053180758),
        "right": (0.5012075726766348, -0.13072791050673693, -0.855395336833628),
        "up": (-0.10656586226451241, 0.9716729812542269, -0.21093917082922764),
        "at": (0.8587401722635728, 0.1968802566617144, 0.47307872708164544),
        "fov": 120.0,
    },
    "production": {
        "position": (0.2987773664960399, -0.4292795540975478, 0.3744106498722445),
        "right": (0.6366502252687154, -0.2573240826879011, -0.7269530835075273),
        "up": (-0.0392187799105648, 0.9306595645238865, -0.36377832664380627),
        "at": (0.7701547821107736, 0.2601097665424687, 0.5824126833963356),
        "fov": 120.0,
    },
}

_log = _make_logger("SelfieCamera", "selfie_camera.log")


class _BigPosMatrix34(ctypes.Structure):
    _fields_ = [
        ("right", basic.Vector3f),
        ("up", basic.Vector3f),
        ("at", basic.Vector3f),
        ("pos", basic.cTkPhysRelVec3),
    ]


class _PhotoModeCameraBehaviour(Structure):
    @function_hook(
        "F3 0F 11 4C 24 ? 55 53 56 57 41 56 48 8D AC 24 ? ? ? ? "
        "48 81 EC ? ? ? ? 44 0F 29 9C 24 ? ? ? ? 49 8B F0"
    )
    def Update(
        self,
        this: "ctypes._Pointer[_PhotoModeCameraBehaviour]",
        lfTimeStep: Annotated[float, ctypes.c_float],
        camera: ctypes.POINTER(basic.cTkPhysRelMat34),
    ) -> None: ...


class _EngineCosmos(Structure):
    @static_function_hook(
        "48 89 5C 24 ? 57 48 81 EC ? ? ? ? 0F 29 74 24 ? 8B DA 48 8B F9 "
        "E8 24 08 AD FD 0F 28 05 ? ? ? ? 4C 8D 44 24"
    )
    @staticmethod
    def GetNodePhysRelMatrix(
        result: ctypes._Pointer[basic.cTkPhysRelMat34],
        node: ctypes.c_uint32,
    ) -> None: ...


def _get_player_matrix(player):
    matrix = basic.cTkPhysRelMat34()
    _EngineCosmos.GetNodePhysRelMatrix(
        ctypes.byref(matrix),
        ctypes.c_uint32(int(player.mRootNode.lookupInt)),
    )
    return matrix


def _atomic_json(path, payload):
    temp_file = f"{path}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(payload, file)
    os.replace(temp_file, path)


def _write_status(request_id, state, message=""):
    try:
        _atomic_json(
            STATUS_FILE,
            {
                "request_id": request_id,
                "state": state,
                "message": message,
                "timestamp": time.time(),
            },
        )
    except OSError:
        _log.exception("Could not write selfie camera status")


def _xyz(vector):
    return (float(vector.x), float(vector.y), float(vector.z))


def _length(vector):
    return math.sqrt(sum(value * value for value in vector))


def _dot(a, b):
    return sum(a[index] * b[index] for index in range(3))


def _normalise(vector):
    length = _length(vector)
    if not math.isfinite(length) or length < 0.001:
        raise ValueError("invalid player transform")
    return tuple(value / length for value in vector)


def _to_world(vector, basis):
    return tuple(
        basis[0][index] * vector[0]
        + basis[1][index] * vector[1]
        + basis[2][index] * vector[2]
        for index in range(3)
    )


def _set_vector(target, values):
    target.x, target.y, target.z = values


def _subtract(a, b):
    return tuple(a[index] - b[index] for index in range(3))


def _big_position_xyz(position):
    return tuple(
        local + offset
        for local, offset in zip(_xyz(position.local), _xyz(position.offset))
    )


def _local(vector, basis):
    return [_dot(vector, axis) for axis in basis]


def _capture_pose(camera):
    """Write the current player-relative photo-mode pose for calibration."""
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")

    player_matrix = _get_player_matrix(player)
    basis = tuple(
        _normalise(_xyz(axis))
        for axis in (player_matrix.right, player_matrix.up, player_matrix.at)
    )
    camera_matrix = camera.contents
    camera_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    player_position = _big_position_xyz(player_matrix.pos)
    position_delta = _subtract(camera_position, player_position)
    _log.info(
        "Selfie capture probe player=%s camera=%s delta=%s distance=%.6f",
        player_position,
        camera_position,
        position_delta,
        _length(position_delta),
    )
    if not all(math.isfinite(value) for value in position_delta):
        raise ValueError("invalid camera position")
    if _length(position_delta) > 100:
        raise ValueError("camera position is outside the safe capture range")

    pose = {
        "position": _local(position_delta, basis),
        "right": _local(_normalise(_xyz(camera_matrix.right)), basis),
        "up": _local(_normalise(_xyz(camera_matrix.up)), basis),
        "at": _local(_normalise(_xyz(camera_matrix.at)), basis),
        "fov": float(gameData.player_state.mPhotoModeSettings.FoV),
    }
    _atomic_json(CAPTURE_FILE, pose)
    return pose


def _apply_pose(camera, profile, set_position=True):
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")
    pose = CAMERA_PROFILES[profile]

    player_matrix = _get_player_matrix(player)
    basis = tuple(_normalise(_xyz(axis)) for axis in (
        player_matrix.right,
        player_matrix.up,
        player_matrix.at,
    ))
    camera_matrix = camera.contents

    _set_vector(camera_matrix.right, _normalise(_to_world(pose["right"], basis)))
    _set_vector(camera_matrix.up, _normalise(_to_world(pose["up"], basis)))
    _set_vector(camera_matrix.at, _normalise(_to_world(pose["at"], basis)))

    relative_position = _to_world(pose["position"], basis)
    player_position = _big_position_xyz(player_matrix.pos)
    desired_position = tuple(
        player_position[index] + relative_position[index]
        for index in range(3)
    )
    if set_position:
        big_position_offset = _xyz(camera_matrix.pos.offset)
        _set_vector(camera_matrix.pos.local, _subtract(desired_position, big_position_offset))
    # The large cGcPlayerState tail moved in Cosmos. Until the new
    # mPhotoModeSettings offset is independently verified, preserve the game's
    # current FOV instead of risking a write into a neighbouring field.
    return player_position, desired_position


def _finish_pose(camera, profile):
    camera_matrix = camera.contents
    actual_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    player_position, desired_position = _apply_pose(
        camera,
        profile,
        set_position=False,
    )
    actual_delta = _subtract(actual_position, player_position)
    desired_delta = _subtract(desired_position, player_position)
    desired_distance_squared = _dot(desired_delta, desired_delta)
    projection = _dot(actual_delta, desired_delta) / desired_distance_squared
    perpendicular = _subtract(
        actual_delta,
        tuple(projection * value for value in desired_delta),
    )

    # Collision may shorten the camera arm, but native camera drift must not
    # move it materially above, below, or beside the calibrated line.
    collision_position = (
        0.0 <= projection <= 1.05
        and _length(perpendicular) <= 0.2
    )
    if not collision_position:
        offset = _xyz(camera_matrix.pos.offset)
        _set_vector(camera_matrix.pos.local, _subtract(desired_position, offset))
    return collision_position


class SelfieCamera(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Apply the permanent collision-aware selfie camera pose."
    __version__ = "1.1-cosmos-transform"

    def __init__(self):
        super().__init__()
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0
        self._request_mtime = None
        self._capture_requested = False
        self._profile = "production"
        _log.info("Selfie camera loaded with permanent pose")

    @on_key_pressed("f11")
    def request_capture(self):
        self._capture_requested = True

    def _clear(self):
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0
        self._profile = "production"

    def _poll_request(self):
        try:
            mtime = os.stat(REQUEST_FILE).st_mtime_ns
        except FileNotFoundError:
            self._request_mtime = None
            self._clear()
            return
        except OSError:
            return

        if mtime == self._request_mtime:
            return
        self._request_mtime = mtime

        try:
            with open(REQUEST_FILE, "r", encoding="utf-8") as file:
                request = json.load(file)
            request_id = str(request["request_id"])
            expires_at = float(request["expires_at"])
            profile = str(request.get("profile", "production")).strip().lower()
            if expires_at <= time.time():
                raise ValueError("request expired")
            if profile not in CAMERA_PROFILES:
                raise ValueError(f"unknown camera profile: {profile}")
            self._request_id = request_id
            self._expires_at = expires_at
            self._profile = profile
            self._ready_frames = 0
            _write_status(request_id, "positioning")
        except Exception as error:
            request_id = str(request.get("request_id", "")) if "request" in locals() else ""
            self._clear()
            _write_status(request_id, "error", str(error))

    @_PhotoModeCameraBehaviour.Update.before
    def before_photo_camera_update(self, this, lfTimeStep, camera):
        try:
            self._poll_request()
            if not self._request_id:
                return
            if time.time() >= self._expires_at:
                _write_status(self._request_id, "error", "request expired")
                self._clear()
                return
            _apply_pose(camera, self._profile)
        except Exception as error:
            request_id = self._request_id or ""
            self._clear()
            _write_status(request_id, "error", str(error))
            _log.error("Selfie camera failed:\n%s", traceback.format_exc())

    @_PhotoModeCameraBehaviour.Update.after
    def after_photo_camera_update(self, this, lfTimeStep, camera):
        if self._capture_requested:
            self._capture_requested = False
            try:
                pose = _capture_pose(camera)
                _log.info("Captured selfie camera diagnostic pose: %s", pose)
            except Exception:
                _log.error("Selfie camera capture failed:\n%s", traceback.format_exc())
        if not self._request_id:
            return
        try:
            _finish_pose(camera, self._profile)
        except Exception as error:
            request_id = self._request_id
            self._clear()
            _write_status(request_id, "error", str(error))
            _log.error("Selfie camera finalization failed:\n%s", traceback.format_exc())
            return
        self._ready_frames += 1
        if self._ready_frames == READY_FRAMES:
            _write_status(self._request_id, "ready")
