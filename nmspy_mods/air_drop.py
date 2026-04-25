# air_drop.py
import ctypes
import logging

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed

import nmspy.data.basic_types as basic
from nmspy.common import gameData
from nmspy.engine import GetNodeAbsoluteTransMatrix

logger = logging.getLogger(__name__)

TELEPORT_FEET = 1000.0
FEET_TO_GAME_UNITS = 0.3048  # assumes 1 game unit is about 1 meter


class AirDrop(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Press Y to teleport the player up into the air."

    @on_key_pressed("y")
    def air_drop(self):
        player = gameData.player
        if player is None or not player.mbSpawned:
            return

        mat = GetNodeAbsoluteTransMatrix(player.mRootNode)
        up_amount = TELEPORT_FEET * FEET_TO_GAME_UNITS

        new_pos = basic.cTkBigPos(
            basic.Vector3f(mat.pos.x, mat.pos.y + up_amount, mat.pos.z),
            basic.Vector3f(0, 0, 0),
        )
        direction = basic.cTkVector3(mat.at.x, mat.at.y, mat.at.z)
        velocity = basic.cTkVector3(0, 0, 0)

        player.SetToPosition(
            ctypes.byref(new_pos),
            ctypes.byref(direction),
            ctypes.byref(velocity),
        )

        if player.mPhysicsController:
            player.mPhysicsController.contents.mTargetVelocity = basic.cTkVector3(0, 0, 0)

        logger.info(f"Air drop: moved player up {TELEPORT_FEET} feet")