#!/usr/bin/env python3
import sys
import time
import os
import signal
import json
_script_start = time.time()
def log(msg):
    sys.stderr.write(f"[{time.time() - _script_start:.2f}s] {msg}\n")
    sys.stderr.flush()
log("Script file started")

# ── Lazy-av shim ──────────────────────────────────────────────────────────────
# picamera2 unconditionally `import av` (PyAV/ffmpeg) for its video encoders,
# which costs ~2.25s at import (loading libavcodec/format/util/swscale). The
# Optocam only does stills + manually-assembled GIFs and never touches a
# picamera2 encoder, so we install a lazy stand-in for `av`: importing it is
# free, and the real ffmpeg libraries only load if something actually accesses
# an attribute (which never happens here). Saves ~2.5s of cold-boot time.
import types as _types
import importlib as _importlib
class _LazyAv(_types.ModuleType):
    def __getattr__(self, name):
        del sys.modules['av']                       # drop the stub…
        real = _importlib.import_module('av')       # …load the genuine PyAV…
        return getattr(real, name)                  # …and delegate.
if 'av' not in sys.modules:
    sys.modules['av'] = _LazyAv('av')
log("lazy-av shim installed")
# ──────────────────────────────────────────────────────────────────────────────

HARDWARE_PROFILE = os.getenv("OPTOCAM_HARDWARE", "original").strip().lower()
WHISPLAY_MODE = HARDWARE_PROFILE == "whisplay"
OPTOCAM_HOME = os.path.abspath(os.getenv(
    "OPTOCAM_HOME",
    os.path.dirname(os.path.abspath(__file__)) if WHISPLAY_MODE else "/home/dkumkum",
))
DISPLAY_ROTATION = int(os.getenv(
    "OPTOCAM_DISPLAY_ROTATION",
    "90" if WHISPLAY_MODE else "0",
))
if DISPLAY_ROTATION not in {0, 90, 180, 270}:
    raise ValueError("OPTOCAM_DISPLAY_ROTATION must be 0, 90, 180, or 270")

# The Whisplay panel is physically 240x280.  Rendering uses the dimensions
# before the final framebuffer rotation, so 90/270-degree modes build a
# 280x240 landscape canvas which becomes a full 240x280 panel frame.
if WHISPLAY_MODE:
    PREVIEW_SIZE = (
        (280, 240) if DISPLAY_ROTATION in {90, 270} else (240, 280)
    )
else:
    PREVIEW_SIZE = (240, 240)
PREVIEW_WIDTH, PREVIEW_HEIGHT = PREVIEW_SIZE

if WHISPLAY_MODE:
    from whisplay_adapter import (
        NullSPI,
        VirtualGPIO,
        WhisplayPWMProxy,
        create_backend,
    )
    GPIO = VirtualGPIO()
    pigpio = None
    spidev = None
    log("Whisplay hardware adapter imported")
else:
    import RPi.GPIO as GPIO
    log("GPIO imported")
    import pigpio
    log("pigpio imported")
    import spidev
    log("spidev imported")
import threading
log("threading imported")

# Preload the two heavy modules that are NOT on the first-preview path —
# picamera2 (~1.2s) and PIL.Image — on a background thread so they overlap with
# numpy + display init on the main thread. picamera2 is only used in main() after
# the join; PIL.Image is only touched by HUD/overlay code (warm-overlays thread),
# which runs well after the join. numpy stays on the main thread because the
# module-level RGB565 LUTs below need it immediately.
Image = None  # populated by the preload thread before any overlay code runs
def _preload_heavy():
    global Image
    import picamera2  # noqa: F401 — populates sys.modules for the instant import in main()
    from PIL import Image as _Image
    Image = _Image
_preload_thread = threading.Thread(target=_preload_heavy, daemon=True)
_preload_thread.start()
log("heavy-preload thread started (picamera2 + PIL)")

log("os imported")
import numpy as np
log("numpy imported")
import gc
import subprocess
log("All imports done")

# ── Early-start readiness waits ───────────────────────────────────────────────
# camera-auto.service starts before udev coldplug finishes, so the heavy imports
# above (numpy, picamera2, PIL) overlap kernel device bring-up instead of running
# serially after it. The trade-off: the device nodes below may not exist yet when
# we reach them, so block here until they do rather than crashing on absent hardware.
def _wait_for_path(path, timeout=30):
    end = time.time() + timeout
    while not os.path.exists(path):
        if time.time() > end:
            log(f"WARN: timed out waiting for {path}")
            return False
        time.sleep(0.02)
    return True

def connect_pigpiod(timeout=30):
    """pigpiod may not be up yet (it also waits on gpiomem). Retry until it is."""
    end = time.time() + timeout
    while True:
        pi = pigpio.pi()
        if pi.connected:
            return pi
        pi.stop()
        if time.time() > end:
            log("WARN: pigpiod connect timed out")
            return pi
        time.sleep(0.1)

if not WHISPLAY_MODE:
    _wait_for_path('/dev/gpiomem')      # RPi.GPIO needs this
    _wait_for_path('/dev/spidev0.0')    # SPI display needs this
    log("hardware nodes ready")
    # Force clean GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    for pin in [21, 20, 5, 26, 13, 6, 19]:
        try:
            GPIO.remove_event_detect(pin)
        except:
            pass
    GPIO.cleanup()
    time.sleep(0.2)
# Pins
RST_PIN = 27
DC_PIN = 25
BL_PIN = 24
BUTTON_CAPTURE = 21
BUTTON_PREVIEW = 20
JOYSTICK_LEFT = 5
JOYSTICK_RIGHT = 26
JOYSTICK_PRESS = 13
JOYSTICK_UP = 6
JOYSTICK_DOWN = 19
# Setup GPIO/display backend.  In Whisplay mode these are virtual input lines;
# whisplay-daemon remains the sole owner of the physical LCD, button and GPIO.
GPIO.setmode(GPIO.BCM)
GPIO.setup(RST_PIN, GPIO.OUT)
GPIO.setup(DC_PIN, GPIO.OUT)
GPIO.setup(BL_PIN, GPIO.OUT)
GPIO.setup(BUTTON_PREVIEW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(BUTTON_CAPTURE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_LEFT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_RIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_PRESS, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_UP, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_DOWN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

_whisplay = None
if WHISPLAY_MODE:
    GPIO.configure_roles(
        capture=BUTTON_CAPTURE,
        preview=BUTTON_PREVIEW,
        left=JOYSTICK_LEFT,
        right=JOYSTICK_RIGHT,
        press=JOYSTICK_PRESS,
        up=JOYSTICK_UP,
        down=JOYSTICK_DOWN,
    )
    _whisplay = create_backend(GPIO, BUTTON_CAPTURE, OPTOCAM_HOME)
    _pi = WhisplayPWMProxy(_whisplay)
    spi = NullSPI()
else:
    _pi = connect_pigpiod()
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 100000000
    spi.mode = 0
    spi.bits_per_word = 8

_pi.set_PWM_frequency(BL_PIN, 1000)
# Keep the backlight OFF until the splash is actually drawn — otherwise it lights
# the uninitialised panel before the splash appears.
_pi.set_PWM_dutycycle(BL_PIN, 0)
# Locks
camera_lock = threading.RLock()
display_lock = threading.Lock()
# Camera configuration cache
class CameraConfigCache:
    def __init__(self, picam2):
        # The existing camera-orientation correction swaps width and height.
        # Request the inverse size so the corrected image matches PREVIEW_SIZE.
        camera_preview_size = (PREVIEW_HEIGHT, PREVIEW_WIDTH)
        self.preview_config = picam2.create_preview_configuration(
            main={"size": camera_preview_size, "format": "RGB888"},
            buffer_count=3,
            queue=False,
            controls={"AfMode": 2, "AfSpeed": 1, "FrameDurationLimits": (100, 25000)}
        )
        self._picam2 = picam2
        self.capture_config = None
        self.gif_config = None

    def get_capture_config(self):
        if self.capture_config is None:
            self.capture_config = self._picam2.create_still_configuration(
                main={"size": (2592, 2592), "format": "RGB888"},
                buffer_count=2
            )
        return self.capture_config

    def get_gif_config(self):
        # 640x640 stream used only while recording a GIF. Built lazily so it
        # never costs anything until the first recording. Continuous AF, frame
        # duration capped so live frames keep flowing during the countdown.
        if self.gif_config is None:
            self.gif_config = self._picam2.create_preview_configuration(
                main={"size": (640, 640), "format": "RGB888"},
                buffer_count=4,
                controls={"AfMode": 2, "AfSpeed": 1, "FrameDurationLimits": (33333, 100000)}
            )
        return self.gif_config
config_cache = None
FONT_PATH = os.path.join(OPTOCAM_HOME, "cmunvt.ttf")

# Shadow cache for preview overlays — only regenerated when value changes
_shadow_cache = {}

# Fully-assembled HUD overlay cache — keyed on the values that define it, so a
# steady scene reuses the composited shadow layer instead of rebuilding it
# (skips ~4 full-frame alpha_composites per preview frame).
_hud_overlay_cache = {"key": None, "img": None}

def make_text_shadow(text, x, y, font):
    from PIL import ImageDraw, ImageFilter
    shadow = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y), text, font=font, fill=(0, 0, 0, 250))
    return shadow.filter(ImageFilter.GaussianBlur(radius=4))

_STANDARD_ISO = [100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600]

AWB_MODES = [
    (1, "Tungsten",    "TNG"),
    (2, "Fluorescent", "FLR"),
    (3, "Indoor",      "IND"),
    (4, "Daylight",    "DAY"),
    (5, "Cloudy",      "CLD"),
]

# (microseconds, display string)
_STANDARD_SHUTTERS = [
    (125, "1/8000"), (156, "1/6400"), (200, "1/5000"), (250, "1/4000"),
    (313, "1/3200"), (400, "1/2500"), (500, "1/2000"), (625, "1/1600"),
    (800, "1/1250"), (1000, "1/1000"), (1250, "1/800"), (1563, "1/640"),
    (2000, "1/500"), (2500, "1/400"), (3125, "1/320"), (4000, "1/250"),
    (5000, "1/200"), (6250, "1/160"), (8000, "1/125"), (10000, "1/100"),
    (12500, "1/80"), (16667, "1/60"), (20000, "1/50"), (25000, "1/40"),
    (33333, "1/30"), (40000, "1/25"), (50000, "1/20"), (66667, "1/15"),
    (100000, "1/10"), (125000, "1/8"), (166667, "1/6"), (200000, "1/5"),
    (250000, "1/4"), (333333, "1/3"), (500000, "1/2"), (1000000, "1\""),
]

def nearest_standard_iso(gain):
    iso = gain * 100
    return min(_STANDARD_ISO, key=lambda s: abs(s - iso))

def nearest_standard_shutter(exp_us):
    return min(_STANDARD_SHUTTERS, key=lambda s: abs(s[0] - exp_us))[1]

def get_cached_shadow(key, text, x, y, font):
    if _shadow_cache.get(key) != text:
        _shadow_cache[key] = text
        _shadow_cache[key + "_img"] = make_text_shadow(text, x, y, font)
    return _shadow_cache[key + "_img"]

# ── Animated centre labels (filter name, "GIF Mode", toasts) ────────────────
# Each label is rendered once into a cached sprite (blurred shadow + white text),
# then composited every frame with an eased fade + slight scale "pop". Kept
# separate from the cached static HUD overlay so it animates without invalidating
# that cache — steady-state cost is unchanged; the extra work only runs while a
# label is on screen.
_centre_msg_sprite_cache = {}

def get_centre_msg_sprite(text):
    if text in _centre_msg_sprite_cache:
        return _centre_msg_sprite_cache[text]
    from PIL import ImageDraw, ImageFilter
    font = load_font(25)
    shadow = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    white  = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    wd = ImageDraw.Draw(white)
    b = wd.textbbox((0, 0), text, font=font)
    mx = (PREVIEW_WIDTH - (b[2] - b[0])) // 2 - b[0]
    my = (PREVIEW_HEIGHT - (b[3] - b[1])) // 2 - b[1]
    ImageDraw.Draw(shadow).text((mx, my), text, font=font, fill=(0, 0, 0, 250))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
    wd.text((mx, my), text, font=font, fill=(255, 255, 255, 255))
    sprite = Image.alpha_composite(shadow, white)
    _centre_msg_sprite_cache[text] = sprite
    return sprite

def _ease_out_back(t, c1=1.9):
    """Ease-out with an overshoot past 1.0 — the bounce (bigger c1 = more bounce)."""
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2

def _ease_centre_msg(age, dur):
    """(alpha, scale): short fade in/out; scale matches the original version —
    a gentle 0.80→1.0 ease-out, no overshoot."""
    A_IN, A_OUT, S_IN = 0.12, 0.18, 0.18
    if age < A_IN:
        alpha = 1 - (1 - age / A_IN) ** 3
    elif age > dur - A_OUT:
        t = max(0.0, (dur - age) / A_OUT)
        alpha = t * t
    else:
        alpha = 1.0
    if age < S_IN:
        t = age / S_IN
        scale = 0.80 + 0.20 * (1 - (1 - t) ** 3)
    else:
        scale = 1.0
    return alpha, scale

def render_centre_msg(base_rgb, text, age, dur):
    """Composite the centre label with a short fade + a scale bounce."""
    a, s = _ease_centre_msg(age, dur)
    sprite = get_centre_msg_sprite(text)
    if abs(s - 1.0) > 0.005:                # handles both shrink (<1) and overshoot (>1)
        sw = max(1, int(PREVIEW_WIDTH * s))
        sh = max(1, int(PREVIEW_HEIGHT * s))
        small = sprite.resize((sw, sh), Image.BILINEAR)
        sprite2 = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
        sprite2.paste(
            small,
            ((PREVIEW_WIDTH - sw) // 2, (PREVIEW_HEIGHT - sh) // 2),
        )
        sprite = sprite2
    if a < 0.999:
        faded = sprite.copy()
        faded.putalpha(sprite.getchannel("A").point(lambda p: int(p * a)))
        sprite = faded
    return Image.alpha_composite(base_rgb.convert("RGBA"), sprite).convert("RGB")

# Filter pill: the slightest bounce when the filter changes (subtle overshoot,
# no opacity change). Pulled out of the cached overlay only while popping.
PILL_POP_DUR = 0.30

def _pill_pop_scale(age):
    t = min(1.0, max(0.0, age / PILL_POP_DUR))
    return 0.90 + 0.10 * _ease_out_back(t)        # 0.90 → ~1.01 → 1.0 (very subtle)

def render_pill_pop(base_rgb, indicator, scale):
    """Composite the filter indicator scaled around its own centre."""
    bbox = indicator.getbbox()
    if bbox is None or abs(scale - 1.0) < 0.005:
        return Image.alpha_composite(base_rgb.convert("RGBA"), indicator).convert("RGB")
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    sw = max(1, int(PREVIEW_WIDTH * scale))
    sh = max(1, int(PREVIEW_HEIGHT * scale))
    resized = indicator.resize((sw, sh), Image.BILINEAR)
    canvas = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    canvas.paste(resized, (int(round(cx * (1 - scale))), int(round(cy * (1 - scale)))))
    return Image.alpha_composite(base_rgb.convert("RGBA"), canvas).convert("RGB")

# AWB label sprite (top-left) — cached per (text, font size). Reused by the
# white-balance transition: the label scale-pops whenever its text changes,
# i.e. on an AWB change and again when the full name collapses to the abbrev.
_awb_sprite_cache = {}
AWB_POP_DUR = 0.30      # gentle, matches the filter pill

def get_awb_sprite(text, font_size):
    key = (text, font_size)
    if key in _awb_sprite_cache:
        return _awb_sprite_cache[key]
    from PIL import ImageDraw, ImageFilter
    font = load_font(font_size)
    shadow = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    white  = Image.new("RGBA", PREVIEW_SIZE, (0, 0, 0, 0))
    wd = ImageDraw.Draw(white)
    b = wd.textbbox((0, 0), text, font=font)
    ax, ay = 15, 15 - b[1]
    ImageDraw.Draw(shadow).text((ax, ay), text, font=font, fill=(0, 0, 0, 250))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
    wd.text((ax, ay), text, font=font, fill=(255, 255, 255, 255))
    sprite = Image.alpha_composite(shadow, white)
    _awb_sprite_cache[key] = sprite
    return sprite

def _awb_pop_scale(age, full):
    t = min(1.0, max(0.0, age / AWB_POP_DUR))
    e = _ease_out_back(t)
    if full:
        return 0.94 + 0.06 * e     # long name: even more subtle
    return 0.90 + 0.10 * e         # abbreviation: gentle, like the filter pill

# Filter indicator circle — cached per filter name, built once on first use
_indicator_cache = {}

def get_filter_indicator(filter_name):
    if filter_name in _indicator_cache:
        return _indicator_cache[filter_name]
    from PIL import ImageDraw, ImageFilter
    # Rendered at SxS supersample then scaled down with LANCZOS so the circle /
    # pill outline is anti-aliased. Cached per filter, so this runs once per
    # filter — the preview loop only ever composites the finished preview layer,
    # so there is no per-frame cost and the fps is unchanged.
    S   = 3
    big_w = PREVIEW_WIDTH * S
    big_h = PREVIEW_HEIGHT * S
    pad = 10 * S
    r   = 17 * S
    cx  = big_w - pad - r
    cy  = pad + r
    font = load_font(24 * S)

    # Build shadow and white layers separately, then composite — same approach as HUD text
    shadow = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    white  = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    wd = ImageDraw.Draw(white)

    # Filters with multi-char labels use a pill; single-char filters use a circle
    _PILL_LABELS = {"B&W": "B&W", "TRI-X": "TX", "Film Standard": "FS"}

    if filter_name in _PILL_LABELS:
        label  = _PILL_LABELS[filter_name]
        tb     = wd.textbbox((0, 0), label, font=font)
        h_pad  = 10 * S
        pill_h = r * 2
        pill_w = (tb[2] - tb[0]) + h_pad * 2
        x1, y0 = big_w - pad, pad
        x0, y1 = x1 - pill_w, y0 + pill_h
        cr  = pill_h // 2
        pcx = (x0 + x1) // 2
        pcy = (y0 + y1) // 2
        tx  = pcx - (tb[0] + tb[2]) // 2
        ty  = pcy - (tb[1] + tb[3]) // 2
        sd.rounded_rectangle([x0, y0, x1, y1], radius=cr, outline=(0, 0, 0, 250), width=2 * S)
        sd.text((tx, ty), label, font=font, fill=(0, 0, 0, 250))
        wd.rounded_rectangle([x0, y0, x1, y1], radius=cr, outline=(255, 255, 255, 255), width=1 * S)
        wd.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))
    else:
        label = filter_name[0]
        tb = wd.textbbox((0, 0), label, font=font)
        tx = cx - (tb[0] + tb[2]) // 2
        ty = cy - (tb[1] + tb[3]) // 2
        if label in ("D", "P", "L", "N"):
            tx += S
        sd.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(0, 0, 0, 250), width=2 * S)
        sd.text((tx, ty), label, font=font, fill=(0, 0, 0, 250))
        wd.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 255, 255, 255), width=1 * S)
        wd.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4 * S))
    layer = Image.alpha_composite(shadow, white).resize(
        PREVIEW_SIZE, Image.LANCZOS
    )
    _indicator_cache[filter_name] = layer
    return layer

# "GIF" badge shown while GIF mode is armed, with a marching-dashes outline.
# A small set of phase variants is pre-rendered once (each with its own blur),
# then cycled per frame — so the animation costs nothing extra at draw time.
_GIF_PILL_PHASES   = 2     # number of pre-rendered dash offsets (one loop)
_GIF_PILL_MARCH_HZ = 4     # phases advanced per second → marching speed
_gif_indicator_phases = None

def _trace_capsule_perimeter(x0, y0, x1, y1, cr, step=1.5):
    """Ordered points around a horizontal capsule (pill) perimeter, used to lay
    dashes along the outline."""
    import math
    pts = []
    xa, xb = x0 + cr, x1 - cr
    cyc = y0 + cr
    d = xa                                   # top edge, left → right
    while d <= xb:
        pts.append((d, y0)); d += step
    a = -math.pi / 2                         # right end semicircle
    while a <= math.pi / 2:
        pts.append((xb + cr * math.cos(a), cyc + cr * math.sin(a))); a += step / cr
    d = xb                                   # bottom edge, right → left
    while d >= xa:
        pts.append((d, y1)); d -= step
    a = math.pi / 2                          # left end semicircle
    while a <= 3 * math.pi / 2:
        pts.append((xa + cr * math.cos(a), cyc + cr * math.sin(a))); a += step / cr
    return pts

def _draw_dashed_path(draw, pts, phase, dash, gap, width, fill):
    """Draw a dashed line along an ordered, closed list of points. `phase` shifts
    the dash pattern along the path (marching ants)."""
    import math
    period = dash + gap
    acc = 0.0
    for p, q in zip(pts, pts[1:] + pts[:1]):
        if ((acc + phase) % period) < dash:
            draw.line([p, q], fill=fill, width=width)
        acc += math.hypot(q[0] - p[0], q[1] - p[1])

def _build_gif_indicator_phases():
    # Rendered at SxS supersample then scaled down with LANCZOS so the dashes
    # come out smooth and anti-aliased instead of jagged. One-time cost.
    from PIL import ImageDraw, ImageFilter
    S   = 3
    big_w = PREVIEW_WIDTH * S
    big_h = PREVIEW_HEIGHT * S
    font = load_font(24 * S)
    label = "GIF"
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tb = tmp.textbbox((0, 0), label, font=font)
    # Sits directly under the filter pill (top-right), sharing its right edge.
    pad    = 10 * S
    pill_h = 34 * S
    pill_w = (tb[2] - tb[0]) + 20 * S
    x1 = big_w - pad
    x0 = x1 - pill_w
    y0 = pad + pill_h + 8 * S
    y1 = y0 + pill_h
    cr = pill_h // 2
    tx = x0 + (pill_w - (tb[0] + tb[2])) // 2
    ty = y0 + (pill_h - (tb[1] + tb[3])) // 2
    pts = _trace_capsule_perimeter(x0, y0, x1, y1, cr, step=2.0)
    dash, gap = 6 * S, 5 * S
    period = dash + gap
    layers = []
    for k in range(_GIF_PILL_PHASES):
        phase = period * k / _GIF_PILL_PHASES   # full pattern shift over one loop
        shadow = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
        white  = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        wd = ImageDraw.Draw(white)
        _draw_dashed_path(sd, pts, phase, dash, gap, 3 * S, (0, 0, 0, 250))
        _draw_dashed_path(wd, pts, phase, dash, gap, 2 * S, (255, 255, 255, 255))
        sd.text((tx, ty), label, font=font, fill=(0, 0, 0, 250))
        wd.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4 * S))
        layer = Image.alpha_composite(shadow, white).resize(
            PREVIEW_SIZE, Image.LANCZOS
        )
        layers.append(layer)
    return layers

def get_gif_mode_indicator(phase_index=0):
    global _gif_indicator_phases
    if _gif_indicator_phases is None:
        _gif_indicator_phases = _build_gif_indicator_phases()
    return _gif_indicator_phases[phase_index % _GIF_PILL_PHASES]

_font_cache = {}  # v2: cache loaded fonts so TTF isn't re-opened every frame
_font_warned = False
def load_font(size):
    global _font_warned
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont
    try:
        _font_cache[size] = ImageFont.truetype(FONT_PATH, size)
    except (OSError, IOError) as e:
        # Bitmap default has no TTF dependency and will always load; surface
        # the reason once so silent fallbacks don't hide misconfiguration
        # (e.g. Pillow built without freetype, or the TTF missing on disk).
        if not _font_warned:
            log(f"TTF load failed ({FONT_PATH}): {e!r} — using bitmap default")
            _font_warned = True
        _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]

# ── Filters ────────────────────────────────────────────────────────────────
FILTERS = ["Film Standard", "Punch", "B&W", "Deep", "Sand", "Eterna", "TRI-X", "Cutout", "No Filter"]
SETTINGS_PATH = os.path.join(OPTOCAM_HOME, "settings.json")

def _default_settings():
    return {
        "filter": "Film Standard",
        "awb": "Daylight",
        "gif_mode": False,
    }

def load_settings():
    settings = _default_settings()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            settings.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"WARN: failed to load settings: {exc}")
    return settings

def save_settings():
    data = {
        "filter": FILTERS[filter_index],
        "awb": AWB_MODES[awb_mode_index][1],
        "gif_mode": bool(gif_mode),
    }
    tmp_path = SETTINGS_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp_path, SETTINGS_PATH)
    except Exception as exc:
        log(f"WARN: failed to save settings: {exc}")

def _settings_index(values, selected, default_index):
    try:
        return values.index(selected)
    except ValueError:
        return default_index

_SETTINGS = load_settings()

def _make_lut(points):
    x = [p[0] for p in points]
    y = [p[1] for p in points]
    return np.interp(np.arange(256), x, y).clip(0, 255).astype(np.uint8)

# Base tone curves
_BASE_CURVES = {
    "B&W":        _make_lut([(0,0),(64,16),(128,160),(192,242),(255,255)]),   # steeper S — deeper blacks, brighter whites
    "Punch":      _make_lut([(0,0),(64,52),(128,148),(192,212),(255,242)]),   # pure blacks, highlights pulled down
    "Sand":       _make_lut([(0,0),(64,50),(128,132),(192,205),(255,255)]),   # gentle S for sepia
    "Deep":       _make_lut([(0,30),(64,70),(128,152),(192,222),(255,255)]),
    "Eterna":        _make_lut([(0,30),(64,78),(128,128),(192,172),(255,215)]),  # very flat, heavy lifted blacks, compressed highlights
    "Film Standard": _make_lut([(0,18),(64,55),(128,140),(192,210),(255,252)]),  # Classic Neg: lifted blacks, strong S, compressed highlights
}

# Channel LUTs — built manually per filter
_CHANNEL_LUTS = {}
_v = np.arange(256, dtype=np.float32)

# B&W — all channels identical, ISP handles grayscale via Saturation=0
_CHANNEL_LUTS["B&W"] = (
    _BASE_CURVES["B&W"],
    _BASE_CURVES["B&W"],
    _BASE_CURVES["B&W"],
)

# Punch — pure blacks, R/G slight boost, B shadow blue fades to zero at midtones
_pc = _BASE_CURVES["Punch"].astype(np.float32)
_punch_shadow_blue = np.maximum(0.0, 65.0 * (1.0 - _v / 105.0))
_CHANNEL_LUTS["Punch"] = (
    np.clip(_pc * 1.05, 0, 255).astype(np.uint8),
    np.clip(_pc * 1.02, 0, 255).astype(np.uint8),
    np.clip(_pc + _punch_shadow_blue, 0, 255).astype(np.uint8),
)

# Sand — toned-down sepia: warm brown tint, image structure preserved
_sc = _BASE_CURVES["Sand"].astype(np.float32)
_CHANNEL_LUTS["Sand"] = (
    np.clip(_sc * 1.08, 0, 255).astype(np.uint8),   # R: subtle warm boost
    np.clip(_sc * 0.92, 0, 255).astype(np.uint8),   # G: slight pullback
    np.clip(_sc * 0.55, 0, 255).astype(np.uint8),   # B: reduced but not crushed → softer sepia
)

# Deep — Matrix-style blue tint: image stays recognisable, blue pervades everything
# Colours desaturated via ISP, blue channel boosted, R/G pulled back
_dc = _BASE_CURVES["Deep"].astype(np.float32)
_CHANNEL_LUTS["Deep"] = (
    np.clip(_dc * 0.55, 0, 255).astype(np.uint8),   # R: heavily pulled back
    np.clip(_dc * 0.70, 0, 255).astype(np.uint8),   # G: pulled back
    np.clip(_dc * 1.35, 0, 255).astype(np.uint8),   # B: strongly boosted
)

# Eterna — Fuji cinema film: flat, cool, muted — maximum latitude look
_et = _BASE_CURVES["Eterna"].astype(np.float32)

_CHANNEL_LUTS["Eterna"] = (
    np.clip(_et * 0.96, 0, 255).astype(np.uint8),   # R: slightly pulled — cool cast
    np.clip(_et * 1.00, 0, 255).astype(np.uint8),   # G: neutral
    np.clip(_et * 1.05, 0, 255).astype(np.uint8),   # B: slight boost — cinematic cool
)

# Film Standard — Fuji Classic Neg: muted reds, teal shadows, cool-neutral character
_fs = _BASE_CURVES["Film Standard"].astype(np.float32)
_CHANNEL_LUTS["Film Standard"] = (
    np.clip(_fs * 0.95, 0, 255).astype(np.uint8),   # R: pulled — muted warm tones
    np.clip(_fs * 1.02, 0, 255).astype(np.uint8),   # G: slight boost — natural greens
    np.clip(_fs * 1.08, 0, 255).astype(np.uint8),   # B: boosted — teal/cool shadow character
)

# Cutout — hard 3-level posterisation: black / mid-grey / white, no smooth gradients
_co = np.zeros(256, dtype=np.uint8)
_co[65:130] = 128    # mid-grey band
_co[130:]   = 255    # white
_CHANNEL_LUTS["Cutout"] = (_co, _co, _co)


# TRI-X — Kodak tritone: black shadows → golden-yellow mids → green highlights
# Colours sampled from the Kodak TRI-X Pan 135-36 box
_TRITON_SHADOW    = np.array([0,   0,   0],   dtype=np.float32)
_TRITON_MID       = np.array([242, 183,  8],  dtype=np.float32)  # Kodak yellow
_TRITON_HIGHLIGHT = np.array([35,  155, 60],  dtype=np.float32)  # Kodak green
_trix_lut = np.zeros((256, 3), dtype=np.float32)
_TRIX_BLACK = 0.18   # hold pure black below this luma
_TRIX_SPLIT = 0.38   # below split: black→yellow   above: yellow→green
for _i in range(256):
    _t = _i / 255.0
    if _t <= _TRIX_BLACK:
        _trix_lut[_i] = _TRITON_SHADOW
    elif _t <= _TRIX_SPLIT:
        _t2 = (_t - _TRIX_BLACK) / (_TRIX_SPLIT - _TRIX_BLACK)
        _trix_lut[_i] = _TRITON_SHADOW + (_TRITON_MID - _TRITON_SHADOW) * _t2
    else:
        _t2 = (_t - _TRIX_SPLIT) / (1.0 - _TRIX_SPLIT)
        _trix_lut[_i] = _TRITON_MID + (_TRITON_HIGHLIGHT - _TRITON_MID) * _t2
_TRIX_LUT = np.clip(_trix_lut, 0, 255).astype(np.uint8)   # shape (256, 3)

# ── Grain tables ──────────────────────────────────────────────────────────────
# Pre-generate one 1024×1024 int16 table per intensity at startup.
# Per-apply cost: two arange calls + modulo indexing — no RNG at save time.
_GRAIN_TABLE_SIZE = 1024
_grain_tables: dict = {}
_grain_rng = np.random.default_rng(0)  # fixed seed → reproducible tables

def _get_grain_table(intensity: int) -> np.ndarray:
    if intensity not in _grain_tables:
        _grain_tables[intensity] = _grain_rng.integers(
            -intensity, intensity + 1,
            (_GRAIN_TABLE_SIZE, _GRAIN_TABLE_SIZE),
            dtype=np.int16,
        )
    return _grain_tables[intensity]

def _apply_grain(arr: np.ndarray, intensity: int) -> np.ndarray:
    """Apply tiled grain to a uint8 H×W×3 array at any resolution."""
    h, w = arr.shape[:2]
    table = _get_grain_table(intensity)
    dy = np.random.randint(0, _GRAIN_TABLE_SIZE)
    dx = np.random.randint(0, _GRAIN_TABLE_SIZE)
    rows = (np.arange(dy, dy + h) % _GRAIN_TABLE_SIZE)[:, np.newaxis]  # (H,1)
    cols = (np.arange(dx, dx + w) % _GRAIN_TABLE_SIZE)[np.newaxis, :]  # (1,W)
    grain = table[rows, cols]                                           # (H,W)
    arr16 = arr.astype(np.int16)
    arr16 += grain[:, :, np.newaxis]   # broadcast same grain to all channels
    return np.clip(arr16, 0, 255).astype(np.uint8)
# ──────────────────────────────────────────────────────────────────────────────

# Grain — all filters at 22, Normal has none
_GRAIN = {"B&W": 27, "Punch": 22, "Sand": 22, "Deep": 22, "Cutout": 22, "TRI-X": 15, "Eterna": 15, "Film Standard": 18}

# ISP saturation — applied once on filter change, free every frame
_FILM_ISP = {
    "No Filter": {"Saturation": 1.0, "Contrast": 1.0, "Brightness": 0.0},
    "B&W":    {"Saturation": 0.0, "Contrast": 1.0, "Brightness": 0.0},
    "Punch":  {"Saturation": 1.3, "Contrast": 1.0, "Brightness": -0.05},
    "Sand":   {"Saturation": 0.45,"Contrast": 1.0, "Brightness": 0.0},
    "Deep":   {"Saturation": 0.6, "Contrast": 1.0, "Brightness": 0.0},
    "Cutout": {"Saturation": 0.0, "Contrast": 1.0, "Brightness": 0.0},
    "TRI-X":       {"Saturation": 1.0, "Contrast": 1.0, "Brightness": 0.0},
    "Eterna":        {"Saturation": 0.75, "Contrast": 1.0, "Brightness": 0.0},
    "Film Standard": {"Saturation": 0.85, "Contrast": 1.0, "Brightness": 0.0},
}

def _apply_filter_by_name(image, name, apply_grain=True):
    """Tone curve + channel shifts in numpy. Saturation handled by ISP hardware."""
    if name == "No Filter":
        return image
    arr = np.array(image, dtype=np.uint8)
    if name == "TRI-X":
        # Luma-based tritone mapping
        luma = (arr[:, :, 0].astype(np.uint32) * 299 +
                arr[:, :, 1].astype(np.uint32) * 587 +
                arr[:, :, 2].astype(np.uint32) * 114 + 500) // 1000
        arr = _TRIX_LUT[luma.clip(0, 255).astype(np.uint8)]  # (H, W, 3)
    elif name == "Cutout":
        # PIL's convert('L') uses optimised C — avoids three large uint32 intermediate arrays
        p = _co[np.array(image.convert('L'), dtype=np.uint8)]
        arr = np.stack([p, p, p], axis=2)
    else:
        r_lut, g_lut, b_lut = _CHANNEL_LUTS[name]
        arr[:, :, 0] = r_lut[arr[:, :, 0]]
        arr[:, :, 1] = g_lut[arr[:, :, 1]]
        arr[:, :, 2] = b_lut[arr[:, :, 2]]
    grain = _GRAIN.get(name, 0) if apply_grain else 0
    if grain:
        arr = _apply_grain(arr, grain)
    return Image.fromarray(arr)

def apply_filter(image):
    return _apply_filter_by_name(image, FILTERS[filter_index], apply_grain=False)
# ──────────────────────────────────────────────────────────────────────────
def send_command(cmd):
    GPIO.output(DC_PIN, GPIO.LOW)
    spi.xfer([cmd])
def send_data(data):
    GPIO.output(DC_PIN, GPIO.HIGH)
    # v3: one transfer for a whole 240x240x2 (115200B) frame instead of ~29
    # fragmented 4KB transfers — far less tearing. Requires spidev.bufsiz to be
    # raised in cmdline.txt (default 4096) so the kernel doesn't split it.
    chunk_size = 131072
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        try:
            spi.writebytes2(chunk)
        except AttributeError:
            spi.writebytes(chunk)
def init_display():
    """Display initialization with improved black levels and contrast"""
    if WHISPLAY_MODE:
        _whisplay.clear()
        return
    print("Initializing display (enhanced blacks)...")
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.05)
    GPIO.output(RST_PIN, GPIO.LOW)
    time.sleep(0.05)
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.08)
    init_commands = [
        (0x36, [0x70]),
        (0x3A, [0x05]),
        (0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33]),
        (0xB7, [0x35]),
        (0xBB, [0x35]),
        (0xC0, [0x2C]),
        (0xC2, [0x01]),
        (0xC3, [0x13]),
        (0xC4, [0x20]),
        (0xC6, [0x0F]),
        (0xD0, [0xA4, 0xA1]),
        (0xE0, [0xF0, 0x00, 0x04, 0x04, 0x04, 0x05, 0x29, 0x33, 0x3E, 0x38, 0x12, 0x12, 0x28, 0x30]),
        (0xE1, [0xF0, 0x07, 0x0A, 0x0D, 0x0B, 0x07, 0x28, 0x33, 0x3E, 0x36, 0x14, 0x14, 0x29, 0x32]),
        (0x21, []),
        (0x11, []),
    ]
    for cmd, data in init_commands:
        send_command(cmd)
        if data:
            GPIO.output(DC_PIN, GPIO.HIGH)
            spi.xfer(data)
        if cmd == 0x11:
            time.sleep(0.08)
    send_command(0x29)
def set_backlight(state):
    _pi.set_PWM_dutycycle(BL_PIN, 255 if state else 0)
def set_backlight_brightness(pct):
    _pi.set_PWM_dutycycle(BL_PIN, int(pct * 2.55))
def clear_display():
    if WHISPLAY_MODE:
        _whisplay.clear()
        return
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.xfer([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.xfer([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(bytearray(240 * 240 * 2))
# v2: precomputed LUT replaces per-frame float32 contrast math
# Equivalent to: (x - 128) * 1.15 + 123  (same as original: *1.15 + 128 - 5)
_CONTRAST_LUT = np.clip(
    (np.arange(256, dtype=np.float32) - 128) * 1.15 + 123,
    0, 255
).astype(np.uint8)

# v3: fold the contrast curve AND the RGB565 mask/shift into three per-channel
# uint16 LUTs, so each frame is three lookups + two ORs instead of a contrast
# gather followed by uint16 masking/shifting. Bit-identical output, ~30% faster.
_c16 = _CONTRAST_LUT.astype(np.uint16)
_R565_LUT = ((_c16 & 0xF8) << 8).astype(np.uint16)
_G565_LUT = ((_c16 & 0xFC) << 3).astype(np.uint16)
_B565_LUT = ((_c16 & 0xF8) >> 3).astype(np.uint16)

def _rotate_display_array(pixels):
    """Rotate framebuffer pixels clockwise by the configured angle."""
    if DISPLAY_ROTATION == 90:
        return np.rot90(pixels, k=-1)
    if DISPLAY_ROTATION == 180:
        return np.rot90(pixels, k=2)
    if DISPLAY_ROTATION == 270:
        return np.rot90(pixels, k=1)
    return pixels

def convert_to_rgb565(image):
    """RGB565 conversion with enhanced contrast, via fused per-channel LUTs."""
    a = np.asarray(image, dtype=np.uint8)
    rgb565 = _R565_LUT[a[:, :, 0]] | _G565_LUT[a[:, :, 1]] | _B565_LUT[a[:, :, 2]]
    if WHISPLAY_MODE:
        rgb565 = _rotate_display_array(rgb565)
    return rgb565.astype('>u2').tobytes()

def _rotate_rgb565_bytes(data):
    """Rotate the pre-converted 240x240 splash in Whisplay mode."""
    if not WHISPLAY_MODE or DISPLAY_ROTATION == 0:
        return data
    expected = 240 * 240 * 2
    if len(data) != expected:
        return data
    pixels = np.frombuffer(data, dtype='>u2').reshape((240, 240))
    return _rotate_display_array(pixels).astype('>u2').tobytes()

def _battery_label():
    """Return plain percentage text for the preview HUD."""
    if not WHISPLAY_MODE or _whisplay is None:
        return None
    level = _whisplay.get_battery_level()
    if level is None:
        return None
    return f"BAT {max(0, min(100, int(level)))}"

def hardware_feedback(kind):
    if WHISPLAY_MODE and _whisplay is not None:
        _whisplay.feedback(kind)

def display_image(image):
    if WHISPLAY_MODE:
        with display_lock:
            _whisplay.draw_rgb565(convert_to_rgb565(image))
        return
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(convert_to_rgb565(image))
def overlay_capture_dot(base_image):
    """Capture indicator dot"""
    img_array = np.array(base_image)
    dot_center = (220, 20)
    dot_radius = 8
    y, x = np.ogrid[:240, :240]
    mask = (x - dot_center[0])**2 + (y - dot_center[1])**2 <= dot_radius**2
    img_array[mask] = [0, 255, 0]
    border = ((x - dot_center[0])**2 + (y - dot_center[1])**2 <= (dot_radius+1)**2) & \
             ((x - dot_center[0])**2 + (y - dot_center[1])**2 > (dot_radius-1)**2)
    img_array[border] = [255, 255, 255]
    return Image.fromarray(img_array)
def show_splash():
    """Display pre-converted RGB565 splash image directly to display"""
    splash_path = os.path.join(OPTOCAM_HOME, "splash.raw")
    if not os.path.exists(splash_path):
        return
    with open(splash_path, "rb") as f:
        data = f.read()
    if WHISPLAY_MODE:
        with display_lock:
            _whisplay.draw_rgb565(_rotate_rgb565_bytes(data))
        return
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(data)

def show_transfer_mode_screen():
    """Display transfer mode info screen on device"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 240), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_title = load_font(17)
    font_label = load_font(16)
    font_value = load_font(20)
    font_hint  = load_font(18)

    # Check how many devices are connected to the hotspot
    try:
        result = subprocess.run(["iw", "dev", "uap0", "station", "dump"],
                                capture_output=True, text=True, timeout=1)
        device_count = result.stdout.count("Station ")
        device_connected = device_count > 0
    except Exception:
        device_count = 0
        device_connected = False

    # Blink when no device connected (0.5s on/off), solid when connected
    dot_visible = device_connected or (int(time.time() * 2) % 2 == 0)
    dot_color   = (60, 200, 80) if device_connected else (90, 90, 90)
    dot_r = 6

    # Title — left-aligned, dot on right aligned with text
    title = "Transfer Mode"
    tb = draw.textbbox((0, 0), title, font=font_title)
    title_y = 13
    draw.text((20, title_y), title, font=font_title, fill=(160, 160, 160))
    dot_cy = title_y + (tb[1] + tb[3]) // 2
    dot_cx = 214
    if dot_visible:
        draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=dot_color)

    # Device count — shown only when devices are connected, left of the dot
    if device_connected:
        count_str = str(device_count)
        cb = draw.textbbox((0, 0), count_str, font=font_title)
        count_w = cb[2] - cb[0]
        count_x = dot_cx - dot_r - count_w - 8
        count_y = dot_cy - (cb[3] - cb[1]) // 2 - cb[1]
        draw.text((count_x, count_y), count_str, font=font_title, fill=dot_color)

    draw.line([(8, 43), (232, 43)], fill=(40, 40, 40), width=1)

    # WiFi
    draw.text((20, 51),  "WiFi",         font=font_label, fill=(100, 100, 100))
    draw.text((20, 69),  "Optocam Zero", font=font_value, fill=(255, 255, 255))

    # Password
    draw.text((20, 99),  "Password",     font=font_label, fill=(100, 100, 100))
    draw.text((20, 117), "0026opto",     font=font_value, fill=(255, 255, 255))

    # Browser
    draw.text((20, 147), "Browser",      font=font_label, fill=(100, 100, 100))
    draw.text((20, 165), "192.168.4.1",  font=font_value, fill=(255, 255, 255))

    draw.line([(8, 197), (232, 197)], fill=(40, 40, 40), width=1)

    # Hint — properly centred in bottom gap (197→240)
    hint = "Hold center to exit"
    hb = draw.textbbox((0, 0), hint, font=font_hint)
    hint_h = hb[3] - hb[1]
    hint_y = 197 + (43 - hint_h) // 2 - hb[1]
    draw.text(((240 - (hb[2] - hb[0])) // 2, hint_y), hint, font=font_hint, fill=(60, 60, 60))

    display_image(img)

GALLERY_DIR = os.path.join(OPTOCAM_HOME, "photos")

_capture_counter = None
_capture_counter_lock = threading.Lock()

def _capture_number_of(filename):
    """Extract the numeric id from an Optocamzero_<n>.<ext> name, or None."""
    if not filename.startswith("Optocamzero_"):
        return None
    stem = filename[len("Optocamzero_"):]
    dot = stem.rfind(".")
    if dot == -1:
        return None
    num = stem[:dot]
    ext = stem[dot:].lower()
    if ext in (".jpg", ".gif") and num.isdigit():
        return int(num)
    return None

def get_next_capture_number():
    """Return next available number using an in-memory counter.
    Scans filesystem only once on first call — rapid shots never collide.
    Numbering is shared across photos (.jpg) and GIFs (.gif) so ids never
    collide between the two media types."""
    global _capture_counter
    with _capture_counter_lock:
        if _capture_counter is None:
            try:
                numbers = [
                    n for f in os.listdir(GALLERY_DIR)
                    if (n := _capture_number_of(f)) is not None
                ] if os.path.exists(GALLERY_DIR) else []
                _capture_counter = max(numbers) + 1 if numbers else 1
            except:
                _capture_counter = 1
        num = _capture_counter
        _capture_counter += 1
        return num

def get_gallery_images():
    """Return list of captured image paths sorted by number"""
    try:
        if not os.path.exists(GALLERY_DIR):
            print("Gallery dir not found")
            return []
        files = [
            os.path.join(GALLERY_DIR, f)
            for f in os.listdir(GALLERY_DIR)
            if _capture_number_of(f) is not None
        ]
        files.sort(key=lambda f: _capture_number_of(os.path.basename(f)))
        print(f"Found {len(files)} images")
        return files
    except Exception as e:
        print(f"Gallery scan error: {e}")
        return []
def display_gallery_image(filepath, index, total, confirm_delete=False):
    """Load and display a gallery image using draft mode for fast decoding"""
    try:
        from PIL import ImageDraw, ImageFilter
        img = Image.open(filepath)
        img.draft("RGB", (240, 240))
        img = img.convert("RGB")
        img = img.resize((240, 240), Image.BILINEAR)

        # Counter bottom-left with 15px padding
        font = load_font(25)
        text = f"{index}/{total}"
        draw = ImageDraw.Draw(img)
        bbox_t = draw.textbbox((0, 0), text, font=font)
        text_h = bbox_t[3] - bbox_t[1]
        x, y = 15, 240 - 15 - text_h
        shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((x, y), text, font=font, fill=(0, 0, 0, 250))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, shadow)
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.text((x, y), text, font=font, fill=(255, 255, 255))

        # Delete confirmation dialog
        if confirm_delete:
            overlay = Image.new("RGBA", (240, 240), (0, 0, 0, 160))
            img = img.convert("RGBA")
            img = Image.alpha_composite(img, overlay)
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            font_dialog = load_font(25)
            font_yes = font_dialog
            # "Delete?" centered
            t1 = "Delete?"
            b1 = draw.textbbox((0, 0), t1, font=font_dialog)
            draw.text(((240 - (b1[2] - b1[0])) // 2, 80), t1, font=font_dialog, fill=(255, 255, 255))
            # "YES: [arrow]" centered as a block
            t2 = "YES: "
            b2 = draw.textbbox((0, 0), t2, font=font_yes)
            text_w = b2[2] - b2[0]
            text_h = b2[3] - b2[1]
            arrow_w = 14
            bx = (240 - text_w - arrow_w) // 2
            by = 118
            draw.text((bx, by), t2, font=font_yes, fill=(255, 255, 255))
            ax = bx + text_w
            mid_y = by + (b2[1] + b2[3]) // 2
            draw.polygon([(ax + 7, mid_y - 8), (ax, mid_y + 6), (ax + 14, mid_y + 6)], fill=(255, 255, 255))
            # "NO: Any Button" centered below in gray
            t3 = "NO: Any Button"
            b3 = draw.textbbox((0, 0), t3, font=font_yes)
            draw.text(((240 - (b3[2] - b3[0])) // 2, by + text_h + 10), t3, font=font_yes, fill=(180, 180, 180))

        display_image(img)
    except Exception as e:
        print(f"Gallery load error: {e}")

def _build_gif_static_overlay(index, total):
    """Pre-blurred RGBA overlay (240x240) shared by every frame of a GIF: the
    'GIF' label (top-left) and the position counter (bottom-left). Built once so
    the expensive GaussianBlur runs a single time per GIF instead of per frame."""
    from PIL import ImageDraw, ImageFilter
    font_lbl = load_font(25)
    shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    white  = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    wd = ImageDraw.Draw(white)

    # "GIF" — top-left
    bl = wd.textbbox((0, 0), "GIF", font=font_lbl)
    lx, ly = 15, 15 - bl[1]
    sd.text((lx, ly), "GIF", font=font_lbl, fill=(0, 0, 0, 250))
    wd.text((lx, ly), "GIF", font=font_lbl, fill=(255, 255, 255, 255))

    # position counter — bottom-left
    pos = f"{index}/{total}"
    bp = wd.textbbox((0, 0), pos, font=font_lbl)
    px, py = 15, 240 - 15 - (bp[3] - bp[1])
    sd.text((px, py), pos, font=font_lbl, fill=(0, 0, 0, 250))
    wd.text((px, py), pos, font=font_lbl, fill=(255, 255, 255, 255))

    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
    return Image.alpha_composite(shadow, white)

def _overlay_gif_frame(rgb_frame, static, font_fc, i, n):
    """Composite the shared static overlay + the per-frame counter (top-right)
    onto one already-resized 240x240 RGB frame."""
    from PIL import ImageDraw
    f = Image.alpha_composite(rgb_frame.convert("RGBA"), static).convert("RGB")
    # Frame counter — cheap offset shadow (dark grey, not pure black, so it
    # blends with the blurred shadows on the other labels)
    fc = f"{i+1}/{n}"
    d = ImageDraw.Draw(f)
    b = d.textbbox((0, 0), fc, font=font_fc)
    x, y = 240 - 15 - b[2], 15 - b[1]
    d.text((x + 1, y + 1), fc, font=font_fc, fill=(60, 60, 60))
    d.text((x, y), fc, font=font_fc, fill=(255, 255, 255))
    return f

# Incremented on every gallery navigation; a background decode thread bails out
# as soon as its token is stale (i.e. the user moved to another image).
_gif_load_token = 0

def start_gif_gallery_load(filepath, index, total):
    """Decode and overlay frame 0 synchronously (fast — one frame), then decode
    the rest in a background thread, appending into the SAME returned list so the
    gallery can start playing immediately instead of stalling ~0.8s on the whole
    GIF. Returns the frames list (with frame 0 already in it)."""
    global _gif_load_token
    _gif_load_token += 1
    token = _gif_load_token
    frames = []
    try:
        gif = Image.open(filepath)
        n = getattr(gif, "n_frames", 1)
        static = _build_gif_static_overlay(index, total)
        font_fc = load_font(22)
        gif.seek(0)
        frames.append(_overlay_gif_frame(
            gif.convert("RGB").resize((240, 240), Image.BILINEAR), static, font_fc, 0, n))

        def decode_rest():
            try:
                g = Image.open(filepath)
                for i in range(1, n):
                    if token != _gif_load_token:
                        return   # user navigated away — stop wasting work
                    g.seek(i)
                    frames.append(_overlay_gif_frame(
                        g.convert("RGB").resize((240, 240), Image.BILINEAR), static, font_fc, i, n))
            except Exception as e:
                print(f"GIF bg decode error: {e}")

        if n > 1:
            threading.Thread(target=decode_rest, daemon=True).start()
    except Exception as e:
        print(f"GIF gallery load error: {e}")
    return frames

def _save_image_async(captured_image, filepath, filename, film_name="No Filter"):
    """Save and verify image in background — runs after preview has already resumed"""
    global saving_active
    try:
        start = time.time()
        captured_image = _apply_filter_by_name(captured_image, film_name)
        captured_image.save(filepath, "JPEG", quality=98, optimize=True)
        # fsync only this file, not the whole filesystem like os.sync()
        with open(filepath, "rb") as f:
            os.fsync(f.fileno())

        if not os.path.exists(filepath):
            print("✗ File not created")
            hardware_feedback("error")
            return

        file_size = os.path.getsize(filepath)
        if file_size < 100000:
            print(f"✗ File too small ({file_size} bytes) - DELETING")
            try:
                os.remove(filepath)
            except:
                pass
            hardware_feedback("error")
            return

        try:
            test_img = Image.open(filepath)
            test_img.verify()
            test_img.close()
        except Exception as e:
            print(f"✗ Corrupted: {e} - DELETING")
            try:
                os.remove(filepath)
            except:
                pass
            hardware_feedback("error")
            return

        print(f"✓ Saved {filename} ({file_size/1024/1024:.2f} MB) in {time.time()-start:.2f}s")
        hardware_feedback("saved")
    except Exception as e:
        print(f"✗ Save error: {e}")
        hardware_feedback("error")
    finally:
        with _save_active_lock:
            saving_active -= 1

def capture_full_res(picam2):
    """
    Capture full-res image. Camera operations run under lock.
    Preview resumes immediately after camera is free — save happens in background.
    """
    global capturing, camera_started, show_focus, config_cache, saving_active
    captured_image = None
    filepath = None
    filename = None
    preview_lens_position = None

    try:
        with camera_lock:
            capturing = True
            show_focus = True
            print("\n=== CAPTURE ===")

            os.makedirs(GALLERY_DIR, exist_ok=True)
            number = get_next_capture_number()
            filename = f"Optocamzero_{number}.jpg"
            filepath = os.path.join(GALLERY_DIR, filename)

            # Stop preview
            if camera_started:
                try:
                    preview_lens_position = picam2.capture_metadata().get("LensPosition")
                except Exception:
                    preview_lens_position = None
                picam2.stop()
                camera_started = False
                time.sleep(0.02)

            # Configure and start capture. The AF loop below polls real frames,
            # so it doubles as the settle — no long fixed sleep needed here.
            picam2.configure(config_cache.get_capture_config())
            picam2.start()
            time.sleep(0.05)

            # Autofocus
            picam2.set_controls({"AfMode": 1, "AfTrigger": 0})
            focus_start = time.time()
            focused = False
            # Continuous AF in preview normally keeps the lens close. Camera
            # Module 3 on the Whisplay/Trixie stack can take longer to report
            # its first full-resolution lock after stream reconfiguration, so
            # allow it more time instead of capturing during an active scan.
            focus_timeout = 1.5 if WHISPLAY_MODE else 0.5
            while time.time() - focus_start < focus_timeout:
                try:
                    metadata = picam2.capture_metadata()
                    af_state = metadata.get("AfState", 0)
                    if af_state == 2:
                        focused = True
                        print(f"✓ Focus: {time.time() - focus_start:.2f}s")
                        break
                    elif af_state == 3:
                        break
                except:
                    pass
                time.sleep(0.03)
            if not focused:
                if preview_lens_position is not None:
                    # Low-contrast scenes can make the one-shot AF report
                    # failure. Preserve the lens position that continuous AF
                    # had established in preview instead of leaving the lens at
                    # the failed scan position.
                    picam2.set_controls({
                        "AfMode": 0,
                        "LensPosition": float(preview_lens_position),
                    })
                    time.sleep(0.08)
                    print("⚠ AF did not lock; kept preview focus")
                else:
                    print("⚠ AF did not lock")
            show_focus = False

            # Capture
            for attempt in range(2):
                try:
                    captured_image = picam2.capture_image()
                    if captured_image and captured_image.size[0] > 0:
                        print(f"✓ Captured: {captured_image.size}")
                        break
                    captured_image = None
                    time.sleep(0.08)
                except Exception as e:
                    print(f"⚠ Attempt {attempt + 1}: {e}")
                    captured_image = None
                    time.sleep(0.08)

            picam2.stop()
            time.sleep(0.02)

            if captured_image is None:
                print("✗ Capture failed")
                hardware_feedback("error")
                return None

            # Rotate in memory (fast)
            captured_image = captured_image.transpose(Image.ROTATE_90)

        # === Camera lock released — preview restarts now ===
        capturing = False
        print("✓ Preview resuming...")

        # Save in background — filter applied inside thread, doesn't block preview
        with _save_active_lock:
            saving_active += 1
        threading.Thread(
            target=_save_image_async,
            args=(captured_image, filepath, filename, FILTERS[filter_index]),
            daemon=True
        ).start()

        return filepath

    except Exception as e:
        print(f"✗ ERROR: {e}")
        hardware_feedback("error")
        import traceback
        traceback.print_exc()
        capturing = False
        return None

# ── GIF recording ───────────────────────────────────────────────────────────
GIF_FRAMES   = 10     # total frames in the GIF
GIF_FPS      = 2      # frames captured per second
GIF_DURATION = 5      # seconds of recording (GIF_FRAMES / GIF_FPS)
GIF_FRAME_MS = 1000 // GIF_FPS   # per-frame display duration in the saved GIF

def _save_gif_async(frames, filepath, filename):
    """Quantise frames to a single shared palette and encode the GIF in the
    background — runs after recording has ended and preview has resumed."""
    global saving_active
    try:
        start = time.time()
        # One adaptive palette built from the first frame, applied to every
        # frame with no dithering — keeps colours stable so the GIF doesn't
        # flicker between frames the way per-frame quantisation would.
        palette_src = frames[0].convert("P", palette=Image.ADAPTIVE, colors=256)
        pal_frames = [
            f.quantize(palette=palette_src, dither=Image.NONE) for f in frames
        ]
        pal_frames[0].save(
            filepath, save_all=True, append_images=pal_frames[1:],
            duration=GIF_FRAME_MS, loop=0, optimize=False, disposal=1,
        )
        with open(filepath, "rb") as f:
            os.fsync(f.fileno())

        if not os.path.exists(filepath) or os.path.getsize(filepath) < 1000:
            print("✗ GIF too small / not created - DELETING")
            try:
                os.remove(filepath)
            except:
                pass
            hardware_feedback("error")
            return

        file_size = os.path.getsize(filepath)
        print(f"✓ Saved {filename} ({file_size/1024:.0f} KB) in {time.time()-start:.2f}s")
        hardware_feedback("saved")
    except Exception as e:
        print(f"✗ GIF save error: {e}")
        hardware_feedback("error")
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
    finally:
        with _save_active_lock:
            saving_active -= 1

# Countdown sprites (shadow + white number) cached per value so the blur runs
# once per number instead of on every recording frame.
_rec_num_cache = {}

def _get_rec_number_layer(num):
    if num in _rec_num_cache:
        return _rec_num_cache[num]
    from PIL import ImageDraw, ImageFilter
    font_num = load_font(49)
    shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    white  = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    wd = ImageDraw.Draw(white)
    b = wd.textbbox((0, 0), num, font=font_num)
    nx = (240 - (b[2] - b[0])) // 2 - b[0]
    ny = (240 - (b[3] - b[1])) // 2 - b[1]
    ImageDraw.Draw(shadow).text((nx, ny), num, font=font_num, fill=(0, 0, 0, 250))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=5))
    wd.text((nx, ny), num, font=font_num, fill=(255, 255, 255, 255))
    layer = Image.alpha_composite(shadow, white)
    _rec_num_cache[num] = layer
    return layer

def _draw_rec_overlay(image, count, age=1.0):
    """Pulsing red recording border + countdown number (cached sprite) on a
    240x240 frame. `age` is seconds since the number last changed — the number
    scales in (0.7→1.0 ease-out) so each step pops."""
    from PIL import ImageDraw
    img = image.convert("RGBA")

    # Static red recording border (no motion)
    ImageDraw.Draw(img).rectangle([0, 0, 239, 239], outline=(235, 45, 45), width=3)

    # Countdown number — very slight scale pop on each change
    layer = _get_rec_number_layer(str(count))
    if age < 0.13:
        t = age / 0.13
        s = 0.95 + 0.05 * (1 - (1 - t) ** 3)
        sw = max(1, int(240 * s))
        small = layer.resize((sw, sw), Image.BILINEAR)
        layer = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        off = (240 - sw) // 2
        layer.paste(small, (off, off))

    img = Image.alpha_composite(img, layer)
    return img.convert("RGB")

def record_gif(picam2):
    """Record a 640x640 GIF: GIF_FRAMES frames at GIF_FPS over GIF_DURATION
    seconds. Drives the display directly (live frame + countdown) for the whole
    recording, then hands encoding to a background thread. Runs inline from the
    main loop — preview is reconfigured to 640 for the duration, then restored."""
    global capturing, camera_started, config_cache, saving_active, gif_recording, gif_cancel_requested

    frames = []
    film_name = FILTERS[filter_index]
    filepath = None
    filename = None
    cancelled = False

    try:
        with camera_lock:
            gif_recording = True
            gif_cancel_requested = False
            print("\n=== GIF RECORD ===")

            os.makedirs(GALLERY_DIR, exist_ok=True)
            number = get_next_capture_number()
            filename = f"Optocamzero_{number}.gif"
            filepath = os.path.join(GALLERY_DIR, filename)

            # Switch to the 640 stream
            if camera_started:
                picam2.stop()
                camera_started = False
                time.sleep(0.05)
            picam2.configure(config_cache.get_gif_config())
            picam2.start()
            # Reconfiguring resets the ISP — re-apply the active filter's
            # saturation/contrast and the current white balance so both the live
            # view and the saved frames match normal preview (e.g. B&W stays B&W).
            picam2.set_controls({**_FILM_ISP[film_name],
                                 "AwbMode": AWB_MODES[awb_mode_index][0]})
            time.sleep(0.15)

            # Track when the counter label last changed so each step can pop.
            rec_label = None
            rec_label_start = 0.0

            # Brief pre-roll holding on the "1/10" mark so the counter registers
            # before the first frame is grabbed.
            preroll_end = time.time() + 0.3
            while time.time() < preroll_end:
                if gif_cancel_requested:
                    cancelled = True
                    break
                try:
                    req = picam2.capture_request()
                    frame = req.make_image("main")
                    req.release()
                    disp = frame.transpose(Image.ROTATE_90).resize((240, 240), Image.BILINEAR)
                    disp = _apply_filter_by_name(disp, film_name, apply_grain=False)
                    lbl = f"1/{GIF_FRAMES}"
                    if lbl != rec_label:
                        rec_label, rec_label_start = lbl, time.time()
                    display_image(_draw_rec_overlay(disp, lbl, time.time() - rec_label_start))
                except Exception:
                    pass
                time.sleep(0.01)

            interval = 1.0 / GIF_FPS
            rec_start = time.time()
            for i in range(GIF_FRAMES):
                if cancelled:
                    break
                target = rec_start + i * interval
                # Spin the live preview until this frame's capture time arrives
                while True:
                    if gif_cancel_requested:
                        cancelled = True
                        break
                    now = time.time()
                    frame_label = f"{i+1}/{GIF_FRAMES}"   # 1/10, 2/10, …
                    try:
                        req = picam2.capture_request()
                        frame = req.make_image("main")
                        req.release()
                    except Exception:
                        frame = None
                    if frame is not None:
                        frame = frame.transpose(Image.ROTATE_90)
                        # Live view: downscale, apply the active filter, add overlay
                        disp = frame.resize((240, 240), Image.BILINEAR)
                        disp = _apply_filter_by_name(disp, film_name, apply_grain=False)
                        if frame_label != rec_label:
                            rec_label, rec_label_start = frame_label, now
                        display_image(_draw_rec_overlay(disp, frame_label, now - rec_label_start))
                    if now >= target:
                        break
                    time.sleep(0.01)

                # Capture the actual GIF frame at full 640, filter without grain
                if not cancelled and frame is not None:
                    gif_frame = _apply_filter_by_name(frame, film_name, apply_grain=False)
                    frames.append(gif_frame)

            picam2.stop()
            time.sleep(0.05)

        # === Camera lock released ===
        if cancelled:
            # Discard the in-process frames; nothing was ever written to disk.
            frames = []
            print("✗ GIF recording cancelled — discarded")
        else:
            print(f"✓ Captured {len(frames)} frames, encoding…")
            if frames:
                with _save_active_lock:
                    saving_active += 1
                threading.Thread(
                    target=_save_gif_async,
                    args=(frames, filepath, filename),
                    daemon=True
                ).start()

    except Exception as e:
        print(f"✗ GIF ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        gif_recording = False
        gif_cancel_requested = False
        # Force the main loop to rebuild the 240 preview cleanly
        camera_started = False

def button_handler():
    """Button and joystick polling"""
    global preview_active, capture_requested, exit_requested
    global gallery_active, gallery_index, gallery_images, gallery_needs_update, gallery_confirm_delete, gallery_empty_message_time
    global awb_mode_index, awb_mode_changed, awb_changed_time, gallery_empty_message_time, gallery_confirm_delete, no_space_message_time, splash_active
    global filter_index, filter_label_time, isp_changed
    global transfer_mode, transfer_screen_shown, _transfer_last_activity, _transfer_dimmed
    global _idle_last_activity, _idle_dimmed
    global gif_mode, gif_record_requested, gif_mode_label_time, gif_cancel_requested

    last_capture = 0
    last_preview = 0
    last_joy_press = 0
    last_joy_up = 0
    last_joy_down = 0
    debounce = 0.3

    # Whisplay capture gestures: click = shutter, double-click = next AWB,
    # hold = Photo/GIF. A short single-click is delayed just long enough to
    # distinguish it from a double-click, then handled normally.
    cap_was_down = False
    cap_down_time = 0
    cap_long_fired = False
    cap_click_pending_at = 0
    cap_double_candidate = False
    CAP_LONG_THRESHOLD = 0.6
    CAP_DOUBLE_WINDOW = 0.35

    left_held_since = 0
    right_held_since = 0
    last_scroll_time = 0
    HOLD_THRESHOLD = 0.5
    FAST_INTERVAL = 0.15

    joy_press_times = []
    TRIPLE_PRESS_WINDOW = 0.8
    joy_press_was_down = False
    joy_press_down_time = 0
    joy_long_press_fired = False

    print("✓ Buttons ready")

    while not exit_requested:
        try:
            if WHISPLAY_MODE:
                GPIO.set_context(gallery_active)
                if _whisplay.exit_requested:
                    exit_requested = True
                    break
            now = time.time()

            # --- Splash screen: any button closes it ---
            if splash_active:
                any_pressed = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_pressed:
                    splash_active = False
                    _idle_last_activity = time.time()
                    joy_press_times.clear()
                    print("Splash closed")
                time.sleep(0.05)
                continue

            # --- Idle activity tracking (camera mode only) ---
            if not transfer_mode:
                any_input = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_input:
                    _idle_last_activity = time.time()
                    if _idle_dimmed:
                        _idle_dimmed = False
                        set_backlight(True)
                        time.sleep(0.3)
                        continue

            # --- Joystick Up: delete confirmation (gallery only) ---
            if not GPIO.input(JOYSTICK_UP):
                if now - last_joy_up > debounce:
                    last_joy_up = now
                    if gallery_active and gallery_images:
                        if gallery_confirm_delete:
                            # Confirmed — delete image
                            filepath = gallery_images[gallery_index]
                            try:
                                os.remove(filepath)
                                print(f"✓ Deleted: {os.path.basename(filepath)}")
                                hardware_feedback("delete")
                                # Remove any cached thumbnails
                                fname = os.path.basename(filepath)
                                thumb_dir = os.path.join(GALLERY_DIR, ".thumbs")
                                for f in os.listdir(thumb_dir) if os.path.exists(thumb_dir) else []:
                                    if f.startswith(fname + "_"):
                                        try: os.remove(os.path.join(thumb_dir, f))
                                        except: pass
                            except Exception as e:
                                print(f"✗ Delete error: {e}")
                                hardware_feedback("error")
                            gallery_images.pop(gallery_index)
                            gallery_confirm_delete = False
                            if not gallery_images:
                                gallery_active = False
                                preview_active = True
                                print("Gallery empty, closing")
                            else:
                                gallery_index = min(gallery_index, len(gallery_images) - 1)
                                gallery_needs_update = True
                        else:
                            # Show confirm dialog
                            gallery_confirm_delete = True
                            gallery_needs_update = True
                            hardware_feedback("delete_pending")
                    elif preview_active and not capturing:
                        filter_index = (filter_index - 1) % len(FILTERS)
                        filter_label_time = now
                        isp_changed = True
                        save_settings()
                        print(f"Filter: {FILTERS[filter_index]}")
                        hardware_feedback("filter")

            # --- Joystick Down: dismiss delete dialog / cycle filter forward ---
            if not GPIO.input(JOYSTICK_DOWN):
                if now - last_joy_down > debounce:
                    last_joy_down = now
                    if gallery_active and gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                    elif preview_active and not capturing:
                        filter_index = (filter_index + 1) % len(FILTERS)
                        filter_label_time = now
                        isp_changed = True
                        save_settings()
                        print(f"Filter: {FILTERS[filter_index]}")
                        hardware_feedback("filter")

            # --- Joystick Press: long press = transfer mode, triple = splash, short = gallery ---
            joy_is_down = not GPIO.input(JOYSTICK_PRESS)

            if joy_is_down and not joy_press_was_down:
                # Button just pressed
                joy_press_down_time = now
                joy_long_press_fired = False
                joy_press_was_down = True

            elif joy_is_down and joy_press_was_down:
                # Still held — check for long press (1.5s)
                if not joy_long_press_fired and now - joy_press_down_time >= 1.5:
                    joy_long_press_fired = True
                    joy_press_times.clear()
                    transfer_mode = not transfer_mode
                    transfer_screen_shown = False
                    if transfer_mode:
                        gallery_active = False
                        splash_active = False
                        preview_active = False
                        print("Transfer mode ON")
                        hardware_feedback("mode")
                        _transfer_last_activity = time.time()
                        _transfer_dimmed = False
                        subprocess.Popen(["sudo", "systemctl", "start", "optocam-hotspot.service"])
                        subprocess.Popen(["sudo", "systemctl", "start", "optocam-gallery.service"])
                    else:
                        preview_active = True
                        print("Transfer mode OFF")
                        hardware_feedback("mode")
                        if _transfer_dimmed:
                            set_backlight(True)
                        _transfer_dimmed = False
                        _idle_last_activity = time.time()
                        _idle_dimmed = False
                        subprocess.Popen(["sudo", "systemctl", "stop", "optocam-hotspot.service"])
                        subprocess.Popen(["sudo", "systemctl", "stop", "optocam-gallery.service"])

            elif not joy_is_down and joy_press_was_down:
                # Button just released — handle short press
                joy_press_was_down = False
                if not joy_long_press_fired and now - joy_press_down_time > 0.02:
                    joy_press_times.append(now)
                    joy_press_times[:] = [t for t in joy_press_times if now - t < TRIPLE_PRESS_WINDOW]
                    if len(joy_press_times) >= 3:
                        joy_press_times.clear()
                        gallery_active = False
                        gallery_confirm_delete = False
                        transfer_mode = False
                        splash_active = True
                        print("Splash activated")
                    elif transfer_mode:
                        pass  # short press does nothing in transfer mode
                    elif gallery_active:
                        if gallery_confirm_delete:
                            gallery_confirm_delete = False
                            gallery_needs_update = True
                        else:
                            gallery_active = False
                            preview_active = True
                            print("Gallery closed")
                            hardware_feedback("gallery")
                    else:
                        gallery_images = get_gallery_images()
                        if gallery_images:
                            gallery_index = len(gallery_images) - 1
                            gallery_active = True
                            preview_active = False
                            gallery_needs_update = True
                            print(f"Gallery opened ({len(gallery_images)} images)")
                            hardware_feedback("gallery")
                        else:
                            gallery_empty_message_time = time.time()
                            print("Gallery empty")
                            hardware_feedback("error")

            # --- Skip everything else in transfer mode ---
            if transfer_mode:
                time.sleep(0.05)
                continue

            # --- Capture: click = shutter, double = AWB, hold = Photo/GIF ---
            cap_is_down = not GPIO.input(BUTTON_CAPTURE)
            cap_single_action = False

            # During a GIF recording, a shutter press cancels it (discards data)
            if gif_recording:
                if cap_is_down and not cap_was_down:
                    gif_cancel_requested = True
                    # Consume this press so the still-held button doesn't misfire
                    # a long-press / capture once recording ends.
                    cap_long_fired = True
                    cap_down_time = now
                    print("🚫 GIF cancel")
                    hardware_feedback("error")
                cap_was_down = cap_is_down

            elif cap_is_down and not cap_was_down:
                cap_was_down = True
                cap_down_time = now
                cap_long_fired = False
                cap_double_candidate = bool(
                    WHISPLAY_MODE and cap_click_pending_at > 0
                    and now - cap_click_pending_at <= CAP_DOUBLE_WINDOW
                )
                if cap_double_candidate:
                    cap_click_pending_at = 0

            elif cap_is_down and cap_was_down:
                if (not cap_long_fired
                        and now - cap_down_time >= CAP_LONG_THRESHOLD
                        and preview_active and not capturing and not gif_recording
                        and not gallery_active):
                    cap_long_fired = True
                    cap_click_pending_at = 0
                    cap_double_candidate = False
                    gif_mode = not gif_mode
                    gif_mode_label_time = now
                    save_settings()
                    print(f"GIF mode {'ON' if gif_mode else 'OFF'}")
                    hardware_feedback("mode")

            elif not cap_is_down and cap_was_down:
                cap_was_down = False
                if cap_long_fired:
                    last_capture = now
                elif WHISPLAY_MODE and cap_double_candidate:
                    cap_double_candidate = False
                    last_capture = now
                    if gallery_active and gallery_images:
                        if gallery_confirm_delete:
                            gallery_confirm_delete = False
                        else:
                            gallery_index = (gallery_index - 1) % len(gallery_images)
                            hardware_feedback("navigate")
                        gallery_needs_update = True
                    elif preview_active and not capturing and not gif_recording:
                        awb_mode_index = (awb_mode_index + 1) % len(AWB_MODES)
                        awb_mode_changed = True
                        awb_changed_time = now
                        save_settings()
                        print(f"AWB: {AWB_MODES[awb_mode_index][1]}")
                        hardware_feedback("awb")
                elif WHISPLAY_MODE:
                    cap_click_pending_at = now
                else:
                    cap_single_action = True

            # Resolve a Whisplay single-click only after the double-click
            # window closes. Do not fire while a possible second click is down.
            if (WHISPLAY_MODE and cap_click_pending_at > 0 and not cap_is_down
                    and now - cap_click_pending_at > CAP_DOUBLE_WINDOW):
                cap_click_pending_at = 0
                cap_single_action = True

            if cap_single_action and now - last_capture > debounce:
                last_capture = now
                if gallery_active:
                    if gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                    else:
                        gallery_active = False
                        preview_active = True
                        print("Gallery closed")
                        hardware_feedback("gallery")
                elif preview_active and not capturing and not gif_recording:
                    try:
                        check_path = GALLERY_DIR if os.path.exists(GALLERY_DIR) else os.path.dirname(GALLERY_DIR)
                        stat = os.statvfs(check_path)
                        free_bytes = stat.f_bavail * stat.f_bsize
                        if free_bytes < 20 * 1024 * 1024:
                            no_space_message_time = time.time()
                            print("✗ No space in card")
                            hardware_feedback("error")
                        elif gif_mode:
                            gif_record_requested = True
                            print("🎞 GIF RECORD")
                            hardware_feedback("record")
                        else:
                            capture_requested = True
                            print("📸 CAPTURE")
                            hardware_feedback("shutter")
                    except:
                        if gif_mode:
                            gif_record_requested = True
                            print("🎞 GIF RECORD")
                            hardware_feedback("record")
                        else:
                            capture_requested = True
                            print("📸 CAPTURE")
                            hardware_feedback("shutter")

            # --- Preview toggle button ---
            if not GPIO.input(BUTTON_PREVIEW):
                if now - last_preview > debounce:
                    if gallery_active and gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        last_preview = now
                    elif not gallery_active:
                        preview_active = not preview_active
                        last_preview = now
                        print(f"👁 {'ON' if preview_active else 'OFF'}")

            # --- Joystick Left / Right (gallery navigation) ---
            if gallery_active and gallery_images:
                left_pressed = not GPIO.input(JOYSTICK_LEFT)
                right_pressed = not GPIO.input(JOYSTICK_RIGHT)

                if left_pressed:
                    if gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        left_held_since = now  # prevent immediate nav
                    elif left_held_since == 0:
                        left_held_since = now
                        gallery_index = (gallery_index - 1) % len(gallery_images)
                        gallery_needs_update = True
                        last_scroll_time = now
                        hardware_feedback("navigate")
                    elif now - left_held_since > HOLD_THRESHOLD:
                        if now - last_scroll_time > FAST_INTERVAL:
                            gallery_index = (gallery_index - 1) % len(gallery_images)
                            gallery_needs_update = True
                            last_scroll_time = now
                else:
                    left_held_since = 0

                if right_pressed:
                    if gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        right_held_since = now
                    elif right_held_since == 0:
                        right_held_since = now
                        gallery_index = (gallery_index + 1) % len(gallery_images)
                        gallery_needs_update = True
                        last_scroll_time = now
                        hardware_feedback("navigate")
                    elif now - right_held_since > HOLD_THRESHOLD:
                        if now - last_scroll_time > FAST_INTERVAL:
                            gallery_index = (gallery_index + 1) % len(gallery_images)
                            gallery_needs_update = True
                            last_scroll_time = now
                else:
                    right_held_since = 0

            # --- Joystick Left / Right (AWB mode in preview) ---
            elif preview_active and not capturing:
                if not GPIO.input(JOYSTICK_LEFT):
                    if now - last_scroll_time > debounce:
                        last_scroll_time = now
                        awb_mode_index = (awb_mode_index - 1) % len(AWB_MODES)
                        awb_mode_changed = True
                        awb_changed_time = now
                        save_settings()
                        print(f"AWB: {AWB_MODES[awb_mode_index][1]}")
                if not GPIO.input(JOYSTICK_RIGHT):
                    if now - last_scroll_time > debounce:
                        last_scroll_time = now
                        awb_mode_index = (awb_mode_index + 1) % len(AWB_MODES)
                        awb_mode_changed = True
                        awb_changed_time = now
                        save_settings()
                        print(f"AWB: {AWB_MODES[awb_mode_index][1]}")

            time.sleep(0.02)

        except Exception as e:
            print(f"Button error: {e}")
            time.sleep(0.1)
# Global state
preview_active = True
capture_requested = False
exit_requested = False
camera_started = False
first_preview_frame_pending = True
cold_start_pending = True   # only the very first preview frame skips the filter
capturing = False
show_focus = False
capture_dot_time = 0
gallery_active = False
gallery_index = 0
gallery_images = []
gallery_needs_update = False
gallery_confirm_delete = False
gallery_empty_message_time = 0
no_space_message_time = 0
splash_active = False
awb_mode_index = _settings_index(
    [mode[1] for mode in AWB_MODES],
    _SETTINGS.get("awb"),
    AWB_MODES.index(next(m for m in AWB_MODES if m[1] == "Daylight")),
)
awb_mode_changed = False
awb_changed_time = 0
_awb_last_label = None     # tracks AWB label text to pop it when it changes
_awb_pop_start = 0.0
filter_index = _settings_index(
    FILTERS,
    _SETTINGS.get("filter"),
    FILTERS.index("Film Standard"),
)
saving_active = 0
_save_active_lock = threading.Lock()
filter_label_time = 0
isp_changed = False
gif_mode = bool(_SETTINGS.get("gif_mode"))  # armed for GIF recording (toggled by long-press)
gif_record_requested = False  # button thread → main loop: start a recording
gif_recording = False         # True while record_gif() owns the camera/display
gif_cancel_requested = False  # shutter press during recording → abort + discard
gif_mode_label_time = 0       # transient "GIF" centre label timestamp
save_settings()
_gif_anim_frames = []         # decoded frames of the GIF currently in the gallery
_gif_anim_index = 0
_gif_anim_last = 0.0
_gif_anim_file = None         # path of the GIF the frames belong to
transfer_mode = False
transfer_screen_shown = False
_transfer_last_refresh = 0
_transfer_last_activity = 0.0
_transfer_dimmed = False
_idle_last_activity = 0.0
_idle_dimmed = False
IDLE_DIM_TIMEOUT = 90.0

def _handle_termination(_signum, _frame):
    global exit_requested
    exit_requested = True

signal.signal(signal.SIGTERM, _handle_termination)

def main():
    log("main() called")
    global preview_active, capture_requested, exit_requested, camera_started
    global capturing, show_focus, capture_dot_time, config_cache
    global gallery_active, gallery_index, gallery_images, gallery_needs_update, gallery_confirm_delete, gallery_empty_message_time
    global awb_mode_index, awb_mode_changed, awb_changed_time, no_space_message_time, splash_active
    global _awb_last_label, _awb_pop_start
    global filter_index, filter_label_time, isp_changed, saving_active
    global transfer_mode, transfer_screen_shown, _transfer_last_refresh, _transfer_last_activity, _transfer_dimmed
    global _idle_last_activity, _idle_dimmed
    global first_preview_frame_pending, cold_start_pending
    global gif_mode, gif_record_requested, gif_recording, gif_mode_label_time
    global _gif_anim_frames, _gif_anim_index, _gif_anim_last, _gif_anim_file

    gc.disable()

    print("=" * 50)
    print("CAMERA - FAST & ROBUST")
    print("=" * 50)

    log("Initializing display...")
    init_display()
    show_splash()          # draw the splash first (backlight still off)…
    set_backlight(True)    # …then light it, so no white flash beforehand
    log("Display ready - showing splash screen")

    log("Waiting for heavy-preload thread (picamera2 + PIL)...")
    print("Initializing camera...")
    _preload_thread.join()
    from picamera2 import Picamera2  # instant — already imported by the preload thread
    log("Picamera2 imported")

    # Early-start safety: the imx708 may still be probing when we start this early.
    # Wait for libcamera to enumerate it before constructing Picamera2 (which would
    # otherwise raise on a not-yet-present camera).
    _cam_t0 = time.time()
    while not Picamera2.global_camera_info():
        if time.time() - _cam_t0 > 30:
            log("WARN: camera enumeration timed out")
            break
        time.sleep(0.05)
    log("camera enumerated")

    picam2 = Picamera2()
    config_cache = CameraConfigCache(picam2)

    print("\n" + "=" * 50)
    print("FEATURES:")
    print("✓ Enhanced black levels")
    print("✓ FAST capture (1.5-2s freeze)")
    print("✓ Robust verification (no 0 KB files)")
    print("✓ Auto-delete corrupted files")
    print("✓ Green dot = capture confirmation")
    if WHISPLAY_MODE:
        print("✓ Whisplay click: Capture / Close gallery")
        print("✓ Whisplay double click: Next AWB / Previous gallery item")
        print("✓ Whisplay hold: Toggle Photo/GIF mode")
        print("✓ PiSugar double click: Next filter / Next gallery item")
        print("✓ PiSugar hold: Open gallery / Delete")
        if _whisplay.mode == "daemon":
            print("✓ PiSugar single click: Return to Whisplay desktop")
        else:
            print("✓ Standalone boot service: Direct hardware ownership")
    else:
        print(f"✓ KEY1 (GPIO {BUTTON_CAPTURE}): Capture / Close gallery")
        print(f"✓ KEY2 (GPIO {BUTTON_PREVIEW}): Toggle preview")
        print(f"✓ Joystick press: Open/close gallery")
        print(f"✓ Joystick left/right: Navigate gallery")
    print("=" * 50 + "\n")

    button_thread = threading.Thread(target=button_handler, daemon=True)
    button_thread.start()

    # Pre-build the supersampled HUD overlays off the main thread so switching
    # filters never has to render one mid-preview (the first-launch hitch).
    def _warm_overlays():
        for f in FILTERS:
            try:
                get_filter_indicator(f)
            except Exception as e:
                print(f"warm indicator error: {e}")
        try:
            get_gif_mode_indicator(0)   # also warm the GIF-mode pill phases
        except Exception as e:
            print(f"warm gif pill error: {e}")
    threading.Thread(target=_warm_overlays, daemon=True).start()

    frame_count = 0
    last_fps_report = time.time()
    _idle_last_activity = time.time()

    try:
        while not exit_requested:

            if WHISPLAY_MODE and _whisplay.exit_requested:
                exit_requested = True
                break

            # === IDLE DIM CHECK (camera mode only) ===
            if not transfer_mode and not splash_active:
                if not _idle_dimmed and time.time() - _idle_last_activity > IDLE_DIM_TIMEOUT:
                    _idle_dimmed = True
                    set_backlight_brightness(8)

            # === SPLASH MODE ===
            if splash_active:
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0
                set_backlight(True)
                show_splash()
                time.sleep(0.05)

            # === TRANSFER MODE ===
            elif transfer_mode:
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0
                if not transfer_screen_shown:
                    transfer_screen_shown = True
                    _transfer_last_refresh = 0
                    _transfer_last_activity = time.time()
                    _transfer_dimmed = False
                    set_backlight(True)
                # Dim after 30s inactivity
                any_pressed = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_pressed:
                    if _transfer_dimmed:
                        _transfer_dimmed = False
                        set_backlight(True)
                        _transfer_last_refresh = 0
                    _transfer_last_activity = time.time()
                elif not _transfer_dimmed and time.time() - _transfer_last_activity > 30:
                    _transfer_dimmed = True
                    set_backlight_brightness(8)
                if time.time() - _transfer_last_refresh >= 0.5:
                    _transfer_last_refresh = time.time()
                    show_transfer_mode_screen()
                time.sleep(0.1)

            # === GALLERY MODE ===
            elif gallery_active:
                # Stop camera if running
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0

                # Display current gallery image when needed
                if gallery_needs_update and gallery_images:
                    gallery_needs_update = False
                    idx = gallery_index  # snapshot index before load
                    total = len(gallery_images)
                    set_backlight(True)
                    is_gif = gallery_images[idx].lower().endswith(".gif")
                    if is_gif and not gallery_confirm_delete:
                        # Show frame 0 now; the rest decode in the background and
                        # stream into this list, so navigation doesn't stall.
                        _gif_anim_frames = start_gif_gallery_load(gallery_images[idx], idx + 1, total)
                        _gif_anim_file = gallery_images[idx]
                        _gif_anim_index = 0
                        _gif_anim_last = time.time()
                        if _gif_anim_frames:
                            display_image(_gif_anim_frames[0])
                        else:
                            display_gallery_image(gallery_images[idx], idx + 1, total, False)
                    else:
                        # Static photo, or GIF first-frame behind the delete dialog
                        _gif_anim_frames = []
                        _gif_anim_file = None
                        display_gallery_image(gallery_images[idx], idx + 1, total, gallery_confirm_delete)
                    print(f"Gallery: {idx + 1}/{total}")

                # Advance GIF animation while its frame is on screen
                if (_gif_anim_frames and not gallery_confirm_delete
                        and gallery_images and gallery_index < len(gallery_images)
                        and gallery_images[gallery_index] == _gif_anim_file):
                    if time.time() - _gif_anim_last >= GIF_FRAME_MS / 1000.0:
                        _gif_anim_last = time.time()
                        _gif_anim_index = (_gif_anim_index + 1) % len(_gif_anim_frames)
                        display_image(_gif_anim_frames[_gif_anim_index])

                time.sleep(0.02)

            # === PREVIEW MODE ===
            elif preview_active and not capturing:
                if not camera_started:
                    with camera_lock:
                        set_backlight(True)
                        picam2.configure(config_cache.preview_config)
                        picam2.start()
                        camera_started = True
                        first_preview_frame_pending = True
                        # Reconfiguring resets the ISP — re-apply the active
                        # filter's saturation/contrast and white balance so the
                        # full effect is back immediately (e.g. after a capture).
                        isp_changed = True
                        awb_mode_changed = True
                        _idle_last_activity = time.time()
                        print("✓ Preview started\n")

                if capture_requested:
                    capture_requested = False
                    capture_dot_time = time.time()
                    threading.Thread(
                        target=capture_full_res,
                        args=(picam2,),
                        daemon=True
                    ).start()

                # GIF recording runs inline (it owns the display for ~5s),
                # then falls through and the preview rebuilds at 240.
                if gif_record_requested:
                    gif_record_requested = False
                    _idle_last_activity = time.time()
                    record_gif(picam2)
                    continue

                if camera_started and not capturing:
                    try:
                        with camera_lock:
                            if camera_started and not capturing:
                                if awb_mode_changed:
                                    awb_mode_changed = False
                                    picam2.set_controls({"AwbMode": AWB_MODES[awb_mode_index][0]})
                                if isp_changed:
                                    isp_changed = False
                                    picam2.set_controls(_FILM_ISP[FILTERS[filter_index]])
                                req = picam2.capture_request()
                                preview_image = req.make_image("main")
                                metadata = req.get_metadata()
                                req.release()
                                preview_image = preview_image.transpose(Image.ROTATE_90)
                                if first_preview_frame_pending and cold_start_pending:
                                    # Genuine cold start only: show one unfiltered
                                    # frame fast. After a capture/restart we skip
                                    # this so the filter never visibly drops out.
                                    cold_start_pending = False
                                    first_preview_frame_pending = False
                                    if preview_image.size != PREVIEW_SIZE:
                                        preview_image = preview_image.resize(PREVIEW_SIZE, Image.LANCZOS)
                                    display_image(preview_image)
                                    log("First preview frame displayed")
                                    hardware_feedback("ready")
                                    frame_count += 1
                                    continue
                                first_preview_frame_pending = False
                                preview_image = apply_filter(preview_image)
                                if preview_image.size != PREVIEW_SIZE:
                                    preview_image = preview_image.resize(PREVIEW_SIZE, Image.LANCZOS)
                                if capture_dot_time > 0 and (time.time() - capture_dot_time) >= 2.0:
                                    capture_dot_time = 0

                                # --- ISO (bottom left) and Shutter (bottom right) ---
                                from PIL import ImageDraw
                                font_hud = load_font(25)
                                font_battery = load_font(18)
                                font_awb_full  = load_font(25)
                                font_awb_abbr  = load_font(24)
                                tmp_draw = ImageDraw.Draw(preview_image)

                                iso_val = str(nearest_standard_iso(metadata.get("AnalogueGain", 1.0)))
                                battery_val = _battery_label()
                                exp = metadata.get("ExposureTime", 10000)
                                shutter_val = nearest_standard_shutter(exp) if exp > 0 else "?"
                                awb_switching = time.time() - awb_changed_time < 1.0
                                awb_label = (AWB_MODES[awb_mode_index][1] if awb_switching
                                             else AWB_MODES[awb_mode_index][2])
                                font_awb = font_awb_full if awb_switching else font_awb_abbr

                                # AWB position: top left — subtract top bearing so visible glyph
                                # sits exactly 15px from the edge, matching ISO/shutter bottom gap
                                b_awb = tmp_draw.textbbox((0, 0), awb_label, font=font_awb)
                                ax = 15
                                ay = 15 - b_awb[1]

                                # AWB label animates on any text change — the full name
                                # on an AWB change and the abbreviation when it takes over.
                                if awb_label != _awb_last_label:
                                    _awb_last_label = awb_label
                                    _awb_pop_start = time.time()
                                awb_popping = time.time() - _awb_pop_start < AWB_POP_DUR
                                awb_font_size = 25 if awb_switching else 24

                                # ISO position: bottom left
                                b_iso = tmp_draw.textbbox((0, 0), iso_val, font=font_hud)
                                ix = 15

                                # Shutter position: bottom right
                                b_sh = tmp_draw.textbbox((0, 0), shutter_val, font=font_hud)
                                sx = PREVIEW_WIDTH - 15 - (b_sh[2] - b_sh[0])

                                # Shared bottom y — use max height so both sit on the same baseline
                                hud_bottom_y = PREVIEW_HEIGHT - 15 - max(
                                    b_iso[3] - b_iso[1], b_sh[3] - b_sh[1]
                                )
                                iy = hud_bottom_y
                                sy = hud_bottom_y

                                # Battery percentage: plain white text, left-aligned
                                # with ISO and one text row above it.  It uses the
                                # same blurred shadow treatment as the other HUD data.
                                battery_y = None
                                if battery_val is not None:
                                    b_battery = tmp_draw.textbbox(
                                        (0, 0), battery_val, font=font_battery
                                    )
                                    battery_y = (
                                        iy + b_iso[1] - 6 - b_battery[3]
                                    )

                                # Transient centre messages (mutually exclusive).
                                # This also clears expired timers, so it must run every
                                # frame regardless of the overlay cache below.
                                centre_msg = None
                                centre_msg_start = 0.0
                                centre_msg_dur = 1.5
                                if gallery_empty_message_time > 0 and time.time() - gallery_empty_message_time < 1.0:
                                    centre_msg = "No image in card"
                                    centre_msg_start, centre_msg_dur = gallery_empty_message_time, 1.0
                                elif gallery_empty_message_time > 0:
                                    gallery_empty_message_time = 0
                                if centre_msg is None and no_space_message_time > 0 and time.time() - no_space_message_time < 1.0:
                                    centre_msg = "No space in card"
                                    centre_msg_start, centre_msg_dur = no_space_message_time, 1.0
                                elif centre_msg is None and no_space_message_time > 0:
                                    no_space_message_time = 0
                                if (centre_msg is None and filter_label_time > 0
                                        and time.time() - filter_label_time < 1.5
                                        and gallery_empty_message_time == 0
                                        and no_space_message_time == 0):
                                    centre_msg = FILTERS[filter_index]
                                    centre_msg_start, centre_msg_dur = filter_label_time, 1.5
                                elif centre_msg is None and filter_label_time > 0:
                                    filter_label_time = 0
                                if (centre_msg is None and gif_mode_label_time > 0
                                        and time.time() - gif_mode_label_time < 1.5):
                                    centre_msg = "GIF Mode" if gif_mode else "Photo Mode"
                                    centre_msg_start, centre_msg_dur = gif_mode_label_time, 1.5
                                elif centre_msg is None and gif_mode_label_time > 0:
                                    gif_mode_label_time = 0

                                # Visible GIF-pill phase (-1 when not armed) — only flips at the march rate
                                gif_phase_mod = (int(time.time() * _GIF_PILL_MARCH_HZ) % _GIF_PILL_PHASES) if gif_mode else -1

                                # Filter pill bounces briefly after a filter change — while
                                # popping it's animated on top, so pulled out of the cache then.
                                pill_popping = (filter_label_time > 0
                                                and time.time() - filter_label_time < PILL_POP_DUR)

                                # Reuse the assembled shadow overlay when nothing that
                                # affects it changed; otherwise rebuild and cache it.
                                # (The centre label and a popping pill are NOT in here —
                                # they're animated on top below, so they never invalidate it.)
                                hud_key = (awb_label, battery_val, iso_val, shutter_val,
                                           FILTERS[filter_index], gif_phase_mod, pill_popping, awb_popping)
                                if _hud_overlay_cache["key"] == hud_key:
                                    overlay = _hud_overlay_cache["img"]
                                else:
                                    iso_shadow = get_cached_shadow("iso", iso_val, ix, iy, font_hud)
                                    sh_shadow = get_cached_shadow("shutter", shutter_val, sx, sy, font_hud)
                                    overlay = Image.alpha_composite(iso_shadow, sh_shadow)
                                    if battery_val is not None:
                                        overlay = Image.alpha_composite(
                                            overlay,
                                            get_cached_shadow(
                                                "battery", battery_val, ix,
                                                battery_y, font_battery,
                                            ),
                                        )
                                    if not awb_popping:
                                        overlay = Image.alpha_composite(
                                            overlay, get_cached_shadow("awb", awb_label, ax, ay, font_awb))
                                    if not pill_popping:
                                        overlay = Image.alpha_composite(overlay, get_filter_indicator(FILTERS[filter_index]))
                                    if gif_mode:
                                        overlay = Image.alpha_composite(overlay, get_gif_mode_indicator(gif_phase_mod))
                                    _hud_overlay_cache["key"] = hud_key
                                    _hud_overlay_cache["img"] = overlay

                                # Single convert → composite → convert for all shadows/overlays
                                preview_image = preview_image.convert("RGBA")
                                preview_image = Image.alpha_composite(preview_image, overlay)
                                preview_image = preview_image.convert("RGB")

                                draw_hud = ImageDraw.Draw(preview_image)
                                if not awb_popping:
                                    draw_hud.text((ax, ay), awb_label, font=font_awb, fill=(255, 255, 255))
                                if battery_val is not None:
                                    draw_hud.text(
                                        (ix, battery_y), battery_val,
                                        font=font_battery, fill=(255, 255, 255),
                                    )
                                draw_hud.text((ix, iy), iso_val, font=font_hud, fill=(255, 255, 255))
                                draw_hud.text((sx, sy), shutter_val, font=font_hud, fill=(255, 255, 255))

                                # Saving spinner — bottom centre, vertically aligned with ISO/shutter
                                if saving_active > 0:
                                    sp_r  = 7
                                    sp_cx = PREVIEW_WIDTH // 2
                                    sp_cy = hud_bottom_y + (b_iso[1] + b_iso[3]) // 2
                                    sp_a  = int(time.time() * 360) % 360
                                    sp_box   = [sp_cx-sp_r,   sp_cy-sp_r,   sp_cx+sp_r,   sp_cy+sp_r]
                                    sp_box_s = [sp_cx-sp_r+1, sp_cy-sp_r+1, sp_cx+sp_r+1, sp_cy+sp_r+1]
                                    draw_hud.arc(sp_box_s, start=sp_a, end=sp_a+270, fill=(0, 0, 0),       width=2)
                                    draw_hud.arc(sp_box,   start=sp_a, end=sp_a+270, fill=(255, 255, 255), width=2)

                                # AWB label pop (scale, on top) on change / full→abbrev switch
                                if awb_popping:
                                    preview_image = render_pill_pop(
                                        preview_image, get_awb_sprite(awb_label, awb_font_size),
                                        _awb_pop_scale(time.time() - _awb_pop_start, awb_switching))

                                # Filter pill bounce (subtle scale, on top) right after a change
                                if pill_popping:
                                    preview_image = render_pill_pop(
                                        preview_image, get_filter_indicator(FILTERS[filter_index]),
                                        _pill_pop_scale(time.time() - filter_label_time))

                                # Animated centre label (filter name / mode toast) — fade + bounce, on top
                                if centre_msg is not None:
                                    preview_image = render_centre_msg(
                                        preview_image, centre_msg,
                                        time.time() - centre_msg_start, centre_msg_dur)

                                display_image(preview_image)
                                frame_count += 1
                                if time.time() - last_fps_report >= 5.0:
                                    elapsed = time.time() - last_fps_report
                                    fps = frame_count / elapsed
                                    print(f"📊 {fps:.1f} fps")
                                    frame_count = 0
                                    last_fps_report = time.time()
                    except Exception as e:
                        print(f"Preview error: {e}")
                        time.sleep(0.1)

                time.sleep(0.001)

            # === PREVIEW OFF ===
            else:
                if camera_started and not capturing:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            set_backlight(False)
                            clear_display()
                            capture_dot_time = 0
                            print("✓ Preview stopped\n")
                time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
        exit_requested = True

    finally:
        with camera_lock:
            if camera_started:
                picam2.stop()
        if WHISPLAY_MODE:
            _whisplay.cleanup()
        else:
            set_backlight(False)
        GPIO.cleanup()
        spi.close()
        gc.enable()
        print("✓ Shutdown complete!")
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Unhandled error: {exc!r}")
        raise
    finally:
        if WHISPLAY_MODE and _whisplay is not None:
            _whisplay.cleanup()
