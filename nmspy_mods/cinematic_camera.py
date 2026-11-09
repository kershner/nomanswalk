# /// script
# dependencies = ["nmspy==184702.0", "pymhf[gui]==0.2.5"]
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
# internal_mod_dir = ""
# mod_dir = ""
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "warning"
# window_name_override = "Cinematic Camera"
# [tool.pymhf.gui]
# shown = false
# ///
"""Standalone, opt-in cinematic tracking for desktop NMS."""
import ctypes as C
import json
import math
import random
import re
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import struct
import threading
import time

from pymhf import FUNCDEF, Mod
from pymhf.core import _internal
from pymhf.core.hooking import manual_hook, on_key_pressed
from pymhf.gui.decorators import FLOAT, BOOLEAN, STRING, gui_button, gui_group
from nmspy.data.basic_types import cTkPhysRelMat34, cTkPhysRelVec3
from nmspy.common import gameData
import nmspy.data.types as nms
from pymhf.core.memutils import get_addressof

# Match function code while allowing relative addresses to move between patches.
# These describe calling conventions and layouts, rather than an EXE version.
NATIVE_SIGNATURES = {
    "camera_update": "40 53 48 83 EC 30 0F 29 74 24 20 48 8B D9 0F 28 F1 E8 ? ? ? ? 48 8B 05 ? ? ? ? 48 85 C0 75 ? 0F 10 05 ? ? ? ? 66 48 0F 7E C1",
    "ray_construct": "48 89 5C 24 08 57 48 83 EC 20 66 0F 6F 05 ? ? ? ? 33 FF 48 89 39 48 8B D9 48 89 79 08 0F 11 41 40 48 89 79 50 48 89 79 58 89 79 60",
    "ray_destroy": "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 80 79 7C 00 48 8B D9 74 ? E8 ? ? ? ? 48 8D 4B 68 C6 43 7D 00 E8 ? ? ? ? 48 8B 53 70",
    "ray_initialise": "48 83 EC 38 48 8B 44 24 68 4C 8B 90 38 0A 00 00 48 8D 44 24 68 48 89 44 24 28 48 8B 44 24 60 48 89 44 24 20 4C 89 54 24 68 E8 ? ? ? ?",
    "ray_set": "48 89 5C 24 10 48 89 6C 24 18 56 41 56 41 57 48 83 EC 40 8B C2 48 8B F1 48 8B 09 4D 8B F1 48 69 E8 D0 00 00 00 4D 8B F8 48 85 C9 74 ?",
    "ray_run": "48 89 5C 24 10 56 48 83 EC 20 33 F6 48 8B D9 39 71 60 76 ? 48 89 7C 24 30 0F 1F 80 00 00 00 00 48 8B 43 58 48 8D 3C B6 48 C1 E7 05 C6 04 07 01",
    "ray_point": "48 89 5C 24 08 57 48 83 EC 20 41 8B C0 48 8B FA 4C 69 C0 D0 00 00 00 4C 03 41 70 45 85 C9 75 ? 49 8D 58 20 EB ? 41 8D 59 FF 48 C1 E3 06",
    "physics_world": "48 8B 11 48 8D 0D ? ? ? ? E9 ? ? ? ? CC 48 8B D1 48 8D 0D ? ? ? ? E9 ? ? ? ? CC 48 8B 05 ? ? ? ? C3",
}


def find_unique_code(data, signature, name):
    pattern = b"".join(b"." if token == "?" else re.escape(bytes([int(token, 16)]))
                       for token in signature.split())
    matches = list(re.finditer(pattern, data, re.DOTALL))
    if len(matches) != 1:
        raise RuntimeError(f"Cannot resolve cinematic {name}: expected one code match, found {len(matches)}")
    return matches[0].start()


def resolve_native_addresses(path):
    import pefile

    raw = Path(path).read_bytes()
    with pefile.PE(data=raw, fast_load=True) as pe:
        text = next(section for section in pe.sections if section.Name.startswith(b".text"))
        code = text.get_data()
        addresses = {name: text.VirtualAddress + find_unique_code(code, signature, name)
                     for name, signature in NATIVE_SIGNATURES.items()}
        # MSVC RTTI identifies the primary camera-manager vtable without a fixed RVA.
        name_offset = raw.find(b".?AVcGcCameraManager@@\0")
        if name_offset < 16:
            raise RuntimeError("Cannot resolve cinematic camera-manager RTTI")
        descriptor = pe.get_rva_from_offset(name_offset - 16)
        vtables = set()
        for match in re.finditer(re.escape(struct.pack("<I", descriptor)), raw):
            col_offset = match.start() - 12
            if col_offset < 0:
                continue
            col = pe.get_rva_from_offset(col_offset)
            signature, offset, construction, _, _, self_rva = struct.unpack_from("<6I", raw, col_offset)
            if (signature, offset, construction, self_rva) != (1, 0, 0, col):
                continue
            pointer = struct.pack("<Q", pe.OPTIONAL_HEADER.ImageBase + col)
            for reference in re.finditer(re.escape(pointer), raw):
                vtable = pe.get_rva_from_offset(reference.start()) + 8
                update = struct.unpack("<Q", pe.get_data(vtable + 16, 8))[0]
                if update == pe.OPTIONAL_HEADER.ImageBase + addresses["camera_update"]:
                    vtables.add(vtable)
        if len(vtables) != 1:
            raise RuntimeError("Cannot resolve cinematic camera-manager vtable")
        addresses["camera_vtable"] = vtables.pop()
        return addresses


NATIVE = resolve_native_addresses(_internal.BINARY_PATH)
PLAYER_OFFSET = 0x72CDE0
PLAYER_GRAPHICS = 0x310
PLAYER_SPAWNED = 0x3160
PLAYER_MODE = 0x72B6CC

DEFAULT_SETTINGS = {
    "maximum_shot_seconds": 40.0,
    "distance_max": 20.0,
    "height_max": 20.05,
    "speed_max": 3.0,
    "zoom_max": 1,
    "smooth_transitions": False,
    "transition_seconds": 3.0,
    "hold_shot": False,
    "vertical_response": 2.0
}
SETTING_BOUNDS = {
    "maximum_shot_seconds": (20, 120), "distance_max": (3, 120),
    "height_max": (1.5, 80), "speed_max": (math.degrees(0.04), 20),
    "zoom_max": (1, 4), "transition_seconds": (0, 15), "vertical_response": (0.1, 20),
}

# Placement is sampled separately from angle, so every angle can be a close shot.
BASE_FRAMING = {
    "distance_scale": 1.0, "height_scale": 1.0, "target_height": 1.4,
    "rear_distance": 20.0, "rear_height": 7.0,
    "front_distance": 20.0, "front_height": 6.0,
    "side_distance": 20.0, "side_height": 6.0, "side_behind": 4.0,
    "orbit_distance": 28.0, "orbit_height": 14.0,
    "orbit_speed_degrees": math.degrees(0.04), "orbit_start_degrees": math.degrees(0.6),
}


def normalize_settings(source):
    """Read only the exposed controls; ignore obsolete saved fields."""
    if not isinstance(source, dict):
        raise ValueError("Camera settings must be an object")
    settings = dict(DEFAULT_SETTINGS)
    for key, default in settings.items():
        value = source.get(key, default)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        else:
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
            low, high = SETTING_BOUNDS[key]
            value = max(low, min(high, value))
        settings[key] = value
    return settings


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, scale):
    return tuple(x * scale for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def length(a):
    return math.sqrt(dot(a, a))


def unit(a):
    size = length(a)
    if not math.isfinite(size) or size < 1e-6:
        raise ValueError("Degenerate camera axis")
    return mul(a, 1 / size)


def cross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])


def validate_basis(right, up, at):
    axes = (right, up, at)
    if any(not all(math.isfinite(x) for x in a) or abs(length(a) - 1) > 0.08 for a in axes):
        raise ValueError("Unexpected native transform axes")
    if any(abs(dot(axes[i], axes[j])) > 0.08 for i, j in ((0, 1), (0, 2), (1, 2))):
        raise ValueError("Native transform is not orthogonal")
    if dot(cross(right, up), at) < 0.9:
        raise ValueError("Unexpected transform handedness")


def look_at(position, target, up, forward_sign):
    at = mul(unit(sub(target, position)), forward_sign)
    right = unit(cross(up, at))
    return right, unit(cross(at, right)), at


def blend_offset(start, end, up, amount):
    """Travel around the subject instead of through them on opposite shots."""
    start_height, end_height = dot(start, up), dot(end, up)
    a = sub(start, mul(up, start_height))
    b = sub(end, mul(up, end_height))
    ar, br = length(a), length(b)
    a, b = unit(a), unit(b)
    angle = math.atan2(dot(up, cross(a, b)), max(-1, min(1, dot(a, b))))
    direction = add(mul(a, math.cos(angle * amount)), mul(cross(up, a), math.sin(angle * amount)))
    return add(mul(direction, ar + (br - ar) * amount),
               mul(up, start_height + (end_height - start_height) * amount))


@dataclass(frozen=True)
class Pose:
    position: tuple
    right: tuple
    up: tuple
    at: tuple
    cut: bool
    shot: str


class Overlay:
    """Restore only bytes still owned by us, in the same live game session."""
    def __init__(self):
        self.pending = None

    def apply(self, address, original, written, owner, write):
        # Arm restoration before writing, so a partial failure remains recoverable.
        self.pending = (address, original, written, owner)
        write(address, written)

    def restore(self, owner, read, write):
        pending, self.pending = self.pending, None
        if pending is None:
            return
        address, original, written, previous_owner = pending
        if owner == previous_owner and read(address, len(written)) == written:
            write(address, original)


class Director:
    SHOTS = ("rear", "side", "front", "high_orbit")

    def __init__(self, settings=None, seed=None):
        self.settings = normalize_settings(settings or DEFAULT_SETTINGS)
        self.minimum, self.maximum = 20.0, self.settings["maximum_shot_seconds"]
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        self.shot = None
        self.remaining = 0.0
        self.elapsed = 0.0
        self.heading = None
        self.position = None
        self.last_player = None
        self.bag = []
        self.stable_up = None
        self.anchor = None
        self.offset = None
        self.transition_from = None
        self.transition_elapsed = 0.0
        self._force_next = False
        self.shot_values = {}
        self.aim_height = None
        self.transition_aim_from = None

    def configure(self, settings):
        previous = self.settings
        self.settings = normalize_settings(settings)
        self.maximum = self.settings["maximum_shot_seconds"]
        self.remaining = min(self.remaining, self.maximum)
        if self.offset is not None and previous["transition_seconds"] != self.settings["transition_seconds"]:
            self.transition_from = self.offset
            self.transition_elapsed = 0.0
            self.transition_aim_from = self.aim_height

    def next(self):
        self.remaining = 0.0
        self._force_next = True

    def update(self, dt, player_position, player_up, player_forward, forward_sign):
        if not math.isfinite(dt) or not 0 < dt <= 0.25:
            raise ValueError("Paused or discontinuous camera timestep")
        if any(not math.isfinite(x) for x in player_position):
            raise ValueError("Invalid player position")
        if self.last_player is not None and length(sub(player_position, self.last_player)) > 40:
            raise ValueError("Player teleported; cinematic camera stopped")
        self.last_player = player_position
        settings = self.settings
        measured_up = unit(player_up)
        if self.stable_up is None:
            self.stable_up = measured_up
        else:
            # Resist jetpack/body pitch and roll while adapting to planet curvature.
            self.stable_up = unit(add(self.stable_up, mul(sub(measured_up, self.stable_up),
                                                        1 - math.exp(-0.5 * dt))))
        up = self.stable_up
        if self.anchor is None:
            self.anchor = player_position
        else:
            delta = sub(player_position, self.anchor)
            vertical = mul(up, dot(delta, up))
            horizontal = sub(delta, vertical)
            # Both the camera and its aim point use this same stabilized anchor.
            # Vertical bob is damped more heavily than travel across the ground.
            self.anchor = add(self.anchor, add(mul(horizontal, 1 - math.exp(-4.0 * dt)),
                                               mul(vertical, 1 - math.exp(-settings["vertical_response"] * dt))))
        cut = self.shot is None or self._force_next or (self.remaining <= 0 and not settings["hold_shot"])
        if cut:
            self._force_next = False
            self.transition_from = self.offset
            self.transition_aim_from = self.aim_height
            self.transition_elapsed = 0.0
            if not self.bag:
                self.bag = list(self.SHOTS)
                self.rng.shuffle(self.bag)
                if self.bag[-1] == self.shot:
                    self.bag[0], self.bag[-1] = self.bag[-1], self.bag[0]
            self.shot = self.bag.pop()
            self.heading = unit(sub(player_forward, mul(up, dot(player_forward, up))))
            self.remaining = self.rng.uniform(self.minimum, self.maximum)
            self.elapsed = 0.0
            self.side = self.rng.choice((-1, 1))
            self.shot_values = dict(BASE_FRAMING)
            distance = self.rng.uniform(3.0, settings["distance_max"])
            height = self.rng.uniform(1.5, settings["height_max"])
            self.shot_values["distance_scale"] = self.rng.uniform(1.0, settings["zoom_max"])
            for prefix in ("rear", "front", "side", "orbit"):
                self.shot_values[prefix + "_distance"] = distance
                self.shot_values[prefix + "_height"] = height
            self.shot_values["side_behind"] = min(4.0, distance * 0.2)
            # A fixed angular speed at 480m sweeps terrain at ~25m/s. Limit
            # sideways travel to 6m/s while retaining the full distance range.
            speed_cap = min(settings["speed_max"], math.degrees(6.0 / (distance * self.shot_values["distance_scale"])))
            self.shot_values["orbit_speed_degrees"] = self.rng.uniform(min(BASE_FRAMING["orbit_speed_degrees"], speed_cap), speed_cap)
        self.remaining -= dt
        self.elapsed += dt
        self.transition_elapsed += dt
        heading = unit(sub(self.heading, mul(up, dot(self.heading, up))))
        right = unit(cross(up, heading))
        framing = self.shot_values
        if self.shot == "rear":
            offset = add(mul(heading, -framing["rear_distance"] * framing["distance_scale"]),
                         mul(up, framing["rear_height"] * framing["height_scale"]))
        elif self.shot == "front":
            offset = add(mul(heading, framing["front_distance"] * framing["distance_scale"]),
                         mul(up, framing["front_height"] * framing["height_scale"]))
        elif self.shot == "side":
            offset = add(add(mul(right, self.side * framing["side_distance"] * framing["distance_scale"]),
                             mul(heading, -framing["side_behind"] * framing["distance_scale"])),
                         mul(up, framing["side_height"] * framing["height_scale"]))
        else:
            angle = self.side * math.radians(framing["orbit_start_degrees"] + self.elapsed * framing["orbit_speed_degrees"])
            radius = framing["orbit_distance"] * framing["distance_scale"]
            offset = add(add(mul(heading, -radius * math.cos(angle)), mul(right, radius * math.sin(angle))),
                         mul(up, framing["orbit_height"] * framing["height_scale"]))
        duration = settings["transition_seconds"] if settings["smooth_transitions"] else 0
        eased = 1.0
        if self.transition_from is not None and self.transition_elapsed < duration:
            t = self.transition_elapsed / duration
            eased = t*t*t * (10 - 15*t + 6*t*t)
            offset = blend_offset(self.transition_from, offset, up, eased)
        self.offset = offset
        self.position = add(self.anchor, offset)
        self.aim_height = framing["target_height"]
        if self.transition_aim_from is not None:
            self.aim_height = self.transition_aim_from + (self.aim_height - self.transition_aim_from) * eased
        target = add(self.anchor, mul(up, self.aim_height))
        axes = look_at(self.position, target, up, forward_sign)
        return Pose(self.position, *axes, cut, self.shot)


BASE = _internal.BASE_ADDRESS
ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "cinematic_camera_settings.json"
STATUS_FILE = ROOT / "cinematic_camera_status.json"
REQUEST_FILE = ROOT / "cinematic_camera_request.json"
# The manager runs every active behaviour before returning;
# a behaviour-level hook is too early.
CAMERA_MANAGER_UPDATE = NATIVE["camera_update"]
CAMERA_MANAGER_VTABLE = NATIVE["camera_vtable"]
LOG = logging.getLogger("StandaloneCinematicCamera")
LOG.setLevel(logging.WARNING)
if not LOG.handlers:
    handler = RotatingFileHandler(ROOT / "cinematic_camera.log", maxBytes=256_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.propagate = False

KERNEL = C.WinDLL("kernel32", use_last_error=True)
KERNEL.GetCurrentProcess.restype = C.c_void_p
PROCESS = KERNEL.GetCurrentProcess()
for name in ("ReadProcessMemory", "WriteProcessMemory"):
    func = getattr(KERNEL, name)
    func.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    func.restype = C.c_int

class ProcessMemory:
    """Reuse per-thread buffers while returning independent read snapshots."""
    def __init__(self, kernel, process):
        self.kernel = kernel
        self.process = process
        self.local = threading.local()

    def _state(self):
        state = getattr(self.local, "state", None)
        if state is None:
            write_count = C.c_size_t()
            state = {"buffers": {}, "write_count": write_count,
                     "write_pointer": C.byref(write_count)}
            self.local.state = state
        return state

    def _buffer(self, state, size):
        cached = state["buffers"].get(size)
        if cached is None:
            count = C.c_size_t()
            cached = (C.create_string_buffer(size), count, C.byref(count))
            state["buffers"][size] = cached
        cached[1].value = 0
        return cached

    def read(self, address, size):
        buffer, count, pointer = self._buffer(self._state(), size)
        if not address or not self.kernel.ReadProcessMemory(self.process, address, buffer, size, pointer) or count.value != size:
            raise RuntimeError(f"Camera memory is unavailable at {address:#x}")
        return buffer.raw

    def write(self, address, data):
        state = self._state()
        size = len(data)
        count = state["write_count"]
        count.value = 0
        # ctypes keeps these immutable bytes alive until the native call returns.
        if not address or not self.kernel.WriteProcessMemory(self.process, address, data, size, state["write_pointer"]) or count.value != size:
            raise RuntimeError("Could not apply camera transform")


MEMORY = ProcessMemory(KERNEL, PROCESS)
read = MEMORY.read
write = MEMORY.write


def data_address():
    data = gameData.data
    return C.addressof(data) if data is not None else 0


def xyz(vector):
    return (float(vector.x), float(vector.y), float(vector.z))


def position(matrix):
    return add(xyz(matrix.pos.local), xyz(matrix.pos.offset))


def set_vector(vector, values):
    vector.x, vector.y, vector.z = values


def setting_control(key, label):
    """Bind pyMHF's standard widgets to settings queued for the game thread."""
    def getter(self):
        return self.settings[key]

    def setter(self, value):
        self.actions.put(("setting", key, value))

    getter.__name__ = key
    getter.__qualname__ = f"CinematicCamera.{key}"
    if isinstance(DEFAULT_SETTINGS[key], bool):
        getter = BOOLEAN(label)(getter)
    else:
        low, high = SETTING_BOUNDS[key]
        getter = FLOAT(label, is_slider=True, min_value=low, max_value=high, format="%.2f")(getter)
    return property(getter, setter)


class CameraTransforms(C.Structure):
    _fields_ = [("current", cTkPhysRelMat34), ("committed", cTkPhysRelMat34), ("previous", cTkPhysRelMat34)]


def native(offset, result, *arguments):
    return C.CFUNCTYPE(result, *arguments)(BASE + offset)


def camera_clearance(desired, up, hit, previous, dt):
    """Lift only out of terrain; ease downward after the ground drops away."""
    if hit is not None and not all(math.isfinite(x) for x in hit):
        raise ValueError("Invalid camera collision position")
    lift = 0.0 if hit is None else max(0.0, dot(sub(hit, desired), up) + 0.25)
    if previous is not None:
        lift = max(lift, previous * math.exp(-2 * dt))
    return add(desired, mul(up, lift)), lift


class TerrainProbeCache:
    """Sample terrain at 10 Hz; resolve against the cached ground every frame."""
    def __init__(self):
        self.next_check = 0.0
        self.owner = None
        self.hit = None
        self.clearance = None

    def place(self, desired, up, owner, dt, cut, now, probe):
        changed_owner = owner != self.owner
        if cut or changed_owner or now >= self.next_check:
            hit = probe()
            self.hit = hit
            self.owner = owner
            self.next_check = now + 0.1
        # Cache a ground position, not a lift: a descending camera must stay
        # above that ground even on frames where we skip the native check.
        resolved, self.clearance = camera_clearance(
            desired, up, self.hit, None if cut or changed_owner else self.clearance, dt)
        return resolved


class CameraCollision:
    # cTkRaycastJob ABI used by these native raycast functions.
    # Use the terrain height-test group and synchronous runner, never job waits.
    construct = native(NATIVE["ray_construct"], C.c_void_p, C.c_void_p)
    destroy = native(NATIVE["ray_destroy"], None, C.c_void_p)
    initialise = native(NATIVE["ray_initialise"], None, C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_void_p)
    set_ray = native(NATIVE["ray_set"], None, C.c_void_p, C.c_int, C.c_void_p, C.c_void_p)
    run = native(NATIVE["ray_run"], None, C.c_void_p)
    get_point = native(NATIVE["ray_point"], C.c_void_p, C.c_void_p, C.c_void_p, C.c_int, C.c_int)
    get_world = native(NATIVE["physics_world"], C.c_void_p, C.c_void_p)

    def __init__(self):
        self.job = C.create_string_buffer(0x80)
        self.construct(self.job)
        self.terrain_cache = TerrainProbeCache()

    def close(self):
        self.destroy(self.job)

    def place(self, desired, up, origin, owner, dt, cut):
        return self.terrain_cache.place(
            desired, up, owner, dt, cut, time.perf_counter(),
            lambda: self._probe(desired, up, origin, owner))

    def _probe(self, desired, up, origin, owner):
        world = self.get_world(owner + 0x72D088)
        if not world:
            raise ValueError("Camera collision world unavailable")
        self.initialise(self.job, 1, b"cinematic_camera.py", 0, b"CameraCollision.place", world)
        a, b, hit = (cTkPhysRelVec3.from_buffer_copy(bytes(origin)) for _ in range(3))
        # Check the ground at the camera, not visibility back to the player.
        set_vector(a.local, sub(add(desired, mul(up, 256)), xyz(a.offset)))
        set_vector(b.local, sub(sub(desired, mul(up, 0.25)), xyz(b.offset)))
        self.set_ray(self.job, 0, C.byref(a), C.byref(b))
        ray = C.c_void_p.from_buffer(self.job, 0x70).value
        write(ray + 8, struct.pack("<I", 0x36))  # Raycast_PlanetHeightTest (excludes props/structures)
        self.run(self.job)
        blocked = struct.unpack("<H", read(ray + 0x70, 2))[0] != 0
        if blocked:
            self.get_point(self.job, C.byref(hit), 0, 0)
        return add(xyz(hit.local), xyz(hit.offset)) if blocked else None


class CinematicCamera(Mod):
    __author__ = "Tyler Kershner"
    __version__ = "0.4.5"
    __description__ = "Standalone on-foot cinematic camera: Insert toggle, Home next shot, End stop."

    def __init__(self):
        super().__init__()
        if C.sizeof(cTkPhysRelMat34) != 0x50 or C.sizeof(CameraTransforms) != 0xF0:
            raise RuntimeError("Unsupported NMSpy camera structure layout")
        LOG.warning("Cinematic camera %s resolved native addresses: %s", self.__version__,
                    {name: hex(address) for name, address in NATIVE.items()})
        self.settings = self._load_settings()
        self.director = Director(settings=self.settings)
        self.actions = queue.SimpleQueue()
        self.overlay = Overlay()
        self._movement_camera = None
        self.enabled = False
        self.last_status = 0.0
        self.last_camera = 0.0
        self.last_pose = None
        self.collision = None
        # This verified engine build uses -Z for camera forward. Inferring it
        # from a look-at-player ray fails when first person sits at the head.
        self.forward_sign = -1
        self.reason = "Loaded; Insert toggles cinematic camera on/off"
        self.last_error = 0.0
        self.last_status_error = 0.0
        self.settings_notice = "Each shot randomly samples distance from 3m and height from 1.5m up to your maximums. Zoom multiplies distance."
        self._last_request = None
        self._io_stop = threading.Event()
        previous_worker = getattr(_internal, "_cinematic_io_stop", None)
        if previous_worker is not None:
            previous_worker.set()
        _internal._cinematic_io_stop = self._io_stop
        self._io_thread = threading.Thread(target=self._service_files, name="cinematic-files", daemon=True)
        self._io_thread.start()

    def _service_files(self):
        """Keep filesystem stalls out of all game-thread detours."""
        while not self._io_stop.is_set():
            try:
                request = json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
                request_id = request["id"]
                if request_id != self._last_request and 0 <= time.time() - float(request["timestamp"]) <= 10:
                    self._last_request = request_id
                    if request.get("action") in ("on", "off"):
                        self.actions.put(request["action"])
            except (OSError, ValueError, TypeError, KeyError):
                pass
            self._publish_status()
            self._io_stop.wait(0.05)

    def _load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                return normalize_settings(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning("Could not load camera settings; using factory defaults: %s", exc)
        return dict(DEFAULT_SETTINGS)

    def _apply_settings(self, settings):
        settings = normalize_settings(settings)
        self.director.configure(settings)
        self.settings = dict(self.director.settings)
        self.settings_notice = "Maximums apply on the next shot. Press Home to draw new values now."

    with gui_group("Playback"):
        @property
        @STRING("Status")
        def camera_status(self):
            state = "ON" if self.enabled else "OFF"
            timer = "held" if self.settings["hold_shot"] else f"{max(0, self.director.remaining):.1f}s"
            return f"{state} | {self.director.shot or 'waiting for player'} | {timer}"

        @gui_button("Toggle cinematic camera (Insert)")
        def toggle_button(self):
            self.actions.put("toggle")

        @gui_button("Next shot (Home)")
        def next_button(self):
            self.actions.put("next")

        hold_shot = setting_control("hold_shot", "Hold current shot (Home still advances)")

    with gui_group("Camera"):
        distance_max = setting_control("distance_max", "Distance max (m)")
        height_max = setting_control("height_max", "Height max (m)")
        speed_max = setting_control("speed_max", "Speed max (orbit degrees/second)")
        zoom_max = setting_control("zoom_max", "Zoom max (distance multiplier)")

        @property
        @STRING("Current shot's sampled placement")
        def current_placement(self):
            values = self.director.shot_values
            if not values:
                return "No shot chosen yet."
            prefix = "orbit" if self.director.shot == "high_orbit" else self.director.shot
            distance = values[prefix + "_distance"] * values["distance_scale"]
            height = values[prefix + "_height"] * values["height_scale"]
            result = f"{self.director.shot}: distance {distance:.2f}m | height {height:.2f}m"
            if prefix == "side":
                result += f" | behind {values['side_behind'] * values['distance_scale']:.2f}m"
            elif prefix == "orbit":
                result += f" | speed {values['orbit_speed_degrees']:.2f} deg/s | start {values['orbit_start_degrees']:.1f} deg"
            return result

    with gui_group("Timing and transitions"):
        maximum_shot_seconds = setting_control("maximum_shot_seconds", "Shot duration max (seconds; min 20)")
        smooth_transitions = setting_control("smooth_transitions", "Smooth transitions (off = hard cuts)")
        transition_seconds = setting_control("transition_seconds", "Transition time (seconds)")

    with gui_group("Jetpack smoothing"):
        vertical_response = setting_control("vertical_response", "Jetpack response (lower = smoother)")

    with gui_group("Saved settings"):
        @property
        @STRING("Settings")
        def settings_status(self):
            return self.settings_notice

        @gui_button("Save settings for next launch")
        def save_settings(self):
            self.actions.put(("save_settings",))

        @gui_button("Reload saved settings")
        def reload_settings(self):
            self.actions.put(("reload_settings",))

        @gui_button("Reset to factory settings")
        def reset_settings(self):
            self.actions.put(("factory_settings",))

    @on_key_pressed("insert")
    def toggle_key(self):
        self.actions.put("toggle")

    @on_key_pressed("home")
    def next_key(self):
        self.actions.put("next")

    @on_key_pressed("end")
    def stop_key(self):
        self.actions.put("stop")


    def _stop(self, reason):
        self.enabled = False
        self.director.reset()
        self.last_pose = None
        if self.collision is not None:
            self.collision.close()
            self.collision = None
        self.forward_sign = -1
        self.reason = reason

    def _restore(self):
        if self.overlay.pending:
            self.overlay.restore(data_address(), read, write)

    def _report_error(self, error):
        # A missing player/camera during a load skips that frame, never the toggle.
        if time.monotonic() - self.last_error > 30:
            LOG.warning("Camera frame skipped; toggle remains %s: %s", self.enabled, error)
            self.last_error = time.monotonic()
        self.reason = f"Waiting for camera: {error}"

    def _settings_action(self, action):
        try:
            if action[0] == "setting":
                _, key, value = action
                self._apply_settings(dict(self.settings, **{key: value}))
            elif action[0] == "save_settings":
                temporary = SETTINGS_FILE.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(self.settings, indent=2) + "\n", encoding="utf-8")
                temporary.replace(SETTINGS_FILE)
                self.settings_notice = f"Saved to {SETTINGS_FILE.name}"
            elif action[0] == "reload_settings":
                self._apply_settings(self._load_settings())
                self.settings_notice = "Loaded saved settings." if SETTINGS_FILE.exists() else "No saved settings; loaded factory settings."
            elif action[0] == "factory_settings":
                self._apply_settings(DEFAULT_SETTINGS)
                self.settings_notice = "Factory settings restored. Saved settings unchanged until Save is pressed."
        except (OSError, ValueError, TypeError) as exc:
            self.settings_notice = f"Settings not applied: {exc}"
            LOG.warning(self.settings_notice)

    def _publish_status(self):
        now = time.monotonic()
        if now - self.last_status < 0.5:
            return
        self.last_status = now
        try:
            temporary = STATUS_FILE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({
                "enabled": self.enabled,
                "timestamp": time.time(),
                "control_protocol": 1,
                "mod_version": self.__version__,
                "reason": self.reason,
            }), encoding="utf-8")
            temporary.replace(STATUS_FILE)
        except OSError as exc:
            if now - self.last_status_error > 30:
                LOG.warning("Could not publish cinematic camera status: %s", exc)
                self.last_status_error = now

    @nms.cGcPlayer.Update.before
    def before_movement(self, this, timestep):
        if not self.enabled:
            return
        try:
            pending = self.overlay.pending
            if not self.enabled or pending is None or self._movement_camera is not None:
                return
            address, original, cinematic, owner = pending
            if get_addressof(this) != owner + PLAYER_OFFSET or data_address() != owner:
                return
            if read(address, len(cinematic)) != cinematic:
                return
            # The player calculates camera-relative movement here. Give it the
            # native view, without touching player facing, input, or shot state.
            self._movement_camera = pending
            self.overlay.pending = None
            write(address, original)
        except Exception as exc:
            self._report_error(exc)

    @nms.cGcPlayer.Update.after
    def after_movement(self, this, timestep):
        pending, self._movement_camera = self._movement_camera, None
        if pending is None:
            return
        try:
            address, original, cinematic, owner = pending
            # Put the same cinematic pose back before rendering. If native code
            # changed the camera during movement, let the final manager hook win.
            if (self.enabled and self.overlay.pending is None and data_address() == owner
                    and read(address, len(original)) == original):
                self.overlay.apply(address, original, cinematic, owner, write)
        except Exception as exc:
            self._report_error(exc)

    @nms.cGcApplication.Update.before
    def before_frame(self, this):
        try:
            while not self.actions.empty():
                action = self.actions.get_nowait()
                if isinstance(action, tuple):
                    self._settings_action(action)
                    continue
                if action in ("stop", "off") or (action == "toggle" and self.enabled):
                    self._stop("Stopped by command or hotkey")
                elif action == "next" and self.enabled:
                    self.director.next()
                elif action == "toggle" or (action == "on" and not self.enabled):
                    self.enabled = True
                    self.last_camera = 0.0
                    self.reason = "Cinematic camera enabled; waiting for player"
            if not self.enabled:
                self._restore()
        except Exception as exc:
            self._report_error(exc)

    @manual_hook("Cinematic.FinalCameraManagerUpdate", offset=CAMERA_MANAGER_UPDATE,
                 func_def=FUNCDEF(None, [C.c_void_p, C.c_float]), detour_time="before")
    def before_camera(self, this, timestep):
        if self.overlay.pending is None:
            return
        try:
            # Restore only at the start of the next camera-manager update. Restoring
            # in Application.Update can erase our pose before the renderer uses it.
            self._restore()
        except Exception as exc:
            self._report_error(exc)

    @manual_hook("Cinematic.FinalCameraManagerUpdate", offset=CAMERA_MANAGER_UPDATE,
                 func_def=FUNCDEF(None, [C.c_void_p, C.c_float]), detour_time="after")
    def after_camera(self, this, timestep):
        try:
            if not self.enabled:
                return
            owner = data_address()
            if not owner:
                raise ValueError("No live game session")
            vtable = struct.unpack("<Q", read(this, 8))[0]
            mode = struct.unpack("<I", read(owner + PLAYER_MODE, 4))[0]
            spawned = read(owner + PLAYER_OFFSET + PLAYER_SPAWNED, 1) == b"\x01"
            # Same mode test as native third-person Update; 8 is excluded for v0.1.
            if vtable != BASE + CAMERA_MANAGER_VTABLE or mode != 0 or not spawned:
                return
            # GetMatrix/GetCommittedMatrix select these same two camera objects
            # using the manager's +0x330 alternate-camera flag.
            alternate = read(this + 0x330, 1) != b"\x00"
            camera = this + (0x230 if alternate else 0x130)
            original = read(camera, C.sizeof(CameraTransforms))
            transforms = CameraTransforms.from_buffer_copy(original)
            player = cTkPhysRelMat34.from_buffer_copy(read(owner + PLAYER_OFFSET + PLAYER_GRAPHICS, 0x50))
            validate_basis(*(xyz(getattr(player, name)) for name in ("right", "up", "at")))
            validate_basis(*(xyz(getattr(transforms.committed, name)) for name in ("right", "up", "at")))
            player_pos = position(player)
            distance = length(sub(position(transforms.committed), player_pos))
            if not 0 <= distance < 100:
                raise ValueError(f"Native camera/player coordinate mismatch ({distance:.2f} m)")
            # Python 3.10 on Windows uses a ~15.6ms monotonic tick. Use QPC
            # for animation deltas to avoid uneven motion.
            now = time.perf_counter()
            elapsed = now - self.last_camera if self.last_camera else 1 / 60
            # Snap framing after a teleport without changing the user's toggle.
            if (self.director.last_player is not None and
                    length(sub(player_pos, self.director.last_player)) > 40):
                self.director.reset()
                self.last_pose = None
            self.last_camera = now
            # Shot timing uses real elapsed time, including unfocused/paused updates.
            dt = max(0.000001, min(elapsed, 0.25))
            self.director.remaining -= max(0.0, elapsed - dt)
            pose = self.director.update(dt, player_pos, xyz(player.up), xyz(player.at), self.forward_sign)
            if length(sub(pose.position, player_pos)) > 600:
                raise ValueError("Cinematic camera exceeded its tracking radius")
            if self.collision is None:
                self.collision = CameraCollision()
            up = self.director.stable_up
            safe = self.collision.place(pose.position, up, player.pos, owner, dt, pose.cut)
            target = add(self.director.anchor, mul(up, self.director.aim_height))
            pose = Pose(safe, *look_at(safe, target, up, self.forward_sign), pose.cut, pose.shot)
            for name in ("current", "committed", "previous"):
                matrix = getattr(transforms, name)
                blending = self.settings["smooth_transitions"] and self.settings["transition_seconds"] > 0
                use_previous = name == "previous" and self.last_pose and (not pose.cut or blending)
                sample = self.last_pose if use_previous else pose
                set_vector(matrix.right, sample.right)
                set_vector(matrix.up, sample.up)
                set_vector(matrix.at, sample.at)
                set_vector(matrix.pos.local, sub(sample.position, xyz(matrix.pos.offset)))
            self.overlay.apply(camera, original, bytes(transforms), owner, write)
            self.last_pose = pose
            if pose.cut:
                self.reason = "Tracking player"
        except Exception as exc:
            # Memory is touched only by game-thread detours, never key/listener threads.
            try:
                self._restore()
            except Exception as restore_error:
                self._report_error(f"Native camera restoration failed: {restore_error}")
            self._report_error(exc)
