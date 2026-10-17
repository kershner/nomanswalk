"""Durable planet outbox, independent of the game and Twitch bot."""

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import threading

import requests


BASE_DIR = Path(__file__).resolve().parent
OUTBOX_FILE = BASE_DIR / "planet_report_outbox.json"
RUNTIME_STATE_FILE = BASE_DIR / "runtime_state.json"
PARAMS_FILE = BASE_DIR / "parameters.json"
_lock = threading.Lock()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default


@contextmanager
def _outbox():
    # Both the running bot and manual/scheduled subprocesses write this file.
    with _lock, open(str(OUTBOX_FILE) + ".lock", "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            state = _read_json(OUTBOX_FILE, {"pending": {}, "uploaded": {}})
            yield state
            temp = str(OUTBOX_FILE) + ".tmp"
            with open(temp, "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, OUTBOX_FILE)
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _identity(item):
    return json.dumps([item["timestamp"], item["galaxy_index"], item["planet_hex"]])


def _fingerprint(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()


def queue_planets(planets, only_missing=False):
    """Persist observations before daily stats can roll over; keep visit times intact."""
    if not planets:
        return
    with _outbox() as state:
        for item in planets:
            key = _identity(item)
            if only_missing and (key in state["pending"] or key in state["uploaded"]):
                continue
            if state["uploaded"].get(key) != _fingerprint(item):
                state["pending"][key] = dict(item)


def daily_planets(daily):
    details = daily.get("planet_details", {})
    return [dict(details[key]) for key in daily.get("planets", []) if key in details]


def upload_pending(endpoint, api_token, timeout=30, output=print):
    """Upload a snapshot; acknowledge only unchanged records after HTTP success."""
    with _outbox() as state:
        pending = dict(state["pending"])
    payload = {"planets": list(pending.values())}
    if not pending:
        output("No planets pending; skipping upload.")
        return payload
    endpoint = str(endpoint or "").strip()
    api_token = str(api_token or "").strip()
    if not endpoint:
        raise ValueError("PLANET_REPORT_ENDPOINT is not configured in parameters.json.")
    if not api_token:
        raise ValueError("no_mans_walk_api_token is not configured in parameters.json.")
    response = requests.post(
        endpoint,
        json=payload,
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=timeout,
        allow_redirects=False,
    )
    output(f"HTTP {response.status_code}")
    output(response.text)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Planet upload failed (HTTP {response.status_code}).")
    with _outbox() as state:
        for key, item in pending.items():
            state["uploaded"][key] = _fingerprint(item)
            if state["pending"].get(key) == item:
                del state["pending"][key]
    return payload


def upload_from_files(output=print):
    # Import existing stats too, including yesterday's stats after a restart.
    runtime = _read_json(RUNTIME_STATE_FILE, {})
    queue_planets(daily_planets(runtime.get("daily", {})), only_missing=True)
    params = _read_json(PARAMS_FILE, {})
    return upload_pending(
        params.get("PLANET_REPORT_ENDPOINT"),
        params.get("no_mans_walk_api_token"),
        output=output,
    )
