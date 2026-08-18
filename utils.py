import win32process
import win32gui
import win32api
import win32con
import pywinauto
import keyboard
import logging
import ctypes
import time
import json
import os

from galaxy_names import get_galaxy_name


log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no_mans_walk.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_path, mode="a", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOW_TITLE = "No Man's Sky"
_last_galaxy_name = None


def _set_dpi_aware():
    """Declare DPI awareness so Win32 coordinate APIs return physical pixels.
    Without this, Windows scales coordinates for 'compatibility' on high-DPI
    displays, causing clicks to land in the wrong spot."""
    try:
        # Windows 10 1703+ — handles per-monitor scaling correctly
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except AttributeError:
        try:
            # Windows 8.1+ fallback
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except AttributeError:
            # Vista fallback
            ctypes.windll.user32.SetProcessDPIAware()

_set_dpi_aware()


def log(msg):
    logging.info(f"{msg}")
    

def send_key(key: str, duration: float = 0.1, modifiers: list[str] | None = None):
    """Focus NMS then send key or key combo."""
    hwnd, dlg = focus_nms()
    if not hwnd:
        return

    modifiers = modifiers or []

    if modifiers:
        log(f"Sending combo: {modifiers}+{key!r} ({duration}s)")
        for m in modifiers:
            keyboard.press(m)

        time.sleep(0.05)
        keyboard.press(key)
        time.sleep(duration)
        keyboard.release(key)

        for m in reversed(modifiers):
            keyboard.release(m)
    else:
        log(f"Holding key: {key!r} for {duration}s")
        keyboard.press(key)
        time.sleep(duration)
        keyboard.release(key)


def focus_nms():
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if not hwnd:
        log("NMS window not found")
        return None, None

    current_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)

    ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, True)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, False)

    for _ in range(20):
        if win32gui.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.05)

    app = pywinauto.Application(backend="win32").connect(handle=hwnd)
    dlg = app.window(handle=hwnd)
    return hwnd, dlg


def _get_status_state():
    from nms_bot import STATE_FILE, get_daily_stats
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f), get_daily_stats()



def _get_galaxy_name(state: dict) -> str | None:
    global _last_galaxy_name

    ua = state.get("universe_address") or {}
    name = ua.get("galaxy_name")

    if not name:
        number = ua.get("galaxy_number")
        if number is None:
            reality_index = ua.get("reality_index")
            if isinstance(reality_index, int):
                number = reality_index + 1
        if isinstance(number, int):
            name = get_galaxy_name(number)

    if name:
        _last_galaxy_name = name
        return name

    return _last_galaxy_name


def get_info_text(countdown: str = "") -> str:
    try:
        state, stats = _get_status_state()
        planet = state.get("planet", {})
        location = state.get("state")

        if location in ("NEXUS", "ANOMALY"):
            activity = "Aboard the Space Anomaly"
        elif location == "FREIGHTER":
            activity = "Aboard a ship"
        elif location == "SPACE_STATION":
            activity = "At a space station"
        elif location == "IN_COCKPIT":
            activity = "In space"
        else:
            name = planet.get("name")
            biome = planet.get("biome")
            activity = f"Walking across {name}" if name else "Walking across a planet"
            if biome:
                activity += f" ({biome})"
        galaxy_name = _get_galaxy_name(state)
        if galaxy_name and location not in ("NEXUS", "ANOMALY", "FREIGHTER", "SPACE_STATION", "IN_COCKPIT"):
            activity += f" • Galaxy: {galaxy_name}"
        parts = [activity]
        if galaxy_name and location in ("NEXUS", "ANOMALY", "FREIGHTER", "SPACE_STATION", "IN_COCKPIT"):
            parts.append(f"Galaxy: {galaxy_name}")
        if countdown:
            parts.append(f"Next planet vote in {countdown}")
        parts.append(
            f"Today: {stats['distance_walked']:,.0f}u walked • "
            f"{stats['planets_visited']} {'planet' if stats['planets_visited'] == 1 else 'planets'} visited • "
            f"{stats['walkers']} {'walker' if stats['walkers'] == 1 else 'walkers'} • "
            f"{stats['commands']} {'command' if stats['commands'] == 1 else 'commands'}"
        )
        return " • ".join(parts)
    except Exception as e:
        log(f"get_info_text failed: {e}")
        return "Could not read game state."


def get_location_text() -> str:
    try:
        state, _ = _get_status_state()
        location = state.get("state")
        planet = state.get("planet", {})
        solar_system = state.get("solar_system", {})
        galaxy_name = _get_galaxy_name(state)

        def with_galaxy(text: str) -> str:
            return f"{text} • Galaxy: {galaxy_name}" if galaxy_name else text

        if location in ("NEXUS", "ANOMALY"):
            return with_galaxy("Aboard the Space Anomaly")
        if location == "FREIGHTER":
            return with_galaxy("Aboard a ship")
        if location == "SPACE_STATION":
            name = solar_system.get("space_station_name")
            return with_galaxy(f"At a space station ({name})" if name else "At a space station")
        if location == "IN_COCKPIT":
            name = solar_system.get("name")
            return with_galaxy(f"In space ({name} system)" if name else "In space")

        mods = state.get("mods", {})
        name = planet.get("name")
        biome = planet.get("biome")
        weather = planet.get("weather_type", "")
        flora = planet.get("life", "")
        fauna = planet.get("creature_life", "")
        activity = name or "A planet"
        if biome:
            activity += f" ({biome})"

        details = " • ".join(filter(None, [
            f"Size: {planet.get('planet_size')}" if planet.get("planet_size") else "",
            "Ringed" if planet.get("has_rings") else "",
            f"Weather: {weather}" if weather else "",
            f"Flora: {flora}" if flora else "",
            f"Fauna: {fauna}" if fauna else "",
            f"Gravity: {mods.get('gravity', 'normal').title()}",
            f"Storming: {'Yes' if mods.get('storm', 'normal') == 'forced' else 'No'}",
            f"Time: {mods.get('time', 'normal').title()}",
        ]))

        text = with_galaxy(activity)
        return f"{text} • {details}" if details else text
    except Exception as e:
        log(f"get_location_text failed: {e}")
        return "Could not read location state."

