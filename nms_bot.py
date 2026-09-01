from utils import focus_nms, send_key, log
from command_block_lists import blocked_commands_for_state, resolve_command_state
from dataclasses import dataclass
from datetime import datetime
from PIL import ImageGrab
from typing import Callable
import threading
import keyboard
import pytz
import win32api
import win32con
import win32gui
import ctypes
import json
import time
import math
import os
import uuid

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "nmspy_mods", "nms_state.json")
TELEPORT_REQUEST_FILE = os.path.join(BASE_DIR, "nmspy_mods", "teleport_request.json")
SELFIE_CAMERA_REQUEST_FILE = os.path.join(BASE_DIR, "nmspy_mods", "selfie_camera_request.json")
SELFIE_CAMERA_STATUS_FILE = os.path.join(BASE_DIR, "nmspy_mods", "selfie_camera_status.json")
STATE_POLL_INTERVAL = 1  # seconds
SECONDS_PER_STEP = 1.0   # how long forward/back holds per unit

MOUSE_STEP = 10
MOUSE_DELAY = 0.05


class SelfieConfig:
    """Editable timing values for the selfie sequence."""

    TURN_RIGHT_SECONDS = 1.0           # Turn the camera to the right
    WALK_FORWARD_SECONDS = 1.0         # Walk forward after turning
    GESTURE_HOLD_SECONDS = 4           # Let the gesture reach its pose
    GESTURE_HOTKEY = "7"               # Gesture quickslot
    PHOTO_MODE_SETTLE_SECONDS = 1      # Wait for photo mode
    AUTOWALK_RESUME_DELAY_SECONDS = 1  # Let photo-mode cleanup finish before K

    CONFIRM_SECONDS = 60              # Confirmation window
    DAILY_UPLOAD_LIMIT = 3            # Successful posts per day
    LIMIT_POSE_HOLD_SECONDS = 10      # Pose time after limit
    MOD_CAMERA_TIMEOUT_SECONDS = 5    # Wait for camera mod

STUCK_USE_Z = True
STUCK_EPS = 10.0         # movement threshold
STUCK_SECONDS = 10       # time without movement
STUCK_COOLDOWN = 15      # min seconds between unstuck attempts

PLANET_LOAD_SECONDS = 50 # how long to wait for a new planet to load after teleport
RUNTIME_STATE_FILE = os.path.join(BASE_DIR, "runtime_state.json")
MAX_WALK_SAMPLE_DISTANCE = 500.0


_autowalk_enabled = False
_last_xy = None
_last_move_t = 0.0
_stuck = False
_stuck_last_cmd = None
_last_unstuck_t = 0.0
_cruise_enabled = False
_boost_enabled = False
_autowalk_command_lock = threading.Lock()

MOVEMENT_COMMANDS = {
    "jet", "walk", "cruise", "boost", "forward", "back",
    "up", "down", "left", "right", "launch",
}
_movement_generation = 0
_movement_generation_lock = threading.Lock()

_daily_stats_lock = threading.Lock()
_runtime_state_lock = threading.Lock()
_daily_stats = {}
_daily_last_position = None
_daily_last_planet = None
_daily_last_state = None


def _today():
    return datetime.now(pytz.timezone("US/Eastern")).date().isoformat()


def _new_daily_stats():
    return {
        "date": _today(),
        "distance_walked": 0.0,
        "planets": [],
        "walkers": [],
        "commands": 0,
        "selfie_uploads": 0,
        "selfie_viewers": [],
        "selfie_planets": [],
    }


def _read_runtime_state():
    try:
        with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_runtime_state(state):
    try:
        tmp = RUNTIME_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, RUNTIME_STATE_FILE)
    except Exception as e:
        log(f"Runtime state save failed: {e}")


def _update_runtime_section(section, value):
    with _runtime_state_lock:
        state = _read_runtime_state()
        state[section] = value
        _write_runtime_state(state)


def _save_daily_stats():
    _update_runtime_section("daily", _daily_stats)


def _ensure_daily_stats():
    global _daily_stats, _daily_last_position, _daily_last_planet, _daily_last_state
    if _daily_stats.get("date") != _today():
        _daily_stats = _new_daily_stats()
        _daily_last_position = None
        _daily_last_planet = None
        _daily_last_state = None
        _save_daily_stats()


def _load_daily_stats():
    global _daily_stats
    daily = _read_runtime_state().get("daily")
    _daily_stats = daily if isinstance(daily, dict) else _new_daily_stats()
    _ensure_daily_stats()

def _planet_key(data):
    ua = data.get("universe_address") or {}
    values = (
        ua.get("reality_index"),
        ua.get("voxel_x"),
        ua.get("voxel_y"),
        ua.get("voxel_z"),
        ua.get("solar_system_index"),
        ua.get("planet_index"),
    )
    if any(v is None for v in values):
        return None
    return ":".join(str(v) for v in values)


def update_daily_movement(state, data):
    global _daily_last_position, _daily_last_planet, _daily_last_state

    with _daily_stats_lock:
        _ensure_daily_stats()
        planet_key = _planet_key(data)
        env = data.get("environment") or {}

        if planet_key and (state == "ON_FOOT" or env.get("inside_atmosphere") is True):
            if planet_key not in _daily_stats["planets"]:
                _daily_stats["planets"].append(planet_key)
                _save_daily_stats()

        pos = env.get("player_position") or {}
        xyz = (pos.get("x"), pos.get("y"), pos.get("z"))
        valid_pos = all(isinstance(v, (int, float)) for v in xyz)
        current = tuple(float(v) for v in xyz) if valid_pos else None

        if (
            current is not None
            and _daily_last_position is not None
            and state == "ON_FOOT"
            and _daily_last_state == "ON_FOOT"
            and planet_key
            and planet_key == _daily_last_planet
            and not is_planet_loading()
        ):
            distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(current, _daily_last_position)))
            if 0.0 < distance <= MAX_WALK_SAMPLE_DISTANCE:
                _daily_stats["distance_walked"] += distance
                _save_daily_stats()

        _daily_last_position = current
        _daily_last_planet = planet_key
        _daily_last_state = state


def record_daily_command(username):
    username = (username or "").strip().lower()
    if not username:
        return
    with _daily_stats_lock:
        _ensure_daily_stats()
        _daily_stats["commands"] += 1
        if username not in _daily_stats["walkers"]:
            _daily_stats["walkers"].append(username)
        _save_daily_stats()


def _nonnegative_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def get_daily_selfie_uploads():
    with _daily_stats_lock:
        _ensure_daily_stats()
        return _nonnegative_int(_daily_stats.get("selfie_uploads", 0))


def has_daily_selfie_upload(username):
    username = (username or "").strip().lower()
    with _daily_stats_lock:
        _ensure_daily_stats()
        viewers = _daily_stats.get("selfie_viewers", [])
        return isinstance(viewers, list) and username in viewers


def has_selfie_planet_upload(planet_key):
    if not planet_key:
        return False
    with _daily_stats_lock:
        _ensure_daily_stats()
        planets = _daily_stats.get("selfie_planets", [])
        return isinstance(planets, list) and planet_key in planets


def record_daily_selfie_upload(username, planet_key=None):
    """Record a successful daily viewer and planet upload."""
    username = (username or "").strip().lower()
    with _daily_stats_lock:
        _ensure_daily_stats()
        viewers = _daily_stats.get("selfie_viewers")
        if not isinstance(viewers, list):
            viewers = _daily_stats["selfie_viewers"] = []
        planets = _daily_stats.get("selfie_planets")
        if not isinstance(planets, list):
            planets = _daily_stats["selfie_planets"] = []
        if not username or username in viewers or (planet_key and planet_key in planets):
            return _nonnegative_int(_daily_stats.get("selfie_uploads", 0))
        viewers.append(username)
        if planet_key:
            planets.append(planet_key)
        _daily_stats["selfie_uploads"] = _nonnegative_int(_daily_stats.get("selfie_uploads", 0)) + 1
        _save_daily_stats()
        return _daily_stats["selfie_uploads"]


def get_daily_stats():
    with _daily_stats_lock:
        _ensure_daily_stats()
        return {
            "distance_walked": float(_daily_stats.get("distance_walked", 0.0)),
            "planets_visited": len(_daily_stats.get("planets", [])),
            "walkers": len(_daily_stats.get("walkers", [])),
            "commands": int(_daily_stats.get("commands", 0)),
        }


_load_daily_stats()

# Shared runtime game state — visible to launcher, dev server, and bot.
def set_runtime_game_state(**values):
    with _runtime_state_lock:
        state = _read_runtime_state()
        game = state.get("game")
        if not isinstance(game, dict):
            game = {}
        game.update(values)
        state["game"] = game
        _write_runtime_state(state)


def get_runtime_game_state():
    state = _read_runtime_state()
    game = state.get("game")
    return game if isinstance(game, dict) else {}


def set_planet_loading(val: bool):
    if val:
        _set_boost(False)
    set_runtime_game_state(planet_loading=bool(val))
    log(f"Planet loading: {val}")


def is_planet_loading() -> bool:
    return bool(get_runtime_game_state().get("planet_loading", False))

def _is_in_cave() -> bool:
    data = NMSState.get_data()
    location = ((data.get("environment") or {}).get("location") or "").strip()
    return location == "Cave"


# ---------------------------------------------------------------------------
# State tracker
# ---------------------------------------------------------------------------
class NMSState:
    _lock = threading.Lock()
    _current: str = "NOT_ON_FOOT"
    _timestamp: float = 0.0
    _data: dict = {}

    @classmethod
    def update(cls, state: str, timestamp: float, data: dict):
        with cls._lock:
            if state != cls._current:
                log(f"State changed: {cls._current} -> {state}")
                cls._current = state
            cls._timestamp = timestamp
            cls._data = data

    @classmethod
    def get(cls) -> str:
        with cls._lock:
            return cls._current

    @classmethod
    def get_data(cls) -> dict:
        with cls._lock:
            return dict(cls._data)


def get_current_planet_key():
    return _planet_key(NMSState.get_data())


def get_command_state(data: dict | None = None, fallback_state: str | None = None) -> str:
    """Return the granular state used by the command block lists."""
    snapshot = NMSState.get_data() if data is None else data
    return resolve_command_state(snapshot, fallback_state or NMSState.get())


def poll_state():
    global _autowalk_enabled

    while True:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            ts = float(data.get("timestamp", 0.0))
            raw_state = data.get("state", "UNKNOWN")
            state = "NOT_ON_FOOT" if raw_state in {"IN_COCKPIT", "GALAXY_MAP", "UNKNOWN"} else "ON_FOOT"
            NMSState.update(state, ts, data)
            update_daily_movement(state, data)

            if state != "ON_FOOT":
                _autowalk_enabled = False
            else:
                if _cruise_enabled:
                    _set_cruise(False)
                if _boost_enabled:
                    _set_boost(False)

            check_if_stuck(state, data)

        except FileNotFoundError:
            log(f"State file not found: {STATE_FILE}")
        except Exception as e:
            log(f"State poll error: {e}")

        time.sleep(STATE_POLL_INTERVAL)


def _reset_stuck():
    global _last_xy, _last_move_t, _stuck, _stuck_last_cmd, _last_unstuck_t
    _last_xy = None
    _last_move_t = 0.0
    _stuck = False
    _stuck_last_cmd = None
    _last_unstuck_t = 0.0


def check_if_stuck(state, data):
    global _last_xy, _last_move_t, _stuck, _stuck_last_cmd

    if state != "ON_FOOT" or not is_walking():
        _reset_stuck()
        return

    if is_planet_loading():
        return  # ignore movement data while a new planet is loading

    pos = (data.get("environment") or {}).get("player_position") or {}
    x, y, z = pos.get("x"), pos.get("y"), pos.get("z")

    has_xy = isinstance(x, (int, float)) and isinstance(y, (int, float))
    has_z = isinstance(z, (int, float))

    if not has_xy:
        return

    use_z = STUCK_USE_Z and has_z
    cur = (float(x), float(y), float(z)) if use_z else (float(x), float(y))

    def dist(a, b) -> float:
        if len(a) == 3 and len(b) == 3:
            return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)
        return math.hypot(a[0]-b[0], a[1]-b[1])

    now = time.time()

    if _last_xy is None:
        _last_xy = cur
        _last_move_t = now
        return

    d = dist(cur, _last_xy)
    elapsed = now - _last_move_t

    if d >= STUCK_EPS:
        _last_xy, _last_move_t, _stuck, _stuck_last_cmd = cur, now, False, None
        return

    if (not _stuck) and elapsed >= STUCK_SECONDS:
        _stuck = True
        _do_unstuck()
        return

    if _stuck and elapsed >= STUCK_SECONDS:
        _do_unstuck()
        return


def _do_unstuck():
    global _stuck_last_cmd, _last_move_t, _last_unstuck_t

    movement_generation = get_movement_generation()
    if not is_walking() or NMSState.get() != "ON_FOOT":
        return

    now = time.time()
    if now - _last_unstuck_t < STUCK_COOLDOWN:
        return  # too soon after last attempt, wait it out

    _last_unstuck_t = now
    _last_move_t = now

    if _is_in_cave():
        log(f"STUCK: in cave, trying sky()")
        COMMANDS["sky"].func()
        _stuck_last_cmd = "sky"
        return

    if _stuck_last_cmd == "jet":
        log(f"STUCK: still stuck after jet, trying right 100")
        COMMANDS["right"].func(["100"], movement_generation)
        _stuck_last_cmd = "right"
    elif _stuck_last_cmd == "right":
        log(f"STUCK: still stuck after right, trying spam_e")
        COMMANDS["spam_e"].func()
        _stuck_last_cmd = "spam_e"
    else:
        log(f"STUCK: trying jet()")
        COMMANDS["jet"].func(None, movement_generation)
        _stuck_last_cmd = "jet"


def is_walking() -> bool:
    return _autowalk_enabled


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
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    if hold_seconds > 0:
        time.sleep(float(hold_seconds))
    else:
        time.sleep(0.1)

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _click_hold_seconds(args) -> float:
    if not args:
        return 0.0
    try:
        seconds = float(args[0])
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return max(0.0, min(10.0, seconds))


def _clamp(val, lo=1, hi=100) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return lo


def right_mouse_click(hold_seconds: float = 0.0):
    focus_nms()
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(float(hold_seconds) if hold_seconds > 0 else 0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def get_movement_generation() -> int:
    with _movement_generation_lock:
        return _movement_generation


def cancel_movement() -> int:
    global _movement_generation
    with _movement_generation_lock:
        _movement_generation += 1
        generation = _movement_generation

    for key in ("w", "s", "space", "shift"):
        keyboard.release(key)
    return generation


def _movement_cancelled(generation: int | None) -> bool:
    return generation is not None and generation != get_movement_generation()


def _hold_movement_key(key: str, duration: float, generation: int | None) -> bool:
    if _movement_cancelled(generation):
        return False

    hwnd, _ = focus_nms()
    if not hwnd or _movement_cancelled(generation):
        return False

    keyboard.press(key)
    try:
        end = time.monotonic() + max(0.0, float(duration))
        while time.monotonic() < end:
            if _movement_cancelled(generation):
                return False
            time.sleep(min(0.05, end - time.monotonic()))
    finally:
        keyboard.release(key)
    return True


def jet(args=None, movement_generation=None):
    """Hold the jetpack key for 2.5 seconds"""
    _hold_movement_key("space", 2.5, movement_generation)


def sky(args=None):
    """Press Y to trigger the sky-drop mod"""
    send_key("y", 0.1)
    

def walk(args=None, movement_generation=None):
    global _autowalk_enabled
    """Start autowalk and begin a fresh stuck-check cycle."""
    with _autowalk_command_lock:
        if _movement_cancelled(movement_generation):
            return

        # K is an in-game toggle, so an already-active !walk must be a no-op.
        # Tapping W again would cancel the game's autowalk while leaving this
        # flag true, causing consecutive !walk commands to disagree with NMS.
        if _autowalk_enabled:
            _reset_stuck()
            return

        if _boost_enabled:
            _set_boost(False)

        # Manual movement exits sitting, dancing, and other emotes before K
        # enables autowalk.
        if not _hold_movement_key("w", 0.1, movement_generation):
            return
        if not _hold_movement_key("k", 0.1, movement_generation):
            return

        # Commit the walking/checker state atomically with respect to !stop's
        # generation bump, so a cancelled !walk cannot turn checking back on.
        with _movement_generation_lock:
            if movement_generation is not None and movement_generation != _movement_generation:
                return
            _autowalk_enabled = True
            _reset_stuck()


def stop(args=None):
    global _autowalk_enabled, _cruise_enabled, _boost_enabled
    """Stop all movement and end autowalk/cruise/boost."""
    with _autowalk_command_lock:
        _autowalk_enabled = False
        _cruise_enabled = False
        _boost_enabled = False
        _reset_stuck()
        keyboard.release("shift")
        send_key("w", 0.1)


def _stop_for_selfie():
    """Stop tracked movement without the general stop command's W tap."""
    global _autowalk_enabled, _cruise_enabled, _boost_enabled
    with _autowalk_command_lock:
        was_autowalking = _autowalk_enabled
        _autowalk_enabled = False
        _cruise_enabled = False
        _boost_enabled = False
        _reset_stuck()
        cancel_movement()

        # K is the configured autowalk toggle and stops it without moving the
        # player. Held cruise/boost movement was already released above.
        if was_autowalking:
            send_key("k", 0.1)


def _set_cruise(enabled: bool, movement_generation=None):
    global _autowalk_enabled, _cruise_enabled
    if enabled:
        if _boost_enabled:
            _set_boost(False)
        if _movement_cancelled(movement_generation):
            return
        hwnd, _ = focus_nms()
        if not hwnd or _movement_cancelled(movement_generation):
            return
        _autowalk_enabled = False
        _reset_stuck()
        keyboard.press("w")
    else:
        keyboard.release("w")
    _cruise_enabled = enabled


def cruise(args=None, movement_generation=None):
    """Toggle holding W while not on foot."""
    if _movement_cancelled(movement_generation):
        return
    _set_cruise(not _cruise_enabled, movement_generation)


def _set_boost(enabled: bool, movement_generation=None):
    global _autowalk_enabled, _boost_enabled
    if enabled:
        if _movement_cancelled(movement_generation):
            return
        hwnd, _ = focus_nms()
        if not hwnd or _movement_cancelled(movement_generation):
            return
        if _cruise_enabled:
            _set_cruise(False)
        _autowalk_enabled = False
        _reset_stuck()
        keyboard.press("w")
        keyboard.press("shift")
    else:
        keyboard.release("shift")
        keyboard.release("w")
    _boost_enabled = enabled


def boost(args=None, movement_generation=None):
    """Toggle holding W + Left Shift while in a ship."""
    if _movement_cancelled(movement_generation):
        return
    _set_boost(not _boost_enabled, movement_generation)


def forward(args=None, movement_generation=None):
    """Hold W for ARG * SECONDS_PER_STEP seconds"""
    n = _clamp(args[0] if args else 1)
    _hold_movement_key("w", n * SECONDS_PER_STEP, movement_generation)


def back(args=None, movement_generation=None):
    """Hold S for ARG * SECONDS_PER_STEP seconds"""
    n = _clamp(args[0] if args else 1)
    _hold_movement_key("s", n * SECONDS_PER_STEP, movement_generation)


def _move_mouse_count(dx: int, dy: int, count, cancelled=lambda: False):
    count = max(0, int(count))
    if not count or cancelled():
        return
    focus_nms()
    for _ in range(count):
        if cancelled():
            return
        move_mouse(dx, dy)
        time.sleep(MOUSE_DELAY)


def _move_mouse_steps(dx: int, dy: int, args, movement_generation):
    _move_mouse_count(
        dx,
        dy,
        _clamp(args[0] if args else 1),
        lambda: _movement_cancelled(movement_generation),
    )


def _move_mouse_for_duration(dx: int, dy: int, duration: float) -> bool:
    """Move the mouse repeatedly for an exact duration."""
    duration = max(0.0, float(duration))
    if duration == 0.0:
        return True

    hwnd, _ = focus_nms()
    if not hwnd:
        return False

    end = time.monotonic() + duration
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        move_mouse(dx, dy)
        time.sleep(min(MOUSE_DELAY, remaining))


def up(args=None, movement_generation=None):
    """Move mouse up ARG steps"""
    _move_mouse_steps(0, -MOUSE_STEP, args, movement_generation)


def down(args=None, movement_generation=None):
    """Move mouse down ARG steps"""
    _move_mouse_steps(0, MOUSE_STEP, args, movement_generation)


def left(args=None, movement_generation=None):
    """Move mouse left ARG steps"""
    _move_mouse_steps(-MOUSE_STEP, 0, args, movement_generation)


def right(args=None, movement_generation=None):
    """Move mouse right ARG steps"""
    _move_mouse_steps(MOUSE_STEP, 0, args, movement_generation)


def camera(args=None):
    send_key("0", 0.1)


def spam_e(args=None):
    """Rapidly tap E (QTEs)"""
    focus_nms()
    for _ in range(15):
        send_key("e", 0.1)
        time.sleep(0.05)


def tap_e(args=None):
    """Tap E once"""
    send_key("e", 0.1)


def hold_e(args=None):
    """Hold E for 5 seconds"""
    send_key("e", 5)


def launch(args=None, movement_generation=None):
    """Hold W for 5 seconds"""
    _hold_movement_key("w", 5, movement_generation)


def land(args=None):
    """Tap E once"""
    tap_e()


def left_click_cmd(args=None):
    """Click or hold the left mouse button for up to 10 seconds."""
    left_click(_click_hold_seconds(args))


def right_click_cmd(args=None):
    global _autowalk_enabled
    """Click or hold the right mouse button for up to 10 seconds."""
    _autowalk_enabled = False
    _reset_stuck()
    focus_nms()
    right_mouse_click(_click_hold_seconds(args))


def coords(args=None):
    global _autowalk_enabled
    """Show photo mode for 10 seconds (shows coordinates)"""
    was_walking = is_walking()
    _autowalk_enabled = False
    _reset_stuck()

    focus_nms()
    send_key("2", 0.1)
    time.sleep(10)
    right_mouse_click()

    if was_walking:
        walk()  # right_mouse_click() stops autowalk in-game, so re-engage it


def start_selfie_gesture(args=None):
    """Prepare a predictable background-facing pose and start its gesture."""
    _stop_for_selfie()

    if not _move_mouse_for_duration(
        MOUSE_STEP,
        0,
        SelfieConfig.TURN_RIGHT_SECONDS,
    ):
        raise RuntimeError("could not turn the camera to the right")

    if not _hold_movement_key("w", SelfieConfig.WALK_FORWARD_SECONDS, None):
        raise RuntimeError("could not walk the player forward")
    send_key(SelfieConfig.GESTURE_HOTKEY, 0.1)


def enter_photo_mode(args=None):
    """Enter photo mode through the same quickslot used by !coords."""
    focus_nms()
    send_key("2", 0.1)


def capture_visible_game_frame():
    """Save the visible NMS client area, including its photo-mode glyph overlay."""
    hwnd, _ = focus_nms()
    if not hwnd:
        raise RuntimeError("NMS window was not found for screenshot capture")

    client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
    client_rect = win32gui.GetClientRect(hwnd)
    width = client_rect[2] - client_rect[0]
    height = client_rect[3] - client_rect[1]
    if width <= 0 or height <= 0:
        raise RuntimeError("NMS client area has invalid dimensions")

    capture_dir = os.path.join(BASE_DIR, "selfie_captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    capture_path = os.path.join(capture_dir, f"selfie_{timestamp}.jpg")
    image = ImageGrab.grab(
        bbox=(
            client_left,
            client_top,
            client_left + width,
            client_top + height,
        ),
        all_screens=True,
    )
    image.convert("RGB").save(capture_path, format="JPEG", quality=95)
    log(f"Selfie: captured visible game frame at {capture_path}")
    return capture_path


def exit_photo_mode(args=None):
    """Exit photo mode with the configured in-game right-click binding."""
    right_mouse_click()


def release_selfie_camera():
    try:
        os.remove(SELFIE_CAMERA_REQUEST_FILE)
    except OSError:
        pass


def position_selfie_camera(timeout_seconds, profile="production"):
    """Request the permanent camera pose and wait until the mod applies it."""
    release_selfie_camera()
    profile = str(profile).strip().lower()
    if profile not in {"dev", "production"}:
        raise ValueError(f"unknown selfie camera profile: {profile}")
    request_id = uuid.uuid4().hex
    deadline = time.monotonic() + float(timeout_seconds)
    temp_file = f"{SELFIE_CAMERA_REQUEST_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            {
                "request_id": request_id,
                "expires_at": time.time() + 60,
                "profile": profile,
            },
            file,
        )
    os.replace(temp_file, SELFIE_CAMERA_REQUEST_FILE)

    try:
        while time.monotonic() < deadline:
            try:
                with open(SELFIE_CAMERA_STATUS_FILE, "r", encoding="utf-8") as file:
                    status = json.load(file)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                status = {}
            if status.get("request_id") == request_id:
                if status.get("state") == "ready":
                    return
                if status.get("state") == "error":
                    raise RuntimeError(status.get("message") or "selfie camera mod failed")
            time.sleep(0.05)
        raise TimeoutError("selfie camera mod did not become ready")
    except Exception:
        release_selfie_camera()
        raise


def end_selfie_gesture(args=None):
    """Tap movement after leaving photo mode so the selfie gesture ends."""
    send_key("w", 0.1)


def ship(args=None):
    send_key("1", 0.1)


def anomaly(args=None):
    stop()
    send_key("3", 0.1)


def pet(args=None):
    send_key("4", 0.1)


def dance(args=None):
    stop()
    send_key("5", 0.1)


def sit(args=None):
    stop()
    send_key("6", 0.1)


def inventory(args=None):
    if _boost_enabled:
        _set_boost(False)
    
    stop()
    send_key("tab", 0.1)


def music(args=None):
    """Toggle the music by sending the "m" key.  Handled by the music_toggle mod"""
    send_key("m", 0.1)


def hud(args=None):
    """Toggle the HUD. Handled by the hud_toggle mod."""
    focus_nms()
    time.sleep(0.2)
    send_key("f5", 0.1)


def day(args=None):
    """Set the in-game time to day. Handled by the time_of_day mod."""
    send_key("f6", 0.1)


def night(args=None):
    """Set the in-game time to night. Handled by the time_of_day mod."""
    send_key("f7", 0.1)


def resume_time(args=None):
    """Resume the game's normal planet time. Handled by the time_of_day mod."""
    send_key("f8", 0.1)



def storm(args=None):
    """Toggle forced storm weather. Handled by the storm_toggle mod."""
    send_key("f9", 0.1)


def gravity(args=None):
    """Toggle low gravity. Handled by the gravity_toggle mod."""
    send_key("f10", 0.1)

def _do_teleport(key, label):
    """Shared logic for any teleport-style action — send a key, wait for planet load, reset state."""
    set_planet_loading(True)
    try:
        send_key(key, 0.1)
        log(f"{label}: waiting {PLANET_LOAD_SECONDS}s for planet to load...")
        time.sleep(PLANET_LOAD_SECONDS)
        stop()
        time.sleep(0.1)
        walk()
    finally:
        set_planet_loading(False)
        

    log(f"{label}: planet load wait complete.")
    

def _validate_portal_address(address: str) -> None:
    """Reject portal fields that NMS reserves or cannot target."""
    planet = int(address[0], 16)
    system = int(address[1:4], 16)
    voxel_y = int(address[4:6], 16)
    voxel_z = int(address[6:9], 16)
    voxel_x = int(address[9:12], 16)

    if not 1 <= planet <= 6:
        raise ValueError("Invalid portal address: the planet glyph must be from 1 to 6.")
    if system in (0, 0xFFF):
        raise ValueError("Invalid portal address: the system cannot be 000 or FFF.")
    if voxel_y == 0x80 or voxel_z == 0x800 or voxel_x == 0x800:
        raise ValueError("Invalid portal address: it contains a reserved center coordinate.")


def _normalize_teleport_destination(args) -> tuple[str | None, int | None]:
    planet = None
    galaxy = None

    for arg in args or []:
        value = str(arg).strip()
        if not value or "=" in value:
            raise ValueError("Use !teleport [12-digit planet hex] [galaxy 1-255].")

        # A portal address is always 12 characters. Check its shape before
        # trying a number so an all-numeric address is not read as a galaxy.
        if len(value) == 12:
            key = "planet"
        else:
            try:
                int(value)
            except ValueError as exc:
                raise ValueError(
                    f"Cannot identify teleport parameter '{value}'. Use a 12-digit planet hex or galaxy 1-255."
                ) from exc
            key = "galaxy"

        if key == "planet":
            if planet is not None:
                raise ValueError("Planet can only be supplied once.")
            planet = value.upper()
            if len(planet) != 12 or any(c not in "0123456789ABCDEF" for c in planet):
                raise ValueError("Planet must be exactly 12 hexadecimal characters.")
            _validate_portal_address(planet)
        else:
            if galaxy is not None:
                raise ValueError("Galaxy can only be supplied once.")
            try:
                galaxy = int(value)
            except ValueError as exc:
                raise ValueError("Galaxy must be a number from 1 to 255.") from exc
            if not 1 <= galaxy <= 255:
                raise ValueError("Galaxy must be a number from 1 to 255.")

    return planet, galaxy

def _write_teleport_request(planet: str | None, galaxy: int | None = None) -> None:
    payload = {}
    if planet is not None:
        # The nmspy mod's file contract still calls this field "address".
        payload["address"] = planet
    if galaxy is not None:
        payload["reality"] = galaxy - 1

    temp_file = f"{TELEPORT_REQUEST_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(temp_file, TELEPORT_REQUEST_FILE)


def teleport(args=None):
    """Teleport to a portal address in an optional galaxy, or randomly when omitted."""
    if not is_command_allowed("teleport"):
        log("Teleport ignored: the command is currently unavailable.")
        return

    planet, galaxy = _normalize_teleport_destination(args)

    if planet or galaxy is not None:
        _write_teleport_request(planet, galaxy)
    _do_teleport("o", "Teleport")


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
    "jet":     Command(jet,     "On foot: hold the jetpack key for 2.5 seconds.", aliases=("j",)),
    "sky":     Command(sky,     "Drop the Walker from high above the planet."),
    "walk":    Command(walk,    "Start continuous forward walking.", aliases=("w",)),
    "stop":    Command(stop,    "Stop all active and queued movement immediately.", aliases=("s",)),
    "cruise":  Command(cruise,  "While in a ship, toggle holding the forward key continuously on/off."),
    "boost":   Command(boost,   "While in a ship, toggle holding forward + boost continuously on/off.", aliases=("engage",)),
    "forward": Command(forward, "Hold the forward key for 1-100 seconds. e.g. !forward 3 holds it for 3 seconds.", aliases=("f",)),
    "back":    Command(back,    "Hold the backward key for 1-100 seconds. e.g. !back 3 holds it for 3 seconds.", aliases=("b",)),
    "up":      Command(up,      "Move the mouse up 1-100 steps. e.g. !up 5 moves it up 5 steps.", aliases=("u",)),
    "down":    Command(down,    "Move the mouse down 1-100 steps. e.g. !down 5 moves it down 5 steps.", aliases=("d", "dn")),
    "left":    Command(left,    "Move the mouse left 1-100 steps. e.g. !left 5 turns left 5 steps.", aliases=("l",)),
    "right":   Command(right,   "Move the mouse right 1-100 steps. e.g. !right 5 turns right 5 steps.", aliases=("r",)),
    "camera":  Command(camera,  "Start a vote to switch between first-person and third-person camera views."),
    "spam_e":  Command(spam_e,  "Rapidly tap the interact key 15 times. Useful for QTEs.", hidden=True),
    "tap_e":   Command(tap_e,   "Tap the interact key once."),
    "hold_e":  Command(hold_e,  "Hold the interact key for 5 seconds."),
    "launch":  Command(launch,  "While in a ship, hold the forward key for 5 seconds to launch."),
    "land":    Command(land,    "While in a ship, tap the interact key once to attempt to land."),
    "left_click": Command(left_click_cmd, "Click or hold left mouse for up to 10 seconds. e.g. !lc 5", aliases=("lc",)),
    "right_click": Command(right_click_cmd, "Click or hold right mouse for up to 10 seconds. e.g. !rc 5", aliases=("rc",)),
    "coords":  Command(coords,  "Start a vote to show the Walker's current planetary coordinates for 10 seconds."),
    "selfie":  Command(start_selfie_gesture, "Set up a selfie and wait for the requesting viewer to use !confirm."),
    "teleport": Command(teleport, "Teleport to [12-digit planet hex] [galaxy 1-255]. Values are optional, auto-detected, and can be in either order; no values means random."),
    "ship": Command(ship, "Select the Walker's ship placement quickslot."),
    "anomaly": Command(anomaly, "Select the Space Anomaly placement quickslot."),
    "pet": Command(pet, "Select the Walker's pet placement quickslot."),
    "dance": Command(dance, "Make the Walker dance."),
    "sit": Command(sit, "Make the Walker sit."),
    "inventory": Command(inventory, "Open the inventory."),
    "music": Command(music, "Start a vote to toggle the stream's in-game music on/off."),
    "hud": Command(hud, "Toggle the in-game HUD."),
    "day": Command(day, "Force the current planet to daytime."),
    "night": Command(night, "Force the current planet to nighttime."),
    "resume_time": Command(resume_time, "Return to the planet's normal day/night cycle."),
    "storm": Command(storm, "Toggle a forced storm on/off."),
    "gravity": Command(gravity, "Toggle low gravity on/off."),
}

# Expand aliases into COMMANDS so lookups work transparently.
# Alias entries point to the same Command object as the canonical name.
for _cmd in list(COMMANDS.values()):
    for _alias in _cmd.aliases:
        if _alias not in COMMANDS:
            COMMANDS[_alias] = _cmd


def get_canonical_command_name(name: str) -> str:
    cmd = COMMANDS.get(name)
    if not cmd:
        return name

    for command_name, command in COMMANDS.items():
        if command is cmd and command_name not in cmd.aliases:
            return command_name

    return name


def is_command_allowed(name: str, state: str | None = None, data: dict | None = None) -> bool:
    canonical_name = get_canonical_command_name(name)
    command_state = get_command_state(data, fallback_state=state)
    return canonical_name not in blocked_commands_for_state(command_state)


def main():
    log("NMS bot started")
    start_state_poller()


if __name__ == "__main__":
    main()
