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
# window_name_override = "Quick Load"
# ///

import ctypes
import logging
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed

import nmspy.data.types as nms

# ── Config ────────────────────────────────────────────────────────────────────

QUICK_LOAD_KEY = "f9"
UI_SLOT_NUMBER = 1   # 1-based UI slot number

# ── Offsets ───────────────────────────────────────────────────────────────────

_OFF_SUBSTATE = 0x0A60   # uint8 sub-state counter
_OFF_0A78     = 0x0A78   # slot-selected flag
_OFF_0AE4     = 0x0AE4   # confirm flag
_OFF_0ADC     = 0x0ADC   # slot number (uint32 — must not be written as uint8)

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_PATH = __file__.replace(".py", ".log")
_flog = logging.getLogger("QuickLoad.file")
_flog.setLevel(logging.DEBUG)
_flog.propagate = False
if not _flog.handlers:
    fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="a")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    _flog.addHandler(fh)

logger = logging.getLogger("QuickLoad")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _rb(base: int, off: int) -> int:
    return ctypes.c_uint8.from_address(base + off).value

def _wb(base: int, off: int, val: int) -> None:
    ctypes.c_uint8.from_address(base + off).value = val

def _wu32(base: int, off: int, val: int) -> None:
    ctypes.c_uint32.from_address(base + off).value = val

# ── Mod ───────────────────────────────────────────────────────────────────────

class QuickLoad(Mod):
    __author__      = "Tyler Kershner"
    __description__ = f"Press {QUICK_LOAD_KEY.upper()} at the main menu to load save slot {UI_SLOT_NUMBER}"
    __version__     = "1.0"

    _step: int = 0

    @on_key_pressed(QUICK_LOAD_KEY)
    def arm_load(self) -> None:
        if self._step != 0:
            return
        logger.info(f"[{QUICK_LOAD_KEY.upper()}] Armed — waiting for save select screen")
        self._step = 1

    @nms.cGcApplicationGameModeSelectorState.Update.before
    def on_update_before(self, this, lfTimeStep) -> None:
        if self._step == 0:
            return
        try:
            ms  = ctypes.addressof(this.contents)
            sub = _rb(ms, _OFF_SUBSTATE)

            if self._step == 1 and sub == 17:
                # Set the slot identity fields, then step into the load path.
                # 0x0ADC is a uint32 — writing only a byte leaves 0xFFFFFF__
                # which the mode selector misreads and routes to new-game init.
                _wb(ms,   _OFF_0A78,    1             )
                _wb(ms,   _OFF_0AE4,    1             )
                _wu32(ms, _OFF_0ADC,    UI_SLOT_NUMBER)
                _wb(ms,   _OFF_SUBSTATE, 18            )
                self._step = 2

            elif self._step == 2:
                # Sub-19 is where the mode selector calls LoadFromPersistentStorage
                _wb(ms, _OFF_SUBSTATE, 19)
                self._step = 0
                logger.info(f"Loading slot {UI_SLOT_NUMBER}...")

        except Exception:
            _flog.error(traceback.format_exc())
            self._step = 0