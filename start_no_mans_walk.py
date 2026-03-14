from utils import BASE_DIR, focus_nms, log, send_key, click_at_percent
from nms_bot import PLANET_LOAD_SECONDS
import obsws_python as obs
import subprocess
import pyautogui
import argparse
import time
import glob
import sys
import os


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
WAIT_FOR_MODE_SELECT = 90

MENU_CLICKS = [
    (0.50, 0.50, 2.0),  # "Using mods" confirm screen
    (0.35, 0.45, 3.0),  # "Play Game" button
    (0.50, 0.36, 3.0),  # Save slot 1 select
    (0.50, 0.36, 3.0),  # Save slot 1 select (again)
]

WAIT_FOR_GAME_LOAD = 90

VENV_PY = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")

DEV_SERVER_CMD = [VENV_PY, "dev_server.py"]
TWITCH_BOT_CMD = [VENV_PY, "nms_twitch_bot.py"]
DEV_SERVER_URL = "http://127.0.0.1:5050"

OBS_DIR = r"C:\Program Files\obs-studio"
OBS_EXE = os.path.join(OBS_DIR, "bin", "64bit", "obs64.exe")
OBS_LOG_DIR = os.path.join(os.path.expandvars("%APPDATA%"), "obs-studio", "logs")
OBS_INIT_WAIT = 10
OBS_RETRY_WAIT = 10
OBS_MAX_RETRIES = 5

VIRTUAL_AUDIO_DEVICE = "VB-Audio Virtual Cable"
SOUNDVOLUMEVIEW_PATH = r"C:\NoMansWalk\utilities\SoundVolumeView\SoundVolumeView.exe"

NMS_EXE_NAME = "NMS.exe"
NMS_LAUNCH_TIMEOUT = 90   
NMS_POLL_INTERVAL = 3    
NMS_MAX_RETRIES = 3    


# ─────────────────────────────────────────────────────────────
# OBS
# ─────────────────────────────────────────────────────────────
def is_process_running(process_name):
    import psutil
    for proc in psutil.process_iter(["name"]):
        if process_name.lower() in (proc.info["name"] or "").lower():
            return True
    return False


def _obs_log_since(since):
    logs = glob.glob(os.path.join(OBS_LOG_DIR, "*.txt"))
    recent = [p for p in logs if os.path.getmtime(p) >= since]
    pool = recent or logs
    return max(pool, key=os.path.getmtime) if pool else None


def _obs_log_has(log_path, *fragments):
    try:
        content = open(log_path, encoding="utf-8", errors="ignore").read().lower()
        return any(f.lower() in content for f in fragments)
    except OSError:
        return False


def start_obs():
    """Launch OBS and wait for the stream to be live."""
    if is_process_running("obs64.exe"):
        log("OBS is already running.")
        return

    obs_args = [OBS_EXE, "--multi", "--minimize-to-tray",
                "--disable-missing-files-check", "--disable-updater"]

    for attempt in range(1, OBS_MAX_RETRIES + 1):
        log(f"Starting OBS (attempt {attempt}/{OBS_MAX_RETRIES})...")
        launch_time = time.time()
        subprocess.Popen(obs_args, cwd=os.path.dirname(OBS_EXE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(OBS_INIT_WAIT)
        obs_log = _obs_log_since(launch_time)

        if obs_log and _obs_log_has(obs_log, "nvenc not supported",
                                    "encoder type 'obs_nvenc_h264_tex' not available",
                                    "failed to initialize module 'obs-nvenc.dll'"):
            log(f"NVENC failure detected (attempt {attempt}), retrying in {OBS_RETRY_WAIT}s...")
            subprocess.run(["taskkill", "/F", "/IM", "obs64.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(OBS_RETRY_WAIT)
            continue

        ws = obs.ReqClient(host="localhost", port=4455, password="")
        ws.start_stream()
        log("OBS stream live.")
        return

    log(f"ERROR: OBS failed to start with NVENC after {OBS_MAX_RETRIES} attempts.")


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
def launch_nms_with_retry():
    """
    Launch NMS via pymhf and block until NMS.exe appears in the process list.
    Retries up to NMS_MAX_RETRIES times to handle Steam error 83.
    Raises RuntimeError if the game never starts.
    """
    for attempt in range(1, NMS_MAX_RETRIES + 1):
        log(f"Launching NMS (attempt {attempt}/{NMS_MAX_RETRIES})...")
        proc = subprocess.Popen(
            [os.path.join("venv", "Scripts", "pymhf.exe"), "run", "nmspy"],
            cwd=BASE_DIR,
        )

        deadline = time.time() + NMS_LAUNCH_TIMEOUT
        while time.time() < deadline:
            if is_process_running(NMS_EXE_NAME):
                log(f"NMS.exe confirmed running (PID {proc.pid}).")
                return proc
            time.sleep(NMS_POLL_INTERVAL)

        # NMS.exe never appeared — kill the launcher and try again
        log(f"NMS.exe did not appear within {NMS_LAUNCH_TIMEOUT}s "
            f"(possible Steam error 83). Terminating launcher and retrying...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    raise RuntimeError(f"NMS failed to launch after {NMS_MAX_RETRIES} attempts.")


def teleport_to_new_planet():
    """Focus NMS, send the teleport key, and wait for the planet to load."""
    log("Teleporting to new planet before stream starts...")
    hwnd, _ = focus_nms()
    if not hwnd:
        log("WARNING: Could not focus NMS for teleport — skipping.")
        return
    send_key("o", 0.1)
    log(f"Teleport key sent. Waiting {PLANET_LOAD_SECONDS}s for planet to load...")
    time.sleep(PLANET_LOAD_SECONDS)
    log("Planet load wait complete.")


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

    if control_mode == "twitch":
        log("Starting OBS before NMS so Game Capture hook attaches cleanly...")
        # set_nms_audio_device()
        start_obs()
        time.sleep(10)  # let OBS fully settle before NMS creates its DX context

    nmspy_proc = launch_nms_with_retry()
    log(f"NMS process started (PID {nmspy_proc.pid})")

    log(f"Waiting {WAIT_FOR_MODE_SELECT}s...")
    time.sleep(WAIT_FOR_MODE_SELECT)

    log("Navigating menus...")
    for i, (px, py, delay) in enumerate(MENU_CLICKS, start=1):
        log(f"Click {i}/{len(MENU_CLICKS)}")
        click_at_percent(px, py, delay_after=delay)

    log(f"Waiting {WAIT_FOR_GAME_LOAD}s for game load...")
    time.sleep(WAIT_FOR_GAME_LOAD)

    log("Disabling HUD via hud_toggle mod...")
    send_key("f5", 0.1)

    log("Teleporting to starting planet...")
    teleport_to_new_planet()

    log("Toggling music with the 'm' key...")
    send_key("m", 0.1)

    if control_mode == "twitch":
        log("Starting Twitch bot...")
        proc = subprocess.Popen(TWITCH_BOT_CMD, cwd=BASE_DIR)
        log(f"Twitch bot started (PID {proc.pid})")
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