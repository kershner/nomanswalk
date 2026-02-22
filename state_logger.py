# /// script
# dependencies = ["pymhf[gui]>=0.1.16"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# shown = false
# always_on_top = false
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "NMS Player State Detector"
# ///

import ctypes
import json
import logging
import os
import time
import traceback
from dataclasses import dataclass
from typing import Optional

from pymhf import Mod, ModState
from pymhf.gui.decorators import STRING
from pymhf.gui import FLOAT

import nmspy.data.types as nms
from nmspy.data.enums import EnvironmentLocation
from nmspy.decorators import on_state_change, on_fully_booted
from nmspy.common import gameData

_base_dir   = os.path.dirname(os.path.abspath(__file__))
_state_path = os.path.join(_base_dir, "nms_state.json")
_log_path   = os.path.join(_base_dir, "nms_state_logger.log")

logger = logging.getLogger("NMSStateDetector")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)

DEFAULT_POLL_INTERVAL = 5.0


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _read_enum32(val) -> int:
    return int.from_bytes(bytes(val)[:4], "little")


def _enum_name(enum_class, value: int) -> str:
    try:
        return enum_class(value).name
    except (ValueError, KeyError):
        return str(value)


def _str(val) -> str:
    try:
        raw = bytes(val)
        return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _vec3(val) -> dict:
    try:
        return {
            "x": round(float(val.x), 3),
            "y": round(float(val.y), 3),
            "z": round(float(val.z), 3),
        }
    except Exception:
        return {"x": 0.0, "y": 0.0, "z": 0.0}


def _validate_address(ga) -> bool:
    try:
        return (
            abs(int(ga.VoxelX)) < 5000
            and abs(int(ga.VoxelZ)) < 5000
            and 0 <= int(ga.VoxelY) <= 255
            and 0 <= int(ga.SolarSystemIndex) < 800
            and 0 <= int(ga.PlanetIndex) <= 5
        )
    except Exception:
        return False


def _write_state(payload: dict):
    payload["timestamp"] = time.time()
    with open(_state_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Data-gathering functions
# ---------------------------------------------------------------------------

def _gather_player_data(current_state: str) -> dict:
    try:
        ps = gameData.player_state
        if ps is None:
            return {}
        health = int(ps.miHealth)
        if not (0 <= health < 50_000_000):
            return {}
        result = {
            "name": _str(ps.mNameWithTitle),
            "health": health,
            "shield": max(0, int(ps.miShield)),
            "units": int(ps.muUnits),
            "nanites": int(ps.muNanites),
            "quicksilver": int(ps.muSpecials),
        }
        if current_state == "IN_COCKPIT":
            result["ship_health"] = max(0, int(ps.miShipHealth))
        return result
    except Exception:
        logger.warning(f"_gather_player_data failed: {traceback.format_exc()}")
        return {}


def _gather_player_movement() -> dict:
    try:
        p = gameData.player
        if p is None:
            return {}
        stamina = float(p.mfStamina)
        if not (0.0 <= stamina <= 1_000_000.0):
            return {}
        return {
            "stamina": round(stamina, 3),
            "jetpack_tank": round(float(p.mfJetpackTank), 3),
            "is_running": bool(p.mbIsRunning),
            "is_auto_walking": bool(p.mbIsAutoWalking),
            "is_dying": bool(p.mbIsDying),
        }
    except Exception:
        logger.warning(f"_gather_player_movement failed: {traceback.format_exc()}")
        return {}


def _gather_universe_address() -> dict:
    try:
        ps = gameData.player_state
        if ps is None:
            return {}
        loc = ps.mLocation
        ga = loc.GalacticAddress
        if not _validate_address(ga):
            return {}
        return {
            "voxel_x": int(ga.VoxelX),
            "voxel_y": int(ga.VoxelY),
            "voxel_z": int(ga.VoxelZ),
            "solar_system_index": int(ga.SolarSystemIndex),
            "planet_index": int(ga.PlanetIndex),
            "reality_index": int(loc.RealityIndex),
        }
    except Exception:
        logger.warning(f"_gather_universe_address failed: {traceback.format_exc()}")
        return {}


def _gather_environment_data(env: nms.cGcPlayerEnvironment) -> dict:
    try:
        loc        = _read_enum32(env.meLocation)
        loc_stable = _read_enum32(env.meLocationStable)
        pos        = _vec3(env.mPlayerTM.pos)
        return {
            "location":               _enum_name(EnvironmentLocation.Enum, loc),
            "location_stable":        _enum_name(EnvironmentLocation.Enum, loc_stable),
            "player_position":        pos,
            "nearest_planet_index":   int(env.miNearestPlanetIndex),
            "distance_from_planet":   round(float(env.mfDistanceFromPlanet), 2),
            "nearest_planet_sealevel": round(float(env.mfNearestPlanetSealevel), 2),
            "inside_atmosphere":      bool(env.mbInsidePlanetAtmosphere),
        }
    except Exception:
        logger.warning(f"_gather_environment_data failed: {traceback.format_exc()}")
        return {}


def _gather_planet_data(planet_ptr: ctypes._Pointer) -> dict:
    try:
        planet = planet_ptr.contents
        pd          = planet.mPlanetData
        pgid        = planet.mPlanetGenerationInputData
        info        = pd.PlanetInfo
        weather_data = pd.Weather
        hazard      = pd.Hazard

        name = _str(pd.Name)
        if not name or not name.isprintable():
            return {}

        def _hv(arr, idx):
            try:
                return round(float(arr[idx]), 3)
            except Exception:
                return None

        return {
            "name":             name,
            "biome":            _enum_name(pgid.Biome.__class__,      _read_enum32(pgid.Biome)),
            "planet_size":      _enum_name(pgid.PlanetSize.__class__,  _read_enum32(pgid.PlanetSize)),
            "has_rings":        bool(pd.Rings.HasRings),
            "is_prime":         bool(pgid.Prime),
            "in_pirate_system": bool(pgid.InPirateSystem),
            "description":      _str(info.PlanetDescription),
            "planet_type":      _str(info.PlanetType),
            "weather_label":    _str(info.Weather),
            "flora_label":      _str(info.Flora),
            "fauna_label":      _str(info.Fauna),
            "resources_label":  _str(info.Resources),
            "is_extreme_weather": bool(info.IsWeatherExtreme),
            "weather_type":     _enum_name(weather_data.WeatherType.__class__,    _read_enum32(weather_data.WeatherType)),
            "weather_intensity": _enum_name(weather_data.WeatherIntensity.__class__, _read_enum32(weather_data.WeatherIntensity)),
            "storm_frequency":  _enum_name(weather_data.StormFrequency.__class__, _read_enum32(weather_data.StormFrequency)),
            "creature_life":    _enum_name(pd.CreatureLife.__class__,   _read_enum32(pd.CreatureLife)),
            "life":             _enum_name(pd.Life.__class__,           _read_enum32(pd.Life)),
            "inhabiting_race":  _enum_name(pd.InhabitingRace.__class__, _read_enum32(pd.InhabitingRace)),
            "sentinel_level":   _enum_name(
                pd.GroundCombatDataPerDifficulty[0].SentinelLevel.__class__,
                _read_enum32(pd.GroundCombatDataPerDifficulty[0].SentinelLevel),
            ),
            "hazards": {
                "temperature_ambient": _hv(hazard.Temperature, 0),
                "temperature_storm":   _hv(hazard.Temperature, 3),
                "toxicity_ambient":    _hv(hazard.Toxicity, 0),
                "toxicity_storm":      _hv(hazard.Toxicity, 3),
                "radiation_ambient":   _hv(hazard.Radiation, 0),
                "radiation_storm":     _hv(hazard.Radiation, 3),
                "life_support_drain":  _hv(hazard.LifeSupportDrain, 0),
            },
            "common_substance":    _str(pd.CommonSubstanceID),
            "uncommon_substance":  _str(pd.UncommonSubstanceID),
            "rare_substance":      _str(pd.RareSubstanceID),
            "in_abandoned_system": bool(pd.InAbandonedSystem),
            "in_empty_system":     bool(pd.InEmptySystem),
        }
    except Exception:
        logger.warning(f"_gather_planet_data failed: {traceback.format_exc()}")
        return {}


def _gather_solar_system_data(planet_ptr: ctypes._Pointer) -> dict:
    try:
        pgid = planet_ptr.contents.mPlanetGenerationInputData
        return {
            "star_type": _enum_name(pgid.Star.__class__, _read_enum32(pgid.Star)),
        }
    except Exception:
        logger.warning(f"_gather_solar_system_data failed: {traceback.format_exc()}")
        return {}


def _build_full_payload(
    current_state: str,
    env_data: dict,
    planet_ptrs: dict,
) -> dict:
    nearest_idx = env_data.get("nearest_planet_index", -1)
    planet_ptr  = planet_ptrs.get(nearest_idx)
    return {
        "state":          current_state,
        "player":         _gather_player_data(current_state),
        "movement":       _gather_player_movement(),
        "universe_address": _gather_universe_address(),
        "environment":    env_data,
        "planet":         _gather_planet_data(planet_ptr)       if planet_ptr else {},
        "solar_system":   _gather_solar_system_data(planet_ptr) if planet_ptr else {},
    }


# ---------------------------------------------------------------------------
# Mod
# ---------------------------------------------------------------------------

class PlayerState:
    ON_FOOT    = "ON_FOOT"
    IN_COCKPIT = "IN_COCKPIT"
    GALAXY_MAP = "GALAXY_MAP"


@dataclass
class DetectorState(ModState):
    current: str = ""
    last_location_stable: int = -1
    in_galaxy_map: bool = False
    galaxy_map_entered_at: float = 0.0


class PlayerStateDetector(Mod):
    __author__ = "you"
    __description__ = "Polls and writes NMS player state to JSON on a timer"
    __version__ = "3.3"

    state = DetectorState()
    _last_env_data: dict = {}
    _last_write_time: float = 0.0
    _poll_interval: float = DEFAULT_POLL_INTERVAL

    # Planet pointers keyed by index (0-5), populated by hooking
    # cGcPlanet.SetupRegionMap which fires once per planet on solar system load.
    _planet_ptrs: dict = {}

    @property
    @STRING("Current State:")
    def current_state(self):
        return self.state.current or "UNKNOWN"

    @current_state.setter
    def current_state(self, value):
        if value != self.state.current:
            self.state.current = value
            self._write_now()

    @property
    @FLOAT("Poll interval (seconds):")
    def poll_interval(self):
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, value):
        self._poll_interval = max(1.0, float(value))

    def _write_now(self):
        _write_state(_build_full_payload(
            self.state.current or "UNKNOWN",
            self._last_env_data,
            self._planet_ptrs,
        ))
        self._last_write_time = time.time()

    def _derive_state(self, loc_stable: int) -> str:
        if self.state.in_galaxy_map:
            return PlayerState.GALAXY_MAP
        if loc_stable in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
            return PlayerState.IN_COCKPIT
        return PlayerState.ON_FOOT

    @on_fully_booted
    def on_game_booted(self):
        self.state.current = ""
        self.state.last_location_stable = -1
        self._last_write_time = 0.0
        self._planet_ptrs = {}
        logger.info("Game booted — state reset")

    @nms.cGcPlanet.SetupRegionMap.after
    def on_planet_setup(self, this: ctypes._Pointer[nms.cGcPlanet]):
        try:
            idx  = int(this.contents.miPlanetIndex)
            name = _str(this.contents.mPlanetData.Name)
            self._planet_ptrs[idx] = this
            logger.info(f"Cached planet {idx}: '{name}'")
        except Exception:
            logger.warning(f"on_planet_setup failed: {traceback.format_exc()}")

    def _restore_from_location(self):
        loc = self.state.last_location_stable
        if loc in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
            self.current_state = PlayerState.IN_COCKPIT
        else:
            self.current_state = PlayerState.ON_FOOT

    @on_state_change("GALAXYMAP")
    def on_enter_galaxy_map(self):
        self.state.in_galaxy_map = True
        self.state.galaxy_map_entered_at = time.time()
        self.current_state = PlayerState.GALAXY_MAP

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        if time.time() - self._last_write_time >= self._poll_interval:
            self._write_now()

    @nms.cGcPlayerEnvironment.Update.after
    def on_player_env_update(
        self,
        this: ctypes._Pointer[nms.cGcPlayerEnvironment],
        lfTimeStep: float,
    ):
        try:
            loc        = _read_enum32(this.contents.meLocation)
            loc_stable = _read_enum32(this.contents.meLocationStable)
        except Exception:
            return

        self._last_env_data = _gather_environment_data(this.contents)

        # loc_stable changed = genuine location transition (entered/exited ship etc.)
        # Clear galaxy map flag unconditionally on any location change.
        if loc_stable != self.state.last_location_stable:
            self.state.last_location_stable = loc_stable
            self.state.in_galaxy_map = False
            if loc_stable in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
                self.current_state = PlayerState.IN_COCKPIT
            else:
                self.current_state = PlayerState.ON_FOOT
        elif (self.state.in_galaxy_map
                and loc == loc_stable
                and time.time() - self.state.galaxy_map_entered_at > 1.0):
            # Galaxy map closed: loc has caught up to loc_stable and we have been
            # in the map for >1s (guards against the brief moment at map open where
            # both values happen to be equal before the game updates loc).
            self.state.in_galaxy_map = False
            self._restore_from_location()