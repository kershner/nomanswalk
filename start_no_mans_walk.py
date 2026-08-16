from utils import BASE_DIR, WINDOW_TITLE, focus_nms, log, send_key
from nms_bot import PLANET_LOAD_SECONDS, STATE_FILE, set_runtime_game_state
import obsws_python as obs
import subprocess
import pyautogui
import argparse
import win32gui
import time
import glob
import sys
import os


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
WAIT_FOR_MODE_SELECT = 35

WAIT_FOR_GAME_LOAD = 90

VENV_PY = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")

DEV_SERVER_CMD = [VENV_PY, "dev_server.py", "--startup"]
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


def _wait_for_obs_ready(ws, timeout=30, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.get_version()
            return True
        except Exception:
            time.sleep(interval)
    return False
    

def _start_stream_with_retry(ws, retries=5, interval=2):
    for i in range(retries):
        try:
            ws.start_stream()
            return True
        except Exception as e:
            if "207" in str(e) or "not ready" in str(e).lower():
                log(f"OBS not ready yet, retrying in {interval}s... ({i+1}/{retries})")
                time.sleep(interval)
            else:
                raise
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
        if not _wait_for_obs_ready(ws):
            log("ERROR: OBS websocket connected but OBS never became ready.")
            return
        if not _start_stream_with_retry(ws):
            log("ERROR: Failed to start stream after retries.")
            return
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
def get_processes_by_name(process_name):
    import psutil

    matches = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == process_name.lower():
                matches.append(proc)
        except psutil.Error:
            pass
    return matches


def nms_window_exists():
    return bool(win32gui.FindWindow(None, WINDOW_TITLE))


def kill_nms():
    if is_process_running(NMS_EXE_NAME):
        log("Killing existing/partial NMS.exe before retry...")
        subprocess.run(
            ["taskkill", "/F", "/IM", NMS_EXE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)


def wait_for_nms_ready(timeout):
    deadline = time.time() + timeout

    while time.time() < deadline:
        procs = get_processes_by_name(NMS_EXE_NAME)

        if procs and nms_window_exists():
            pid = procs[0].pid
            log(f"NMS.exe confirmed running with window (PID {pid}).")
            return True

        time.sleep(NMS_POLL_INTERVAL)

    return False


def wait_for_fresh_state(timeout=60):
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            if os.path.exists(STATE_FILE):
                age = time.time() - os.path.getmtime(STATE_FILE)
                if age < 10:
                    log("Fresh NMS state file confirmed.")
                    return True
        except OSError:
            pass

        time.sleep(2)

    log("WARNING: NMS state file did not become fresh in time.")
    return False


def launch_nms_with_retry():
    """
    Launch NMS through pymhf and require both:
    - NMS.exe is running
    - the No Man's Sky window exists

    Retries cleanly if Steam/pymhf fails to fully start the game.
    """
    for attempt in range(1, NMS_MAX_RETRIES + 1):
        log(f"Launching NMS (attempt {attempt}/{NMS_MAX_RETRIES})...")

        kill_nms()

        launcher_proc = subprocess.Popen(
            [os.path.join("venv", "Scripts", "pymhf.exe"), "run", "nmspy"],
            cwd=BASE_DIR,
        )

        if wait_for_nms_ready(NMS_LAUNCH_TIMEOUT):
            return launcher_proc

        log(
            f"NMS did not fully start within {NMS_LAUNCH_TIMEOUT}s. "
            "Terminating launcher and retrying..."
        )

        try:
            launcher_proc.terminate()
            launcher_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            launcher_proc.kill()

        kill_nms()

    raise RuntimeError(f"NMS failed to fully launch after {NMS_MAX_RETRIES} attempts.")


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

    set_runtime_game_state(planet_loading=True, startup_ready=False)

    if control_mode == "dev":
        log("Starting dev server...")
        proc = subprocess.Popen(DEV_SERVER_CMD, cwd=BASE_DIR)
        log(f"Dev server started (PID {proc.pid})")
    else:
        log("Starting Twitch bot...")
        proc = subprocess.Popen(TWITCH_BOT_CMD, cwd=BASE_DIR)
        log(f"Twitch bot started (PID {proc.pid})")

    if control_mode == "twitch":
        log("Starting OBS before NMS so Game Capture hook attaches cleanly...")
        # set_nms_audio_device()
        start_obs()
        time.sleep(10)  # let OBS fully settle before NMS creates its DX context

    nmspy_proc = launch_nms_with_retry()
    log("NMS launch confirmed.")

    log(f"Waiting {WAIT_FOR_MODE_SELECT}s...")
    time.sleep(WAIT_FOR_MODE_SELECT)

    log("Sending quick_load key (F9)...")
    send_key("f9", 0.1)

    log(f"Waiting {WAIT_FOR_GAME_LOAD}s for game load...")
    time.sleep(WAIT_FOR_GAME_LOAD)

    wait_for_fresh_state(timeout=60)

    log("Disabling HUD via hud_toggle mod...")
    send_key("f5", 0.1)

    log("Toggling music with the 'm' key...")
    send_key("m", 0.1)

    if control_mode == "dev":
        log("Dev mode — skipping OBS and Twitch bot.")

    set_runtime_game_state(planet_loading=False, startup_ready=True)
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