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
# window_name_override = "Quick Load"
# ///

import ctypes
import logging
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed

import nmspy.data.types as nms


QUICK_LOAD_KEY = "f9"
UI_SLOT_NUMBER = 0

_OFF_PHASE = 0x0A74
_OFF_SELECTED_FLAG = 0x0A8C
_OFF_PENDING_SLOT = 0x0AEC
_OFF_SELECTED_SLOT = 0x0AF0
_OFF_LOAD_REQUESTED = 0x0AF4
_OFF_CONFIRM = 0x0AF8

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_load.log")


def _build_file_logger() -> logging.Logger:
    log = logging.getLogger("QuickLoad.file")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if not log.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="w")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)

    return log


_flog = _build_file_logger()
logger = logging.getLogger("QuickLoad")

_flog.info("=== quick_load.py loaded ===")


def _rb(base: int, off: int) -> int:
    return ctypes.c_uint8.from_address(base + off).value


def _wb(base: int, off: int, val: int) -> None:
    ctypes.c_uint8.from_address(base + off).value = val


def _ru32(base: int, off: int) -> int:
    return ctypes.c_uint32.from_address(base + off).value


def _wu32(base: int, off: int, val: int) -> None:
    ctypes.c_uint32.from_address(base + off).value = val


class QuickLoad(Mod):
    __author__ = "Tyler Kershner"
    __description__ = f"Press {QUICK_LOAD_KEY.upper()} at the main menu to load save slot {UI_SLOT_NUMBER}"
    __version__ = "1.8"

    def __init__(self):
        super().__init__()
        self._armed = False
        self._last_phase = None
        _flog.info("QuickLoad mod instantiated")

    @on_key_pressed(QUICK_LOAD_KEY)
    def arm_load(self) -> None:
        _flog.info("[%s] key hook fired; armed=%s", QUICK_LOAD_KEY.upper(), self._armed)

        if self._armed:
            return

        logger.info("[%s] Armed — waiting for save select screen", QUICK_LOAD_KEY.upper())
        _flog.info("[%s] armed — waiting for mode selector phase 17", QUICK_LOAD_KEY.upper())

        self._armed = True
        self._last_phase = None

    @nms.cGcApplicationGameModeSelectorState.Update.before
    def on_update_before(self, this, lfTimeStep) -> None:
        if not self._armed:
            return

        try:
            ms = ctypes.addressof(this.contents)
            phase = _ru32(ms, _OFF_PHASE)

            if phase != self._last_phase:
                _flog.info(
                    "selector update: addr=0x%X phase=%s selected_flag=%s pending=%s slot=%s load_requested=%s confirm=%s",
                    ms,
                    phase,
                    _rb(ms, _OFF_SELECTED_FLAG),
                    _ru32(ms, _OFF_PENDING_SLOT),
                    _ru32(ms, _OFF_SELECTED_SLOT),
                    _ru32(ms, _OFF_LOAD_REQUESTED),
                    _rb(ms, _OFF_CONFIRM),
                )
                self._last_phase = phase

            if phase == 17:
                _flog.info("phase 17 found; emulating normal manual-load transition for slot=%s", UI_SLOT_NUMBER)

                _wb(ms, _OFF_SELECTED_FLAG, 1)
                _wu32(ms, _OFF_PENDING_SLOT, UI_SLOT_NUMBER)
                _wu32(ms, _OFF_SELECTED_SLOT, UI_SLOT_NUMBER)
                _wu32(ms, _OFF_LOAD_REQUESTED, 1)
                _wu32(ms, _OFF_PHASE, 18)

                self._armed = False
                logger.info("Loading slot %s...", UI_SLOT_NUMBER)
                _flog.info("Loading slot %s...", UI_SLOT_NUMBER)

        except Exception:
            _flog.error(traceback.format_exc())
            self._armed = False
