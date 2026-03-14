# /// script
# dependencies = ["nmspy>=0.1.0", "pymhf[gui]>=0.2.2"]
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
import time

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from pymhf.gui.decorators import BOOLEAN

from nmspy.common import gameData
from nmspy.decorators import on_fully_booted

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud_toggle.log")

def _build_file_logger() -> logging.Logger:
    flog = logging.getLogger("HUDToggle.file")
    flog.setLevel(logging.DEBUG)
    flog.propagate = False
    if not flog.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="w")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        flog.addHandler(fh)
    return flog

_flog = _build_file_logger()

logger = logging.getLogger("HUDToggle")

# Offset of HUDHidden within cGcUserSettingsData (bool, 1 byte).
# This is the same flag the in-game Options > General > HUD menu writes.
_HUD_HIDDEN_OFFSET = 0x3A8C

# Size of cGcUserSettingsData. The game keeps two copies back-to-back
# (double-buffered current + backup), so we write to both.
_STRUCT_SIZE = 0x3AB0

_APP_DATA_SIZE = 0x864D60

# Exact field values used to locate cGcUserSettingsData within cGcApplication.Data.
# These must match your current in-game settings — update if needed:
#   Language:        0=English, 1=French, 2=Italian, 3=German, 4=Spanish, ...
#   PlayerVoice:     0=Off, 1=High, 2=Low, 3=Alien
#   SuitVoice:       0=Off, 1=High, 2=Low
#   TemperatureUnit: 0=Invalid, 1=Celsius, 2=Fahrenheit, 3=Kelvin
_FINGERPRINT = [
    (0x3A08, 0),  # Language        = English
    (0x3A38, 0),  # PlayerVoice     = Low
    (0x3A58, 0),  # SuitVoice       = Off
    (0x3A5C, 2),  # TemperatureUnit = Fahrenheit
]


def _find_settings(mpdata_addr: int) -> list[int]:
    """Scan cGcApplication.Data for copies of cGcUserSettingsData using field fingerprinting."""
    hits = []
    i = 0
    try:
        while i < _APP_DATA_SIZE - _STRUCT_SIZE:
            if all(
                ctypes.c_int32.from_address(mpdata_addr + i + off).value == val
                for off, val in _FINGERPRINT
            ):
                hits.append(mpdata_addr + i)
            i += 16
    except Exception as exc:
        _flog.error(f"_find_settings scan error at offset 0x{i:X}: {exc!r}")
    return hits


class HUDToggle(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "HUD Toggle"
    __version__ = "1.0"

    def __init__(self):
        super().__init__()
        self._hud_hidden: bool = False
        self._settings_addrs: list[int] = []
        # _flog.info("=== hud_toggle loaded ===")
        self._try_init()

    def _try_init(self):
        if self._settings_addrs:
            return
        try:
            app = gameData.GcApplication
            if not app or not app.mpData:
                return
            mpdata_addr = ctypes.addressof(app.mpData.contents)
            hits = _find_settings(mpdata_addr)
            if not hits:
                _flog.error(
                    "cGcUserSettingsData not found — update _FINGERPRINT to match your settings."
                )
                return
            self._settings_addrs = hits
            self._hud_hidden = bool(
                ctypes.c_uint8.from_address(hits[0] + _HUD_HIDDEN_OFFSET).value
            )
            # _flog.info(f"Ready. Found {len(hits)} settings instance(s). HUDHidden={self._hud_hidden}")
        except Exception as exc:
            _flog.error(f"_try_init failed: {exc!r}")

    @on_fully_booted
    def on_booted(self):
        self._try_init()

    # ── Core toggle ───────────────────────────────────────────────────────────

    def _apply(self, new_state: bool, source: str) -> None:
        if not self._settings_addrs:
            self._try_init()
        if not self._settings_addrs:
            _flog.error(f"[{source}] Settings not found — cannot toggle HUD")
            return
        try:
            for addr in self._settings_addrs:
                ctypes.c_uint8.from_address(addr + _HUD_HIDDEN_OFFSET).value = int(new_state)
            self._hud_hidden = new_state
            logger.info(f"[{source}] HUD -> {'HIDDEN' if new_state else 'VISIBLE'}")
            # _flog.info(f"[{source}] HUD -> {'HIDDEN' if new_state else 'VISIBLE'}")
        except Exception as exc:
            _flog.error(f"[{source}] toggle failed: {exc!r}")

    # ── Key binding ───────────────────────────────────────────────────────────

    @on_key_pressed("f5")
    def toggle_hud(self) -> None:
        self._apply(not self._hud_hidden, "F5")

    # ── GUI toggle ────────────────────────────────────────────────────────────

    @property
    @BOOLEAN("HUD hidden:")
    def hud_hidden(self) -> bool:
        return self._hud_hidden

    @hud_hidden.setter
    def hud_hidden(self, value: bool) -> None:
        if value != self._hud_hidden:
            self._apply(value, "GUI")