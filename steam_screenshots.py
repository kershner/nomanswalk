"""Locate and manage No Man's Sky screenshots created by Steam."""

from pathlib import Path
import os
import re
import time

try:
    import winreg
except ImportError:  # pragma: no cover - only relevant off Windows
    winreg = None


NMS_STEAM_APP_ID = "275850"
STEAM_ID64_BASE = 76561197960265728
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _steam_roots():
    roots = []
    if winreg is not None:
        registry_values = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, key_path, value_name in registry_values:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    roots.append(Path(winreg.QueryValueEx(key, value_name)[0]))
            except OSError:
                pass

    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            roots.append(Path(base) / "Steam")

    unique = []
    seen = set()
    for root in roots:
        normalized = str(root.expanduser().resolve()).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(root)
    return unique


def _most_recent_account_id(steam_root: Path):
    loginusers = steam_root / "config" / "loginusers.vdf"
    try:
        text = loginusers.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for steam_id64, block in re.findall(r'"(\d{17})"\s*\{(.*?)\n\s*\}', text, re.DOTALL):
        if re.search(r'"MostRecent"\s*"1"', block, re.IGNORECASE):
            account_id = int(steam_id64) - STEAM_ID64_BASE
            if account_id >= 0:
                return str(account_id)
    return None


def _image_metadata(directory):
    metadata = {}
    if not directory:
        return metadata
    try:
        for path in Path(directory).iterdir():
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                try:
                    stat = path.stat()
                    metadata[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return metadata


def _latest_image_time(directory: Path):
    return max((item[0] for item in _image_metadata(directory).values()), default=0)


def find_nms_screenshot_dir():
    """Find the active or most recently used Steam NMS screenshot directory."""
    candidates = []
    active_candidates = []

    for steam_root in _steam_roots():
        userdata = steam_root / "userdata"
        active_account_id = _most_recent_account_id(steam_root)
        try:
            account_dirs = [path for path in userdata.iterdir() if path.is_dir()]
        except OSError:
            continue

        for account_dir in account_dirs:
            screenshots = account_dir / "760" / "remote" / NMS_STEAM_APP_ID / "screenshots"
            if not screenshots.is_dir():
                continue
            candidates.append(screenshots)
            if account_dir.name == active_account_id:
                active_candidates.append(screenshots)

    pool = active_candidates or candidates
    if not pool:
        return None
    return str(max(pool, key=_latest_image_time))


def snapshot_screenshots(directory):
    """Return file metadata used to distinguish the next captured screenshot."""
    return _image_metadata(directory)


def wait_for_new_screenshot(directory, before, timeout_seconds=15, captured_after_ns=None):
    """Wait for a new/changed Steam screenshot and for its size to stabilize."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_path = None
    last_size = None
    stable_polls = 0

    while time.monotonic() < deadline:
        if not directory or not Path(directory).is_dir():
            directory = find_nms_screenshot_dir()
        if not directory:
            time.sleep(0.1)
            continue

        current = snapshot_screenshots(directory)
        changed = [
            (path, metadata)
            for path, metadata in current.items()
            if (path not in before or before[path] != metadata)
            and (captured_after_ns is None or metadata[0] >= captured_after_ns - 2_000_000_000)
        ]
        if changed:
            path, metadata = max(changed, key=lambda item: item[1][0])
            size = metadata[1]
            if path == last_path and size == last_size and size > 0:
                stable_polls += 1
                if stable_polls >= 2:
                    return path
            else:
                last_path = path
                last_size = size
                stable_polls = 0
        time.sleep(0.1)
    return None


def delete_screenshot(path):
    """Delete a captured screenshot and its matching Steam thumbnail."""
    if not path:
        return True
    screenshot = Path(path)
    targets = (screenshot, screenshot.parent / "thumbnails" / screenshot.name)
    for target in targets:
        for attempt in range(5):
            try:
                target.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 4:
                    return False
                time.sleep(0.2)
    return True
