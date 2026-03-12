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

_tlog = _make_logger("Teleporter", "random_teleporter.log")
# _tlog.info("=" * 60)
# _tlog.info("teleporter.py loaded")

RESPAWN_PORTAL = internal_enums.RespawnReason.Portal
VOXEL_XZ_MAX = 2000
VOXEL_Y_MAX = 255
SYSTEM_MAX = 599
SAFE_PLANET_INDEX = 0

_fsm_state_str = basic.cTkFixedString[0x10]()


def _tread_location(label):
    try:
        ps = gameData.player_state
        if ps is None:
            return
        ga = ps.mLocation.GalacticAddress
        # _tlog.info("[%s] voxel=(%d,%d,%d)  sys=%d  planet=%d",
        #            label, int(ga.VoxelX), int(ga.VoxelY), int(ga.VoxelZ),
        #            int(ga.SolarSystemIndex), int(ga.PlanetIndex))
    except Exception:
        _tlog.error("[%s] exception:\n%s", label, traceback.format_exc())


def _write_location(ps, vx, vy, vz, sys_idx, planet_idx):
    ga = ps.mLocation.GalacticAddress
    ga.VoxelX = vx
    ga.VoxelY = vy
    ga.VoxelZ = vz
    ga.SolarSystemIndex = sys_idx
    ga.PlanetIndex = planet_idx
    ps.mLocation.RealityIndex = 0


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


def _prepare_teleport(state, vx, vy, vz, sys_idx, planet_idx=SAFE_PLANET_INDEX):
    """Write the destination into game memory and arm the deferred trigger.

    Called from key-press hooks (unsafe to call StateChange here).
    The actual StateChange fires on the next Update tick via _flush_deferred_teleport.
    """
    if state.loading:
        _tlog.warning("[TELEPORT] Load in progress (%.1fs) — ignoring", time.time() - state.load_start_time)
        return
    if state.teleport_deferred:
        _tlog.warning("[TELEPORT] Teleport already queued — ignoring duplicate key press")
        return
    ps = gameData.player_state
    if ps is None:
        _tlog.error("[TELEPORT] player_state is None")
        return
    _tread_location("BEFORE")
    vx = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vx))
    vy = max(0, min(VOXEL_Y_MAX, vy))
    vz = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, vz))
    sys_idx = max(0, min(SYSTEM_MAX, sys_idx))
    state.dest_vx = vx
    state.dest_vy = vy
    state.dest_vz = vz
    state.dest_sys = sys_idx
    state.dest_planet = planet_idx
    # _tlog.info("[TELEPORT] Destination written → voxel=(%d,%d,%d)  sys=%d  planet=%d; deferring StateChange to next Update tick",
    #            vx, vy, vz, sys_idx, planet_idx)
    _write_location(ps, vx, vy, vz, sys_idx, planet_idx)
    _tread_location("AFTER WRITE")
    state.teleport_deferred = True


def _flush_deferred_teleport(state):
    """Called from on_main_loop (Update.after) — safe context for StateChange."""
    if not state.teleport_deferred:
        return
    state.teleport_deferred = False
    # _tlog.info("[TELEPORT] Flushing deferred teleport → calling _trigger_load from Update tick")
    _trigger_load(state)


# ===========================================================================
# Mod
# ===========================================================================

class Teleporter(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Random teleporter"
    __version__ = "1.0"

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

    @on_key_pressed("o")
    def key_random(self):
        # _tlog.info("*** KEY:O  RANDOM GALAXY ***")
        _prepare_teleport(self.state,
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, VOXEL_Y_MAX),
                          random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                          random.randint(0, SYSTEM_MAX))

    @on_key_pressed("[")
    def key_nearby(self):
        # _tlog.info("*** KEY:[  NEARBY ***")
        ps = gameData.player_state
        if ps is None:
            return
        ga = ps.mLocation.GalacticAddress
        cur_sys = int(ga.SolarSystemIndex)
        new_sys = cur_sys
        while new_sys == cur_sys:
            new_sys = random.randint(0, SYSTEM_MAX)
        _prepare_teleport(self.state, int(ga.VoxelX), int(ga.VoxelY), int(ga.VoxelZ), new_sys)
