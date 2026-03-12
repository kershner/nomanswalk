import json
import logging
import os
import time
from dataclasses import dataclass

from pymhf import ModState

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


@dataclass
class NMSModState(ModState):
    # state logger
    current: str = ""
    last_location_stable: int = -1
    in_galaxy_map: bool = False
    galaxy_map_entered_at: float = 0.0
    # teleporter
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
# Low-level read helpers
# ===========================================================================

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
    with open(os.path.join(_base_dir, "nms_state.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
