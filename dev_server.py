from flask import Flask, jsonify, send_from_directory
from nms_bot import COMMANDS, start_state_poller
import os

app = Flask(__name__)

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "templates/dev_control.html")


@app.route("/cmd/<path:raw>")
def run_command(raw):
    parts = raw.strip().lower().split()
    if not parts:
        return jsonify({"ok": False, "error": "empty input"}), 400
    name, *args = parts
    if name not in COMMANDS:
        return jsonify({"ok": False, "error": f"unknown command: {name}"}), 404
    try:
        COMMANDS[name](args)
        return jsonify({"ok": True, "cmd": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    port = 5050
    print(f"\n  NMS Dev Server running")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Phone:   http://{ip}:{port}\n")
    
    start_state_poller()
    app.run(host="0.0.0.0", port=port, debug=False)
