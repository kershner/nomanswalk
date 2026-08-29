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

import logging
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
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

_flog = _make_logger("TimeOfDay.file", "time_of_day.log")
logger = logging.getLogger("TimeOfDay")

_flog.info("=== time_of_day.py loaded ===")


class TimeOfDay(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Set or release No Man's Sky time-of-day debug override with F6/F7/F8."
    __version__ = "1.2"

    def __init__(self):
        super().__init__()
        self._last_status = None

    def _sync_status(self) -> None:
        value = float(globals.GcDebugOptions.ForceTimeOfDay)
        if abs(value - DAY_TIME) < 0.001:
            status = "day"
        elif abs(value - NIGHT_TIME) < 0.001:
            status = "night"
        else:
            status = "normal"

        if status != self._last_status:
            self._last_status = status
            set_mod_status("time", status)

    @nms.cGcSky.Update.after
    def on_sky_update(self, this, lfTimeStep) -> None:
        self._sync_status()

    def _set_time(self, value: float, source: str) -> None:
        try:
            before = globals.GcDebugOptions.ForceTimeOfDay
            globals.GcDebugOptions.ForceTimeOfDay = float(value)
            after = globals.GcDebugOptions.ForceTimeOfDay

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
