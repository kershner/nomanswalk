from utils import BASE_DIR, focus_nms, log, send_key
import subprocess
import argparse
import pyautogui
import win32gui
import win32api
import win32con
import time
import sys
import os


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
WAIT_FOR_MODE_SELECT = 30

MENU_CLICKS = [
    (0.50, 0.50, 2.0),  # "Using mods" confirm screen
    (0.35, 0.45, 2.0),  # "Play Game" button
    (0.50, 0.35, 2.0),  # Save slot 1 select
]

WAIT_FOR_GAME_LOAD = 60

# Disable HUD sequence
DISABLE_HUD_CLICKS = [
    (0.73, 0.05, 1.5),  # OPTIONS tab
    (0.10, 0.80, 1.5),  # General
    (0.60, 0.90, 1.5),  # HUD toggle
    (0.40, 0.60, 1.5),  # Apply
]
DISABLE_HUD_MENU_OPEN_DELAY = 2.0
DISABLE_HUD_ESC_DELAY = 0.8

VENV_PY = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")

DEV_SERVER_CMD = [VENV_PY, "dev_server.py"]
TWITCH_BOT_CMD = [VENV_PY, "nms_twitch_bot.py"]
DEV_SERVER_URL = "http://127.0.0.1:5050"


def click_at_percent(px, py, delay_after=1.0):
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

    win32api.SetCursorPos((sx, sy))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    log(f"Clicked ({px:.2f}, {py:.2f}) -> screen ({sx}, {sy})")
    time.sleep(delay_after)


def disable_hud_clicks():
    hwnd, _dlg = focus_nms()
    if not hwnd:
        return False

    log("Disable HUD: opening menu (ESC)...")
    send_key("esc", 0.1)
    time.sleep(DISABLE_HUD_MENU_OPEN_DELAY)

    log("Disable HUD: navigating options...")
    for i, (px, py, delay) in enumerate(DISABLE_HUD_CLICKS, start=1):
        log(f"  HUD click {i}/{len(DISABLE_HUD_CLICKS)}")
        click_at_percent(px, py, delay_after=delay)

    log("Disable HUD: exiting menu (ESC x2)...")
    send_key("esc", 0.1)
    time.sleep(DISABLE_HUD_ESC_DELAY)
    send_key("esc", 0.1)

    return True


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["dev", "twitch"], default="dev")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    control_mode = args.mode

    pyautogui.FAILSAFE = True

    nms_proc = subprocess.Popen(
        [os.path.join("venv", "Scripts", "pymhf.exe"), "run", "state_logger.py"],
        cwd=BASE_DIR,
    )
    log(f"NMS process started (PID {nms_proc.pid})")

    log(f"Waiting {WAIT_FOR_MODE_SELECT}s...")
    time.sleep(WAIT_FOR_MODE_SELECT)

    log("Navigating menus...")
    for i, (px, py, delay) in enumerate(MENU_CLICKS, start=1):
        log(f"Click {i}/{len(MENU_CLICKS)}")
        click_at_percent(px, py, delay_after=delay)

    log(f"Waiting {WAIT_FOR_GAME_LOAD}s for game load...")
    time.sleep(WAIT_FOR_GAME_LOAD)

    log("Disabling HUD via menu clicks...")
    disable_hud_clicks()

    if control_mode == "dev":
        log("Starting dev server...")
        proc = subprocess.Popen(DEV_SERVER_CMD, cwd=BASE_DIR)
        log(f"Dev server started (PID {proc.pid})")
    else:
        log("Starting twitch bot...")
        proc = subprocess.Popen(TWITCH_BOT_CMD, cwd=BASE_DIR)
        log(f"Twitch bot started (PID {proc.pid})")
        
    log("Startup sequence complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user.")
        sys.exit(0)
    except pyautogui.FailSafeException:
        log("PyAutoGUI failsafe triggered.")
        sys.exit(1)
