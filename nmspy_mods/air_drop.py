import ctypes
import logging
import math
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed

import nmspy.data.basic_types as basic
import nmspy.data.types as nms
from nmspy.engine import GetNodeAbsoluteTransMatrix


TELEPORT_FEET = 1000.0
FEET_TO_GAME_UNITS = 0.3048

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
    __version__ = "1.7-planet-up"

    def __init__(self):
        super().__init__()
        self._last_player_ptr = None
        self._last_env_ptr = None
        self._player_update_count = 0
        self._env_update_count = 0
        _flog.info("AirDrop mod instantiated")

    @nms.cGcPlayer.Update.before
    def on_player_update(self, this, lfStep):
        self._last_player_ptr = this
        self._player_update_count += 1

    @nms.cGcPlayerEnvironment.Update.before
    def on_player_environment_update(self, this, lfTimeStep):
        self._last_env_ptr = this
        self._env_update_count += 1

    def _get_player(self):
        try:
            if self._last_player_ptr:
                return self._last_player_ptr.contents
        except Exception:
            pass

        return None

    def _get_environment(self):
        try:
            if self._last_env_ptr:
                return self._last_env_ptr.contents
        except Exception:
            pass

        return None

    def _get_position_and_direction(self, player):
        env = self._get_environment()

        if env is not None:
            try:
                mat = env.mPlayerTM

                if _matrix_looks_valid(mat):
                    _flog.info(
                        "using environment mPlayerTM pos=(%s, %s, %s) at=(%s, %s, %s)",
                        mat.pos.x,
                        mat.pos.y,
                        mat.pos.z,
                        mat.at.x,
                        mat.at.y,
                        mat.at.z,
                    )
                    return mat.pos, mat.at
            except Exception:
                _flog.error("failed reading environment mPlayerTM")
                _flog.error(traceback.format_exc())

        root_node = player.mRootNode
        mat = GetNodeAbsoluteTransMatrix(root_node)

        _flog.info(
            "using player root transform pos=(%s, %s, %s) at=(%s, %s, %s)",
            mat.pos.x,
            mat.pos.y,
            mat.pos.z,
            mat.at.x,
            mat.at.y,
            mat.at.z,
        )

        if not _matrix_looks_valid(mat):
            return None, None

        return mat.pos, mat.at

    def _get_up_vector(self, pos):
        env = self._get_environment()

        if env is not None:
            try:
                up = env.mUp
                ux = float(up.x)
                uy = float(up.y)
                uz = float(up.z)

                normalized = _normalize_vec(ux, uy, uz)

                if normalized is not None and _vector_looks_valid(*normalized):
                    _flog.info(
                        "using environment mUp=(%.6f, %.6f, %.6f)",
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

                dx = float(pos.x) - float(planet.x)
                dy = float(pos.y) - float(planet.y)
                dz = float(pos.z) - float(planet.z)

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

            pos, facing = self._get_position_and_direction(player)

            if pos is None or facing is None:
                _flog.info("aborting: player transform does not look valid")
                return

            up_x, up_y, up_z = self._get_up_vector(pos)
            up_amount = TELEPORT_FEET * FEET_TO_GAME_UNITS

            new_x = float(pos.x) + (up_x * up_amount)
            new_y = float(pos.y) + (up_y * up_amount)
            new_z = float(pos.z) + (up_z * up_amount)

            new_pos = basic.cTkBigPos(
                basic.Vector3f(new_x, new_y, new_z),
                basic.Vector3f(0, 0, 0),
            )
            direction = basic.cTkVector3(float(facing.x), float(facing.y), float(facing.z))
            velocity = basic.cTkVector3(0, 0, 0)

            _flog.info(
                "calling SetToPosition old_pos=(%s, %s, %s) up=(%.6f, %.6f, %.6f) new_pos=(%s, %s, %s)",
                pos.x,
                pos.y,
                pos.z,
                up_x,
                up_y,
                up_z,
                new_pos.local.x,
                new_pos.local.y,
                new_pos.local.z,
            )

            player.SetToPosition(
                ctypes.byref(new_pos),
                ctypes.byref(direction),
                ctypes.byref(velocity),
            )

            _flog.info("SetToPosition returned")

            try:
                if player.mPhysicsController:
                    player.mPhysicsController.contents.mTargetVelocity = basic.cTkVector3(0, 0, 0)
                    _flog.info("cleared physics target velocity")
                else:
                    _flog.info("player.mPhysicsController is empty")
            except Exception:
                _flog.error("failed clearing physics target velocity")
                _flog.error(traceback.format_exc())

            logger.info("Air drop: moved player up %s feet", TELEPORT_FEET)
            _flog.info("Air drop complete: moved player up %s feet", TELEPORT_FEET)

        except Exception:
            _flog.error("unhandled air_drop error")
            _flog.error(traceback.format_exc())