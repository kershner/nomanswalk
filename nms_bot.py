from utils import focus_nms, send_key, log
from dataclasses import dataclass
from typing import Callable
import threading
import win32api
import win32con
import ctypes
import json
import time
import math
import os

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nms_state.json")
STATE_POLL_INTERVAL = 1  # seconds
SECONDS_PER_STEP = 1.0   # how long forward/back holds per unit

STUCK_EPS = 10.0        # movement threshold
STUCK_SECONDS = 10      # time without movement

_last_walk_t = 0.0
_last_stop_t = 0.0
_last_xy = None
_last_move_t = 0.0
_stuck = False
_stuck_last_cmd = None 


# ---------------------------------------------------------------------------
# State tracker
# ---------------------------------------------------------------------------
class NMSState:
    _lock = threading.Lock()
    _current: str = "UNKNOWN"
    _timestamp: float = 0.0

    @classmethod
    def update(cls, state: str, timestamp: float):
        with cls._lock:
            if state != cls._current:
                log(f"State changed: {cls._current} -> {state}")
                cls._current = state
            cls._timestamp = timestamp

    @classmethod
    def get(cls) -> str:
        with cls._lock:
            return cls._current


def poll_state():
    while True:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            ts = float(data.get("timestamp", 0.0))
            state = data.get("state", "UNKNOWN")
            NMSState.update(state, ts)

            check_if_stuck(state, data, ts)

        except FileNotFoundError:
            log(f"State file not found: {STATE_FILE}")
        except Exception as e:
            log(f"State poll error: {e}")

        time.sleep(STATE_POLL_INTERVAL)


def check_if_stuck(state, data, timestamp):
    global _last_xy, _last_move_t, _stuck, _stuck_last_cmd

    if not is_walking():
        _stuck = False
        _last_xy = None
        _stuck_last_cmd = None
        return

    pos = (data.get("environment") or {}).get("player_position") or {}
    x, y = pos.get("x"), pos.get("y")
    if state == "ON_FOOT" and isinstance(x, (int, float)) and isinstance(y, (int, float)):
        xy = (float(x), float(y))
        if _last_xy is None:
            _last_xy, _last_move_t = xy, timestamp
        elif math.hypot(xy[0] - _last_xy[0], xy[1] - _last_xy[1]) >= STUCK_EPS:
            # Moving again — reset everything
            _last_xy, _last_move_t, _stuck, _stuck_last_cmd = xy, timestamp, False, None
        elif not _stuck and (timestamp - _last_move_t) >= STUCK_SECONDS:
            _stuck = True
            _do_unstuck(timestamp)
        elif _stuck and (timestamp - _last_move_t) >= STUCK_SECONDS:
            # Still stuck after last attempt — try next action
            _do_unstuck(timestamp)


def _do_unstuck(timestamp):
    global _stuck_last_cmd, _last_move_t

    _last_move_t = timestamp

    if _stuck_last_cmd == "jet":
        log(f"STUCK: still stuck after jet, trying right 30")
        COMMANDS["right"].func(["30"])
        _stuck_last_cmd = "right"
    elif _stuck_last_cmd == "right":
        log(f"STUCK: still stuck after right, trying tap_e")
        COMMANDS["tap_e"].func()
        _stuck_last_cmd = "tap_e"
    else:  # None or "tap_e"
        log(f"STUCK: trying jet()")
        COMMANDS["jet"].func()
        _stuck_last_cmd = "jet"


def is_walking() -> bool:
    return _last_walk_t > _last_stop_t


def start_state_poller():
    t = threading.Thread(target=poll_state, daemon=True)
    t.start()
    log("State poller started")


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------
def move_mouse(dx: int, dy: int):
    ctypes.windll.user32.mouse_event(0x0001, dx, dy, 0, 0)


def left_click(hold_seconds: float = 0.0):
    focus_nms()
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    if hold_seconds > 0:
        time.sleep(float(hold_seconds))
    else:
        time.sleep(0.02)  # normal click tap

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _clamp(val, lo=1, hi=50) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return lo


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def jet(args=None):
    """Tap spacebar (jetpack burst)"""
    send_key("space", 8)

def dig(args=None):
    """Hold left click for 3 seconds"""
    left_click(3.0)


def walk(args=None):
    global _last_walk_t
    """Toggle autowalk (backslash)"""
    send_key("\\", 0.1)
    _last_walk_t = time.time()


def stop(args=None):
    global _last_stop_t
    """Send "w" key to end autowalk"""
    send_key("w", 0.1)
    _last_stop_t = time.time()
    

def forward(args=None):
    """Hold W for ARG * SECONDS_PER_STEP seconds"""
    n = _clamp(args[0] if args else 1)
    send_key("w", n * SECONDS_PER_STEP)


def back(args=None):
    """Hold S for ARG * SECONDS_PER_STEP seconds"""
    n = _clamp(args[0] if args else 1)
    send_key("s", n * SECONDS_PER_STEP)


def up(args=None):
    """Move mouse up ARG steps"""
    n = _clamp(args[0] if args else 1)
    focus_nms()
    for _ in range(n):
        move_mouse(0, -10)
        time.sleep(0.05)


def down(args=None):
    """Move mouse down ARG steps"""
    n = _clamp(args[0] if args else 1)
    focus_nms()
    for _ in range(n):
        move_mouse(0, 10)
        time.sleep(0.05)


def left(args=None):
    """Move mouse left ARG steps"""
    n = _clamp(args[0] if args else 1)
    focus_nms()
    for _ in range(n):
        move_mouse(-10, 0)
        time.sleep(0.05)


def right(args=None):
    """Move mouse right ARG steps"""
    n = _clamp(args[0] if args else 1)
    focus_nms()
    for _ in range(n):
        move_mouse(10, 0)
        time.sleep(0.05)


def camera(args=None):
    send_key("0", 0.1, ["ctrl"])


def tap_e(args=None):
    """Rapidly tap E (QTEs)"""
    focus_nms()
    for _ in range(15):
        send_key("e", 0.1)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
@dataclass
class Command:
    func: Callable
    help: str = ""


COMMANDS: dict[str, Command] = {
    "jet":     Command(jet,     "Jetpack burst."),
    "dig":     Command(dig,     "Hold left-click for 3s to dig terrain."),
    "walk":    Command(walk,    "Toggle autowalk on/off."),
    "stop":    Command(stop,    "Stop autowalking."),
    "forward": Command(forward, "Walk forward N steps. e.g. !forward 3"),
    "back":    Command(back,    "Walk backward N steps. e.g. !back 3"),
    "up":      Command(up,      "Look up N steps. e.g. !up 5"),
    "down":    Command(down,    "Look down N steps. e.g. !down 5"),
    "left":    Command(left,    "Turn left N steps. e.g. !left 5"),
    "right":   Command(right,   "Turn right N steps. e.g. !right 5"),
    "camera":  Command(camera,  "Toggle third person camera."),
    "tap_e":   Command(tap_e,   "Rapidly tap E. Useful for QTEs."),
}


def main():
    log("NMS bot started")
    start_state_poller()


if __name__ == "__main__":
    main()