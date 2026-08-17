from flask import Flask, jsonify, request, send_from_directory
from nms_twitch_bot import Config, NMSBot
from nms_bot import get_runtime_game_state, set_runtime_game_state
import argparse
import asyncio
import os
import threading


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--startup", action="store_true")
    return p.parse_args()


ARGS = parse_args()

if not ARGS.startup:
    set_runtime_game_state(planet_loading=False, startup_ready=True)

app = Flask(__name__)

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

_messages = []
_messages_lock = threading.Lock()
_loop = asyncio.new_event_loop()
_bot = None
_ctx = None


class DevAuthor:
    name = Config.TWITCH_CHANNEL


class DevContext:
    author = DevAuthor()
    message = None

    async def send(self, text):
        with _messages_lock:
            _messages.append(text)


async def _wait_for_startup():
    while not get_runtime_game_state().get("startup_ready", False):
        await asyncio.sleep(0.5)
    await _bot._start_runtime(_ctx, run_startup=True)


async def _init_bot():
    global _bot, _ctx
    _bot = NMSBot(dev_mode=True)
    _ctx = DevContext()
    if ARGS.startup:
        asyncio.create_task(_wait_for_startup())
    else:
        asyncio.create_task(_bot._start_runtime(_ctx, run_startup=False))


def _run_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_init_bot())
    _loop.run_forever()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "templates/dev_control.html")


@app.route("/cmd/<path:raw>")
def run_command(raw):
    if not _bot or not _ctx:
        return jsonify({"ok": False, "error": "bot not ready"}), 503

    content = raw.strip()
    if not content:
        return jsonify({"ok": False, "error": "empty input"}), 400
    if not content.startswith("!"):
        content = "!" + content

    try:
        future = asyncio.run_coroutine_threadsafe(
            _bot.process_chat_message(_ctx, content, _ctx.message), _loop
        )
        future.result(timeout=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/chat")
def chat():
    try:
        after = max(0, int(request.args.get("after", 0)))
    except ValueError:
        after = 0

    with _messages_lock:
        messages = _messages[after:]
        cursor = len(_messages)

    return jsonify({"messages": messages, "cursor": cursor})


if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    port = 5050
    print(f"\n  NMS Dev Server running")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Phone:   http://{ip}:{port}\n")

    threading.Thread(target=_run_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
