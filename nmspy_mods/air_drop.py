import ctypes
import logging
import math
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import Structure, on_key_pressed, static_function_hook

import nmspy.data.basic_types as basic
import nmspy.data.types as nms
from nmspy.common import gameData


class _PlayerCosmos(Structure):
    @static_function_hook(
        "48 89 5C 24 ? 48 89 6C 24 ? 48 89 74 24 ? 57 48 83 EC ? "
        "48 8B FA 48 8B D9 48 8B 15 ? ? ? ? 48 8D 0D ? ? ? ? 49 8B F1 49 8B E8"
    )
    @staticmethod
    def SetToPosition(
        player: ctypes.c_void_p,
        position: ctypes._Pointer[basic.cTkBigPos],
        direction: ctypes._Pointer[basic.cTkVector3],
        velocity: ctypes._Pointer[basic.cTkVector3],
    ):
        pass


TELEPORT_FEET = 1000.0
FEET_TO_GAME_UNITS = 0.3048
_COSMOS_ENV_UP_OFFSET = 0x50

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "air_drop.log")


def _build_file_logger() -> logging.Logger:
    log = logging.getLogger("AirDrop.file")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if not log.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="w")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)

    return log


_flog = _build_file_logger()
logger = logging.getLogger("AirDrop")

_flog.info("=== air_drop.py loaded ===")


def _valid_float(value: float) -> bool:
    return math.isfinite(float(value)) and abs(float(value)) < 100_000_000


def _vec_len(x: float, y: float, z: float) -> float:
    return math.sqrt((x * x) + (y * y) + (z * z))


def _normalize_vec(x: float, y: float, z: float):
    length = _vec_len(x, y, z)

    if length < 0.0001:
        return None

    return x / length, y / length, z / length


def _vector_looks_valid(x: float, y: float, z: float) -> bool:
    if not (_valid_float(x) and _valid_float(y) and _valid_float(z)):
        return False

    return _vec_len(x, y, z) > 0.0001


def _matrix_looks_valid(mat) -> bool:
    try:
        vals = (
            float(mat.pos.x),
            float(mat.pos.y),
            float(mat.pos.z),
            float(mat.at.x),
            float(mat.at.y),
            float(mat.at.z),
        )

        if not all(_valid_float(v) for v in vals):
            return False

        pos_nonzero = any(abs(v) > 0.001 for v in vals[:3])
        dir_nonzero = any(abs(v) > 0.001 for v in vals[3:])

        return pos_nonzero and dir_nonzero

    except Exception:
        return False


class AirDrop(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Press Y to teleport the player up away from the planet."
    __version__ = "2.6-cosmos-player-offset"

    def __init__(self):
        super().__init__()
        self._last_player_ptr = None
        self._last_env_ptr = None
        self._player_update_count = 0
        self._env_update_count = 0
        _flog.info("AirDrop mod instantiated")

    def _get_player(self):
        try:
            if self._last_player_ptr:
                return self._last_player_ptr.contents
        except Exception:
            pass

        try:
            return gameData.player
        except Exception:
            return None

    def _get_environment(self):
        try:
            if self._last_env_ptr:
                return self._last_env_ptr.contents
        except Exception:
            pass

        try:
            return gameData.player_environment
        except Exception:
            return None

    def _get_position_and_direction(self):
        env = self._get_environment()
        if env is None:
            return None, None

        matrix = basic.cTkPhysRelMat34.from_address(ctypes.addressof(env))
        pos = matrix.pos
        facing = matrix.at
        local = pos.local
        offset = pos.offset
        if not all(
            _valid_float(value)
            for value in (
                local.x, local.y, local.z,
                offset.x, offset.y, offset.z,
            )
        ):
            return None, None
        if not _vector_looks_valid(float(facing.x), float(facing.y), float(facing.z)):
            return None, None
        return pos, facing

    @on_key_pressed("f4")
    def probe_position(self):
        """Read-only Cosmos position probe; does not move the player."""
        try:
            player = self._get_player()
            if player is None:
                _flog.info("[F4 PROBE] player unavailable")
                return
            env = self._get_environment()
            if env is None:
                _flog.info("[F4 PROBE] environment unavailable")
                return
            matrix = basic.cTkPhysRelMat34.from_address(ctypes.addressof(env))
            _flog.info(
                "[F4 PROBE RAW] player=0x%X env=0x%X local=(%r, %r, %r) offset=(%r, %r, %r) at=(%r, %r, %r)",
                ctypes.addressof(player),
                ctypes.addressof(env),
                float(matrix.pos.local.x), float(matrix.pos.local.y), float(matrix.pos.local.z),
                float(matrix.pos.offset.x), float(matrix.pos.offset.y), float(matrix.pos.offset.z),
                float(matrix.at.x), float(matrix.at.y), float(matrix.at.z),
            )
            pos, facing = self._get_position_and_direction()
            if pos is None or facing is None:
                _flog.info("[F4 PROBE] position unavailable or implausible")
                return
            up = self._get_up_vector(pos)
            _flog.info(
                "[F4 PROBE] local=(%.3f, %.3f, %.3f) offset=(%.3f, %.3f, %.3f) up=(%.6f, %.6f, %.6f) facing=(%.6f, %.6f, %.6f)",
                float(pos.local.x), float(pos.local.y), float(pos.local.z),
                float(pos.offset.x), float(pos.offset.y), float(pos.offset.z),
                up[0], up[1], up[2],
                float(facing.x), float(facing.y), float(facing.z),
            )
        except Exception:
            _flog.error("[F4 PROBE] failed")
            _flog.error(traceback.format_exc())

    def _get_up_vector(self, pos):
        env = self._get_environment()

        if env is not None:
            try:
                # Cosmos expanded the transform at the start of
                # cGcPlayerEnvironment. nmspy's historical +0x40 mUp field
                # now aliases the player's absolute position; the actual
                # outward unit vector moved to +0x50.
                up = basic.Vector3f.from_address(
                    ctypes.addressof(env) + _COSMOS_ENV_UP_OFFSET
                )
                ux = float(up.x)
                uy = float(up.y)
                uz = float(up.z)

                normalized = _normalize_vec(ux, uy, uz)

                if normalized is not None and _vector_looks_valid(*normalized):
                    _flog.info(
                        "using Cosmos environment mUp=(%.6f, %.6f, %.6f)",
                        normalized[0],
                        normalized[1],
                        normalized[2],
                    )
                    return normalized
            except Exception:
                _flog.error("failed reading environment mUp")
                _flog.error(traceback.format_exc())

            try:
                planet = env.mNearestPlanetPos

                dx = float(pos.local.x) - float(planet.x)
                dy = float(pos.local.y) - float(planet.y)
                dz = float(pos.local.z) - float(planet.z)

                normalized = _normalize_vec(dx, dy, dz)

                if normalized is not None and _vector_looks_valid(*normalized):
                    _flog.info(
                        "using position-minus-planet up=(%.6f, %.6f, %.6f) planet=(%s, %s, %s)",
                        normalized[0],
                        normalized[1],
                        normalized[2],
                        planet.x,
                        planet.y,
                        planet.z,
                    )
                    return normalized
            except Exception:
                _flog.error("failed deriving up from nearest planet position")
                _flog.error(traceback.format_exc())

        _flog.info("falling back to fixed -Y up vector")
        return 0.0, -1.0, 0.0

    @on_key_pressed("y")
    def air_drop(self):
        _flog.info("[Y] key hook fired")
        _flog.info(
            "seen updates player=%s env=%s player_ptr=%r env_ptr=%r",
            self._player_update_count,
            self._env_update_count,
            self._last_player_ptr,
            self._last_env_ptr,
        )

        player = self._get_player()

        if player is None:
            _flog.info("aborting: no cGcPlayer.Update pointer captured yet")
            return

        try:
            _flog.info("actual player base=0x%X", ctypes.addressof(player))

            try:
                _flog.info("player.mbSpawned=%r", player.mbSpawned)
            except Exception:
                _flog.error("failed reading player.mbSpawned")
                _flog.error(traceback.format_exc())

            pos, facing = self._get_position_and_direction()

            if pos is None or facing is None:
                _flog.info("aborting: player transform does not look valid")
                return

            up_x, up_y, up_z = self._get_up_vector(pos)
            up_amount = TELEPORT_FEET * FEET_TO_GAME_UNITS

            new_pos = basic.cTkBigPos(
                basic.Vector3f(
                    float(pos.local.x) + (up_x * up_amount),
                    float(pos.local.y) + (up_y * up_amount),
                    float(pos.local.z) + (up_z * up_amount),
                ),
                basic.Vector3f(
                    float(pos.offset.x),
                    float(pos.offset.y),
                    float(pos.offset.z),
                ),
            )
            direction = basic.cTkVector3(
                float(facing.x), float(facing.y), float(facing.z)
            )
            velocity = basic.cTkVector3(0, 0, 0)

            _flog.info(
                "calling SetToPosition player=0x%X old_local=(%s, %s, %s) up=(%.6f, %.6f, %.6f) new_local=(%s, %s, %s)",
                ctypes.addressof(player),
                pos.local.x,
                pos.local.y,
                pos.local.z,
                up_x,
                up_y,
                up_z,
                new_pos.local.x,
                new_pos.local.y,
                new_pos.local.z,
            )

            _PlayerCosmos.SetToPosition(
                ctypes.c_void_p(ctypes.addressof(player)),
                ctypes.byref(new_pos),
                ctypes.byref(direction),
                ctypes.byref(velocity),
            )
            _flog.info("SetToPosition invocation finished")
            logger.info("Air drop: moved player up %s feet", TELEPORT_FEET)

        except Exception:
            _flog.error("unhandled air_drop error")
            _flog.error(traceback.format_exc())
