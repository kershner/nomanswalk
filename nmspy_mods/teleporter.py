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
# Galaxy constants
# ---------------------------------------------------------------------------
GALAXY_MIN = 0
GALAXY_MAX = 254
GALAXY_EISSENTAM = 9


# ---------------------------------------------------------------------------
# Other limits
# ---------------------------------------------------------------------------
RESPAWN_PORTAL = internal_enums.RespawnReason.Portal
VOXEL_XZ_MAX = 2000
VOXEL_Y_MAX = 255
SYSTEM_MAX = 599
SAFE_PLANET_INDEX = 0
LOAD_TIMEOUT_S = 25.0


_tlog = _make_logger("Teleporter", "random_teleporter.log")
_fsm_state_str = basic.cTkFixedString[0x10]()

_live_game_state_ptr = None
_live_game_state_update_count = 0


def _get_player_state():
    try:
        if _live_game_state_ptr:
            return _live_game_state_ptr.contents.mPlayerState
    except Exception:
        pass

    try:
        return gameData.player_state
    except Exception:
        return None


def _get_location():
    ps = _get_player_state()
    if ps is None:
        return None
    return ps.mLocation


def _read_location_dict():
    loc = _get_location()
    if loc is None:
        return None

    ga = loc.GalacticAddress

    return {
        "planet": int(ga.PlanetIndex),
        "system": int(ga.SolarSystemIndex),
        "voxel_x": int(ga.VoxelX),
        "voxel_y": int(ga.VoxelY),
        "voxel_z": int(ga.VoxelZ),
        "reality": int(loc.RealityIndex),
    }


def _tread_location(label):
    try:
        loc = _get_location()
        data = _read_location_dict()

        if loc is None or data is None:
            _tlog.error("[%s] location unavailable", label)
            return

        _tlog.info(
            "[%s] loc_addr=0x%X planet=%d sys=%d voxel=(%d,%d,%d) reality=%d game_state_updates=%d",
            label,
            ctypes.addressof(loc),
            data["planet"],
            data["system"],
            data["voxel_x"],
            data["voxel_y"],
            data["voxel_z"],
            data["reality"],
            _live_game_state_update_count,
        )

    except Exception:
        _tlog.error("[%s] exception:\n%s", label, traceback.format_exc())


def _write_location(vx, vy, vz, sys_idx, planet_idx, reality_idx):
    loc = _get_location()

    if loc is None:
        return False

    try:
        loc.GalacticAddress.PlanetIndex = int(planet_idx)
        loc.GalacticAddress.SolarSystemIndex = int(sys_idx)
        loc.GalacticAddress.VoxelX = int(vx)
        loc.GalacticAddress.VoxelY = int(vy)
        loc.GalacticAddress.VoxelZ = int(vz)
        loc.RealityIndex = int(reality_idx)
        return True

    except Exception:
        _tlog.error("[WRITE] location write failed:\n%s", traceback.format_exc())
        return False


def _trigger_load(state) -> bool:
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

        app.StateChange(ctypes.c_uint64(addr), ctypes.c_uint64(0), False)
        return True

    except Exception:
        _tlog.error("[TRIGGER] Exception:\n%s", traceback.format_exc())
        state.warp_pending = False
        state.loading = False
        return False


def _prepare_teleport(
    state,
    vx,
    vy,
    vz,
    sys_idx,
    planet_idx=SAFE_PLANET_INDEX,
    reality_idx=None,
):
    if state.loading:
        _tlog.warning(
            "[TELEPORT] Load in progress (%.1fs) — ignoring",
            time.time() - state.load_start_time,
        )
        return

    if state.teleport_deferred:
        _tlog.warning("[TELEPORT] Teleport already queued — ignoring duplicate key press")
        return

    if _get_location() is None:
        _tlog.error("[TELEPORT] Cannot get player mLocation")
        return

    if reality_idx is None:
        reality_idx = random.randint(GALAXY_MIN, GALAXY_MAX)

    vx = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, int(vx)))
    vy = max(-VOXEL_Y_MAX, min(VOXEL_Y_MAX, int(vy)))
    vz = max(-VOXEL_XZ_MAX, min(VOXEL_XZ_MAX, int(vz)))
    sys_idx = max(0, min(SYSTEM_MAX, int(sys_idx)))
    planet_idx = max(0, int(planet_idx))
    reality_idx = max(GALAXY_MIN, min(GALAXY_MAX, int(reality_idx)))

    _tread_location("BEFORE")
    _tlog.info(
        "[TELEPORT] Writing -> reality=%d planet=%d sys=%d voxel=(%d,%d,%d)",
        reality_idx,
        planet_idx,
        sys_idx,
        vx,
        vy,
        vz,
    )

    state.dest_vx = vx
    state.dest_vy = vy
    state.dest_vz = vz
    state.dest_sys = sys_idx
    state.dest_planet = planet_idx
    state.dest_reality = reality_idx

    if not _write_location(vx, vy, vz, sys_idx, planet_idx, reality_idx):
        _tlog.error("[TELEPORT] Write failed")
        return

    _tread_location("AFTER WRITE")
    state.teleport_deferred = True


def _flush_deferred_teleport(state):
    if state.loading and (time.time() - state.load_start_time) > LOAD_TIMEOUT_S:
        _tlog.info("[TELEPORT] Load timeout — clearing loading flag")
        state.loading = False
        state.warp_pending = False

    if not state.teleport_deferred:
        return

    state.teleport_deferred = False
    _trigger_load(state)


class Teleporter(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "Random teleporter"
    __version__ = "1.2-live-player-state"

    state = NMSModState()

    @nms.cGcGameState.Update.before
    def on_game_state_update(self, this, lfTimeStep):
        global _live_game_state_ptr, _live_game_state_update_count

        try:
            _live_game_state_ptr = this
            _live_game_state_update_count += 1
        except Exception:
            pass

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        _flush_deferred_teleport(self.state)

    @nms.cTkFSMState.StateChange.after
    def on_fsm_state_change(
        self,
        this,
        lNewStateID: ctypes._Pointer[basic.cTkFixedString[0x10]],
        lpUserData,
        lbForceRestart,
    ):
        try:
            name = str(lNewStateID.contents)

            if name == "APPVIEW":
                if self.state.loading:
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
                self.state.warp_pending = False
                self.state.last_respawn_reason = int(RESPAWN_PORTAL)
                return int(RESPAWN_PORTAL)

        except Exception:
            _tlog.warning("[RESPAWN] hook error:\n%s", traceback.format_exc())

        return None

    @on_key_pressed("o")
    def key_random_local(self):
        reality_idx = None

        _prepare_teleport(
            self.state,
            random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
            random.randint(0, VOXEL_Y_MAX),
            random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
            random.randint(0, SYSTEM_MAX),
            reality_idx=reality_idx,
        )

    @on_key_pressed("p")
    def key_random_galaxy(self):
        cur = _read_location_dict()

        if cur is None:
            _tlog.error("[P] Cannot read current location")
            return

        cur_reality = cur["reality"]
        new_reality = cur_reality

        while new_reality == cur_reality:
            new_reality = random.randint(GALAXY_MIN, GALAXY_MAX)

        _prepare_teleport(
            self.state,
            random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
            random.randint(0, VOXEL_Y_MAX),
            random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
            random.randint(0, SYSTEM_MAX),
            reality_idx=new_reality,
        )

    @on_key_pressed("[")
    def key_nearby(self):
        cur = _read_location_dict()

        if cur is None:
            _tlog.error("[[] Cannot read current location")
            return

        cur_sys = cur["system"]
        new_sys = cur_sys

        while new_sys == cur_sys:
            new_sys = random.randint(0, SYSTEM_MAX)

        _prepare_teleport(
            self.state,
            cur["voxel_x"],
            cur["voxel_y"],
            cur["voxel_z"],
            new_sys,
            planet_idx=cur["planet"],
            reality_idx=cur["reality"],
        )