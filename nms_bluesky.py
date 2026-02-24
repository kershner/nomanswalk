"""
nms_bluesky.py  –  Bluesky integration for the No Man's Sky bot.

Import:
    from nms_bluesky import login, ensure_live, clear_live, post_clip

Standalone (creates a clip and posts it autonomously):
    python nms_bluesky.py
"""

from atproto import Client
from datetime import datetime, timezone, timedelta
from io import BytesIO
from utils import get_status_text
import requests
import httpx
import logging
import random
import time
import json
import os


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

TWITCH_CLIP_URL = "https://api.twitch.tv/helix/clips"

TAGS_POOL = [
    "NMS", "Exploration", "Automation", "Chill", "Cozy",
    "Twitch", "Interactive", "Gaming", "ProcGen",
    "Survival", "Relaxing", "Casual", "Python", "Programming",
    "Streaming"
]

def _pick_tags():
    chosen = random.sample(TAGS_POOL, min(5, len(TAGS_POOL)))
    return ["nomanssky"] + chosen


def _load_params(params_file="parameters.json"):
    if not os.path.exists(params_file):
        raise FileNotFoundError(f"Missing {params_file}")
    with open(params_file, "r") as f:
        return json.load(f)


def login(params_file="parameters.json") -> Client:
    params = _load_params(params_file)
    handle = params.get("BLUESKY_HANDLE", "")
    password = params.get("BLUESKY_APP_PASSWORD", "")
    if not handle or not password:
        raise ValueError("parameters.json must include BLUESKY_HANDLE and BLUESKY_APP_PASSWORD")
    client = Client()
    client.login(handle, password)
    client.request._client.timeout = httpx.Timeout(30.0)
    return client


# ─────────────────────────────────────────────────────────────
# Live status
# ─────────────────────────────────────────────────────────────
STATUS_COLL = "app.bsky.actor.status"
STATUS_RKEY = "self"
MAX_MINUTES = 30
REFRESH_EARLY = timedelta(minutes=5)
LIVE_URI = "https://www.twitch.tv/nomanswalk"


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _record(title: str) -> dict:
    return {
        "$type": STATUS_COLL,
        "status": "app.bsky.actor.status#live",
        "createdAt": _now_z(),
        "durationMinutes": MAX_MINUTES,
        "embed": {
            "$type": "app.bsky.embed.external",
            "external": {
                "$type": "app.bsky.embed.external#external",
                "uri": LIVE_URI,
                "title": _clamp(title, 100),
                "description": "",
            },
        },
    }


def set_live(client: Client, title: str) -> None:
    client.com.atproto.repo.put_record(
        data={"repo": client.me.did, "collection": STATUS_COLL, "rkey": STATUS_RKEY, "record": _record(title)}
    )
    log.info(f"Bluesky live status set: {title}")


def ensure_live(client: Client, title: str) -> None:
    try:
        rec = client.com.atproto.repo.get_record(
            params={"repo": client.me.did, "collection": STATUS_COLL, "rkey": STATUS_RKEY}
        )
        val = rec.value.model_dump() if hasattr(rec.value, "model_dump") else rec.value
        created = datetime.fromisoformat(val["createdAt"].replace("Z", "+00:00"))
        mins = int(val.get("durationMinutes", 0))
        exp = created + timedelta(minutes=mins) if mins else None
        needs_refresh = (
            val.get("status") != "app.bsky.actor.status#live"
            or mins != MAX_MINUTES
            or not exp
            or exp - datetime.now(timezone.utc) <= REFRESH_EARLY
        )
        cur = val.get("embed", {}).get("external", {}) if isinstance(val.get("embed"), dict) else {}
        if needs_refresh or cur.get("title") != title:
            set_live(client, title)
    except Exception:
        set_live(client, title)


def clear_live(client: Client) -> None:
    try:
        client.com.atproto.repo.delete_record(
            data={"repo": client.me.did, "collection": STATUS_COLL, "rkey": STATUS_RKEY}
        )
        log.info("Bluesky live status cleared.")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Twitch helpers
# ─────────────────────────────────────────────────────────────
def _get_twitch_token(params, tokens_file="oauth_tokens.json"):
    with open(tokens_file, "r") as f:
        tokens = json.load(f)
    expires_at = int(tokens.get("expires_at") or 0)
    if expires_at and time.time() < (expires_at - 60):
        return tokens["access_token"]
    log.info("Twitch token expired, refreshing...")
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id":     params["CLIENT_ID"],
        "client_secret": params["CLIENT_SECRET"],
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    r.raise_for_status()
    new_tokens = r.json()
    tokens["access_token"] = new_tokens["access_token"]
    tokens["refresh_token"] = new_tokens.get("refresh_token", tokens["refresh_token"])
    tokens["expires_at"] = int(time.time()) + int(new_tokens.get("expires_in", 14400))
    with open(tokens_file, "w") as f:
        json.dump(tokens, f, indent=2, sort_keys=True)
    log.info("Twitch token refreshed and saved.")
    return tokens["access_token"]


def _create_clip(headers, broadcaster_id):
    r = requests.post(TWITCH_CLIP_URL, headers=headers, params={"broadcaster_id": broadcaster_id})
    if r.status_code == 404 and "offline" in r.text.lower():
        raise Exception("Stream is not live — cannot create clip.")
    if r.status_code not in [200, 202]:
        raise Exception(f"Clip creation failed: {r.status_code} - {r.text}")
    clip_id = r.json()["data"][0]["id"]
    log.info(f"Clip created: {clip_id} — waiting for it to be ready...")
    for _ in range(10):
        r = requests.get(TWITCH_CLIP_URL, headers=headers, params={"id": clip_id})
        data = r.json().get("data")
        if data and data[0].get("url"):
            return clip_id, data[0]
        time.sleep(3)
    raise Exception("Clip URL never became available")


def _download_clip(clip_id, headers, broadcaster_id):
    r = requests.get(
        "https://api.twitch.tv/helix/clips/downloads",
        headers=headers,
        params={"broadcaster_id": broadcaster_id, "editor_id": broadcaster_id, "clip_id": clip_id},
    )
    if r.status_code != 200:
        raise Exception(f"Failed to fetch clip download URL: {r.status_code} - {r.text}")

    download_url = r.json()["data"][0].get("landscape_download_url")
    if not download_url:
        raise Exception("No landscape_download_url in response")

    video = requests.get(download_url, stream=True)
    video.raise_for_status()

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{clip_id}.mp4")
    with open(path, "wb") as f:
        for chunk in video.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    log.info(f"Downloaded: {os.path.getsize(path) / 1024 / 1024:.2f} MB → {path}")
    return path


# ─────────────────────────────────────────────────────────────
# Post a clip (fully autonomous)
# ─────────────────────────────────────────────────────────────
def post_clip(bsky_client: Client, params_file="parameters.json"):
    params = _load_params(params_file)
    status = get_status_text()
    status_text = status.get("main", "").strip()

    broadcaster_id = params["NMS_TWITCH_BROADCASTER_ID"]
    token = _get_twitch_token(params)
    headers = {"Authorization": f"Bearer {token}", "Client-Id": params["CLIENT_ID"]}

    clip_id, clip_data = _create_clip(headers, broadcaster_id)
    clip_url = clip_data.get("url", f"https://clips.twitch.tv/{clip_id}")

    video_path = _download_clip(clip_id, headers, broadcaster_id)

    try:
        with open(video_path, "rb") as f:
            video_data = f.read()
        if len(video_data) > 50 * 1024 * 1024:
            raise Exception("Video exceeds Bluesky's 50 MB limit")

        blob = bsky_client.com.atproto.repo.upload_blob(BytesIO(video_data), headers={"Content-Type": "video/mp4"}).blob

        tags = _pick_tags()
        tag_line = " ".join(f"#{t}" for t in tags)
        full_text = f"{status_text}\n\n{tag_line}"

        facets = []
        for tag in tags:
            needle = f"#{tag}"
            start = full_text.find(needle)
            if start == -1:
                continue
            end = start + len(needle)
            facets.append({
                "index": {
                    "byteStart": len(full_text[:start].encode()),
                    "byteEnd": len(full_text[:end].encode()),
                },
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
            })

        record = {
            "text": full_text,
            "createdAt": bsky_client.get_current_time_iso(),
            "facets": facets,
            "embed": {
                "$type": "app.bsky.embed.video",
                "video": blob,
                "alt": status_text,
                "aspectRatio": {"width": 16, "height": 9},
            },
        }

        bsky_client.com.atproto.repo.create_record(
            data={"repo": bsky_client.me.did, "collection": "app.bsky.feed.post", "record": record}
        )
        log.info(f"Posted to Bluesky: {status_text[:80]}")

    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
            log.info("Cleaned up local clip file")


# ─────────────────────────────────────────────────────────────
# Standalone
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    client = login()
    post_clip(client)