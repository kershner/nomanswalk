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


TELEPORT_FEET = 2500.0
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
    return math.isfinite(value) and abs(value) < 100_000_000


def _matrix_looks_valid(mat) -> bool:
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


class AirDrop(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Press Y to teleport the player up into the air."
    __version__ = "1.6"

    def __init__(self):
        super().__init__()
        self._last_player_ptr = None
        self._update_count = 0
        _flog.info("AirDrop mod instantiated")

    @nms.cGcPlayer.Update.before
    def on_player_update(self, this, lfStep):
        self._last_player_ptr = this
        self._update_count += 1

    @on_key_pressed("y")
    def air_drop(self):
        _flog.info("[Y] key hook fired")
        _flog.info("seen cGcPlayer.Update count=%s ptr=%r", self._update_count, self._last_player_ptr)

        if not self._last_player_ptr:
            _flog.info("aborting: no cGcPlayer.Update pointer captured yet")
            return

        try:
            player = self._last_player_ptr.contents
            _flog.info("actual player base=0x%X", ctypes.addressof(player))

            try:
                _flog.info("player.mbSpawned=%r", player.mbSpawned)
            except Exception:
                _flog.error("failed reading player.mbSpawned")
                _flog.error(traceback.format_exc())

            root_node = player.mRootNode
            mat = GetNodeAbsoluteTransMatrix(root_node)

            _flog.info(
                "current pos=(%s, %s, %s) at=(%s, %s, %s)",
                mat.pos.x,
                mat.pos.y,
                mat.pos.z,
                mat.at.x,
                mat.at.y,
                mat.at.z,
            )

            if not _matrix_looks_valid(mat):
                _flog.info("aborting: player transform does not look valid")
                return

            up_amount = TELEPORT_FEET * FEET_TO_GAME_UNITS

            new_pos = basic.cTkBigPos(
                basic.Vector3f(mat.pos.x, mat.pos.y - up_amount, mat.pos.z),
                basic.Vector3f(0, 0, 0),
            )
            direction = basic.cTkVector3(mat.at.x, mat.at.y, mat.at.z)
            velocity = basic.cTkVector3(0, 0, 0)

            _flog.info(
                "calling SetToPosition new_pos=(%s, %s, %s)",
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