# /// script
# dependencies = ["nmspy==170671.2", "pymhf[gui]==0.2.3"]
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
import json
import os
import random
import time
import traceback
from typing import Annotated

from pymhf import Mod
from pymhf.core.hooking import Structure, function_hook, on_key_pressed
from pymhf.utils.partial_struct import Field, partial_struct

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


# ---------------------------------------------------------------------------
# Other limits
# ---------------------------------------------------------------------------
RESPAWN_PORTAL = internal_enums.RespawnReason.Portal
VOXEL_XZ_MAX = 2000
VOXEL_Y_MAX = 255
SYSTEM_MAX = 599
SAFE_PLANET_INDEX = 0
LOAD_TIMEOUT_S = 25.0
RAW_PORTAL_ALLOCATION_SIZE = 0x1000
TELEPORT_REQUEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teleport_request.json")


_tlog = _make_logger("Teleporter", "random_teleporter.log")
_fsm_state_str = basic.cTkFixedString[0x10]()

_live_game_state_ptr = None
_live_game_state_update_count = 0


# Only fields used by portal warping are mapped. Game-owned instances are
# preferred; the raw path compensates with a larger backing allocation.
@partial_struct
class cGcPortalComponent(Structure):
    mTargetUA: Annotated[ctypes.c_ulonglong, Field(ctypes.c_ulonglong, 0x88)]
    mbActive: Annotated[ctypes.c_bool, Field(ctypes.c_bool, 0xA0)]

    @function_hook(
        "40 53 48 83 EC ? 33 C0 66 C7 81 ? ? ? ? ? ? 48 89 81"
    )
    def OnAttached(self, this: "ctypes._Pointer[cGcPortalComponent]"):
        pass

    @function_hook(
        "48 8B C4 55 56 48 8D 68 ? 48 81 EC ? ? ? ? 48 8B F1 48 89 58"
    )
    def Prepare(self, this: "ctypes._Pointer[cGcPortalComponent]"):
        pass

    @function_hook(
        "40 55 41 56 48 8D 6C 24 ? 48 81 EC ? ? ? ? 80 B9"
    )
    def WarpPlayer(
        self,
        this: "ctypes._Pointer[cGcPortalComponent]",
        a2: ctypes.c_ulonglong,
        a3: ctypes.c_ulonglong,
        a4: ctypes.c_ulonglong,
    ):
        pass


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


def _write_reality_index(reality_idx):
    loc = _get_location()
    if loc is None:
        return False

    try:
        loc.RealityIndex = int(reality_idx)
        return True
    except Exception:
        _tlog.error("[WRITE] reality index write failed:\n%s", traceback.format_exc())
        return False



def _signed_portal_coord(value, bits):
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def _decode_portal_address(address):
    address = str(address).strip().upper()

    if len(address) != 12 or any(c not in "0123456789ABCDEF" for c in address):
        raise ValueError("invalid 12-character portal address")

    planet = int(address[0], 16)
    system = int(address[1:4], 16)
    voxel_y = int(address[4:6], 16)
    voxel_z = int(address[6:9], 16)
    voxel_x = int(address[9:12], 16)

    if not 1 <= planet <= 6:
        raise ValueError("portal planet glyph must be from 1 to 6")
    if system in (0, 0xFFF):
        raise ValueError("portal system index cannot be 000 or FFF")
    if voxel_y == 0x80:
        raise ValueError("portal Y coordinate 80 is reserved")
    if voxel_z == 0x800:
        raise ValueError("portal Z coordinate 800 is reserved")
    if voxel_x == 0x800:
        raise ValueError("portal X coordinate 800 is reserved")

    return {
        "planet": planet - 1,
        "system": system,
        "voxel_y": _signed_portal_coord(voxel_y, 8),
        "voxel_z": _signed_portal_coord(voxel_z, 12),
        "voxel_x": _signed_portal_coord(voxel_x, 12),
    }


def _pack_universe_address(destination, reality_idx):
    """Pack decoded address fields into NMS's internal 64-bit UA layout."""
    # GalacticAddress/portal UAs store the planet number as 1..6, while the
    # environment and our decoded destination use a zero-based 0..5 index.
    packed_planet = int(destination["planet"]) + 1
    return (
        (int(destination["voxel_x"]) & 0xFFF)
        | ((int(destination["voxel_z"]) & 0xFFF) << 12)
        | ((int(destination["voxel_y"]) & 0xFF) << 24)
        | ((int(reality_idx) & 0xFF) << 32)
        | ((int(destination["system"]) & 0xFFF) << 40)
        | ((packed_planet & 0xF) << 52)
    )


def _read_teleport_request():
    try:
        with open(TELEPORT_REQUEST_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        # Consume every successfully parsed request, including requests whose
        # values fail validation, so a bad request cannot be retried forever.
        os.remove(TELEPORT_REQUEST_FILE)
        address = payload.get("address")
        destination = _decode_portal_address(address) if address else None
        reality_idx = payload.get("reality")
        if reality_idx is not None:
            reality_idx = int(reality_idx)
            if not GALAXY_MIN <= reality_idx <= GALAXY_MAX:
                raise ValueError("galaxy reality index must be from 0 to 254")
        return address.upper() if address else None, destination, reality_idx
    except Exception:
        _tlog.error("[ADDRESS] Could not read teleport request:\n%s", traceback.format_exc())
        return None, None, None

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
    system_max=SYSTEM_MAX,
    voxel_xz_max=VOXEL_XZ_MAX,
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

    vx = max(-voxel_xz_max, min(voxel_xz_max, int(vx)))
    vy = max(-VOXEL_Y_MAX, min(VOXEL_Y_MAX, int(vy)))
    vz = max(-voxel_xz_max, min(voxel_xz_max, int(vz)))
    sys_idx = max(0, min(system_max, int(sys_idx)))
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
    __description__ = "Random and portal-address teleporter"
    __version__ = "1.9.1-packed-ua-cleanup"

    state = NMSModState()
    _portal_component = None
    _portal_component_origin = None
    _raw_portal_buffer = None
    _raw_component_address = None

    @cGcPortalComponent.Prepare.after
    def capture_portal_component(
        self,
        this: ctypes._Pointer[cGcPortalComponent],
    ):
        """Remember a fully initialized, game-owned portal component."""
        try:
            component = this.contents
            component_address = ctypes.addressof(component)
            if component_address == self._raw_component_address:
                _tlog.info("[PORTAL] Raw component initialized by Prepare")
                return
            self._portal_component = component
            current = _read_location_dict()
            self._portal_component_origin = (
                current["reality"],
                current["voxel_x"],
                current["voxel_y"],
                current["voxel_z"],
                current["system"],
                current["planet"],
            ) if current else None
            _tlog.info(
                "[PORTAL] Captured live component addr=0x%X target_ua=%016X origin=%s",
                component_address,
                int(component.mTargetUA),
                self._portal_component_origin,
            )
        except Exception:
            self._portal_component = None
            self._portal_component_origin = None
            _tlog.warning("[PORTAL] Component capture failed:\n%s", traceback.format_exc())

    def _clear_portal_component(self, reason):
        if self._portal_component is not None:
            _tlog.info("[PORTAL] Releasing captured component (%s)", reason)
        self._portal_component = None
        self._portal_component_origin = None

    def _clear_raw_component(self, reason):
        if self._raw_portal_buffer is not None:
            _tlog.info("[PORTAL] Releasing raw component buffer (%s)", reason)
        self._raw_portal_buffer = None
        self._raw_component_address = None

    def _record_destination(self, destination, reality_idx):
        self.state.dest_vx = destination["voxel_x"]
        self.state.dest_vy = destination["voxel_y"]
        self.state.dest_vz = destination["voxel_z"]
        self.state.dest_sys = destination["system"]
        self.state.dest_planet = destination["planet"]
        self.state.dest_reality = reality_idx

    def _raw_portal_warp(self, address, destination, reality_idx):
        """Issue a portal warp without requiring a previously loaded portal."""
        if self.state.loading or self.state.teleport_deferred:
            _tlog.warning("[PORTAL] A teleport is already in progress")
            return

        try:
            # mTargetUA is NMS's packed internal universe address, not the
            # human-facing 12-glyph portal code interpreted as one integer.
            target_ua = _pack_universe_address(destination, reality_idx)

            # The real component is larger than our 0xA8 mapping. Keep an
            # oversized backing buffer alive for the entire load.
            buffer = ctypes.create_string_buffer(RAW_PORTAL_ALLOCATION_SIZE)
            component = cGcPortalComponent.from_buffer(buffer)
            self._raw_portal_buffer = buffer
            self._raw_component_address = ctypes.addressof(component)

            _tread_location("BEFORE RAW PORTAL")
            _tlog.info(
                "[PORTAL] Direct raw warp -> glyphs=%s packed_ua=%016X reality=%d planet=%d sys=%d voxel=(%d,%d,%d)",
                address,
                target_ua,
                reality_idx,
                destination["planet"],
                destination["system"],
                destination["voxel_x"],
                destination["voxel_y"],
                destination["voxel_z"],
            )

            self._record_destination(destination, reality_idx)

            # The packed UA owns the destination; RealityIndex also needs to be
            # seeded on player state for cross-galaxy portal warps.
            if not _write_reality_index(reality_idx):
                raise RuntimeError("could not seed requested galaxy")

            component.OnAttached()
            component.mTargetUA = target_ua
            component.mbActive = True
            component.Prepare()
            self.state.loading = True
            self.state.load_start_time = time.time()
            component.WarpPlayer(0, 0, 0)
        except Exception:
            self.state.loading = False
            self._clear_raw_component("raw warp failed")
            _tlog.error("[PORTAL] Direct raw warp failed:\n%s", traceback.format_exc())

    def _native_portal_warp(self, address, destination, reality_idx):
        """Use the game's portal pipeline so it retains the target planet."""
        component = self._portal_component
        if component is None:
            return False

        if self.state.loading or self.state.teleport_deferred:
            _tlog.warning("[PORTAL] A teleport is already in progress")
            return True

        try:
            target_ua = _pack_universe_address(destination, reality_idx)

            _tread_location("BEFORE NATIVE PORTAL")
            _tlog.info(
                "[PORTAL] Native warp -> glyphs=%s packed_ua=%016X reality=%d planet=%d sys=%d voxel=(%d,%d,%d)",
                address,
                target_ua,
                reality_idx,
                destination["planet"],
                destination["system"],
                destination["voxel_x"],
                destination["voxel_y"],
                destination["voxel_z"],
            )

            self._record_destination(destination, reality_idx)

            if not _write_reality_index(reality_idx):
                _tlog.error("[PORTAL] Could not seed requested galaxy")
                return True

            component.mTargetUA = target_ua
            component.mbActive = True
            self.state.loading = True
            self.state.load_start_time = time.time()
            component.WarpPlayer(0, 0, 0)

            # The component belongs to the scene being left. Never reuse it;
            # the destination portal's Prepare hook can supply the next one.
            self._clear_portal_component("native warp issued")
            return True
        except Exception:
            self.state.loading = False
            self._clear_portal_component("native warp failed")
            _tlog.error("[PORTAL] Native warp failed:\n%s", traceback.format_exc())
            return True

    @nms.cGcGameState.Update.before
    def on_game_state_update(self, this, lfTimeStep):
        global _live_game_state_ptr, _live_game_state_update_count

        try:
            _live_game_state_ptr = this
            _live_game_state_update_count += 1

            if self._portal_component_origin is not None and not self.state.loading:
                current = _read_location_dict()
                if current is not None:
                    current_origin = (
                        current["reality"],
                        current["voxel_x"],
                        current["voxel_y"],
                        current["voxel_z"],
                        current["system"],
                        current["planet"],
                    )
                    if current_origin != self._portal_component_origin:
                        self._clear_portal_component("player location changed")
        except Exception:
            pass

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        _flush_deferred_teleport(self.state)
        if self._raw_portal_buffer is not None and not self.state.loading:
            self._clear_raw_component("load finished or timed out")

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
                self._clear_raw_component("APPVIEW reached")

            elif name in ("APPLOCALLOAD", "MODESELECTOR", "APPSHUTDOWN", "APPGLOBALLOAD"):
                self._clear_portal_component("FSM state %s" % name)
                if name != "APPLOCALLOAD":
                    self._clear_raw_component("FSM state %s" % name)
                if self.state.loading:
                    if name != "APPLOCALLOAD":
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
    def key_teleport(self):
        if not os.path.exists(TELEPORT_REQUEST_FILE):
            _prepare_teleport(
                self.state,
                random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                random.randint(0, VOXEL_Y_MAX),
                random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                random.randint(0, SYSTEM_MAX),
            )
            return

        address, destination, requested_reality = _read_teleport_request()
        if destination is None and requested_reality is None:
            return

        cur = _read_location_dict()
        if cur is None:
            _tlog.error("[ADDRESS] Cannot read current galaxy for %s", address)
            return

        reality_idx = cur["reality"] if requested_reality is None else requested_reality
        if destination is None:
            destination = {
                "voxel_x": random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                "voxel_y": random.randint(0, VOXEL_Y_MAX),
                "voxel_z": random.randint(-VOXEL_XZ_MAX, VOXEL_XZ_MAX),
                "system": random.randint(0, SYSTEM_MAX),
                "planet": SAFE_PLANET_INDEX,
            }
        _tlog.info("[ADDRESS] %s -> reality=%d destination=%s", address, reality_idx, destination)
        if address and self._native_portal_warp(address, destination, reality_idx):
            return

        if address:
            _tlog.info("[PORTAL] No live component; issuing direct raw portal warp")
            self._raw_portal_warp(address, destination, reality_idx)
            return
        _prepare_teleport(
            self.state,
            destination["voxel_x"],
            destination["voxel_y"],
            destination["voxel_z"],
            destination["system"],
            planet_idx=destination["planet"],
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
