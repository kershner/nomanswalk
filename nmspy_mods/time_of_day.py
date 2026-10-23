# /// script
# dependencies = ["nmspy==170671.5", "pymhf[gui]==0.2.4"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "Time Of Day"
# ///

import ctypes
import logging
import math
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from pymhf.core import _internal
from nmspy.globals import globals

import nmspy.data.types as nms
from shared_state import _make_logger, set_mod_status


DAY_KEY = "f6"
NIGHT_KEY = "f7"
RESUME_TIME_KEY = "f8"

# ForceTimeOfDay appears to be a 0.0-1.0 day-cycle debug override.
# If daytime/nighttime are offset in-game, adjust these two values only.
DAY_TIME = 0.5
NIGHT_TIME = 0.0

# -1.0 releases the debug override and resumes the game's actual planet time.
NORMAL_TIME = -1.0

# This executable's reflection metadata and serializer both put ForceTimeOfDay
# at 0x780. NMSpy 181442.1 declares 0x784 (ForceWaterColourIndex in this build),
# whose default integer -1 reads as NaN. Limit the workaround to this exact EXE.
_TIME_OFFSETS = {"d7e4c0b20e2fa555af2b7e1b358e05cebf8fabc8": 0x780}


def _time_field():
    debug = globals.GcDebugOptions
    offset = _TIME_OFFSETS.get(_internal.BINARY_HASH)
    if offset is None:
        offset = type(debug).ForceTimeOfDay.offset
    return ctypes.c_float.from_address(ctypes.addressof(debug) + offset)

_flog = _make_logger("TimeOfDay.file", "time_of_day.log")
logger = logging.getLogger("TimeOfDay")

_flog.info("=== time_of_day.py loaded ===")


class TimeOfDay(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Set or release No Man's Sky time-of-day debug override with F6/F7/F8."
    __version__ = "1.3"

    def __init__(self):
        super().__init__()
        self._last_status = None

    def _sync_status(self) -> None:
        value = float(_time_field().value)
        if not math.isfinite(value) or not -1.01 <= value <= 1.01:
            return
        if abs(value - DAY_TIME) < 0.001:
            status = "day"
        elif abs(value - NIGHT_TIME) < 0.001:
            status = "night"
        else:
            status = "normal"

        if status != self._last_status:
            self._last_status = status
            set_mod_status("time", status)

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this) -> None:
        self._sync_status()

    def _set_time(self, value: float, source: str) -> None:
        try:
            time_field = _time_field()
            before = float(time_field.value)
            if not math.isfinite(before) or not -1.01 <= before <= 1.01:
                _flog.error(
                    "[%s] refusing ForceTimeOfDay write: current value %r is not plausible",
                    source,
                    before,
                )
                return
            time_field.value = float(value)
            after = time_field.value

            self._sync_status()

            msg = f"[{source}] ForceTimeOfDay {before} -> {after}"
            logger.info(msg)
            _flog.info(msg)
        except Exception:
            _flog.error("[%s] failed to set time", source)
            _flog.error(traceback.format_exc())

    @on_key_pressed(DAY_KEY)
    def set_day(self) -> None:
        self._set_time(DAY_TIME, DAY_KEY.upper())

    @on_key_pressed(NIGHT_KEY)
    def set_night(self) -> None:
        self._set_time(NIGHT_TIME, NIGHT_KEY.upper())

    @on_key_pressed(RESUME_TIME_KEY)
    def resume_time(self) -> None:
        self._set_time(NORMAL_TIME, RESUME_TIME_KEY.upper())
