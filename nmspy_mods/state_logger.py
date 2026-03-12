# /// script
# dependencies = ["nmspy>=0.1.0", "pymhf[gui]>=0.2.2"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# always_on_top = false
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "State Logger"
# ///

import ctypes
import time
import traceback

from pymhf import Mod
from pymhf.gui import FLOAT
from pymhf.gui.decorators import STRING

import nmspy.data.types as nms
from nmspy.data.enums import EnvironmentLocation
from nmspy.decorators import on_state_change, on_fully_booted
from nmspy.common import gameData

from shared_state import NMSModState, _make_logger, _read_enum32, _enum_name, _str, _vec3, _validate_address, _write_state

_slog = _make_logger("StateDetector", "nms_state_logger.log")
# _slog.info("=" * 60)
# _slog.info("state_logger.py loaded")

DEFAULT_POLL_INTERVAL = 5.0


# ===========================================================================
# Payload builders — each gathers one slice of game state into a plain dict.
# They all return {} on failure so the final JSON is still valid.
# ===========================================================================

def _gather_player_data(current_state):
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
        _slog.warning("_gather_player_data failed: %s", traceback.format_exc())
        return {}


def _gather_player_movement():
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
        _slog.warning("_gather_player_movement failed: %s", traceback.format_exc())
        return {}


def _gather_universe_address():
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
        _slog.warning("_gather_universe_address failed: %s", traceback.format_exc())
        return {}


def _gather_environment_data(env):
    try:
        loc = _read_enum32(env.meLocation)
        loc_stable = _read_enum32(env.meLocationStable)
        return {
            "location": _enum_name(EnvironmentLocation.Enum, loc),
            "location_stable": _enum_name(EnvironmentLocation.Enum, loc_stable),
            "player_position": _vec3(env.mPlayerTM.pos),
            "nearest_planet_index": int(env.miNearestPlanetIndex),
            "distance_from_planet": round(float(env.mfDistanceFromPlanet), 2),
            "nearest_planet_sealevel": round(float(env.mfNearestPlanetSealevel), 2),
            "inside_atmosphere": bool(env.mbInsidePlanetAtmosphere),
        }
    except Exception:
        _slog.warning("_gather_environment_data failed: %s", traceback.format_exc())
        return {}


def _gather_planet_data(planet_ptr):
    try:
        planet = planet_ptr.contents
        pd = planet.mPlanetData
        pgid = planet.mPlanetGenerationInputData
        info = pd.PlanetInfo
        weather_data = pd.Weather
        hazard = pd.Hazard
        name = _str(pd.Name)
        if not name or not name.isprintable():
            return {}
        def _hv(arr, idx):
            try:
                return round(float(arr[idx]), 3)
            except Exception:
                return None
        return {
            "name": name,
            "biome": _enum_name(pgid.Biome.__class__, _read_enum32(pgid.Biome)),
            "planet_size": _enum_name(pgid.PlanetSize.__class__, _read_enum32(pgid.PlanetSize)),
            "has_rings": bool(pd.Rings.HasRings),
            "is_prime": bool(pgid.Prime),
            "in_pirate_system": bool(pgid.InPirateSystem),
            "description": _str(info.PlanetDescription),
            "planet_type": _str(info.PlanetType),
            "weather_label": _str(info.Weather),
            "flora_label": _str(info.Flora),
            "fauna_label": _str(info.Fauna),
            "resources_label": _str(info.Resources),
            "is_extreme_weather": bool(info.IsWeatherExtreme),
            "weather_type": _enum_name(weather_data.WeatherType.__class__, _read_enum32(weather_data.WeatherType)),
            "weather_intensity": _enum_name(weather_data.WeatherIntensity.__class__, _read_enum32(weather_data.WeatherIntensity)),
            "storm_frequency": _enum_name(weather_data.StormFrequency.__class__, _read_enum32(weather_data.StormFrequency)),
            "creature_life": _enum_name(pd.CreatureLife.__class__, _read_enum32(pd.CreatureLife)),
            "life": _enum_name(pd.Life.__class__, _read_enum32(pd.Life)),
            "inhabiting_race": _enum_name(pd.InhabitingRace.__class__, _read_enum32(pd.InhabitingRace)),
            "sentinel_level": _enum_name(
                pd.GroundCombatDataPerDifficulty[0].SentinelLevel.__class__,
                _read_enum32(pd.GroundCombatDataPerDifficulty[0].SentinelLevel),
            ),
            "hazards": {
                "temperature_ambient": _hv(hazard.Temperature, 0),
                "temperature_storm": _hv(hazard.Temperature, 3),
                "toxicity_ambient": _hv(hazard.Toxicity, 0),
                "toxicity_storm": _hv(hazard.Toxicity, 3),
                "radiation_ambient": _hv(hazard.Radiation, 0),
                "radiation_storm": _hv(hazard.Radiation, 3),
                "life_support_drain": _hv(hazard.LifeSupportDrain, 0),
            },
            "common_substance": _str(pd.CommonSubstanceID),
            "uncommon_substance": _str(pd.UncommonSubstanceID),
            "rare_substance": _str(pd.RareSubstanceID),
            "in_abandoned_system": bool(pd.InAbandonedSystem),
            "in_empty_system": bool(pd.InEmptySystem),
        }
    except Exception:
        _slog.warning("_gather_planet_data failed: %s", traceback.format_exc())
        return {}


def _gather_solar_system_data(planet_ptr):
    try:
        pgid = planet_ptr.contents.mPlanetGenerationInputData
        return {"star_type": _enum_name(pgid.Star.__class__, _read_enum32(pgid.Star))}
    except Exception:
        _slog.warning("_gather_solar_system_data failed: %s", traceback.format_exc())
        return {}


def _build_full_payload(current_state, env_data, planet_ptrs):
    nearest_idx = env_data.get("nearest_planet_index", -1)
    planet_ptr = planet_ptrs.get(nearest_idx)
    return {
        "state": current_state,
        "player": _gather_player_data(current_state),
        "movement": _gather_player_movement(),
        "universe_address": _gather_universe_address(),
        "environment": env_data,
        "planet": _gather_planet_data(planet_ptr) if planet_ptr else {},
        "solar_system": _gather_solar_system_data(planet_ptr) if planet_ptr else {},
    }


# ===========================================================================
# Mod
# ===========================================================================

class StateLogger(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "State logger"
    __version__ = "1.0"

    state = NMSModState()

    _last_env_data: dict = {}
    _last_write_time: float = 0.0
    _poll_interval: float = DEFAULT_POLL_INTERVAL
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
        _write_state(_build_full_payload(self.state.current or "UNKNOWN", self._last_env_data, self._planet_ptrs))
        self._last_write_time = time.time()

    def _restore_from_location(self):
        loc = self.state.last_location_stable
        if loc in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
            self.current_state = "IN_COCKPIT"
        else:
            self.current_state = "ON_FOOT"

    @on_fully_booted
    def on_game_booted(self):
        self.state.current = ""
        self.state.last_location_stable = -1
        self._last_write_time = 0.0
        self._planet_ptrs = {}
        # _slog.info("Game booted — state reset")

    @nms.cGcPlanet.SetupRegionMap.after
    def on_planet_setup(self, this: ctypes._Pointer[nms.cGcPlanet]):
        try:
            idx = int(this.contents.miPlanetIndex)
            name = _str(this.contents.mPlanetData.Name)
            self._planet_ptrs[idx] = this
            # _slog.info("Cached planet %d: '%s'", idx, name)
        except Exception:
            _slog.warning("on_planet_setup failed: %s", traceback.format_exc())

    @on_state_change("GALAXYMAP")
    def on_enter_galaxy_map(self):
        self.state.in_galaxy_map = True
        self.state.galaxy_map_entered_at = time.time()
        self.current_state = "GALAXY_MAP"

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        if time.time() - self._last_write_time >= self._poll_interval:
            self._write_now()

    @nms.cGcPlayerEnvironment.Update.after
    def on_player_env_update(self, this: ctypes._Pointer[nms.cGcPlayerEnvironment], lfTimeStep: float):
        try:
            loc = _read_enum32(this.contents.meLocation)
            loc_stable = _read_enum32(this.contents.meLocationStable)
        except Exception:
            return
        self._last_env_data = _gather_environment_data(this.contents)
        if loc_stable != self.state.last_location_stable:
            self.state.last_location_stable = loc_stable
            self.state.in_galaxy_map = False
            if loc_stable in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
                self.current_state = "IN_COCKPIT"
            else:
                self.current_state = "ON_FOOT"
        elif (self.state.in_galaxy_map
              and loc == loc_stable
              and time.time() - self.state.galaxy_map_entered_at > 1.0):
            # location stabilised after leaving galaxy map — restore foot/cockpit state
            self.state.in_galaxy_map = False
            self._restore_from_location()
