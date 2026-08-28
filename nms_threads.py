"""Threads publishing for No Man's Walk.

Media is uploaded to Bluesky first. Threads then imports the original public
Bluesky blob, so this integration does not require separate media hosting.

Run ``python nms_threads.py authorize`` once to create the ignored credentials
and token files used by the bot.
"""

from __future__ import annotations

from getpass import getpass
from urllib.parse import parse_qs, urlencode, urlparse
import json
import os
import secrets
import time
import webbrowser

import requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "threads_credentials.json")
TOKENS_FILE = os.path.join(BASE_DIR, "threads_tokens.json")
REDIRECT_URI = "https://localhost:8787/threads/callback"
GRAPH_API = "https://graph.threads.com/v1.0"
TOKEN_API = "https://graph.threads.com"
SCOPES = "threads_basic,threads_content_publish"

MAX_TEXT = 500
REQUEST_TIMEOUT = 30
PROCESSING_TIMEOUT = 180
PROCESSING_POLL_SECONDS = 3
REFRESH_EARLY_SECONDS = 14 * 24 * 60 * 60


class ThreadsError(RuntimeError):
    pass


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def _write_json(path: str, data: dict) -> None:
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def _request(method: str, url: str, operation: str, **kwargs) -> dict:
    response = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.ok and isinstance(payload, dict):
        return payload
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if isinstance(error, dict):
        message = error.get("message") or error.get("error_user_msg")
    else:
        message = str(error)
    message = message or response.text[:500] or f"HTTP {response.status_code}"
    raise ThreadsError(f"{operation} failed: {message}")


def _prompt_credentials() -> dict:
    existing = _read_json(CREDENTIALS_FILE)
    existing_id = str(existing.get("app_id") or "").strip()
    prompt = f"Threads App ID [{existing_id}]: " if existing_id else "Threads App ID: "
    app_id = input(prompt).strip() or existing_id
    app_secret = getpass("Threads App Secret: ").strip()
    if not app_secret:
        app_secret = str(existing.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise ThreadsError("Both the Threads App ID and App Secret are required")
    credentials = {"app_id": app_id, "app_secret": app_secret}
    _write_json(CREDENTIALS_FILE, credentials)
    return credentials


def is_configured() -> bool:
    tokens = _read_json(TOKENS_FILE)
    return bool(tokens.get("access_token") and tokens.get("user_id"))


def _save_token(payload: dict, user_id: str | int | None = None) -> dict:
    access_token = str(payload.get("access_token") or "").strip()
    user_id = str(user_id or payload.get("user_id") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not access_token or not user_id or not expires_in:
        raise ThreadsError("Threads returned an incomplete token response")
    now = int(time.time())
    saved = {
        "access_token": access_token,
        "expires_at": now + expires_in,
        "issued_at": now,
        "user_id": user_id,
    }
    _write_json(TOKENS_FILE, saved)
    return saved


def authorize() -> None:
    credentials = _prompt_credentials()
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "client_id": credentials["app_id"],
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    })
    authorization_url = f"https://threads.com/oauth/authorize?{query}"

    print("\nOpening Threads authorization in your browser.")
    print("After approval, localhost may show a connection or certificate error.")
    print("Copy the complete URL from the browser address bar and paste it below.\n")
    print(authorization_url)
    webbrowser.open(authorization_url)
    redirected_url = input("\nRedirected URL: ").strip()
    params = parse_qs(urlparse(redirected_url).query)
    returned_state = (params.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise ThreadsError("OAuth state did not match; authorization was not accepted")
    code = (params.get("code") or [""])[0]
    if not code:
        detail = (params.get("error_description") or params.get("error") or [""])[0]
        raise ThreadsError(f"No authorization code was returned{': ' + detail if detail else ''}")

    short_lived = _request(
        "POST",
        f"{TOKEN_API}/oauth/access_token",
        "Threads authorization-code exchange",
        data={
            "client_id": credentials["app_id"],
            "client_secret": credentials["app_secret"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )

    long_lived = _request(
        "GET",
        f"{TOKEN_API}/access_token",
        "Threads long-lived token exchange",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": credentials["app_secret"],
            "access_token": short_lived["access_token"],
        },
    )
    saved = _save_token(long_lived, short_lived.get("user_id"))
    print(f"\nThreads authorization saved for user {saved['user_id']}.")


def _fresh_tokens() -> dict:
    tokens = _read_json(TOKENS_FILE)
    access_token = str(tokens.get("access_token") or "").strip()
    user_id = str(tokens.get("user_id") or "").strip()
    if not access_token or not user_id:
        raise ThreadsError("Threads is not authorized; run `python nms_threads.py authorize`")

    expires_at = int(tokens.get("expires_at") or 0)
    issued_at = int(tokens.get("issued_at") or 0)
    now = int(time.time())
    if not expires_at or expires_at <= now:
        raise ThreadsError("Threads token expired; run `python nms_threads.py authorize` again")
    if expires_at - now > REFRESH_EARLY_SECONDS or now - issued_at < 24 * 60 * 60:
        return tokens

    refreshed = _request(
        "GET",
        f"{TOKEN_API}/refresh_access_token",
        "Threads token refresh",
        params={"grant_type": "th_refresh_token", "access_token": access_token},
    )
    return _save_token(refreshed, user_id)


def _clamp_text(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def _wait_until_ready(container_id: str, access_token: str) -> None:
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while time.monotonic() < deadline:
        payload = _request(
            "GET",
            f"{GRAPH_API}/{container_id}",
            "Threads media-status check",
            params={"fields": "status,error_message", "access_token": access_token},
        )
        status = str(payload.get("status") or "").upper()
        if status in {"FINISHED", "PUBLISHED"}:
            return
        if status in {"ERROR", "EXPIRED"}:
            detail = payload.get("error_message") or status
            raise ThreadsError(f"Threads could not process media: {detail}")
        time.sleep(PROCESSING_POLL_SECONDS)
    raise ThreadsError("Threads media processing timed out")


def post_media(media_url: str, media_type: str, text: str, topic_tag: str = "No Man's Sky") -> str:
    """Publish one Bluesky-hosted image or video to Threads."""
    media_type = str(media_type or "").upper()
    if media_type not in {"IMAGE", "VIDEO"}:
        raise ValueError(f"Unsupported Threads media type: {media_type}")
    if not str(media_url or "").startswith("https://"):
        raise ValueError("Threads media URL must use HTTPS")

    tokens = _fresh_tokens()
    access_token = tokens["access_token"]
    user_id = tokens["user_id"]
    data = {
        "media_type": media_type,
        "text": _clamp_text(text),
        "access_token": access_token,
    }
    data["image_url" if media_type == "IMAGE" else "video_url"] = media_url
    if topic_tag:
        data["topic_tag"] = topic_tag[:50]

    container = _request(
        "POST",
        f"{GRAPH_API}/{user_id}/threads",
        "Threads media-container creation",
        data=data,
    )
    container_id = str(container.get("id") or "")
    if not container_id:
        raise ThreadsError("Threads did not return a media-container ID")

    _wait_until_ready(container_id, access_token)
    published = _request(
        "POST",
        f"{GRAPH_API}/{user_id}/threads_publish",
        "Threads publishing",
        data={"creation_id": container_id, "access_token": access_token},
    )
    media_id = str(published.get("id") or "")
    if not media_id:
        raise ThreadsError("Threads did not return a published media ID")

    details = _request(
        "GET",
        f"{GRAPH_API}/{media_id}",
        "Threads permalink lookup",
        params={"fields": "id,permalink", "access_token": access_token},
    )
    return str(details.get("permalink") or media_id)


def status() -> None:
    tokens = _read_json(TOKENS_FILE)
    if not tokens.get("access_token") or not tokens.get("user_id"):
        print("Threads is not authorized.")
        return
    expires_at = int(tokens.get("expires_at") or 0)
    expiry = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)) if expires_at else "unknown"
    print(f"Threads user: {tokens.get('user_id')}")
    print(f"Token expires: {expiry}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="No Man's Walk Threads integration")
    parser.add_argument("command", choices=("authorize", "status"))
    args = parser.parse_args()
    try:
        authorize() if args.command == "authorize" else status()
    except (ThreadsError, requests.RequestException) as error:
        raise SystemExit(str(error)) from error
