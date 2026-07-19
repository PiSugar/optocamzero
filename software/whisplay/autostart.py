#!/usr/bin/env python3
"""Register and launch Optocam through whisplay-daemon during boot."""

import argparse
import json
import os
import socket
import time


def request(socket_path, cmd, payload=None):
    body = {"version": 1, "cmd": cmd, "payload": payload or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(socket_path)
        client.sendall((json.dumps(body) + "\n").encode())
        response = json.loads(client.makefile("r").readline())
    if not response.get("ok"):
        raise RuntimeError(response.get("error", f"{cmd} failed"))
    return response.get("payload", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--socket", default="/tmp/whisplay-daemon.sock")
    parser.add_argument("--wait", type=float, default=60.0)
    args = parser.parse_args()
    home = os.path.abspath(args.home)

    deadline = time.monotonic() + args.wait
    while True:
        try:
            request(args.socket, "health.ping")
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise SystemExit("timed out waiting for whisplay-daemon")
            time.sleep(0.5)

    request(args.socket, "app.register", {
        "app_id": "optocamzero",
        "display_name": "Optocam Zero",
        "icon": "OC",
        "launch_command": os.path.join(home, "run_optocamzero.sh"),
        "cwd": home,
        "env": {
            "OPTOCAM_HARDWARE": "whisplay",
            "OPTOCAM_HOME": home,
            "OPTOCAM_WHISPLAY_BACKEND": "daemon",
        },
        "exit_gesture": "none",
        "priority": 50,
        "use_daemon_default_log": True,
        "persist": True,
    })
    apps = request(args.socket, "app.list").get("apps", [])
    current = next((app for app in apps if app.get("app_id") == "optocamzero"), None)
    if current and current.get("running"):
        print("Optocam Zero is already running")
        return
    request(args.socket, "app.launch", {"app_id": "optocamzero"})
    print("Optocam Zero launch requested")


if __name__ == "__main__":
    main()
