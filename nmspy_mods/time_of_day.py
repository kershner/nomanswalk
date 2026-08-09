# /// script
# dependencies = ["nmspy>=0.1.0", "pymhf[gui]>=0.2.2"]
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
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from nmspy.globals import globals
from shared_state import set_mod_status


DAY_KEY = "f6"
NIGHT_KEY = "f7"
RESUME_TIME_KEY = "f8"

# ForceTimeOfDay appears to be a 0.0-1.0 day-cycle debug override.
# If daytime/nighttime are offset in-game, adjust these two values only.
DAY_TIME = 0.5
NIGHT_TIME = 0.0

# -1.0 releases the debug override and resumes the game's actual planet time.
NORMAL_TIME = -1.0

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "time_of_day.log")


def _build_file_logger() -> logging.Logger:
    log = logging.getLogger("TimeOfDay.file")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if not log.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="a")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)

    return log


_flog = _build_file_logger()
logger = logging.getLogger("TimeOfDay")

_flog.info("=== time_of_day.py loaded ===")


class TimeOfDay(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Set or release No Man's Sky time-of-day debug override with F6/F7/F8."
    __version__ = "1.2"

    def _set_time(self, value: float, source: str) -> None:
        try:
            before = globals.GcDebugOptions.ForceTimeOfDay
            globals.GcDebugOptions.ForceTimeOfDay = float(value)
            after = globals.GcDebugOptions.ForceTimeOfDay

            if value == DAY_TIME:
                set_mod_status("time", "day")
            elif value == NIGHT_TIME:
                set_mod_status("time", "night")
            else:
                set_mod_status("time", "normal")

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
