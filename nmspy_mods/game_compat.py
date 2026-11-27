"""Current game layout corrections for layouts not yet in NMSpy."""

import ctypes
import logging


# Verified against the installed Steam builds on 2026-11-25 and 2026-11-26.
# cGcApplication::Data::Data at RVA 0x2CB090 constructs GameState at
# +0xEB0 and Simulation at +0x4CD620. Their nested PlayerState, Player,
# Environment and PlayerEnvironment offsets are unchanged.
_GAME_STATE_OFFSET = 0xEB0
_SIMULATION_OFFSET = 0x4CD620
_USER_SETTINGS_OFFSET = 0x307C68


def user_settings_offset():
    # RVA 0x365356 passes Data+0x307C68 to the settings apply routine.
    # HUDHidden/HUDMinimal remain at +0x6D68/+0x6D69 within settings.
    return _USER_SETTINGS_OFFSET


def simulation_offset():
    return _SIMULATION_OFFSET


def _member_property(struct_type, offset):
    def read_member(game_data):
        data = game_data.data
        if data is not None:
            return struct_type.from_address(ctypes.addressof(data) + offset)

    return property(read_member)


def apply_game_layout():
    """Correct shared GameData accessors using the current verified layout.

    Keep this in the project rather than editing the installed dependency.
    All mods using NMSpy's shared gameData object receive the same correction.
    Executable hashes do not select layouts: unrelated patches must not revert
    these accessors to obsolete NMSpy offsets. Actual layout changes still
    require updating the offsets above.
    """
    from nmspy.common import gameData
    import nmspy.data.types as nms

    game_state, simulation = _GAME_STATE_OFFSET, _SIMULATION_OFFSET
    data_type = type(gameData)
    data_type.game_state = _member_property(nms.cGcGameState, game_state)
    data_type.simulation = _member_property(nms.cGcSimulation, simulation)
    logging.getLogger(__name__).info(
        "Applied NMS layout correction: GameState=0x%X Simulation=0x%X",
        game_state, simulation,
    )
