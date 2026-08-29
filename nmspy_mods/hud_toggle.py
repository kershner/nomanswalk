# /// script
# dependencies = ["nmspy==170671.5", "pymhf[gui]==0.2.4"]
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
# window_name_override = "HUD Toggle"
# ///

import ctypes
import logging
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from pymhf.gui.decorators import BOOLEAN

from nmspy.common import gameData


_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud_toggle.log")

_STRUCT_SIZE = 0x3AB0
_APP_DATA_SIZE = 0x92AB10
_HUD_HIDDEN_OFFSET = 0x3A8C


def _build_file_logger() -> logging.Logger:
    log = logging.getLogger("HUDToggle.file")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if not log.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="w")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)

    return log


_flog = _build_file_logger()
logger = logging.getLogger("HUDToggle")

_flog.info("=== hud_toggle.py loaded ===")


def _u8(addr: int) -> int:
    return ctypes.c_uint8.from_address(addr).value


def _i32(addr: int) -> int:
    return ctypes.c_int32.from_address(addr).value


def _u32(addr: int) -> int:
    return ctypes.c_uint32.from_address(addr).value


def _u64(addr: int) -> int:
    return ctypes.c_uint64.from_address(addr).value


def _valid_array(addr: int) -> bool:
    ptr = _u64(addr)
    size = _u32(addr + 0x08)
    flag = _u8(addr + 0x0C)

    if flag not in (0, 1):
        return False

    if size > 100_000:
        return False

    if ptr == 0:
        return size == 0

    return 0x10000 <= ptr <= 0x00007FFFFFFFFFFF


def _looks_like_settings(addr: int) -> bool:
    try:
        nonempty_arrays = 0

        for off in range(0x00, 0x100, 0x10):
            if not _valid_array(addr + off):
                return False

            if _u64(addr + off) != 0 and _u32(addr + off + 0x08) > 0:
                nonempty_arrays += 1

        if nonempty_arrays < 2:
            return False

        ranges = [
            (0x39C4, 0, 2),
            (0x39CC, 0, 1),
            (0x3A00, 0, 1),
            (0x3A08, 0, 20),
            (0x3A2C, 0, 1),
            (0x3A38, 0, 3),
            (0x3A54, 0, 2),
            (0x3A58, 0, 2),
            (0x3A5C, 0, 3),
            (0x3A64, 0, 1),
            (0x3A68, 0, 3),
        ]

        for off, low, high in ranges:
            val = _i32(addr + off)
            if val < low or val > high:
                return False

        for off in range(0x3A7C, 0x3AA3):
            if _u8(addr + off) not in (0, 1):
                return False

        return True

    except Exception:
        return False


def _find_settings(mpdata_addr: int) -> list[int]:
    hits = []

    for off in range(0, _APP_DATA_SIZE - _STRUCT_SIZE, 0x10):
        addr = mpdata_addr + off

        if _looks_like_settings(addr):
            hits.append(addr)

    return hits


class HUDToggle(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Toggle HUDHidden with F5."
    __version__ = "2.5"

    def __init__(self):
        super().__init__()
        self._hud_hidden = False
        self._settings_addrs = []
        _flog.info("HUDToggle mod instantiated")

    def _init_settings(self) -> bool:
        if self._settings_addrs:
            return True

        try:
            app = gameData.GcApplication
            _flog.info("GcApplication=%r", app)

            if not app or not app.mpData:
                _flog.info("app/mpData not ready")
                return False

            mpdata_addr = ctypes.addressof(app.mpData.contents)
            _flog.info("mpData=0x%X; scanning for settings", mpdata_addr)

            hits = _find_settings(mpdata_addr)

            if not hits:
                _flog.error("no cGcUserSettingsData candidates found")
                return False

            self._settings_addrs = hits
            self._hud_hidden = bool(_u8(hits[0] + _HUD_HIDDEN_OFFSET))

            _flog.info(
                "found %s candidate(s): %s",
                len(hits),
                ", ".join(f"0x{x:X}" for x in hits),
            )
            _flog.info("current HUDHidden=%s", self._hud_hidden)

            return True

        except Exception:
            _flog.error("_init_settings failed")
            _flog.error(traceback.format_exc())
            return False

    def _apply(self, hidden: bool, source: str) -> None:
        if not self._init_settings():
            _flog.error("[%s] settings unavailable; cannot toggle", source)
            return

        try:
            for addr in self._settings_addrs:
                before = _u8(addr + _HUD_HIDDEN_OFFSET)
                ctypes.c_uint8.from_address(addr + _HUD_HIDDEN_OFFSET).value = int(hidden)
                after = _u8(addr + _HUD_HIDDEN_OFFSET)

                _flog.info("[%s] 0x%X HUDHidden %s -> %s", source, addr, before, after)

            self._hud_hidden = hidden

            msg = f"[{source}] HUD -> {'HIDDEN' if hidden else 'VISIBLE'}"
            logger.info(msg)
            _flog.info(msg)

        except Exception:
            _flog.error("[%s] toggle failed", source)
            _flog.error(traceback.format_exc())

    @on_key_pressed("f5")
    def toggle_hud(self) -> None:
        _flog.info("[F5] key hook fired; current hidden=%s", self._hud_hidden)
        self._apply(not self._hud_hidden, "F5")

    @property
    @BOOLEAN("HUD hidden:")
    def hud_hidden(self) -> bool:
        return self._hud_hidden

    @hud_hidden.setter
    def hud_hidden(self, value: bool) -> None:
        if value != self._hud_hidden:
            self._apply(value, "GUI")
