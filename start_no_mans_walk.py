from utils import BASE_DIR, focus_nms, log, send_key, click_at_percent
import subprocess
import argparse
import pyautogui
import pygetwindow as gw
import time
import sys
import os


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
WAIT_FOR_MODE_SELECT = 35

MENU_CLICKS = [
    (0.50, 0.50, 2.0),  # "Using mods" confirm screen
    (0.35, 0.45, 2.0),  # "Play Game" button
    (0.50, 0.35, 2.0),  # Save slot 1 select
]

WAIT_FOR_GAME_LOAD = 55

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

OBS_EXE = r"C:\OBS\bin\64bit\obs64.exe"
WAIT_FOR_OBS_STREAM = 25  # seconds to wait after OBS launches for stream to go live

VIRTUAL_AUDIO_DEVICE = "VB-Audio Virtual Cable"
SOUNDVOLUMEVIEW_PATH = r"E:\NMS Modding\no_mans_walk\utilities\SoundVolumeView\SoundVolumeView.exe"


# ─────────────────────────────────────────────────────────────
# OBS
# ─────────────────────────────────────────────────────────────
def is_process_running(process_name):
    import psutil
    for proc in psutil.process_iter(["name"]):
        if process_name.lower() in (proc.info["name"] or "").lower():
            return True
    return False


def close_obs_safe_mode_prompt():
    """Close any pre-existing OBS warning/crash dialog before launch."""
    time.sleep(1)
    for window in gw.getAllWindows():
        if "OBS Studio" in window.title and "Warning" in window.title:
            log("Closing existing OBS warning dialog...")
            window.activate()
            time.sleep(0.5)
            pyautogui.press("enter")
            time.sleep(1)


def handle_obs_safe_mode_prompt():
    """Dismiss the 'Run in Safe Mode?' prompt if it appears after launch."""
    time.sleep(3)
    for window in gw.getAllWindows():
        if "OBS Studio" in window.title and "Safe Mode" in window.title:
            log("OBS Safe Mode prompt detected — selecting 'Run Normally'...")
            window.activate()
            time.sleep(0.5)
            pyautogui.press("tab")
            pyautogui.press("enter")
            time.sleep(1)


def start_obs():
    """Launch OBS with --startstreaming and wait for the stream to be live."""
    if is_process_running("obs64.exe"):
        log("OBS is already running.")
        return

    log("Starting OBS Studio and streaming...")
    try:
        close_obs_safe_mode_prompt()

        subprocess.Popen(
            [OBS_EXE, "--startstreaming", "--multi"],
            cwd=os.path.dirname(OBS_EXE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )

        time.sleep(5)  # Give OBS time to initialise
        handle_obs_safe_mode_prompt()

        log(f"Waiting {WAIT_FOR_OBS_STREAM}s for stream to go live...")
        time.sleep(WAIT_FOR_OBS_STREAM)
        log("OBS stream should be live.")

    except Exception as e:
        log(f"Failed to start OBS: {e}")


def set_nms_audio_device():
    """Route NMS audio output to the virtual cable so only game audio hits the stream."""
    try:
        subprocess.run(
            [SOUNDVOLUMEVIEW_PATH, "/SetAppDefault", VIRTUAL_AUDIO_DEVICE, "1", "NMS.exe"],
            shell=True,
        )
        log(f"Set NMS audio output to {VIRTUAL_AUDIO_DEVICE}.")
    except Exception as e:
        log(f"Failed to set NMS audio device: {e}")


# ─────────────────────────────────────────────────────────────
# NMS helpers
# ─────────────────────────────────────────────────────────────
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

    if control_mode == "twitch":
        log("Starting Twitch bot...")
        proc = subprocess.Popen(TWITCH_BOT_CMD, cwd=BASE_DIR)
        log(f"Twitch bot started (PID {proc.pid})")
        log("Routing NMS audio to virtual cable...")
        set_nms_audio_device()
        start_obs()
    else:
        log("Dev mode — skipping OBS and Twitch bot.")
        log("Starting dev server...")
        proc = subprocess.Popen(DEV_SERVER_CMD, cwd=BASE_DIR)
        log(f"Dev server started (PID {proc.pid})")

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