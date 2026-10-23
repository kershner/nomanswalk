"""Queue an Atlas explosion with F3 and spawn it on the game update thread."""

import ctypes
import math
import threading
import time

from pymhf import Mod
from pymhf.core.hooking import Structure, on_key_pressed, static_function_hook
from pymhf.core.memutils import map_struct

import nmspy.data.basic_types as basic
import nmspy.data.types as nms
from nmspy.common import gameData
from shared_state import NMSModState, _make_logger


EFFECT_ID = "ATLASEXPLOSION"
FORWARD_DISTANCE = 80.0
UP_DISTANCE = 10.0
EFFECT_SECONDS = 10.0
BURST_COUNT = 3
BURST_INTERVAL_SECONDS = 0.2
COOLDOWN_SECONDS = 15.0
REQUEST_SECONDS = 5.0
_log = _make_logger("NukeEffect", "nuke_effect.log")


class _NukeEngine(Structure):
    # Same Cosmos node-matrix accessor used by selfie_camera. The player now
    # stores a PhysRel matrix; copying mPosition into cTkMatrix34 is invalid.
    @static_function_hook(
        "40 57 48 83 EC 20 49 8B F8 44 8B C1 41 C1 E8 13 45 85 C0 "
        "0F 84 ? ? ? ? 8B C1 25 FF FF 07 00 3D FF FF 07 00 0F 84 ? ? ? ? "
        "81 E1 FF FF 07 00 48 89 5C 24 30 48 8B 1D ? ? ? ?"
    )
    @staticmethod
    def GetNodeMatrices(
        node: ctypes.c_uint32,
        local: ctypes.c_void_p,
        absolute: ctypes.POINTER(basic.cTkMatrix34),
    ) -> None: ...


def _spawn_matrix(player):
    matrix = basic.cTkMatrix34()
    _NukeEngine.GetNodeMatrices(
        ctypes.c_uint32(int(player.mRootNode.lookupInt)),
        ctypes.c_void_p(), ctypes.byref(matrix),
    )
    for vector in (matrix.pos, matrix.at, matrix.up, matrix.right):
        if not all(math.isfinite(float(getattr(vector, axis))) for axis in "xyz"):
            raise ValueError("Player transform contains non-finite values")
    for vector in (matrix.at, matrix.up, matrix.right):
        length = math.sqrt(sum(float(getattr(vector, axis)) ** 2 for axis in "xyz"))
        if not 0.9 <= length <= 1.1:
            raise ValueError("Player transform has an invalid basis")
    for axis in "xyz":
        setattr(matrix.pos, axis, float(getattr(matrix.pos, axis))
                + FORWARD_DISTANCE * float(getattr(matrix.at, axis))
                + UP_DISTANCE * float(getattr(matrix.up, axis)))
    return matrix


class NukeRuntimeState:
    """Keep scene ownership across pyMHF's replacement of the mod instance."""

    def __init__(self):
        self.manager = None
        self.active_nodes = {}
        self.next_allowed = 0.0


class NukeEffect(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "F3: trigger three rapid Atlas space explosions on a planet."
    __version__ = "1.2"
    state = NMSModState()

    @property
    def runtime(self):
        # Use the already registered ModState field: pyMHF 0.2.5 does not
        # discover newly added ModState fields when reloading an older mod.
        if not hasattr(self.state, "nuke_runtime"):
            self.state.nuke_runtime = NukeRuntimeState()
        return self.state.nuke_runtime

    @property
    def _manager(self):
        return self.runtime.manager

    @_manager.setter
    def _manager(self, value):
        self.runtime.manager = value

    @property
    def _active_nodes(self):
        return self.runtime.active_nodes

    @property
    def _next_allowed(self):
        return self.runtime.next_allowed

    @_next_allowed.setter
    def _next_allowed(self, value):
        self.runtime.next_allowed = value

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._requested_until = 0.0
        self._burst_remaining = 0
        self._burst_next_at = 0.0
        self._burst_matrix = None
        _log.info("Nuke effect loaded; F3 queues %s", EFFECT_ID)

    @nms.cGcParticleManager.AddEffect_Internal.before
    def capture_manager(self, this, result, explosion_id, parent_node, matrix, permanent):
        # Capture a real instance from normal game particles. There is no
        # particle-manager field exposed on gameData.simulation in NMSpy.
        if this:
            if self._manager is None:
                _log.info("Captured live particle manager")
            self._manager = this.contents

    @on_key_pressed("f3")
    def request_nuke(self):
        # Key callbacks run outside the game update. Never call native particle
        # functions here. Collapse repeated requests instead of building a queue.
        with self._lock:
            self._requested_until = time.monotonic() + REQUEST_SECONDS
        _log.info("Nuke request queued; particle manager ready=%s", self._manager is not None)

    @nms.cTkFSMState.StateChange.before
    def reset_on_load(self, this, new_state, user_data, force_restart):
        if str(new_state.contents) in {
            "APPLOCALLOAD", "APPGLOBALLOAD", "MODESELECTOR", "APPSHUTDOWN",
        }:
            # Loading owns destruction of the old scene. Do not retire handles
            # or dereference a particle manager retained from that scene.
            self._manager = None
            self._active_nodes.clear()
            self._burst_remaining = 0
            self._burst_matrix = None
            with self._lock:
                self._requested_until = 0.0

    def _retire_due(self, now):
        for node, retire_at in list(self._active_nodes.items()):
            if now < retire_at:
                continue
            try:
                handle = basic.TkHandle()
                handle.lookupInt = node
                self._manager.RetireEffectsAndDestroyNodeOnCompletion(handle)
                del self._active_nodes[node]
                _log.info("Retired nuke effect node=%#x", node)
            except Exception:
                # Retain failed handles and retry without preventing cleanup
                # of the other effects in the burst.
                self._active_nodes[node] = now + REQUEST_SECONDS
                _log.exception("Nuke effect retirement failed node=%#x", node)

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        # This shared application hook remains active when a mod is reloaded.
        # An isolated cGcPlayer.Update hook can stop delivering after reload
        # with the installed pyMHF, leaving requests untouched indefinitely.
        player = gameData.player
        if player is not None:
            self.on_player_update(ctypes.pointer(player), 0.0)

    def on_player_update(self, this, step):
        now = time.monotonic()
        try:
            if self.state.loading or self._manager is None:
                with self._lock:
                    if self._requested_until and now > self._requested_until:
                        self._requested_until = 0.0
                        _log.warning("Nuke request expired while waiting for loading/particle manager")
                return
            self._retire_due(now)
            with self._lock:
                requested_until = self._requested_until
                self._requested_until = 0.0
            if requested_until:
                if now > requested_until:
                    _log.info("Nuke request expired")
                elif now < self._next_allowed or self._active_nodes:
                    _log.info("Nuke ignored during cooldown")
                else:
                    self._burst_remaining = BURST_COUNT
                    self._burst_next_at = now
                    self._burst_matrix = None
            if not self._burst_remaining or now < self._burst_next_at:
                return
            player = this.contents
            environment = gameData.player_environment
            if not player.mbSpawned or environment is None or not environment.IsOnPlanet():
                _log.info("Nuke ignored: player is not spawned on a planet")
                self._burst_remaining = 0
                return
            if self._burst_matrix is None:
                self._burst_matrix = _spawn_matrix(player)
                self._next_allowed = now + COOLDOWN_SECONDS
            matrix = self._burst_matrix
            # Allocate the output structure in Python so it stays alive through
            # the call. pyMHF returns a pointer for native structure returns.
            effect = nms.EffectInstance()
            effect_id = basic.TkID0x10(EFFECT_ID)
            address = self._manager.AddPermanentEffect(
                ctypes.byref(effect), ctypes.byref(effect_id), ctypes.byref(matrix),
            )
            if not address:
                raise RuntimeError("AddPermanentEffect returned no instance")
            instance = map_struct(address, nms.EffectInstance)
            node = int(instance.mNodeHandle.lookupInt)
            if node in (0, 0xFFFFFFFF) or not instance.mpExplosion:
                raise RuntimeError("Atlas explosion did not produce a live effect")
            self._active_nodes[node] = now + EFFECT_SECONDS
            self._burst_remaining -= 1
            self._burst_next_at = now + BURST_INTERVAL_SECONDS
            _log.info("Spawned %s effect node=%#x", EFFECT_ID, node)
        except Exception:
            _log.exception("Nuke effect failed")
            # Cancel the remaining burst on failure; already spawned nodes
            # remain tracked for cleanup.
            self._burst_remaining = 0
            with self._lock:
                self._requested_until = 0.0
