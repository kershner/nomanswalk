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
# window_name_override = "NMS Mods"
# ///

import ctypes
import json
import logging
import os
import random
import time
import traceback
from dataclasses import dataclass, field

from pymhf import Mod, ModState
from pymhf.core.hooking import on_key_pressed
from pymhf.gui.decorators import STRING
from pymhf.gui import FLOAT

import nmspy.data.types as nms
import nmspy.data.basic_types as basic
from nmspy.data.enums import EnvironmentLocation, internal_enums
from nmspy.decorators import on_state_change, on_fully_booted
from nmspy.common import gameData

# ===========================================================================
# Logging
# ===========================================================================

_base_dir = os.path.dirname(os.path.abspath(__file__))

def _make_logger(name, filename):
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
        fh = logging.FileHandler(os.path.join(_base_dir, filename), mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)
        log.propagate = False
    return log

_tlog = _make_logger("Teleporter",    "random_teleporter.log")
_slog = _make_logger("StateDetector", "nms_state_logger.log")

_tlog.info("=" * 60)
_tlog.info("nms_mods.py loaded")
_slog.info("=" * 60)
_slog.info("nms_mods.py loaded")

# ===========================================================================
# Constants
# ===========================================================================

RESPAWN_PORTAL    = internal_enums.RespawnReason.Portal
VOXEL_XZ_MAX      = 2000
VOXEL_Y_MAX       = 255
SYSTEM_MAX        = 599
SAFE_PLANET_INDEX = 0
DEFAULT_POLL_INTERVAL = 5.0

# ===========================================================================
# State logger helpers
# ===========================================================================

_state_path = os.path.join(_base_dir, "nms_state.json")

def _read_enum32(val) -> int:
    return int.from_bytes(bytes(val)[:4], "little")

def _enum_name(enum_class, value: int) -> str:
    try:
        return enum_class(value).name
    except (ValueError, KeyError):
        return str(value)

def _str(val) -> str:
    try:
        return bytes(val).split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    except Exception:
        return ""

def _vec3(val) -> dict:
    try:
        return {"x": round(float(val.x), 3), "y": round(float(val.y), 3), "z": round(float(val.z), 3)}
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

def _gather_player_data(current_state: str) -> dict:
    try:
        ps = gameData.player_state
        if ps is None:
            return {}
        health = int(ps.miHealth)
        if not (0 <= health < 50_000_000):
            return {}
        result = {
            "name":        _str(ps.mNameWithTitle),
            "health":      health,
            "shield":      max(0, int(ps.miShield)),
            "units":       int(ps.muUnits),
            "nanites":     int(ps.muNanites),
            "quicksilver": int(ps.muSpecials),
        }
        if current_state == "IN_COCKPIT":
            result["ship_health"] = max(0, int(ps.miShipHealth))
        return result
    except Exception:
        _slog.warning("_gather_player_data failed: %s", traceback.format_exc())
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
            "stamina":         round(stamina, 3),
            "jetpack_tank":    round(float(p.mfJetpackTank), 3),
            "is_running":      bool(p.mbIsRunning),
            "is_auto_walking": bool(p.mbIsAutoWalking),
            "is_dying":        bool(p.mbIsDying),
        }
    except Exception:
        _slog.warning("_gather_player_movement failed: %s", traceback.format_exc())
        return {}

def _gather_universe_address() -> dict:
    try:
        ps = gameData.player_state
        if ps is None:
            return {}
        loc = ps.mLocation
        ga  = loc.GalacticAddress
        if not _validate_address(ga):
            return {}
        return {
            "voxel_x":            int(ga.VoxelX),
            "voxel_y":            int(ga.VoxelY),
            "voxel_z":            int(ga.VoxelZ),
            "solar_system_index": int(ga.SolarSystemIndex),
            "planet_index":       int(ga.PlanetIndex),
            "reality_index":      int(loc.RealityIndex),
        }
    except Exception:
        _slog.warning("_gather_universe_address failed: %s", traceback.format_exc())
        return {}

def _gather_environment_data(env) -> dict:
    try:
        loc        = _read_enum32(env.meLocation)
        loc_stable = _read_enum32(env.meLocationStable)
        return {
            "location":                _enum_name(EnvironmentLocation.Enum, loc),
            "location_stable":         _enum_name(EnvironmentLocation.Enum, loc_stable),
            "player_position":         _vec3(env.mPlayerTM.pos),
            "nearest_planet_index":    int(env.miNearestPlanetIndex),
            "distance_from_planet":    round(float(env.mfDistanceFromPlanet), 2),
            "nearest_planet_sealevel": round(float(env.mfNearestPlanetSealevel), 2),
            "inside_atmosphere":       bool(env.mbInsidePlanetAtmosphere),
        }
    except Exception:
        _slog.warning("_gather_environment_data failed: %s", traceback.format_exc())
        return {}

def _gather_planet_data(planet_ptr) -> dict:
    try:
        planet       = planet_ptr.contents
        pd           = planet.mPlanetData
        pgid         = planet.mPlanetGenerationInputData
        info         = pd.PlanetInfo
        weather_data = pd.Weather
        hazard       = pd.Hazard
        name         = _str(pd.Name)
        if not name or not name.isprintable():
            return {}
        def _hv(arr, idx):
            try:
                return round(float(arr[idx]), 3)
            except Exception:
                return None
        return {
            "name":               name,
            "biome":              _enum_name(pgid.Biome.__class__,       _read_enum32(pgid.Biome)),
            "planet_size":        _enum_name(pgid.PlanetSize.__class__,  _read_enum32(pgid.PlanetSize)),
            "has_rings":          bool(pd.Rings.HasRings),
            "is_prime":           bool(pgid.Prime),
            "in_pirate_system":   bool(pgid.InPirateSystem),
            "description":        _str(info.PlanetDescription),
            "planet_type":        _str(info.PlanetType),
            "weather_label":      _str(info.Weather),
            "flora_label":        _str(info.Flora),
            "fauna_label":        _str(info.Fauna),
            "resources_label":    _str(info.Resources),
            "is_extreme_weather": bool(info.IsWeatherExtreme),
            "weather_type":       _enum_name(weather_data.WeatherType.__class__,      _read_enum32(weather_data.WeatherType)),
            "weather_intensity":  _enum_name(weather_data.WeatherIntensity.__class__, _read_enum32(weather_data.WeatherIntensity)),
            "storm_frequency":    _enum_name(weather_data.StormFrequency.__class__,   _read_enum32(weather_data.StormFrequency)),
            "creature_life":      _enum_name(pd.CreatureLife.__class__,   _read_enum32(pd.CreatureLife)),
            "life":               _enum_name(pd.Life.__class__,           _read_enum32(pd.Life)),
            "inhabiting_race":    _enum_name(pd.InhabitingRace.__class__, _read_enum32(pd.InhabitingRace)),
            "sentinel_level":     _enum_name(
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
        _slog.warning("_gather_planet_data failed: %s", traceback.format_exc())
        return {}

def _gather_solar_system_data(planet_ptr) -> dict:
    try:
        pgid = planet_ptr.contents.mPlanetGenerationInputData
        return {"star_type": _enum_name(pgid.Star.__class__, _read_enum32(pgid.Star))}
    except Exception:
        _slog.warning("_gather_solar_system_data failed: %s", traceback.format_exc())
        return {}

def _build_full_payload(current_state, env_data, planet_ptrs) -> dict:
    nearest_idx = env_data.get("nearest_planet_index", -1)
    planet_ptr  = planet_ptrs.get(nearest_idx)
    return {
        "state":            current_state,
        "player":           _gather_player_data(current_state),
        "movement":         _gather_player_movement(),
        "universe_address": _gather_universe_address(),
        "environment":      env_data,
        "planet":           _gather_planet_data(planet_ptr)       if planet_ptr else {},
        "solar_system":     _gather_solar_system_data(planet_ptr) if planet_ptr else {},
    }

# ===========================================================================
# Teleporter helpers
# ===========================================================================

_fsm_state_str = basic.cTkFixedString[0x10]()

def _tread_location(label):
    try:
        ps = gameData.player_state
        if ps is None:
            return
        ga = ps.mLocation.GalacticAddress
        _tlog.info("[%s] voxel=(%d,%d,%d)  sys=%d  planet=%d",
                   label, int(ga.VoxelX), int(ga.VoxelY), int(ga.VoxelZ),
                   int(ga.SolarSystemIndex), int(ga.PlanetIndex))
    except Exception:
        _tlog.error("[%s] exception:\n%s", label, traceback.format_exc())

def _write_location(ps, vx, vy, vz, sys_idx, planet_idx):
    ga = ps.mLocation.GalacticAddress
    ga.VoxelX            = vx
    ga.VoxelY            = vy
    ga.VoxelZ            = vz
    ga.SolarSystemIndex  = sys_idx
    ga.PlanetIndex       = planet_idx
    ps.mLocation.RealityIndex = 0

def _trigger_load(state) -> bool:
    global _fsm_state_str
    try:
        app = gameData.GcApplication
        if app is None:
            _tlog.error("[TRIGGER] GcApplication is None")
            return False
        state.warp_pending    = True
        state.warp_time       = time.time()
        state.loading         = True
        state.load_start_time = time.time()
        _fsm_state_str.set("APPLOCALLOAD")
        addr = ctypes.addressof(_fsm_state_str)
        _tlog.info("[TRIGGER] StateChange → APPLOCALLOAD")
        app.StateChange(ctypes.c_uint64(addr), ctypes.c_uint64(0), False)
        _tlog.info("[TRIGGER] StateChange returned")
        return True
    except Exception:
        _tlog.error("[TRIGGER] Exception:\n%s", traceback.format_exc())
        state.warp_pending = False
        state.loading      = False
        return False

def _prepare_teleport(state, vx, vy, vz, sys_idx, planet_idx=SAFE_PLANET_INDEX):
    """Write the destination into game memory and arm the deferred trigger.

    Called from key-press hooks (unsafe to call StateChange here).
    The actual StateChange fires on the next Update tick via _flush_deferred_teleport.
    """
    if state.loading:
        _tlog.warning("[TELEPORT] Load in progress (%.1fs) — ignoring",
                      time.time() - state.load_start_time)
        return
    if state.teleport_deferred:
        _tlog.warning("[TELEPORT] Teleport already queued — ignoring duplicate key press")
        return
    ps = gameData.player_state
    if ps is None:
        _tlog.error("[TELEPORT] player_state is None")
        return
    _tread_location("BEFORE")
    vx      = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vx))
    vy      = max(0,              min(VOXEL_Y_MAX,  vy))
    vz      = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vz))
    sys_idx = max(0, min(SYSTEM_MAX, sys_idx))
    state.dest_vx     = vx
    state.dest_vy     = vy
    state.dest_vz     = vz
    state.dest_sys    = sys_idx
    state.dest_planet = planet_idx
    _tlog.info("[TELEPORT] Destination written → voxel=(%d,%d,%d)  sys=%d  planet=%d; "
               "deferring StateChange to next Update tick",
               vx, vy, vz, sys_idx, planet_idx)
    _write_location(ps, vx, vy, vz, sys_idx, planet_idx)
    _tread_location("AFTER WRITE")
    state.teleport_deferred = True


def _flush_deferred_teleport(state):
    """Called from on_main_loop (Update.after) — safe context for StateChange."""
    if not state.teleport_deferred:
        return
    state.teleport_deferred = False
    _tlog.info("[TELEPORT] Flushing deferred teleport → calling _trigger_load from Update tick")
    _trigger_load(state)

# ===========================================================================
# Combined ModState
# ===========================================================================

@dataclass
class CombinedState(ModState):
    # --- state logger ---
    current: str = ""
    last_location_stable: int = -1
    in_galaxy_map: bool = False
    galaxy_map_entered_at: float = 0.0
    # --- teleporter ---
    last_respawn_reason: int = -1
    warp_pending: bool = False
    warp_time: float = 0.0
    loading: bool = False
    load_start_time: float = 0.0
    dest_vx: int = 0
    dest_vy: int = 0
    dest_vz: int = 0
    dest_sys: int = 0
    dest_planet: int = 0
    teleport_deferred: bool = False  # armed by key hook, fired by Update tick

# ===========================================================================
# Single combined Mod
# ===========================================================================

class NMSMods(Mod):
    __author__      = "Tyler Kershner"
    __description__ = "State logger + random teleporter"
    __version__     = "1.0"

    state = CombinedState()

    _last_env_data: dict    = {}
    _last_write_time: float = 0.0
    _poll_interval: float   = DEFAULT_POLL_INTERVAL
    _planet_ptrs: dict      = {}

    # ------------------------------------------------------------------
    # State logger GUI properties
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # State logger methods
    # ------------------------------------------------------------------

    def _write_now(self):
        _write_state(_build_full_payload(
            self.state.current or "UNKNOWN",
            self._last_env_data,
            self._planet_ptrs,
        ))
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
        _slog.info("Game booted — state reset")

    @nms.cGcPlanet.SetupRegionMap.after
    def on_planet_setup(self, this: ctypes._Pointer[nms.cGcPlanet]):
        try:
            idx  = int(this.contents.miPlanetIndex)
            name = _str(this.contents.mPlanetData.Name)
            self._planet_ptrs[idx] = this
            _slog.info("Cached planet %d: '%s'", idx, name)
        except Exception:
            _slog.warning("on_planet_setup failed: %s", traceback.format_exc())

    @on_state_change("GALAXYMAP")
    def on_enter_galaxy_map(self):
        self.state.in_galaxy_map = True
        self.state.galaxy_map_entered_at = time.time()
        self.current_state = "GALAXY_MAP"

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        # Fire any pending teleport first — StateChange is safe to call here.
        _flush_deferred_teleport(self.state)

        if time.time() - self._last_write_time >= self._poll_interval:
            self._write_now()

    @nms.cGcPlayerEnvironment.Update.after
    def on_player_env_update(self, this: ctypes._Pointer[nms.cGcPlayerEnvironment], lfTimeStep: float):
        try:
            loc        = _read_enum32(this.contents.meLocation)
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
            self.state.in_galaxy_map = False
            self._restore_from_location()

    # ------------------------------------------------------------------
    # Teleporter hooks
    # ------------------------------------------------------------------

    @nms.cTkFSMState.StateChange.after
    def on_fsm_state_change(self, this,
                             lNewStateID: ctypes._Pointer[basic.cTkFixedString[0x10]],
                             lpUserData, lbForceRestart):
        try:
            name = str(lNewStateID.contents)
            _tlog.info("[FSM] → '%s'", name)
            if name == "APPVIEW":
                if self.state.loading:
                    _tlog.info("[FSM] Load complete (%.1fs) — ready",
                               time.time() - self.state.load_start_time)
                    self.state.loading = False
            elif name in ("MODESELECTOR", "APPSHUTDOWN", "APPGLOBALLOAD"):
                if self.state.loading:
                    _tlog.warning("[FSM] Unexpected state '%s' while loading — clearing", name)
                    self.state.loading      = False
                    self.state.warp_pending = False
        except Exception:
            _tlog.warning("[FSM] hook error:\n%s", traceback.format_exc())

    @nms.cGcApplicationLocalLoadState.GetRespawnReason.before
    def on_respawn_before(self, this):
        try:
            if self.state.warp_pending:
                _tlog.info("[RESPAWN] Intercepting → Portal (11)  elapsed=%.3fs",
                           time.time() - self.state.warp_time)
                self.state.warp_pending        = False
                self.state.last_respawn_reason = int(RESPAWN_PORTAL)
                return int(RESPAWN_PORTAL)
            else:
                _tlog.info("[RESPAWN] GetRespawnReason (not our warp)")
        except Exception:
            _tlog.warning("[RESPAWN] hook error:\n%s", traceback.format_exc())
        return None

    # ------------------------------------------------------------------
    # Teleporter keys
    # ------------------------------------------------------------------

    @on_key_pressed("o")
    def key_random(self):
        _tlog.info("*** KEY:O  RANDOM GALAXY ***")
        _prepare_teleport(self.state,
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, VOXEL_Y_MAX),
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, SYSTEM_MAX))

    @on_key_pressed("[")
    def key_nearby(self):
        _tlog.info("*** KEY:[  NEARBY ***")
        ps = gameData.player_state
        if ps is None:
            return
        ga      = ps.mLocation.GalacticAddress
        cur_sys = int(ga.SolarSystemIndex)
        new_sys = cur_sys
        while new_sys == cur_sys:
            new_sys = random.randint(0, SYSTEM_MAX)
        _prepare_teleport(self.state,
                          int(ga.VoxelX), int(ga.VoxelY), int(ga.VoxelZ), new_sys)

    @on_key_pressed("]")
    def key_repeat(self):
        _tlog.info("*** KEY:]  REPEAT ***")
        s = self.state
        if s.dest_sys == 0 and s.dest_vx == 0 and s.dest_vz == 0:
            _tlog.warning("[KEY:]] No previous destination stored yet")
            return
        _prepare_teleport(self.state, s.dest_vx, s.dest_vy, s.dest_vz, s.dest_sys, s.dest_planet)