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
# window_name_override = "Teleporter"
# ///

import ctypes
import random
import time
import traceback

from pymhf import Mod
from pymhf.core.hooking import on_key_pressed

import nmspy.data.types as nms
import nmspy.data.basic_types as basic
from nmspy.data.enums import internal_enums
from nmspy.common import gameData

from shared_state import NMSModState, _make_logger

# ---------------------------------------------------------------------------
# Offsets verified via memory scanning
# ---------------------------------------------------------------------------
GAMESTATE_OFFSET = 0x10f0   # mpData.mGameState (was 0xDB0 pre-update)
PLAYER_STATE_OFF = 0xAAD0   # mGameState.mPlayerState
GA_ADDR_OFFSET   = 0x180    # cGcPlayerState.mLocation.GalacticAddress

# cGcGalacticAddressData field layout:
#   PlanetIndex      +0x00
#   SolarSystemIndex +0x04
#   VoxelX           +0x08
#   VoxelY           +0x0C
#   VoxelZ           +0x10
#   RealityIndex     +0x14
GA_PLANET_IDX = 0x00
GA_SOLAR_IDX  = 0x04
GA_VOXEL_X    = 0x08
GA_VOXEL_Y    = 0x0C
GA_VOXEL_Z    = 0x10
GA_REALITY    = 0x14

# ---------------------------------------------------------------------------
# Galaxy constants
# ---------------------------------------------------------------------------
GALAXY_MIN        = 0    # Euclid
GALAXY_MAX        = 254  # 255 galaxies total (indices 0–254)
GALAXY_EISSENTAM  = 9    # Eissentam — lush galaxy (paradise planets more common)

# ---------------------------------------------------------------------------
# Other limits
# ---------------------------------------------------------------------------
RESPAWN_PORTAL    = internal_enums.RespawnReason.Portal
VOXEL_XZ_MAX      = 2000
VOXEL_Y_MAX       = 255
SYSTEM_MAX        = 599
SAFE_PLANET_INDEX = 0


def _get_mpdata_addr():
    try:
        app = gameData.GcApplication
        if app is None:
            return 0
        return ctypes.cast(app.mpData, ctypes.c_void_p).value or 0
    except Exception:
        return 0


def _get_ps_addr():
    """Return raw address of cGcPlayerState in game memory."""
    mp = _get_mpdata_addr()
    if not mp:
        return 0
    return mp + GAMESTATE_OFFSET + PLAYER_STATE_OFF


def _get_ga_base():
    """Return raw address of GalacticAddress in game memory."""
    ps = _get_ps_addr()
    if not ps:
        return 0
    return ps + GA_ADDR_OFFSET


def _read_ga_int(field_off):
    base = _get_ga_base()
    if not base:
        return 0
    return ctypes.c_int32.from_address(base + field_off).value


def _write_ga_int(field_off, value):
    base = _get_ga_base()
    if not base:
        return False
    ctypes.c_int32.from_address(base + field_off).value = value
    return True


_tlog = _make_logger("Teleporter", "random_teleporter.log")
# _tlog.info("=" * 60)
# _tlog.info("teleporter.py loaded")
_fsm_state_str = basic.cTkFixedString[0x10]()


def _tread_location(label):
    try:
        _tlog.info("[%s] planet=%d sys=%d voxel=(%d,%d,%d) reality=%d",
                   label,
                   _read_ga_int(GA_PLANET_IDX),
                   _read_ga_int(GA_SOLAR_IDX),
                   _read_ga_int(GA_VOXEL_X),
                   _read_ga_int(GA_VOXEL_Y),
                   _read_ga_int(GA_VOXEL_Z),
                   _read_ga_int(GA_REALITY))
    except Exception:
        _tlog.error("[%s] exception:\n%s", label, traceback.format_exc())


def _write_location(vx, vy, vz, sys_idx, planet_idx, reality_idx):
    """Write destination to GalacticAddress using confirmed arithmetic offsets."""
    _write_ga_int(GA_PLANET_IDX, planet_idx)
    _write_ga_int(GA_SOLAR_IDX,  sys_idx)
    _write_ga_int(GA_VOXEL_X,    vx)
    _write_ga_int(GA_VOXEL_Y,    vy)
    _write_ga_int(GA_VOXEL_Z,    vz)
    _write_ga_int(GA_REALITY,    reality_idx)


def _trigger_load(state) -> bool:
    # Triggers a local load by pushing the APPLOCALLOAD FSM state.
    # Must be called from the main Update tick — not from a key hook.
    global _fsm_state_str
    try:
        app = gameData.GcApplication
        if app is None:
            _tlog.error("[TRIGGER] GcApplication is None")
            return False
        state.warp_pending = True
        state.warp_time = time.time()
        state.loading = True
        state.load_start_time = time.time()
        _fsm_state_str.set("APPLOCALLOAD")
        addr = ctypes.addressof(_fsm_state_str)
        # _tlog.info("[TRIGGER] StateChange → APPLOCALLOAD")
        app.StateChange(ctypes.c_uint64(addr), ctypes.c_uint64(0), False)
        # _tlog.info("[TRIGGER] StateChange returned")
        return True
    except Exception:
        _tlog.error("[TRIGGER] Exception:\n%s", traceback.format_exc())
        state.warp_pending = False
        state.loading = False
        return False


def _prepare_teleport(state, vx, vy, vz, sys_idx,
                      planet_idx=SAFE_PLANET_INDEX, reality_idx=None):
    """Write destination into game memory and arm the deferred trigger.

    reality_idx defaults to a random galaxy if not specified.
    Called from key-press hooks — StateChange fires on the next Update tick.
    """
    if state.loading:
        _tlog.warning("[TELEPORT] Load in progress (%.1fs) — ignoring",
                      time.time() - state.load_start_time)
        return
    if state.teleport_deferred:
        _tlog.warning("[TELEPORT] Teleport already queued — ignoring duplicate key press")
        return
    if not _get_ga_base():
        _tlog.error("[TELEPORT] Cannot get GA address")
        return

    if reality_idx is None:
        reality_idx = random.randint(GALAXY_MIN, GALAXY_MAX)

    vx          = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vx))
    vy          = max(-VOXEL_Y_MAX,  min(VOXEL_Y_MAX,  vy))
    vz          = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vz))
    sys_idx     = max(0, min(SYSTEM_MAX, sys_idx))
    reality_idx = max(GALAXY_MIN, min(GALAXY_MAX, reality_idx))

    _tread_location("BEFORE")
    _tlog.info("[TELEPORT] Writing → reality=%d planet=%d sys=%d voxel=(%d,%d,%d)",
               reality_idx, planet_idx, sys_idx, vx, vy, vz)

    state.dest_vx     = vx
    state.dest_vy     = vy
    state.dest_vz     = vz
    state.dest_sys    = sys_idx
    state.dest_planet = planet_idx

    _write_location(vx, vy, vz, sys_idx, planet_idx, reality_idx)
    _tread_location("AFTER WRITE")
    state.teleport_deferred = True


LOAD_TIMEOUT_S = 25.0

def _flush_deferred_teleport(state):
    """Called from on_main_loop (Update.after) — safe context for StateChange."""
    if state.loading and (time.time() - state.load_start_time) > LOAD_TIMEOUT_S:
        _tlog.info("[TELEPORT] Load timeout — clearing loading flag")
        state.loading = False
        state.warp_pending = False
    if not state.teleport_deferred:
        return
    state.teleport_deferred = False
    _trigger_load(state)


# ===========================================================================
# Mod
# ===========================================================================

class Teleporter(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Random teleporter"
    __version__ = "1.1"

    state = NMSModState()

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        _flush_deferred_teleport(self.state)

    @nms.cTkFSMState.StateChange.after
    def on_fsm_state_change(self, this,
                             lNewStateID: ctypes._Pointer[basic.cTkFixedString[0x10]],
                             lpUserData, lbForceRestart):
        try:
            name = str(lNewStateID.contents)
            # _tlog.info("[FSM] → '%s'", name)
            if name == "APPVIEW":
                if self.state.loading:
                    # _tlog.info("[FSM] Load complete (%.1fs) — ready", time.time() - self.state.load_start_time)
                    self.state.loading = False
            elif name in ("MODESELECTOR", "APPSHUTDOWN", "APPGLOBALLOAD"):
                if self.state.loading:
                    _tlog.warning("[FSM] Unexpected state '%s' while loading — clearing", name)
                    self.state.loading = False
                    self.state.warp_pending = False
        except Exception:
            _tlog.warning("[FSM] hook error:\n%s", traceback.format_exc())

    @nms.cGcApplicationLocalLoadState.GetRespawnReason.before
    def on_respawn_before(self, this):
        try:
            if self.state.warp_pending:
                # _tlog.info("[RESPAWN] Intercepting → Portal (11)  elapsed=%.3fs", time.time() - self.state.warp_time)
                self.state.warp_pending = False
                self.state.last_respawn_reason = int(RESPAWN_PORTAL)
                return int(RESPAWN_PORTAL)
        except Exception:
            _tlog.warning("[RESPAWN] hook error:\n%s", traceback.format_exc())
        return None

    # -----------------------------------------------------------------------
    # Key bindings
    # -----------------------------------------------------------------------

    @on_key_pressed("o")
    def key_random_local(self):
        """Random system + coords, stays in the current galaxy."""
        cur_reality = _read_ga_int(GA_REALITY)
        
        # Override galaxy here
        # cur_reality = GALAXY_EISSENTAM
        
        _prepare_teleport(self.state,
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, VOXEL_Y_MAX),
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, SYSTEM_MAX),
                          reality_idx=cur_reality)

    @on_key_pressed("p")
    def key_random_galaxy(self):
        """Fully random warp — random galaxy, coords, and system."""
        cur_reality = _read_ga_int(GA_REALITY)
        new_reality = cur_reality
        while new_reality == cur_reality:
            new_reality = random.randint(GALAXY_MIN, GALAXY_MAX)
        _prepare_teleport(self.state,
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, VOXEL_Y_MAX),
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, SYSTEM_MAX),
                          reality_idx=new_reality)

    @on_key_pressed("[")
    def key_nearby(self):
        """Random nearby system, same galaxy and voxel region."""
        cur_sys     = _read_ga_int(GA_SOLAR_IDX)
        cur_reality = _read_ga_int(GA_REALITY)
        new_sys = cur_sys
        while new_sys == cur_sys:
            new_sys = random.randint(0, SYSTEM_MAX)
        _prepare_teleport(self.state,
                          _read_ga_int(GA_VOXEL_X),
                          _read_ga_int(GA_VOXEL_Y),
                          _read_ga_int(GA_VOXEL_Z),
                          new_sys,
                          reality_idx=cur_reality)