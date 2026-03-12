# /// script
# dependencies = ["pymhf[gui]>=0.2.2"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# always_on_top = true
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "Music Toggle"
# ///

import ctypes
import logging
import os
from datetime import datetime

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed, static_function_hook, Structure
from pymhf.gui.decorators import BOOLEAN

# MASTER_MUSIC_LEVEL is NMS's own RTPC for the music volume bus —
# the same value the in-game audio slider writes to.
MASTER_MUSIC_LEVEL = ctypes.c_uint32(0xF8F6ACB4)
AK_INVALID_GAME_OBJECT = ctypes.c_uint64(0xFFFFFFFFFFFFFFFF)


# SetRTPCValue isn't in nmspy's audiokinetic.py yet; defined here until
# it can be contributed upstream to nmspy/data/audiokinetic.py as AK.SoundEngine.SetRTPCValue.
class _AKExtra(Structure):
    @static_function_hook(
        exported_name=(
            "?SetRTPCValue@SoundEngine@AK@@YA?AW4AKRESULT@@"
            "IM_KHW4AkCurveInterpolation@@_N@Z"
        )
    )
    @staticmethod
    def SetRTPCValue(
        in_rtpcID: ctypes.c_uint32,
        in_value: ctypes.c_float,
        in_gameObjectID: ctypes.c_uint64,
        in_uDuration: ctypes.c_int32,
        in_eFadeCurve: ctypes.c_int32,
        in_bBypassInterp: ctypes.c_bool,
    ) -> ctypes.c_int32:
        pass


_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "musicToggle.log")

def _build_file_logger() -> logging.Logger:
    flog = logging.getLogger("MusicToggle.file")
    flog.setLevel(logging.DEBUG)
    flog.propagate = False
    if not flog.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="a")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        flog.addHandler(fh)
    return flog

_flog = _build_file_logger()
# _flog.info(f"=== musicToggle loaded  {datetime.now().isoformat()} ===")

logger = logging.getLogger("MusicToggle")


class MusicToggle(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Music Toggle"
    __version__ = "1.0"

    def __init__(self):
        super().__init__()
        self._music_enabled: bool = True  # reflects actual game state, not just intent

    @property
    @BOOLEAN("Music enabled:")
    def music_enabled(self) -> bool:
        return self._music_enabled

    @music_enabled.setter
    def music_enabled(self, value: bool) -> None:
        if value != self._music_enabled:
            self._apply(value, "GUI")

    @on_key_pressed("m")
    def toggle_music(self) -> None:
        self._apply(not self._music_enabled, "KEY M")

    def _apply(self, new_state: bool, source: str) -> None:
        self._music_enabled = new_state
        volume = ctypes.c_float(100.0 if new_state else 0.0)
        state = "ON" if new_state else "OFF"
        logger.info(f"[{source}] Music → {state}")
        # _flog.info(f"[{source}] Music → {state}  RTPC={volume.value}")
        try:
            result = _AKExtra.SetRTPCValue(
                MASTER_MUSIC_LEVEL,
                volume,
                AK_INVALID_GAME_OBJECT,
                ctypes.c_int32(0),    # 0ms = instant
                ctypes.c_int32(4),    # AkCurveInterpolation_Linear
                ctypes.c_bool(False),
            )
            if result != 1:  # AK_Success = 1; revert state so it reflects reality
                self._music_enabled = not new_state
                _flog.warning(f"SetRTPCValue failed — reverting state to {'ON' if not new_state else 'OFF'}")
        except Exception as exc:
            self._music_enabled = not new_state
            _flog.error(f"SetRTPCValue FAILED: {exc!r} — reverting state")