#!/usr/bin/env python3
"""Register the installed Optocam Zero app with whisplay-daemon."""

import argparse
import json
import os
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--socket", default="/tmp/whisplay-daemon.sock")
    args = parser.parse_args()

    home = os.path.abspath(args.home)
    request = {
        "version": 1,
        "cmd": "app.register",
        "payload": {
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
            "exit_gesture": "quad_click",
            "priority": 50,
            "use_daemon_default_log": True,
            "persist": True,
        },
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(args.socket)
        client.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(client.makefile("r").readline())
    if not response.get("ok"):
        raise SystemExit(response.get("error", "whisplay-daemon registration failed"))
    print(f"Registered Optocam Zero from {home}")


if __name__ == "__main__":
    main()
