#!/usr/bin/env python3
"""Whisplay HAT and PiSugar 3 adapter for Optocam Zero.

With whisplay-daemon running, hardware is accessed exclusively through its IPC
API.  A standalone backend is also available for dedicated-camera images where
the daemon is disabled; it uses Whisplay's official ``WhisplayBoard`` runtime.
"""

from __future__ import annotations

from collections import defaultdict
import json
import mmap
import os
import socket
import sys
import threading
import time


WHISPLAY_SOCKET = os.getenv("WHISPLAY_DAEMON_SOCKET", "/tmp/whisplay-daemon.sock")
PISUGAR_SOCKETS = ("/tmp/pisugar-server.sock", "/run/pisugar-server.sock")
PISUGAR_EVENT_HOST = os.getenv("PISUGAR_EVENT_HOST", "127.0.0.1")
PISUGAR_EVENT_PORT = int(os.getenv("PISUGAR_EVENT_PORT", "8423"))
APP_ID = "optocamzero"


class VirtualGPIO:
    """Small RPi.GPIO-compatible input shim used by the legacy event loop."""

    BCM = "BCM"
    IN = "IN"
    OUT = "OUT"
    PUD_UP = "PUD_UP"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self._states = defaultdict(lambda: 1)
        self._lock = threading.Lock()
        self._gallery_active = False
        self.roles = {}

    def configure_roles(self, **roles):
        self.roles = dict(roles)
        with self._lock:
            for pin in roles.values():
                self._states[pin] = 1

    def setwarnings(self, _enabled):
        pass

    def setmode(self, _mode):
        pass

    def setup(self, pin, _direction, pull_up_down=None):
        del pull_up_down
        with self._lock:
            self._states.setdefault(pin, 1)

    def output(self, pin, value):
        with self._lock:
            self._states[pin] = int(bool(value))

    def input(self, pin):
        with self._lock:
            return self._states[pin]

    def remove_event_detect(self, _pin):
        pass

    def cleanup(self):
        with self._lock:
            for pin in tuple(self._states):
                self._states[pin] = 1

    def set_pressed(self, pin, pressed):
        with self._lock:
            self._states[pin] = 0 if pressed else 1

    def pulse(self, pin, duration=0.12):
        def worker():
            self.set_pressed(pin, True)
            time.sleep(duration)
            self.set_pressed(pin, False)

        threading.Thread(target=worker, daemon=True).start()

    def set_context(self, gallery_active):
        self._gallery_active = bool(gallery_active)

    def handle_pisugar_event(self, event_name):
        """Translate available PiSugar gestures into legacy controls."""
        if event_name == "single":
            role = "preview"
        elif event_name == "double":
            role = "right" if self._gallery_active else "down"
        elif event_name == "long":
            role = "up" if self._gallery_active else "press"
        else:
            return
        pin = self.roles.get(role)
        if pin is not None:
            self.pulse(pin)


class NullSPI:
    """No-op object that keeps the direct-ST7789 code importable."""

    max_speed_hz = 0
    mode = 0
    bits_per_word = 8

    def open(self, *_args):
        pass

    def close(self):
        pass

    def xfer(self, _data):
        return []

    def writebytes(self, _data):
        pass

    def writebytes2(self, _data):
        pass


class WhisplayPWMProxy:
    def __init__(self, backend):
        self.backend = backend

    def set_PWM_frequency(self, _pin, _frequency):
        pass

    def set_PWM_dutycycle(self, _pin, duty):
        brightness = max(0, min(100, round(float(duty) * 100 / 255)))
        self.backend.set_backlight(brightness)

    def stop(self):
        pass


class WhisplayBackend:
    WIDTH = 240
    HEIGHT = 280
    LEGACY_CONTENT_HEIGHT = 240
    LEGACY_CONTENT_Y = (HEIGHT - LEGACY_CONTENT_HEIGHT) // 2
    FRAME_SIZE = WIDTH * HEIGHT * 2
    FEEDBACK_STYLES = {
        "ready": ((0, 120, 35), 0.35),
        "shutter": ((150, 150, 150), 0.16),
        "filter": ((0, 75, 180), 0.20),
        "awb": ((150, 95, 0), 0.24),
        "navigate": ((0, 100, 160), 0.14),
        "gallery": ((0, 130, 110), 0.22),
        "mode": ((110, 30, 160), 0.24),
        "record": ((180, 0, 20), 0.30),
        "delete_pending": ((200, 80, 0), 0.30),
        "delete": ((180, 0, 0), 0.22),
        "saved": ((0, 150, 35), 0.38),
        "error": ((200, 0, 0), 0.55),
    }

    def __init__(self, gpio: VirtualGPIO, capture_pin: int, home: str):
        self._init_shared(gpio, capture_pin, home)
        self.mode = "daemon"
        self.session_token = None
        self.framebuffer_file = None
        self.framebuffer = None
        self.framebuffer_lock = threading.Lock()
        self._register_and_acquire()
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()
        self._start_shared()
        print("Whisplay backend: daemon")

    def _init_shared(self, gpio: VirtualGPIO, capture_pin: int, home: str):
        self.gpio = gpio
        self.capture_pin = capture_pin
        self.home = os.path.abspath(home)
        self.running = True
        self.exit_requested = False
        self._battery_level = None
        self._battery_lock = threading.Lock()
        self._feedback_generation = 0
        self._feedback_lock = threading.Lock()

    def _start_shared(self):
        self._update_battery()
        self._pisugar_thread = threading.Thread(target=self._pisugar_loop, daemon=True)
        self._pisugar_thread.start()

    def _request(self, cmd, payload=None, timeout=2.0):
        body = {"version": 1, "cmd": cmd, "payload": payload or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(WHISPLAY_SOCKET)
            client.sendall((json.dumps(body) + "\n").encode())
            response_line = client.makefile("r").readline().strip()
        if not response_line:
            raise RuntimeError(f"empty response for {cmd}")
        response = json.loads(response_line)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", f"{cmd} failed"))
        return response.get("payload", {})

    def _register_and_acquire(self):
        launch_script = os.path.join(self.home, "run_optocamzero.sh")
        self._request(
            "app.register",
            {
                "app_id": APP_ID,
                "display_name": "Optocam Zero",
                "icon": "OC",
                "launch_command": launch_script,
                "cwd": self.home,
                "env": {
                    "OPTOCAM_HARDWARE": "whisplay",
                    "OPTOCAM_HOME": self.home,
                    "OPTOCAM_WHISPLAY_BACKEND": "daemon",
                },
                # PiSugar single click is the only Home gesture. Whisplay click,
                # double-click and hold all belong to the camera UI.
                "exit_gesture": "none",
                "priority": 50,
                "use_daemon_default_log": True,
                "persist": True,
            },
        )
        deadline = time.monotonic() + 8.0
        last_error = None
        while time.monotonic() < deadline:
            try:
                focus = self._request("app.focus.acquire", {"app_id": APP_ID})
                self.session_token = focus["session_token"]
                fb = self._request(
                    "framebuffer.acquire",
                    {"app_id": APP_ID, "session_token": self.session_token},
                )
                self._attach_framebuffer(fb["buffer_handle"])
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"could not acquire Whisplay foreground: {last_error}")

    def _attach_framebuffer(self, path):
        self._detach_framebuffer()
        self.framebuffer_file = open(path, "r+b")
        self.framebuffer = mmap.mmap(self.framebuffer_file.fileno(), self.FRAME_SIZE)

    def _detach_framebuffer(self):
        with self.framebuffer_lock:
            if self.framebuffer is not None:
                try:
                    self.framebuffer.close()
                except Exception:
                    pass
            if self.framebuffer_file is not None:
                try:
                    self.framebuffer_file.close()
                except Exception:
                    pass
            self.framebuffer = None
            self.framebuffer_file = None

    def draw_rgb565(self, content):
        content = bytes(content)
        legacy_size = self.WIDTH * self.LEGACY_CONTENT_HEIGHT * 2
        if len(content) == self.FRAME_SIZE:
            frame = content
        elif len(content) == legacy_size:
            padding = bytes(self.WIDTH * self.LEGACY_CONTENT_Y * 2)
            frame = padding + content + padding
        else:
            raise ValueError(
                f"expected {self.FRAME_SIZE} or {legacy_size} RGB565 bytes, "
                f"got {len(content)}"
            )
        with self.framebuffer_lock:
            if self.framebuffer is not None:
                self.framebuffer[:] = frame

    def clear(self):
        with self.framebuffer_lock:
            if self.framebuffer is not None:
                self.framebuffer[:] = bytes(self.FRAME_SIZE)

    def set_backlight(self, brightness):
        try:
            self._request("backlight.set", {"brightness": int(brightness)})
        except Exception:
            pass

    def set_rgb(self, r, g, b):
        try:
            self._request("led.set", {"r": int(r), "g": int(g), "b": int(b)})
        except Exception:
            pass

    def feedback(self, kind):
        color, duration = self.FEEDBACK_STYLES.get(kind, ((0, 80, 120), 0.18))
        with self._feedback_lock:
            self._feedback_generation += 1
            generation = self._feedback_generation

        def worker():
            self.set_rgb(*color)
            time.sleep(duration)
            with self._feedback_lock:
                if generation != self._feedback_generation or not self.running:
                    return
            self.set_rgb(0, 0, 0)

        threading.Thread(target=worker, daemon=True).start()

    def get_battery_level(self):
        with self._battery_lock:
            return self._battery_level

    def _event_loop(self):
        while self.running:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(WHISPLAY_SOCKET)
                    request = {
                        "version": 1,
                        "cmd": "events.subscribe",
                        "payload": {"app_id": APP_ID},
                    }
                    client.sendall((json.dumps(request) + "\n").encode())
                    reader = client.makefile("r")
                    if not reader.readline():
                        raise RuntimeError("event subscription rejected")
                    for line in reader:
                        if not self.running:
                            return
                        event = json.loads(line)
                        name = event.get("event")
                        if name == "button_pressed":
                            self.gpio.set_pressed(self.capture_pin, True)
                        elif name == "button_released":
                            self.gpio.set_pressed(self.capture_pin, False)
                        elif name in {"app_exit_requested", "app_focus_revoked"}:
                            self.exit_requested = True
                            self.gpio.set_pressed(self.capture_pin, False)
                            if name == "app_focus_revoked":
                                # PiSugar Home revokes immediately, before the
                                # app's normal cleanup can release focus. Undo a
                                # possible idle dim so the desktop stays visible.
                                self.set_backlight(100)
                                self.session_token = None
                                self._detach_framebuffer()
            except Exception:
                if self.running:
                    time.sleep(0.25)

    def _pisugar_socket(self):
        return next((path for path in PISUGAR_SOCKETS if os.path.exists(path)), None)

    def _pisugar_request(self, command, timeout=0.6):
        path = self._pisugar_socket()
        if not path:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(path)
                client.sendall((command.strip() + "\n").encode())
                return client.recv(4096).decode("utf-8", "replace").strip()
        except Exception:
            return None

    @staticmethod
    def _response_tail(response):
        if not response:
            return ""
        return response.split(":", 1)[1].strip() if ":" in response else response.strip()

    def _pisugar_loop(self):
        next_battery_poll = time.monotonic() + 5.0
        while self.running:
            try:
                with socket.create_connection(
                    (PISUGAR_EVENT_HOST, PISUGAR_EVENT_PORT), timeout=2.0
                ) as client:
                    client.settimeout(0.5)
                    pending = b""
                    print(
                        f"PiSugar TCP events: {PISUGAR_EVENT_HOST}:"
                        f"{PISUGAR_EVENT_PORT}"
                    )
                    while self.running:
                        try:
                            chunk = client.recv(4096)
                            if not chunk:
                                raise ConnectionError("PiSugar TCP stream closed")
                            pending += chunk
                            while b"\n" in pending:
                                raw_line, pending = pending.split(b"\n", 1)
                                event_name = raw_line.decode(
                                    "utf-8", "replace"
                                ).strip().lower()
                                usable = event_name in {"double", "long"}
                                if event_name == "single" and self.mode == "standalone":
                                    usable = True
                                if usable:
                                    print(f"PiSugar {event_name} gesture")
                                    self.gpio.handle_pisugar_event(event_name)
                                # In daemon mode `single` remains the Home event;
                                # standalone mode uses it as preview on/off.
                        except socket.timeout:
                            pass
                        if time.monotonic() >= next_battery_poll:
                            self._update_battery()
                            next_battery_poll = time.monotonic() + 5.0
            except Exception as exc:
                if not self.running:
                    break
                print(f"PiSugar TCP reconnecting: {exc}")
                if time.monotonic() >= next_battery_poll:
                    self._update_battery()
                    next_battery_poll = time.monotonic() + 5.0
                time.sleep(0.5)

    def _update_battery(self):
        response = self._pisugar_request("get battery")
        level = None
        if response:
            try:
                level = max(0, min(100, round(float(self._response_tail(response)))))
            except (TypeError, ValueError):
                level = None
        with self._battery_lock:
            self._battery_level = level

    def _cleanup_shared(self):
        self.running = False
        with self._feedback_lock:
            self._feedback_generation += 1
        self.set_rgb(0, 0, 0)

    def cleanup(self):
        if not self.running:
            return
        self._cleanup_shared()
        if self.session_token:
            # Hand the desktop a lit display even if Optocam happened to be in
            # its idle-dim state when Home was requested.
            self.set_backlight(100)
            try:
                self._request(
                    "app.focus.release",
                    {"app_id": APP_ID, "session_token": self.session_token},
                )
            except Exception:
                pass
        self.session_token = None
        self._detach_framebuffer()


class StandaloneWhisplayBackend(WhisplayBackend):
    """Direct Whisplay HAT backend used only when the daemon is disabled."""

    def __init__(self, gpio: VirtualGPIO, capture_pin: int, home: str):
        self._init_shared(gpio, capture_pin, home)
        self.mode = "standalone"
        runtime_dir = os.getenv("WHISPLAY_RUNTIME_DIR", self.home)
        if runtime_dir not in sys.path:
            sys.path.insert(0, runtime_dir)
        try:
            from whisplay import WhisplayBoard
        except ImportError as exc:
            raise RuntimeError(
                f"Whisplay runtime not found in {runtime_dir}; install/copy whisplay.py first"
            ) from exc
        self.board = WhisplayBoard()
        self.board.on_button_press(lambda: self.gpio.set_pressed(self.capture_pin, True))
        self.board.on_button_release(lambda: self.gpio.set_pressed(self.capture_pin, False))
        self._start_shared()
        print("Whisplay backend: standalone")

    @staticmethod
    def _full_frame(content):
        content = bytes(content)
        legacy_size = (
            WhisplayBackend.WIDTH * WhisplayBackend.LEGACY_CONTENT_HEIGHT * 2
        )
        if len(content) == WhisplayBackend.FRAME_SIZE:
            return content
        if len(content) == legacy_size:
            padding = bytes(
                WhisplayBackend.WIDTH * WhisplayBackend.LEGACY_CONTENT_Y * 2
            )
            return padding + content + padding
        raise ValueError(
            f"expected {WhisplayBackend.FRAME_SIZE} or {legacy_size} "
            f"RGB565 bytes, got {len(content)}"
        )

    def draw_rgb565(self, content):
        self.board.draw_image(0, 0, self.WIDTH, self.HEIGHT, self._full_frame(content))

    def clear(self):
        self.board.fill_screen(0)

    def set_backlight(self, brightness):
        self.board.set_backlight(int(brightness))

    def set_rgb(self, r, g, b):
        self.board.set_rgb(int(r), int(g), int(b))

    def cleanup(self):
        if not self.running:
            return
        self._cleanup_shared()
        self.board.set_backlight(0)
        self.board.cleanup()


def create_backend(gpio, capture_pin, home):
    requested = os.getenv("OPTOCAM_WHISPLAY_BACKEND", "auto").strip().lower()
    if requested not in {"auto", "daemon", "standalone"}:
        raise ValueError(f"invalid OPTOCAM_WHISPLAY_BACKEND: {requested}")
    try:
        if requested == "standalone":
            return StandaloneWhisplayBackend(gpio, capture_pin, home)
        if requested == "daemon" or os.path.exists(WHISPLAY_SOCKET):
            return WhisplayBackend(gpio, capture_pin, home)
        return StandaloneWhisplayBackend(gpio, capture_pin, home)
    except Exception as exc:
        print(f"Whisplay initialization failed: {exc}", file=sys.stderr)
        raise
