"""Small, executable-specific corrections for layouts not yet in NMSpy."""

import ctypes
import logging


# Steam NMS.exe installed 2026-11-25 (SHA-1, as used by pyMHF).
# cGcApplication::Data::Data at RVA 0x2CB090 constructs GameState at
# +0xEB0 and Simulation at +0x4CD620. Their nested PlayerState, Player,
# Environment and PlayerEnvironment offsets are unchanged.
_APP_LAYOUTS = {
    "013a91b8edc9714f3323aa77f658d24b210b1737": (0xEB0, 0x4CD620, 0x307C68),
}


def user_settings_offset(binary_hash):
    # RVA 0x365356 passes Data+0x307C68 to the settings apply routine.
    # HUDHidden/HUDMinimal remain at +0x6D68/+0x6D69 within settings.
    layout = _APP_LAYOUTS.get(binary_hash)
    return layout[2] if layout else 0x307C08


def simulation_offset(binary_hash):
    layout = _APP_LAYOUTS.get(binary_hash)
    return layout[1] if layout else 0x4CD580


def _member_property(struct_type, offset):
    def read_member(game_data):
        data = game_data.data
        if data is not None:
            return struct_type.from_address(ctypes.addressof(data) + offset)

    return property(read_member)


def apply_game_layout():
    """Correct shared GameData accessors only for the verified executable.

    Keep this in the project rather than editing the installed dependency.
    All mods using NMSpy's shared gameData object receive the same correction.
    """
    from pymhf.core import _internal
    from nmspy.common import gameData
    import nmspy.data.types as nms

    layout = _APP_LAYOUTS.get(_internal.BINARY_HASH)
    if layout is None:
        return

    game_state, simulation, _ = layout
    data_type = type(gameData)
    data_type.game_state = _member_property(nms.cGcGameState, game_state)
    data_type.simulation = _member_property(nms.cGcSimulation, simulation)
    logging.getLogger(__name__).info(
        "Applied NMS layout correction: GameState=0x%X Simulation=0x%X",
        game_state, simulation,
    )
