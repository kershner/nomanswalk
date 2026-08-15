import logging
import os
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from nmspy.globals import globals

import nmspy.data.types as nms
from shared_state import set_mod_status


_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storm_toggle.log")


def _build_file_logger() -> logging.Logger:
    log = logging.getLogger("StormToggle.file")
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if not log.handlers:
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="a")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)

    return log


_flog = _build_file_logger()
logger = logging.getLogger("StormToggle")

_flog.info("=== storm_toggle.py loaded ===")


class StormToggle(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Toggle No Man's Sky forced storm weather with F9."
    __version__ = "1.2"

    def __init__(self):
        super().__init__()
        self._forced_storm = None

    def _read_debug_bool(self, name: str):
        try:
            return bool(getattr(globals.GcDebugOptions, name))
        except Exception:
            return None

    def _write_debug_bool(self, name: str, value: bool) -> None:
        setattr(globals.GcDebugOptions, name, bool(value))

    def _read_sky(self, name: str):
        try:
            return getattr(globals.GcSkyGlobals, name)
        except Exception:
            return None

    def _write_sky(self, name: str, value) -> None:
        setattr(globals.GcSkyGlobals, name, value)

    def _snapshot(self) -> str:
        return (
            f"DisableStorms={self._read_debug_bool('DisableStorms')} "
            f"ForceExtremeWeather={self._read_debug_bool('ForceExtremeWeather')} "
            f"ForceLoadAllWeather={self._read_debug_bool('ForceLoadAllWeather')} "
            f"ForceStormSetting={self._read_sky('ForceStormSetting')} "
            f"ForceStormStrength={self._read_sky('ForceStormStrength')} "
            f"StormWarningTime={self._read_sky('StormWarningTime')} "
            f"StormTransitionTime={self._read_sky('StormTransitionTime')}"
        )

    def _apply_forced_storm(self) -> None:
        self._write_debug_bool("DisableStorms", False)
        self._write_debug_bool("ForceLoadAllWeather", True)
        self._write_debug_bool("ForceExtremeWeather", True)
        self._write_sky("ForceStormSetting", True)
        self._write_sky("ForceStormStrength", 1.0)

    def _sync_status(self) -> None:
        setting = self._read_sky("ForceStormSetting")
        strength = self._read_sky("ForceStormStrength")
        try:
            forced = bool(setting) and float(strength) > 0.0
        except (TypeError, ValueError):
            forced = bool(setting) and bool(strength)

        if forced != self._forced_storm:
            self._forced_storm = forced
            set_mod_status("storm", "forced" if forced else "normal")

    @nms.cGcPlanet.Construct.after
    def on_planet_construct(self, this, *args) -> None:
        self._sync_status()

    @nms.cGcPlanet.Generate.after
    def on_planet_generate(self, this, *args) -> None:
        self._sync_status()

    @nms.cGcPlanet.SetupRegionMap.after
    def on_region_map(self, this) -> None:
        self._sync_status()

    @nms.cGcPlanet.UpdateWeather.after
    def on_update_weather(self, this, lfTimeStep) -> None:
        self._sync_status()

    def _start_storm(self, source: str) -> None:
        try:
            before = self._snapshot()

            self._apply_forced_storm()
            self._sync_status()

            msg = f"[{source}] forced storm ON: {before} -> {self._snapshot()}"
            logger.info(msg)
            _flog.info(msg)
        except Exception:
            _flog.error("[%s] failed to force storm on", source)
            _flog.error(traceback.format_exc())

    def _stop_storm(self, source: str) -> None:
        try:
            before = self._snapshot()

            self._write_sky("ForceStormStrength", 0.0)
            self._write_sky("ForceStormSetting", False)
            self._write_debug_bool("ForceExtremeWeather", False)
            self._write_debug_bool("DisableStorms", True)
            self._sync_status()

            msg = f"[{source}] forced storm OFF: {before} -> {self._snapshot()}"
            logger.info(msg)
            _flog.info(msg)
        except Exception:
            _flog.error("[%s] failed to force storm off", source)
            _flog.error(traceback.format_exc())

    @on_key_pressed("f9")
    def toggle_storm(self) -> None:
        try:
            self._sync_status()
            if self._forced_storm:
                self._stop_storm("F9")
            else:
                self._start_storm("F9")
        except Exception:
            _flog.error("[F9] failed to toggle storm")
            _flog.error(traceback.format_exc())
