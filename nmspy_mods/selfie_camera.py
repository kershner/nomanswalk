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
# Cosmos inserted 0x400 bytes at the front of cGcPlayerState. The historical
# mPhotoModeSettings.FoV address was 0x1A0D0 + 0x34; its Cosmos address is
# therefore 0x1A504 from the live cGcPlayerState base.
_COSMOS_PHOTO_MODE_FOV_OFFSET = 0x1A504

# Built-in fallbacks used until F11 creates a machine-local calibration file.
CAMERA_PROFILES = {
    "dev": {
        "position": (0.3819905381652802, 0.13626888326393963, 0.21394229053180758),
        "right": (0.5012075726766348, -0.13072791050673693, -0.855395336833628),
        "up": (-0.10656586226451241, 0.9716729812542269, -0.21093917082922764),
        "at": (0.8587401722635728, 0.1968802566617144, 0.47307872708164544),
        "fov": 120.0,
    },
    "production": {
        "position": (0.5934469182252805, -0.27753873303405047, 0.4219922329895168),
        "right": (0.42277857580864464, 1.680967289763996e-05, -0.9062330158562485),
        "up": (-0.2908833671699134, 0.9470882292568501, -0.13568621987484347),
        "at": (0.8582803320316302, 0.3209733594723489, 0.40041350267759346),
        "fov": 120.0,
    },
}

_log = _make_logger("SelfieCamera", "selfie_camera.log")
_PLAYER_MODEL_SCENE = "MODELS/COMMON/PLAYER/PLAYERCHARACTER/PLAYERCHARACTER.SCENE.MBIN"
_PLAYER_CAMERA_ANCHOR = "player01_spine_TopSHJnt"
_player_model_handle = None
_player_anchor_handle = None


class _BigPosMatrix34(ctypes.Structure):
    _fields_ = [
        ("right", basic.Vector3f),
        ("up", basic.Vector3f),
        ("at", basic.Vector3f),
        ("pos", basic.cTkPhysRelVec3),
    ]


class _CosmosCamera(ctypes.Structure):
    # Cosmos keeps the exposed/current, committed/render, and previous camera
    # transforms consecutively.  PhotoModeCameraBehaviour.Update commits via
    # the engine setter at +0x50, so changing only +0x00 no longer reaches the
    # transform consumed by rendering.
    _fields_ = [
        ("current", _BigPosMatrix34),
        ("committed", _BigPosMatrix34),
        ("previous", _BigPosMatrix34),
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
        camera: ctypes.POINTER(_CosmosCamera),
    ) -> None: ...


class _EngineCosmos(Structure):
    @static_function_hook(
        "40 57 48 83 EC 20 49 8B F8 44 8B C1 41 C1 E8 13 45 85 C0 "
        "0F 84 ? ? ? ? 8B C1 25 FF FF 07 00 3D FF FF 07 00 0F 84 ? ? ? ? "
        "81 E1 FF FF 07 00 48 89 5C 24 30 48 8B 1D ? ? ? ?"
    )
    @staticmethod
    def GetNodeMatrices(
        node: ctypes.c_uint32,
        local: ctypes.c_void_p,
        absolute: ctypes._Pointer[basic.cTkMatrix34],
    ) -> None: ...

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


def _get_player_absolute_matrix(player):
    matrix = basic.cTkMatrix34()
    _EngineCosmos.GetNodeMatrices(
        ctypes.c_uint32(int(player.mRootNode.lookupInt)),
        ctypes.c_void_p(),
        ctypes.byref(matrix),
    )
    return matrix


def _probe_player_node_tree(player, maximum=160):
    """Read a bounded snapshot of the live player graphics-node hierarchy."""
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    image_base = kernel32.GetModuleHandleW("NMS.exe")
    if not image_base:
        raise RuntimeError("NMS module base is unavailable")

    # Current node-manager global, verified from the exact
    # Engine.GetNodeAbsoluteTransMatrix match in the 2026-09-10 executable.
    manager = ctypes.c_void_p.from_address(image_base + 0x6E13DC8).value
    if not manager:
        raise RuntimeError("scene-node manager is unavailable")
    lookup = ctypes.c_void_p.from_address(manager + 0xE8).value
    records = ctypes.c_void_p.from_address(manager + 0x70).value
    objects = ctypes.c_void_p.from_address(manager + 0x90).value
    root_handle = int(player.mRootNode.lookupInt)
    root_index = ctypes.c_int32.from_address(
        lookup + (root_handle & 0x7FFFF) * 4
    ).value

    def node_details(index):
        obj = ctypes.c_void_p.from_address(objects + index * 8).value
        handle = ctypes.c_uint32.from_address(obj + 8).value
        owner = ctypes.c_void_p.from_address(obj + 0x20).value
        string = ctypes.c_void_p.from_address(owner).value if owner else None
        name = ""
        if string:
            length = ctypes.c_size_t.from_address(string + 0x18).value
            chars = (
                ctypes.c_void_p.from_address(string).value
                if length > 15
                else string
            )
            if chars and length < 1024:
                name = ctypes.string_at(chars, length).decode(errors="replace")
                name = name.split("\x00", 1)[0]
        matrix = basic.cTkMatrix34()
        _EngineCosmos.GetNodeMatrices(
            ctypes.c_uint32(handle), ctypes.c_void_p(), ctypes.byref(matrix)
        )
        record = records + index * 20
        first_child = ctypes.c_int32.from_address(record + 8).value
        next_sibling = ctypes.c_int32.from_address(record + 0x10).value
        return handle, name, _xyz(matrix.pos), first_child, next_sibling

    rows = []
    stack = [(root_index, 0)]
    seen = set()
    while stack and len(rows) < maximum:
        index, depth = stack.pop()
        if index < 0 or index in seen:
            continue
        seen.add(index)
        handle, name, position, child, sibling = node_details(index)
        rows.append((depth, index, handle, name, position))
        if sibling >= 0:
            stack.append((sibling, depth))
        if child >= 0:
            stack.append((child, depth + 1))
    return rows


def _get_player_model_matrix(player):
    """Return player-facing axes positioned on the rendered upper torso."""
    global _player_model_handle, _player_anchor_handle

    if _player_model_handle is not None and _player_anchor_handle is not None:
        matrix = basic.cTkMatrix34()
        anchor = basic.cTkMatrix34()
        _EngineCosmos.GetNodeMatrices(
            ctypes.c_uint32(_player_model_handle),
            ctypes.c_void_p(),
            ctypes.byref(matrix),
        )
        _EngineCosmos.GetNodeMatrices(
            ctypes.c_uint32(_player_anchor_handle),
            ctypes.c_void_p(),
            ctypes.byref(anchor),
        )
        if (
            _length(_xyz(matrix.right)) >= 0.001
            and _length(_xyz(anchor.right)) >= 0.001
        ):
            _set_vector(matrix.pos, _xyz(anchor.pos))
            return matrix
        _player_model_handle = None
        _player_anchor_handle = None

    for _depth, _index, handle, name, _position in _probe_player_node_tree(
        player, maximum=5000
    ):
        if name == _PLAYER_MODEL_SCENE:
            _player_model_handle = handle
        elif name == _PLAYER_CAMERA_ANCHOR:
            _player_anchor_handle = handle

    if _player_model_handle is None:
        raise RuntimeError("animated player model node was not found")
    if _player_anchor_handle is None:
        raise RuntimeError("animated player torso anchor was not found")

    _log.info(
        "Resolved player camera reference model=%#x anchor=%#x (%s)",
        _player_model_handle,
        _player_anchor_handle,
        _PLAYER_CAMERA_ANCHOR,
    )
    matrix = basic.cTkMatrix34()
    anchor = basic.cTkMatrix34()
    _EngineCosmos.GetNodeMatrices(
        ctypes.c_uint32(_player_model_handle), ctypes.c_void_p(), ctypes.byref(matrix)
    )
    _EngineCosmos.GetNodeMatrices(
        ctypes.c_uint32(_player_anchor_handle), ctypes.c_void_p(), ctypes.byref(anchor)
    )
    _set_vector(matrix.pos, _xyz(anchor.pos))
    return matrix


def _atomic_json(path, payload):
    temp_file = f"{path}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(payload, file)
    last_error = None
    for _ in range(20):
        try:
            os.replace(temp_file, path)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.05)
    raise last_error


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
        return True
    except OSError:
        _log.exception("Could not write selfie camera status")
        return False


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


def _camera_basis(camera):
    matrix = camera.contents.committed
    return tuple(
        _normalise(_xyz(axis))
        for axis in (matrix.right, matrix.up, matrix.at)
    )


def _camera_transforms(camera):
    contents = camera.contents
    return (contents.current, contents.committed, contents.previous)


def _photo_mode_fov_slot():
    try:
        player_state = gameData.player_state
        if player_state is None:
            return None
        return ctypes.c_float.from_address(
            ctypes.addressof(player_state) + _COSMOS_PHOTO_MODE_FOV_OFFSET
        )
    except Exception:
        return None


def _read_photo_mode_fov():
    slot = _photo_mode_fov_slot()
    if slot is None:
        return None
    value = float(slot.value)
    return value if math.isfinite(value) and 1.0 <= value <= 180.0 else None


def _set_photo_mode_fov(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value) or not 1.0 <= value <= 180.0:
        return False

    slot = _photo_mode_fov_slot()
    if slot is None:
        return False

    # Refuse to write unless the target currently contains a plausible FOV.
    # This keeps a future layout change from turning this into a blind write.
    current = float(slot.value)
    if not math.isfinite(current) or not 1.0 <= current <= 180.0:
        return False
    slot.value = value
    return True


def _validated_pose(value, fallback):
    if not isinstance(value, dict):
        raise ValueError("selfie calibration must be a JSON object")

    pose = {}
    for name in ("position", "right", "up", "at"):
        vector = value.get(name)
        if not isinstance(vector, (list, tuple)) or len(vector) != 3:
            raise ValueError(f"selfie calibration {name} must contain three numbers")
        vector = tuple(float(component) for component in vector)
        if not all(math.isfinite(component) for component in vector):
            raise ValueError(f"selfie calibration {name} contains a non-finite number")
        if name == "position":
            if _length(vector) > 100.0:
                raise ValueError("selfie calibration position is outside the safe range")
        elif _length(vector) < 0.001:
            raise ValueError(f"selfie calibration {name} is not a direction")
        pose[name] = vector

    try:
        fov = float(value.get("fov", fallback["fov"]))
    except (TypeError, ValueError):
        fov = float(fallback["fov"])
    pose["fov"] = fov if math.isfinite(fov) and fov > 0.0 else float(fallback["fov"])
    return pose


def _load_pose(profile):
    """Load the latest F11 calibration, falling back to the bundled profile."""
    fallback = CAMERA_PROFILES[profile]
    try:
        with open(CAPTURE_FILE, "r", encoding="utf-8") as file:
            pose = _validated_pose(json.load(file), fallback)
        _log.info("Using F11 selfie calibration from %s", CAPTURE_FILE)
        return pose
    except FileNotFoundError:
        _log.info("No F11 selfie calibration found; using %s fallback", profile)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        _log.exception("Invalid F11 selfie calibration; using %s fallback", profile)
    return _validated_pose(fallback, fallback)


def _capture_pose(camera, basis):
    """Write the current player-relative photo-mode pose for calibration."""
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")

    player_matrix = _get_player_model_matrix(player)
    basis = tuple(
        _normalise(_xyz(axis))
        for axis in (player_matrix.right, player_matrix.up, player_matrix.at)
    )
    camera_matrix = camera.contents.committed
    camera_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    player_position = _xyz(player_matrix.pos)
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
        "fov": _read_photo_mode_fov() or 120.0,
    }
    _atomic_json(CAPTURE_FILE, pose)
    return pose


def _apply_pose(camera, pose, basis, set_position=True):
    player = gameData.player
    if player is None:
        raise RuntimeError("player is unavailable")

    player_matrix = _get_player_model_matrix(player)
    basis = tuple(
        _normalise(_xyz(axis))
        for axis in (player_matrix.right, player_matrix.up, player_matrix.at)
    )
    camera_matrices = _camera_transforms(camera)

    world_right = _normalise(_to_world(pose["right"], basis))
    world_up = _normalise(_to_world(pose["up"], basis))
    world_at = _normalise(_to_world(pose["at"], basis))
    for camera_matrix in camera_matrices:
        _set_vector(camera_matrix.right, world_right)
        _set_vector(camera_matrix.up, world_up)
        _set_vector(camera_matrix.at, world_at)

    relative_position = _to_world(pose["position"], basis)
    player_position = _xyz(player_matrix.pos)
    desired_position = tuple(
        player_position[index] + relative_position[index]
        for index in range(3)
    )
    if set_position:
        for camera_matrix in camera_matrices:
            big_position_offset = _xyz(camera_matrix.pos.offset)
            _set_vector(
                camera_matrix.pos.local,
                _subtract(desired_position, big_position_offset),
            )
    _set_photo_mode_fov(pose["fov"])
    return player_position, desired_position


def _finish_pose(camera, pose, basis):
    camera_matrix = camera.contents.committed
    actual_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    player_position, desired_position = _apply_pose(
        camera,
        pose,
        basis,
        set_position=False,
    )
    # The native update can ease the camera toward its own arm position.  That
    # movement was previously accepted whenever it happened to lie near the
    # player-to-camera ray, which made a valid F11 calibration settle at a
    # different point.  The selfie contract is now exact: native code may run
    # between the hooks, but the calibrated position wins on every frame.
    for transform in _camera_transforms(camera):
        offset = _xyz(transform.pos.offset)
        _set_vector(transform.pos.local, _subtract(desired_position, offset))
    final_position = tuple(
        local + offset
        for local, offset in zip(
            _xyz(camera_matrix.pos.local),
            _xyz(camera_matrix.pos.offset),
        )
    )
    return actual_position, final_position, player_position, desired_position


class SelfieCamera(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Apply the permanent exact selfie camera pose."
    __version__ = "2.8-patch-node-manager"

    def __init__(self):
        super().__init__()
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0
        self._request_mtime = None
        self._capture_requested = False
        self._profile = "production"
        self._pose = None
        self._ready_published = False
        self._photo_session_basis = None
        self._last_photo_frame_at = 0.0
        _log.info("Selfie camera loaded version %s", self.__version__)

    @on_key_pressed("f11")
    def request_capture(self):
        self._capture_requested = True

    def _clear(self):
        self._request_id = None
        self._expires_at = 0.0
        self._ready_frames = 0
        self._profile = "production"
        self._pose = None
        self._ready_published = False

    def _prepare_photo_session(self, camera):
        """Latch the native entry-camera basis once per photo session."""
        now = time.monotonic()
        if self._photo_session_basis is None or now - self._last_photo_frame_at > 1.0:
            self._photo_session_basis = _camera_basis(camera)
            _log.info(
                "Selfie photo-entry camera basis latched: %s",
                self._photo_session_basis,
            )
            try:
                player = gameData.player
                phys_relative = _get_player_matrix(player)
                absolute = _get_player_absolute_matrix(player)
                _log.info(
                    "Selfie player transforms handle=%#x phys_relative=%s absolute=%s "
                    "phys_position=%s absolute_position=%s",
                    int(player.mRootNode.lookupInt),
                    tuple(_xyz(axis) for axis in (phys_relative.right, phys_relative.up, phys_relative.at)),
                    tuple(_xyz(axis) for axis in (absolute.right, absolute.up, absolute.at)),
                    _big_position_xyz(phys_relative.pos),
                    _xyz(absolute.pos),
                )
            except Exception:
                _log.exception("Could not probe selfie player node buffers")
        self._last_photo_frame_at = now
        return self._photo_session_basis

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
            self._pose = _load_pose(profile)
            self._ready_frames = 0
            self._ready_published = False
            _write_status(request_id, "positioning")
        except Exception as error:
            request_id = str(request.get("request_id", "")) if "request" in locals() else ""
            self._clear()
            _write_status(request_id, "error", str(error))

    @_PhotoModeCameraBehaviour.Update.before
    def before_photo_camera_update(self, this, lfTimeStep, camera):
        try:
            basis = self._prepare_photo_session(camera)
            self._poll_request()
            if not self._request_id:
                return
            if time.time() >= self._expires_at:
                _write_status(self._request_id, "error", "request expired")
                self._clear()
                return
            if self._pose is None:
                raise RuntimeError("selfie camera pose is unavailable")
            _apply_pose(camera, self._pose, basis)
            if self._ready_frames == 0:
                _log.info(
                    "Selfie camera FOV requested=%s current=%s",
                    self._pose["fov"],
                    _read_photo_mode_fov(),
                )
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
                if self._photo_session_basis is None:
                    raise RuntimeError("photo-session basis is unavailable")
                pose = _capture_pose(camera, self._photo_session_basis)
                _log.info("Captured selfie camera diagnostic pose: %s", pose)
            except Exception:
                _log.error("Selfie camera capture failed:\n%s", traceback.format_exc())
        if not self._request_id:
            return
        try:
            if self._pose is None:
                raise RuntimeError("selfie camera pose is unavailable")
            (
                native_position,
                final_position,
                player_position,
                desired_position,
            ) = _finish_pose(camera, self._pose, self._photo_session_basis)
            if self._ready_frames == 0:
                _log.info(
                    "Selfie camera position player=%s desired=%s native=%s final=%s "
                    "local_pose=%s",
                    player_position,
                    desired_position,
                    native_position,
                    final_position,
                    self._pose["position"],
                )
        except Exception as error:
            request_id = self._request_id
            self._clear()
            _write_status(request_id, "error", str(error))
            _log.error("Selfie camera finalization failed:\n%s", traceback.format_exc())
            return
        self._ready_frames += 1
        if self._ready_frames >= READY_FRAMES and not self._ready_published:
            self._ready_published = _write_status(self._request_id, "ready")
