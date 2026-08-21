import ctypes
import logging
import traceback
from dataclasses import dataclass, field

from pymhf import Mod, ModState
from pymhf.core.hooking import on_key_pressed

import nmspy.data.types as nms
from shared_state import _make_logger, set_mod_status


LOW_GRAVITY_MULTIPLIER = 0.2
NORMAL_GRAVITY_MULTIPLIER = 1.0

_flog = _make_logger("GravityToggle.file", "gravity_toggle.log")
logger = logging.getLogger("GravityToggle")

_flog.info("=== gravity_toggle.py loaded ===")


@dataclass
class GravityToggleState(ModState):
    low_gravity_enabled: bool | None = None
    planets: list[nms.cGcPlanet] = field(default_factory=list)


class GravityToggle(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Toggle No Man's Sky planet gravity with F10."
    __version__ = "1.0"

    state = GravityToggleState()

    @nms.cGcPlanet.SetupRegionMap.after
    def on_region_map(self, this: ctypes._Pointer[nms.cGcPlanet]) -> None:
        try:
            planet = this.contents
            if all(ctypes.addressof(p) != ctypes.addressof(planet) for p in self.state.planets):
                self.state.planets.append(planet)

            msg = f"cached planet; count={len(self.state.planets)} low_gravity={self.state.low_gravity_enabled}"
            logger.info(msg)
            _flog.info(msg)
        except Exception:
            _flog.error("failed while caching planet")
            _flog.error(traceback.format_exc())

    @nms.cGcPlanet.UpdateGravity.after
    def on_update_gravity(self, this: ctypes._Pointer[nms.cGcPlanet], lfNewGravityMultiplier: float):
        low_gravity = abs(float(lfNewGravityMultiplier) - LOW_GRAVITY_MULTIPLIER) < 0.001
        if low_gravity != self.state.low_gravity_enabled:
            self.state.low_gravity_enabled = low_gravity
            set_mod_status("gravity", "low" if low_gravity else "normal")

    def _apply_gravity(self, multiplier: float, source: str) -> None:
        try:
            if not self.state.planets:
                msg = f"[{source}] no cached planets yet; teleport/load a planet, then try again"
                logger.warning(msg)
                _flog.warning(msg)
                return

            for planet in self.state.planets:
                planet.UpdateGravity(float(multiplier))

            msg = f"[{source}] set gravity multiplier to {multiplier} on {len(self.state.planets)} cached planet(s)"
            logger.info(msg)
            _flog.info(msg)
        except Exception:
            _flog.error("[%s] failed to apply gravity", source)
            _flog.error(traceback.format_exc())

    @on_key_pressed("f10")
    def toggle_gravity(self) -> None:
        multiplier = NORMAL_GRAVITY_MULTIPLIER if self.state.low_gravity_enabled else LOW_GRAVITY_MULTIPLIER
        self._apply_gravity(multiplier, "F10")
