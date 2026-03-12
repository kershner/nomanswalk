from utils import focus_nms, send_key, log, click_at_percent
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
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nmspy_mods", "nms_state.json")
STATE_POLL_INTERVAL = 1  # seconds
SECONDS_PER_STEP = 1.0   # how long forward/back holds per unit

STUCK_USE_Z = True
STUCK_EPS = 10.0         # movement threshold
STUCK_SECONDS = 10       # time without movement
STUCK_COOLDOWN = 15      # min seconds between unstuck attempts

PLANET_LOAD_SECONDS = 50 # how long to wait for a new planet to load after teleport

_last_walk_t = 0.0
_last_stop_t = 0.0
_last_xy = None
_last_move_t = 0.0
_stuck = False
_stuck_last_cmd = None
_last_unstuck_t = 0.0

# Planet-load lockout — set during teleport to pause stuck-checking and
# block new commands in the Twitch bot.
_planet_loading = False
_planet_loading_lock = threading.Lock()


def set_planet_loading(val: bool):
    global _planet_loading
    with _planet_loading_lock:
        _planet_loading = bool(val)
    log(f"Planet loading: {val}")


def is_planet_loading() -> bool:
    with _planet_loading_lock:
        return _planet_loading


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

    if is_planet_loading():
        return  # ignore movement data while a new planet is loading

    if not is_walking():
        _stuck = False
        _last_xy = None
        _stuck_last_cmd = None
        return

    if state != "ON_FOOT":
        # Optional: uncomment if you want visibility when stuck-check is skipped.
        # log(f"STUCK-CHECK: skipped (state={state})")
        return

    pos = (data.get("environment") or {}).get("player_position") or {}
    x, y, z = pos.get("x"), pos.get("y"), pos.get("z")

    has_xy = isinstance(x, (int, float)) and isinstance(y, (int, float))
    has_z = isinstance(z, (int, float))

    if not has_xy:
        # Optional: uncomment if you want visibility when stuck-check is skipped.
        # log(f"STUCK-CHECK: skipped (bad coords x={x!r} y={y!r} z={z!r})")
        return

    # Keep _last_xy as a 2-tuple or 3-tuple depending on whether we’re using Z.
    use_z = STUCK_USE_Z and has_z
    cur = (float(x), float(y), float(z)) if use_z else (float(x), float(y))

    def dist(a, b) -> float:
        if len(a) == 3 and len(b) == 3:
            return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)
        return math.hypot(a[0]-b[0], a[1]-b[1])

    now = time.time()

    if _last_xy is None:
        _last_xy = cur
        _last_move_t = now  # wall clock
        return

    d = dist(cur, _last_xy)
    elapsed = now - _last_move_t

    if d >= STUCK_EPS:
        # Moving again — reset everything
        _last_xy, _last_move_t, _stuck, _stuck_last_cmd = cur, now, False, None
        return

    if (not _stuck) and elapsed >= STUCK_SECONDS:
        _stuck = True
        log(
            "STUCK: trigger fired "
            f"(state={state}, use_z={use_z}, d={d:.3f} < eps={STUCK_EPS}, "
            f"elapsed={elapsed:.2f}s >= {STUCK_SECONDS}s, "
            f"last={_last_xy}, cur={cur}, last_cmd={_stuck_last_cmd})"
        )
        _do_unstuck(timestamp)
        return

    if _stuck and elapsed >= STUCK_SECONDS:
        log(
            "STUCK: still stuck "
            f"(state={state}, use_z={use_z}, d={d:.3f} < eps={STUCK_EPS}, "
            f"elapsed={elapsed:.2f}s >= {STUCK_SECONDS}s, "
            f"last={_last_xy}, cur={cur}, last_cmd={_stuck_last_cmd})"
        )
        _do_unstuck(timestamp)
        return


def _do_unstuck(timestamp):
    global _stuck_last_cmd, _last_move_t, _last_unstuck_t

    now = time.time()
    if now - _last_unstuck_t < STUCK_COOLDOWN:
        return  # too soon after last attempt, wait it out

    _last_unstuck_t = now
    _last_move_t = now

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


def right_mouse_click():
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


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
    global _last_walk_t, _last_move_t, _last_xy
    """Toggle autowalk (backslash)"""
    send_key("k", 0.1)
    _last_walk_t = time.time()
    _last_move_t = time.time()   # reset stuck timer so it doesn't fire immediately
    _last_xy = None              # reset position baseline


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


def coords(args=None):
    """CTRL + 2 to show photo mode for 10 seconds (shows coordinates)"""
    was_walking = is_walking()
    global _last_stop_t
    _last_stop_t = time.time()  # pause stuck-checking for the duration

    focus_nms()
    send_key("2", 0.1, ["ctrl"])
    time.sleep(10)
    right_mouse_click()

    if was_walking:
        walk()  # right_mouse_click() stops autowalk in-game, so re-engage it


def music(args=None):
    """Toggle the music by sending the "m" key.  Handled by the music_toggle mod"""
    send_key("m", 0.1)

def _do_teleport(key, label):
    """Shared logic for any teleport-style action — send a key, wait for planet load, reset state."""
    global _last_xy, _last_move_t, _stuck, _stuck_last_cmd
    set_planet_loading(True)
    try:
        send_key(key, 0.1)
        log(f"{label}: waiting {PLANET_LOAD_SECONDS}s for planet to load...")
        time.sleep(PLANET_LOAD_SECONDS)

        # Reset stuck-checker state so stale position data doesn't fire immediately
        _last_xy = None
        _last_move_t = time.time()
        _stuck = False
        _stuck_last_cmd = None
        log(f"{label}: planet load wait complete.")
        walk()
    finally:
        set_planet_loading(False)


def teleport(args=None):
    """Send the 'O' key to trigger a random planet teleport, then wait for the planet to load."""
    _do_teleport("o", "Teleport")


def next_planet(args=None):
    """Send the '[' key to teleport to a nearby planet, then wait for the planet to load."""
    _do_teleport("[", "NextPlanet")


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
@dataclass
class Command:
    func: Callable
    help: str = ""
    aliases: tuple = ()   # e.g. aliases=("f", "fw")
    hidden: bool = False  # if True, omitted from !help listing


COMMANDS: dict[str, Command] = {
    "jet":     Command(jet,     "Jetpack burst.",                        aliases=("j",)),
    "dig":     Command(dig,     "Hold left-click for 3s to dig terrain.",aliases=("d",)),
    "walk":    Command(walk,    "Toggle autowalk on/off.",               aliases=("w",)),
    "stop":    Command(stop,    "Stop autowalking.",                     aliases=("s",)),
    "forward": Command(forward, "Walk forward N steps. e.g. !forward 3", aliases=("f",)),
    "back":    Command(back,    "Walk backward N steps. e.g. !back 3",   aliases=("b",)),
    "up":      Command(up,      "Look up N steps. e.g. !up 5",           aliases=("u",)),
    "down":    Command(down,    "Look down N steps. e.g. !down 5",       aliases=("dn",)),
    "left":    Command(left,    "Turn left N steps. e.g. !left 5",       aliases=("l",)),
    "right":   Command(right,   "Turn right N steps. e.g. !right 5",     aliases=("r",)),
    "camera":  Command(camera,  "Toggle third person camera."),
    "tap_e":   Command(tap_e,   "Rapidly tap E. Useful for QTEs.", hidden=True),
    "coords":  Command(coords,  "Show planet coordinates for 10 seconds."),
    "teleport": Command(teleport,    "Teleport to a random planet.", hidden=True),
    "next_planet": Command(next_planet, "Teleport to a nearby planet.", hidden=True),
    "music": Command(music,   "Toggles music on/off."),
}

# Expand aliases into COMMANDS so lookups work transparently.
# Alias entries point to the same Command object as the canonical name.
for _cmd in list(COMMANDS.values()):
    for _alias in _cmd.aliases:
        if _alias not in COMMANDS:
            COMMANDS[_alias] = _cmd


def main():
    log("NMS bot started")
    start_state_poller()


if __name__ == "__main__":
    main()