"""Run on the streaming PC to replace Twitch tokens after browser authorization."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import requests


REDIRECT_URI = "http://localhost:3000"
REQUIRED_SCOPES = {
    "chat:read", "chat:edit", "channel:manage:broadcast",
    "clips:edit", "channel:manage:clips",
}


def wait_for_code(client_id, scopes):
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # The callback URL contains a private authorization code.

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid = (
                urlsplit(self.path).path == "/"
                and secrets.compare_digest(query.get("state", [""])[0], state)
            )
            if not valid:
                status, text = 400, "Invalid callback. Return to the Twitch authorization page."
            elif query.get("error"):
                result["error"] = True
                status, text = 400, "Authorization declined. Return to PowerShell."
            elif query.get("code"):
                result["code"] = query["code"][0]
                status, text = 200, "Authorization received. Return to PowerShell to check the result."
            else:
                status, text = 400, "No authorization code received."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))

    # Bind before opening the browser so a busy port leaves tokens untouched.
    with HTTPServer(("127.0.0.1", 3000), Callback) as server:
        server.timeout = 1
        url = "https://id.twitch.tv/oauth2/authorize?" + urlencode({
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(sorted(scopes)),
            "state": state,
            "force_verify": "true",
        })
        print("Opening Twitch. Sign in as nomanswalk and authorize the app.")
        print("If the browser does not open, open this URL on this PC:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if result.get("error"):
        raise RuntimeError("Authorization was declined. Existing tokens were not changed.")
    if not result.get("code"):
        raise RuntimeError("Authorization timed out. Existing tokens were not changed.")
    return result["code"]


def exchange_and_validate(client_id, client_secret, code, expected_login, scopes):
    response = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed (HTTP {response.status_code}). "
            "Check the app's CLIENT_ID, CLIENT_SECRET, and registered redirect URL."
        )
    payload = response.json()
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = int(payload.get("expires_in") or 0)
    if not access or not refresh or expires_in <= 0:
        raise RuntimeError("Twitch did not return a complete, renewable token pair.")
    received_at = int(time.time())
    validation = requests.get("https://id.twitch.tv/oauth2/validate", headers={
        "Authorization": f"OAuth {access}",
    }, timeout=20)
    if validation.status_code != 200:
        raise RuntimeError(f"Token validation failed (HTTP {validation.status_code}).")
    account = validation.json()
    if account.get("client_id") != client_id:
        raise RuntimeError("The new token belongs to a different Twitch app.")
    if str(account.get("login", "")).lower() != expected_login.lower():
        raise RuntimeError(f"Wrong Twitch account. Sign in as {expected_login} and try again.")
    granted = set(account.get("scopes") or [])
    if not scopes.issubset(granted):
        raise RuntimeError("Twitch did not grant all requested permissions. Authorize again.")
    return {
        "access_token": access, "refresh_token": refresh,
        "expires_at": received_at + expires_in,
        "scopes": sorted(granted), "token_type": payload.get("token_type", "bearer"),
    }


def save_tokens(path, tokens):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".twitch-auth-",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(tokens, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent,
                        help="Folder containing parameters.json and oauth_tokens.json")
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        params = json.loads((directory / "parameters.json").read_text(encoding="utf-8-sig"))
        client_id = str(params.get("CLIENT_ID") or "").strip()
        client_secret = str(params.get("CLIENT_SECRET") or "").strip()
        if not client_id or not client_secret:
            raise RuntimeError("parameters.json must contain CLIENT_ID and CLIENT_SECRET.")
        path = directory / "oauth_tokens.json"
        scopes = set(REQUIRED_SCOPES)
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8-sig"))
            saved_scopes = old.get("scopes") or old.get("scope") or []
            scopes.update(saved_scopes.split() if isinstance(saved_scopes, str) else saved_scopes)
        print("Stop any running Twitch bot processes before continuing.")
        print("In the existing Twitch app settings, add this OAuth Redirect URL:")
        print(REDIRECT_URI)
        print("https://dev.twitch.tv/console/apps")
        print(f"Use the existing app with Client ID: {client_id}")
        input("Press Enter once the bot is stopped and that redirect URL is saved: ")
        code = wait_for_code(client_id, scopes)
        tokens = exchange_and_validate(client_id, client_secret, code, "nomanswalk", scopes)
        save_tokens(path, tokens)
        print(f"Success: verified nomanswalk and saved {path}.")
        print("You can now start nms_twitch_bot.py. Automatic token renewal will resume.")
    except RuntimeError as error:
        print(f"Authorization failed: {error}")
        return 1
    except (OSError, ValueError, requests.RequestException):
        print("Authorization failed: check the files, network connection, and whether port 3000 is free.")
        print("Existing tokens were not changed unless the success message appeared.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nAuthorization cancelled. Existing tokens were not changed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
