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


def log(msg):
    logging.info(f"{msg}")


def click_at_percent(px, py, delay_after=0.05, move_cursor=True):
    hwnd, _dlg = focus_nms()
    if not hwnd:
        return

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w = right - left
    h = bottom - top

    cx = int(w * px)
    cy = int(h * py)

    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx = ox + cx
    sy = oy + cy

    if move_cursor:
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.05)

    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    if move_cursor:
        log(f"Clicked ({px:.2f}, {py:.2f}) -> screen ({sx}, {sy})")
    time.sleep(delay_after)


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


def get_status_text() -> str:
    try:
        from nms_bot import STATE_FILE
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        
        planet = state.get("planet", {})
        name = planet.get("name", "unknown")
        biome = planet.get("biome", "")
        size = planet.get("planet_size", "")
        rings = "Ringed" if planet.get("has_rings") else ""
        
        weather = planet.get("weather_type", "")
        weather = f"Weather: {weather}" if weather else ""

        flora = planet.get("life", "")
        flora = f"Flora: {flora}" if flora else ""
        
        fauna = planet.get("creature_life", "")
        fauna = f"Fauna: {fauna}" if fauna else ""
        planet_stats = " • ".join(filter(None, [biome, size, rings, weather, flora, fauna]))
        main_status = f"Walking across {name} in the Euclid galaxy."

        return {
            "main": main_status,
            "details": planet_stats
        }

    except Exception as e:
        log(f"get_status_text failed: {e}")
        return "Could not read game state."