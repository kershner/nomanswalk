# /// script
# dependencies = ["nmspy>=0.1.0", "pymhf[gui]>=0.2.2"]
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
from pymhf.core.hooking import Structure, function_hook, on_key_pressed

import nmspy.data.basic_types as basic
from nmspy.common import gameData
from nmspy.engine import GetNodeAbsoluteTransMatrix

from shared_state import _make_logger


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(_BASE_DIR, "selfie_camera_request.json")
STATUS_FILE = os.path.join(_BASE_DIR, "selfie_camera_status.json")
CAPTURE_FILE = os.path.join(_BASE_DIR, "selfie_camera_capture.json")
READY_FRAMES = 3

# Permanent player-relative pose captured in photo mode on 2026-08-20.
CAMERA_POSITION = (-0.3073918382419175, 0.2348574820314755, -2.521882869120346)
CAMERA_RIGHT = (-0.960508675704743, -0.22498442489819998, -0.16372261628494633)
CAMERA_UP = (-0.23841988066374892, 0.9688205520422111, 0.06739954090441043)
CAMERA_AT = (0.14345398809150398, 0.10377255833787635, -0.9842013041717624)
CAMERA_FOV = 70.0

_log = _make_logger("SelfieCamera", "selfie_camera.log")


class _BigPosMatrix34(ctypes.Structure):
    _fields_ = [
        ("right", basic.Vector3f),
        ("up", basic.Vector3f),
        ("at", basic.Vector3f),
        ("pos", basic.cTkPhysRelVec3),
    ]


class _TkCamera(ctypes.Structure):
    _fields_ = [
        ("vtable", ctypes.c_void_p),
        ("unknown", ctypes.c_byte * 8),
        ("matrix", _BigPosMatrix34),
    ]


class _PhotoModeCameraBehaviour(Structure):
    @function_hook(
        "F3 0F 11 4C 24 ? 55 53 56 57 41 56 48 8D AC 24 ? ? ? ? "
        "48 81 EC ? ? ? ? 44 0F 29 A4 24 ? ? ? ? 49 8B F8"
    )
    def Update(
        self,
        this: "ctypes._Pointer[_PhotoModeCameraBehaviour]",
        lfTimeStep: Annotated[float, ctypes.c_float],
        camera: ctypes.POINTER(_TkCamera),
    ) -> None: ...


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


def _local(vector, basis):
    return [_dot(vector, axis) for axis in basis]


def _capture_pose(camera):
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")

    player_matrix = GetNodeAbsoluteTransMatrix(player.mRootNode)
    basis = tuple(
        _normalise(_xyz(axis))
        for axis in (player_matrix.right, player_matrix.up, player_matrix.at)
    )
    camera_matrix = camera.contents.matrix
    camera_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    position_delta = _subtract(camera_position, _xyz(player_matrix.pos))
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


def _apply_pose(camera, set_position=True):
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")

    player_matrix = GetNodeAbsoluteTransMatrix(player.mRootNode)
    basis = tuple(_normalise(_xyz(axis)) for axis in (
        player_matrix.right,
        player_matrix.up,
        player_matrix.at,
    ))
    camera_matrix = camera.contents.matrix

    _set_vector(camera_matrix.right, _normalise(_to_world(CAMERA_RIGHT, basis)))
    _set_vector(camera_matrix.up, _normalise(_to_world(CAMERA_UP, basis)))
    _set_vector(camera_matrix.at, _normalise(_to_world(CAMERA_AT, basis)))

    relative_position = _to_world(CAMERA_POSITION, basis)
    player_position = _xyz(player_matrix.pos)
    desired_position = tuple(
        player_position[index] + relative_position[index]
        for index in range(3)
    )
    if set_position:
        big_position_offset = _xyz(camera_matrix.pos.offset)
        _set_vector(camera_matrix.pos.local, _subtract(desired_position, big_position_offset))
    gameData.player_state.mPhotoModeSettings.FoV = CAMERA_FOV
    return player_position, desired_position


def _finish_pose(camera):
    camera_matrix = camera.contents.matrix
    actual_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    player_position, desired_position = _apply_pose(camera, set_position=False)
    actual_delta = _subtract(actual_position, player_position)
    desired_delta = _subtract(desired_position, player_position)
    actual_distance = _length(actual_delta)
    desired_distance = _length(desired_delta)

    # Native collision may push the camera toward the player, but it should not
    # move it farther away or to the opposite side.
    collision_position = actual_distance <= desired_distance + 0.5 and (
        actual_distance < 0.1
        or _dot(actual_delta, desired_delta) >= 0.8 * actual_distance * desired_distance
    )
    if not collision_position:
        offset = _xyz(camera_matrix.pos.offset)
        _set_vector(camera_matrix.pos.local, _subtract(desired_position, offset))
    return collision_position


class SelfieCamera(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Apply the permanent collision-aware selfie camera pose."
    __version__ = "1.0"

    def __init__(self):
        super().__init__()
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0
        self._request_mtime = None
        self._capture_requested = False
        _log.info("Selfie camera loaded with permanent pose")

    @on_key_pressed("f11")
    def request_capture(self):
        self._capture_requested = True

    def _clear(self):
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0

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
            if expires_at <= time.time():
                raise ValueError("request expired")
            self._request_id = request_id
            self._expires_at = expires_at
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
            _apply_pose(camera)
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
                _log.info("Captured production selfie pose: %s", pose)
            except Exception:
                _log.error("Selfie camera capture failed:\n%s", traceback.format_exc())
        if not self._request_id:
            return
        try:
            _finish_pose(camera)
        except Exception as error:
            request_id = self._request_id
            self._clear()
            _write_status(request_id, "error", str(error))
            _log.error("Selfie camera finalization failed:\n%s", traceback.format_exc())
            return
        self._ready_frames += 1
        if self._ready_frames == READY_FRAMES:
            _write_status(self._request_id, "ready")
