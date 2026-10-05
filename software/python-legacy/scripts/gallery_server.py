#!/usr/bin/env python3
"""OptoCam Gallery Server — serves photos over WiFi hotspot"""
import os
import io
import json
import time
import zipfile
import tempfile
import threading
import resource

# Some Whisplay images boot with ``cgroup_disable=memory``, which makes
# systemd's MemoryMax= unavailable.  Keep an in-process address-space limit as
# a second line of defence so a malformed/high-resolution image can stop only
# the gallery instead of starving PID 1 and tripping the hardware watchdog.
GALLERY_MAX_AS_MB = int(os.getenv("OPTOCAM_GALLERY_MAX_AS_MB", "0"))
if GALLERY_MAX_AS_MB > 0:
    max_as_bytes = GALLERY_MAX_AS_MB * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (max_as_bytes, max_as_bytes))
    except (OSError, ValueError):
        # MemoryMax still protects installations whose kernel has memcg.
        pass

from flask import Flask, send_from_directory, render_template_string, Response, request
from magic_service import (
    MagicModeStore,
    is_ai_generated,
    read_magic_source_metadata,
    read_magic_connection_settings,
    save_magic_connection_settings,
)

OPTOCAM_HOME = os.path.abspath(os.getenv("OPTOCAM_HOME", "/home/dkumkum"))
PHOTOS_DIR = os.path.abspath(os.getenv(
    "OPTOCAM_PHOTOS_DIR", os.path.join(OPTOCAM_HOME, "photos")
))
GALLERY_PORT = int(os.getenv("OPTOCAM_GALLERY_PORT", "80"))
GALLERY_PRELOAD = os.getenv("OPTOCAM_GALLERY_PRELOAD", "1").lower() not in {
    "0", "false", "no", "off"
}
GALLERY_THREADED = os.getenv("OPTOCAM_GALLERY_THREADED", "1").lower() not in {
    "0", "false", "no", "off"
}

MEDIA_EXTS = (".jpg", ".gif")

app = Flask(__name__)
MAGIC_MODE_STORE = MagicModeStore(os.path.join(OPTOCAM_HOME, "magic_modes.json"))
MAGIC_ENV_PATH = os.path.join(OPTOCAM_HOME, ".env")

# token -> {"sent": int, "total": int, "ts": float} — live zip download progress.
DOWNLOAD_PROGRESS = {}


def capture_number_of(filename):
    """Numeric id from Optocamzero_<n>.<ext>, or None. Shared ordering across
    photos (.jpg) and GIFs (.gif)."""
    if not filename.startswith("Optocamzero_"):
        return None
    stem = filename[len("Optocamzero_"):]
    dot = stem.rfind(".")
    if dot == -1:
        return None
    num, ext = stem[:dot], stem[dot:].lower()
    if ext in MEDIA_EXTS and num.isdigit():
        return int(num)
    return None


def list_media():
    """All media files (photos + GIFs) newest-first."""
    if not os.path.exists(PHOTOS_DIR):
        return []
    files = [f for f in os.listdir(PHOTOS_DIR) if capture_number_of(f) is not None]
    files.sort(key=lambda f: capture_number_of(f), reverse=True)
    return files

def paired_audio_filename(filename):
    if not filename.lower().endswith(".jpg"):
        return None
    candidate = os.path.splitext(os.path.basename(filename))[0] + ".wav"
    return candidate if os.path.exists(os.path.join(PHOTOS_DIR, candidate)) else None

def expand_with_paired_audio(filenames):
    """Return safe, existing image/GIF names plus each photo's paired WAV."""
    expanded = []
    seen = set()
    for filename in filenames:
        base = os.path.basename(filename)
        if capture_number_of(base) is None:
            continue
        for member in (base, paired_audio_filename(base)):
            if member and member not in seen and os.path.exists(os.path.join(PHOTOS_DIR, member)):
                seen.add(member)
                expanded.append(member)
    return expanded

HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Optocam Zero</title>
<style>
@font-face {
    font-family: 'CamFont';
    src: url('/font/cmunvt.ttf') format('truetype');
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #080808;
    color: #d0d0d0;
    font-family: 'CamFont', 'Courier New', monospace;
    padding: 14px;
    -webkit-tap-highlight-color: transparent;
    overscroll-behavior: none;
}
/* While the viewer is open, make the page background black too, so no gray
   (body color) shows in any strip the fixed viewer doesn't cover on iOS. */
body:has(#viewer.open) { background: #000; }
header {
    border-bottom: 1px solid #1e1e1e;
    padding-top: 11px;    /* less top than bottom to offset the 14px body padding above */
    padding-bottom: 25px;
    margin-bottom: 14px;
    /* Stretch past the body's 14px padding so the divider spans the full width. */
    margin-left: -14px;
    margin-right: -14px;
    display: flex;
    flex-direction: column;
    align-items: center;
}
.logo { height: 30px; width: auto; display: block; }
@media (min-width: 768px) {
    header { padding-top: 16px; padding-bottom: 30px; }
}
.meta {
    display: flex;
    gap: 18px;
    font-size: 12px;
    color: #555;
    letter-spacing: 1px;
}
/* Counter and free-space share one size (the counter's). */
.meta span { font-size: 12px; }
/* Row holding the grouped meta (left) and the grid-density toggle (right). */
.meta-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    padding-bottom: 50px;
}
@media (min-width: 768px) {
    .grid { grid-template-columns: repeat(5, 1fr); }   /* desktop default */
    /* Gap tightens as density increases: 5-col 8px, 6-col 7px, 7-col 6px. */
    .grid.dcols-5 { grid-template-columns: repeat(5, 1fr); }
    .grid.dcols-6 { grid-template-columns: repeat(6, 1fr); gap: 7px; }
    .grid.dcols-7 { grid-template-columns: repeat(7, 1fr); gap: 6px; }
    /* Uniform control sizing across all desktop densities:
       21px selection ring, 22px download icon. */
    .grid .sel-circle::before { width: 21px; height: 21px; }
    .grid .item.sel .sel-circle::after { top: 17.5px; left: 17.5px; }
    .grid .dl-icon { width: 22px; height: 22px; }
}
@media (min-width: 1200px) {
    .grid {
        max-width: 1400px;
        margin: 0 auto;
    }
}
/* Stop iOS Safari from hijacking long-press on thumbnails (the image
   callout/"Save Image" menu, magnified preview and image drag) — otherwise it
   cancels the touch and the glide-to-select can't run. */
.grid, .grid * {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
}
.img-btn img { -webkit-user-drag: none; }
.item {
    background: #101010;
    border: 1px solid #1a1a1a;
    border-radius: 3px;
    overflow: hidden;
    cursor: pointer;
}
.item.sel { border-color: #1a1a1a; }
.img-wrap { position: relative; }
/* Tint selected thumbnails — overlay sits above the image but below the
   dot / download / GIF-badge controls (z-index 2). */
.item.sel .img-wrap::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    pointer-events: none;
    z-index: 1;
}
.img-btn {
    display: block;
    width: 100%;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
}
.img-btn img {
    width: 100%;
    aspect-ratio: 1 / 1;
    object-fit: cover;
    display: block;
}
.sel-circle {
    position: absolute;
    /* Anchored to the very corner so there's no dead margin around the dot;
       the ring (::before) is offset inward to keep its visual position. */
    top: 0;
    left: 0;
    /* Visible ring stays 23px (the ::before); the button itself is larger to
       give a bigger, easier-to-hit tap target without changing the look. */
    width: 54px;
    height: 54px;
    border-radius: 50%;
    border: none;
    background: transparent;
    cursor: pointer;
    z-index: 2;
    transition: none;
    touch-action: pan-y;   /* vertical drags still scroll; horizontal glide selects */
}
.sel-circle::before {
    content: '';
    position: absolute;   /* offset inward from the corner-anchored button */
    top: 7px;
    left: 7px;
    width: 23px;
    height: 23px;
    border-radius: 50%;
    border: 1.5px solid rgba(255,255,255,0.45);
    background: rgba(0,0,0,0.45);
    transition: background 0.12s, border-color 0.12s;
    box-sizing: border-box;
}
.item.sel .sel-circle::before {
    background: #fff;
    border-color: #fff;
}
.item.sel .sel-circle::after {
    content: '';
    position: absolute;
    /* Centered on the 23px ring (offset 7px inward from the button corner). */
    top: 18.5px;
    left: 18.5px;
    width: 5px;
    height: 9px;
    border: 2px solid #000;
    border-top: none;
    border-left: none;
    transform: translate(-60%, -65%) rotate(45deg);
}
.dl-icon {
    position: absolute;
    top: 7px;
    right: 7px;
    width: 24px;
    height: 24px;
    background: rgba(0,0,0,0.6);
    border: none;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
    z-index: 2;
}
.dl-icon svg { width: 13px; height: 13px; }
.gif-badge {
    position: absolute;
    bottom: 7px;
    left: 7px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px 8px 2px;        /* extra top padding nudges the text down */
    font-family: 'CamFont', monospace;
    font-size: 10px;
    letter-spacing: 1px;
    color: #fff;
    background: rgba(0,0,0,0.6);
    border: none;
    border-radius: 999px;        /* fully rounded ends → pill */
    z-index: 2;
    pointer-events: none;
}
.ai-badge {
    position: absolute;
    bottom: 7px;
    right: 7px;
    padding: 4px 7px 2px;
    font-size: 10px;
    letter-spacing: 1px;
    color: #111;
    background: #fff;
    border-radius: 999px;
    z-index: 2;
    pointer-events: none;
}
.magic-badge, .magic-processing-badge {
    position: absolute;
    bottom: 7px;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 2;
    pointer-events: none;
}
.magic-badge {
    left: 7px;
    color: #111;
    background: #fff;
}
.magic-badge svg { width: 16px; height: 16px; }
.magic-processing-badge {
    right: 7px;
    background: rgba(0,0,0,0.68);
}
.magic-processing-badge::before {
    content: '';
    width: 15px;
    height: 15px;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin .8s linear infinite;
}
.audio-badge {
    position: absolute;
    bottom: 7px;
    left: 7px;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 2;
    pointer-events: none;
}
.audio-badge svg { width: 15px; height: 15px; }
.viewer-audio-badge {
    position: absolute;
    right: 22px;
    bottom: 18px;
    display: none;
    background: rgba(0,0,0,0.44);
}
.viewer-audio-badge svg {
    opacity: 0.58;
    transition: opacity 0.16s ease;
}
.viewer-audio-badge.playing svg {
    opacity: 1;
}
/* Spinner over a GIF poster whose animation is still loading in the
   background. Hidden until the poster JPG has actually loaded (gets `.show`
   in JS), and removed the moment the live GIF swaps in. Sits above the image
   but below the badge/controls, and ignores clicks. */
.grid-spin {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 30px;
    height: 30px;
    margin: -15px 0 0 -15px;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    z-index: 1;
    pointer-events: none;
    filter: drop-shadow(0 0 1px rgba(0,0,0,0.65));
    display: none;
}
.grid-spin.show { display: block; }

/* ── Toolbar (meta + filter/order controls), below the header divider ── */
.toolbar {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 28px;
}
.controls {
    display: flex;
    justify-content: space-between;
    gap: 22px;
    flex-wrap: wrap;
}
@media (min-width: 768px) {
    /* Desktop: meta on the left, controls on the right, one row. */
    .toolbar {
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
    }
    #density-group { display: none; }   /* density toggle is mobile-only */
}
@media (min-width: 1200px) {
    /* Match the grid's centered max width so the toolbar's left and right
       edges line up with the image block. */
    .toolbar {
        max-width: 1400px;
        margin-left: auto;
        margin-right: auto;
    }
}
.density-group button {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 6px 10px;
}
.density-group svg { display: block; fill: currentColor; }
/* Grid density (mobile only — desktop keeps its 4/5 columns). */
@media (max-width: 767px) {
    /* Gap tightens as density increases: 2-col 8px, 3-col 7px, 4-col 6px. */
    .grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
    .grid.cols-3 { grid-template-columns: repeat(3, 1fr); gap: 7px; }
    .grid.cols-4 { grid-template-columns: repeat(4, 1fr); gap: 6px; }
    /* 4-col also drops the per-thumb download icon. */
    .grid.cols-4 .dl-icon { display: none; }
    #desktop-density-group { display: none; }   /* desktop-only selector */
    /* Denser grids: smaller selection ring (20px) and, on 3-col, download icon. */
    .grid.cols-3 .sel-circle::before,
    .grid.cols-4 .sel-circle::before { width: 20px; height: 20px; }
    .grid.cols-3 .item.sel .sel-circle::after,
    .grid.cols-4 .item.sel .sel-circle::after { top: 17px; left: 17px; }
    .grid.cols-3 .dl-icon { width: 21px; height: 21px; }
}
.ctrl-group {
    display: inline-flex;
    border: 1px solid #1e1e1e;
    border-radius: 11px;
    overflow: hidden;
}
.ctrl-group button {
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 1px;
    color: #555;
    background: #080808;
    border: none;
    padding: 7px 12px;
    cursor: pointer;
    transition: color 0.12s, background 0.12s;
}
.ctrl-group button + button { border-left: 1px solid #1e1e1e; }
.ctrl-group button.active { color: #fff; background: #141414; }

/* ── Viewer ── */
#viewer {
    position: fixed;
    inset: 0;
    background: #000;
    z-index: 100;
    flex-direction: column;
    display: none;
    pointer-events: none;
    transform: translateZ(0);   /* own compositing layer — avoids iOS paint bleed */
}
#viewer.open { display: flex; pointer-events: auto; }
.viewer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    border-bottom: 1px solid #1a1a1a;
    flex-shrink: 0;
}
/* Counter + filename live in a bottom bar (below the image) on all sizes now,
   so the header's center copy is unused. */
.viewer-info { display: none; }
.viewer-controls { display: flex; align-items: center; gap: 8px; }
.viewer-fname {
    font-size: 12px;
    color: #666;
    letter-spacing: 0.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
}
/* Bottom bar under the image: filename left-aligned, counter right-aligned
   (set by markup order). Full-width divider continuous with the nav line. */
.viewer-info-m {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 11px 14px;
    border-top: 1px solid #1a1a1a;
    flex-shrink: 0;
}
@media (min-width: 768px) {
    /* Match the bottom bar's height to the top header (57px: 32px controls +
       12px*2 padding + 1px border), and group filename + counter together in
       the center rather than pushing them to the edges. */
    .viewer-info-m {
        min-height: 57px;
        padding-top: 12px;
        padding-bottom: 12px;
        justify-content: center;
        gap: 40px;
    }
}
.viewer-back {
    font-family: 'CamFont', monospace;
    font-size: 13px;
    color: #aaa;
    background: none;
    border: none;
    cursor: pointer;
    letter-spacing: 1px;
    padding: 4px 0;
    display: flex;
    align-items: center;
    gap: 5px;
}
.viewer-pos {
    font-size: 16px;
    color: #aaa;
    letter-spacing: 1px;
}
.viewer-dl {
    width: 32px;
    height: 32px;
    background: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
}
.viewer-dl svg { width: 14px; height: 14px; }
.viewer-del {
    width: 32px;
    height: 32px;
    background: #141414;
    border: 1px solid #3a1a1a;
    border-radius: 50%;
    color: #e8554f;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
}
.viewer-del svg { width: 14px; height: 14px; }
.viewer-hq {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    border: 1px solid #2a2a2a;
    background: #141414;
    color: #383838;
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 0.5px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding-left: 1px;
    transition: border-color 0.15s, color 0.15s;
}
.viewer-hq.active { border-color: #888; color: #fff; }
.viewer-body {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
}
.viewer-body img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}
.spinner {
    position: absolute;
    width: 32px;
    height: 32px;
    border: 2px solid #222;
    border-top-color: #888;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: none;
}
.spinner.active { display: block; }
@keyframes spin { to { transform: rotate(360deg); } }
.viewer-nav {
    display: flex;
    border-top: 1px solid #1a1a1a;
    flex-shrink: 0;
    background: #000;
}
.nav-btn {
    flex: 1;
    padding: 14px;
    background: none;
    border: none;
    color: #666;
    font-family: 'CamFont', monospace;
    font-size: 18px;
    cursor: pointer;
    letter-spacing: 1px;
}
.nav-btn:active { background: #111; color: #fff; }
.nav-btn:first-child { border-right: 1px solid #1a1a1a; }
.side-nav { display: none; }
@media (min-width: 768px) {
    .viewer-nav { display: none; }
    .side-nav {
        display: flex;
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        width: 44px;
        height: 80px;
        align-items: center;
        justify-content: center;
        background: none;
        border: none;
        color: #555;
        font-size: 22px;
        cursor: pointer;
        transition: color 0.15s;
    }
    .side-nav:hover { color: #ccc; }
    /* 12px inside the constrained viewer-body, i.e. at the image column edge. */
    .side-nav.left-nav { left: 12px; }
    .side-nav.right-nav { right: 12px; }
}
@media (min-width: 1200px) {
    .side-nav svg { width: 19px; height: 19px; }
}
@media (min-width: 768px) {
    /* Header + bottom bar stay full-width so their divider lines span the whole
       screen, with their content constrained to the gallery column via padding.
       The image area (viewer-body) IS constrained to the column so the side-nav
       arrows, positioned inside it, sit at the column edge. */
    .viewer-header {
        padding-left: max(14px, calc((100% - 1400px) / 2));
        padding-right: max(14px, calc((100% - 1400px) / 2));
    }
    .viewer-body {
        width: 100%;
        max-width: 1428px;
        margin-left: auto;
        margin-right: auto;
        padding: 0 14px;
    }
}

/* ── Selection bar ── */
#sel-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: #080808;
    border-top: 1px solid #2a2a2a;
    padding: 12px 16px;
    padding-top: 14px;
    padding-bottom: calc(12px + env(safe-area-inset-bottom));
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    z-index: 50;
}
/* Short background skirt below the bar to cover the few-px gap that can flash
   during a fast scroll while Safari's URL bar collapses. */
#sel-bar::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    height: 24px;
    background: #080808;
    pointer-events: none;
}
@media (min-width: 768px) {
    /* Desktop: group the content in the center rather than spreading it edge-to-edge. */
    #sel-bar { padding-top: 26px; padding-bottom: 34px; justify-content: center; gap: 28px; }
}
#sel-bar.open { display: flex; animation: sel-bar-up 0.18s ease-out; }
@keyframes sel-bar-up {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
}
.sel-left { display: flex; align-items: center; gap: 9px; }
.sel-info { display: flex; flex-direction: column; gap: 2px; }
#sel-count { font-size: 12px; color: #888; letter-spacing: 1px; line-height: 1; }
#sel-size { font-size: 12px; color: #555; letter-spacing: 1px; line-height: 1; }
#desel-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid #2a2a2a;
    border-radius: 50%;
    cursor: pointer;
    padding: 0;
    width: 30px;
    height: 30px;
    color: #fff;
}
#desel-btn svg { display: block; }
.sel-bar-btns { display: flex; gap: 8px; }
#dl-all-btn, #del-btn {
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 1px;
    background: #080808;
    border: 1px solid #333;
    border-radius: 11px;
    padding: 8px 14px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
}
#dl-all-btn { color: #fff; }
#del-btn { color: #e8554f; border-color: #3a1a1a; }

/* ── Confirm popup ── */
#confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 200;
    display: none;
    pointer-events: none;
    align-items: center;
    justify-content: center;
}
#confirm-overlay.open { display: flex; pointer-events: auto; }
#confirm-box {
    background: #080808;
    border: 1px solid #2a2a2a;
    border-radius: 11px;
    padding: 24px 20px;
    width: 80%;
    max-width: 280px;
    text-align: center;
}
#confirm-box p {
    font-size: 12px;
    color: #aaa;
    letter-spacing: 1px;
    margin-bottom: 20px;
    line-height: 1.6;
}

/* Magic-mode editor */
#magic-overlay {
    position: fixed;
    inset: 0;
    z-index: 450;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 18px;
    background: rgba(0,0,0,.82);
}
#magic-overlay.open { display: flex; }
.magic-panel {
    width: min(680px, 100%);
    max-height: 88vh;
    overflow: auto;
    padding: 20px;
    background: #101010;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
}
.magic-head, .magic-actions { display:flex; justify-content:space-between; align-items:center; gap:12px; }
.magic-head { margin-bottom: 16px; }
.magic-head h2 { font-size: 15px; letter-spacing: 1px; color:#fff; }
.magic-config { padding: 4px 0 14px; }
.magic-field { display:block; margin-top:12px; font-size:11px; letter-spacing:1px; color:#777; }
.magic-field input[type="password"], .magic-field input[type="url"] {
    width:100%; margin-top:7px; padding:10px; color:#eee; background:#080808;
    border:1px solid #292929; border-radius:6px; font:13px 'CamFont', monospace;
}
.magic-key-state { display:block; margin-top:6px; color:#555; font-size:10px; }
.magic-clear { display:flex; align-items:center; gap:8px; margin-top:9px; color:#777; font-size:10px; }
.magic-row { border-top:1px solid #242424; padding:14px 0; }
.magic-row input, .magic-row textarea {
    width:100%; margin-top:8px; padding:10px; color:#eee; background:#080808;
    border:1px solid #292929; border-radius:6px; font:13px 'CamFont', monospace;
    -webkit-user-select:text; user-select:text;
}
.magic-row textarea { min-height:90px; resize:vertical; }
.magic-actions { margin-top:16px; }
.magic-panel button {
    padding:8px 12px; color:#ddd; background:#181818; border:1px solid #333;
    border-radius:7px; font:11px 'CamFont', monospace; letter-spacing:1px; cursor:pointer;
}
.magic-panel button.primary { color:#111; background:#fff; border-color:#fff; }
.magic-remove { float:right; color:#d88 !important; }
#magic-status { min-height:16px; margin-top:12px; color:#888; font-size:11px; }
.confirm-btns { display: flex; gap: 10px; }
.confirm-btns button {
    flex: 1;
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 2px;
    padding: 8px 16px;
    border-radius: 11px;
    cursor: pointer;
    background: #080808;
    border: 1px solid #333;
}
#confirm-yes { color: #e03030; border-color: #3a1a1a; }
#confirm-no  { color: #aaa; }
.empty {
    text-align: center;
    color: #333;
    font-size: 13px;
    letter-spacing: 2px;
    margin-top: 60px;
}
#top-btn {
    position: fixed;
    bottom: 20px;
    right: 22px;
    width: 38px;
    height: 38px;
    background: #080808;
    border: 1px solid #2a2a2a;
    border-radius: 50%;
    color: #fff;
    display: none;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 40;
    transition: bottom 0.18s ease;
}
#top-btn.visible { display: flex; }
/* Lift it above the selection bar when that bar is open. */
body:has(#sel-bar.open) #top-btn { bottom: calc(76px + env(safe-area-inset-bottom)); }
@media (min-width: 768px) {
    body:has(#sel-bar.open) #top-btn { bottom: 104px; }
}
#drag-select {
    position: fixed;
    border: 1px solid rgba(255,255,255,0.35);
    background: rgba(255,255,255,0.05);
    pointer-events: none;
    z-index: 30;
    display: none;
}
#dl-progress {
    position: fixed;
    left: 50%;
    transform: translateX(-50%);
    width: 60%;                  /* centered, short enough to clear the go-up circle */
    max-width: 360px;
    bottom: 35px;                 /* resting — tracks the go-up button */
    height: 10px;
    background: #080808;          /* track — matches the selection overlay */
    border: 1px solid #2a2a2a;
    border-radius: 999px;
    overflow: hidden;
    z-index: 300;
    display: none;
    pointer-events: none;
    transition: bottom 0.18s ease;
}
/* Rise with the go-up button when the selection bar is open. */
body:has(#sel-bar.open) #dl-progress {
    bottom: calc(91px + env(safe-area-inset-bottom));
}
@media (min-width: 768px) {
    body:has(#sel-bar.open) #dl-progress { bottom: 119px; }
}
#dl-progress.active { display: block; }
#dl-progress-fill {
    height: 100%;
    width: 0;
    background: #fff;
    border-radius: 999px;
    transition: width 0.18s linear;
}
</style>
</head>
<body>

<header>
  <img class="logo" src="/logo" alt="OptoCam">
</header>

<div class="toolbar">
  <div class="meta-row">
    <div class="meta">
      <span>{{ count }} IMAGE{% if count != 1 %}S{% endif %}</span>
      <span>{{ free_space }} FREE</span>
    </div>
    <div class="ctrl-group"><button onclick="openMagicEditor()">MAGIC</button></div>
    {% if has_media %}
    <div class="ctrl-group density-group" id="density-group">
      <button data-val="2" class="active" onclick="setDensity('2')" aria-label="2 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="5" height="5"/><rect x="8" y="1" width="5" height="5"/>
          <rect x="1" y="8" width="5" height="5"/><rect x="8" y="8" width="5" height="5"/>
        </svg>
      </button>
      <button data-val="3" onclick="setDensity('3')" aria-label="3 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="3" height="3"/><rect x="5.5" y="1" width="3" height="3"/><rect x="10" y="1" width="3" height="3"/>
          <rect x="1" y="5.5" width="3" height="3"/><rect x="5.5" y="5.5" width="3" height="3"/><rect x="10" y="5.5" width="3" height="3"/>
          <rect x="1" y="10" width="3" height="3"/><rect x="5.5" y="10" width="3" height="3"/><rect x="10" y="10" width="3" height="3"/>
        </svg>
      </button>
      <button data-val="4" onclick="setDensity('4')" aria-label="4 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="2.25" height="2.25"/><rect x="4.25" y="1" width="2.25" height="2.25"/><rect x="7.5" y="1" width="2.25" height="2.25"/><rect x="10.75" y="1" width="2.25" height="2.25"/>
          <rect x="1" y="4.25" width="2.25" height="2.25"/><rect x="4.25" y="4.25" width="2.25" height="2.25"/><rect x="7.5" y="4.25" width="2.25" height="2.25"/><rect x="10.75" y="4.25" width="2.25" height="2.25"/>
          <rect x="1" y="7.5" width="2.25" height="2.25"/><rect x="4.25" y="7.5" width="2.25" height="2.25"/><rect x="7.5" y="7.5" width="2.25" height="2.25"/><rect x="10.75" y="7.5" width="2.25" height="2.25"/>
          <rect x="1" y="10.75" width="2.25" height="2.25"/><rect x="4.25" y="10.75" width="2.25" height="2.25"/><rect x="7.5" y="10.75" width="2.25" height="2.25"/><rect x="10.75" y="10.75" width="2.25" height="2.25"/>
        </svg>
      </button>
    </div>
    {% endif %}
  </div>
  {% if has_media %}
  <div class="controls">
    <div class="ctrl-group" id="filter-group">
      <button data-val="all" class="active" onclick="setFilter('all')">ALL</button>
      <button data-val="photos" onclick="setFilter('photos')">PHOTOS</button>
      <button data-val="gifs" onclick="setFilter('gifs')">GIFS</button>
    </div>
    <div class="ctrl-group" id="order-group">
      <button data-val="newest" class="active" onclick="setOrder('newest')">NEWEST</button>
      <button data-val="oldest" onclick="setOrder('oldest')">OLDEST</button>
    </div>
    <div class="ctrl-group density-group" id="desktop-density-group">
      <button data-val="5" class="active" onclick="setDesktopDensity('5')" aria-label="5 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="5" height="5"/><rect x="8" y="1" width="5" height="5"/>
          <rect x="1" y="8" width="5" height="5"/><rect x="8" y="8" width="5" height="5"/>
        </svg>
      </button>
      <button data-val="6" onclick="setDesktopDensity('6')" aria-label="6 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="3" height="3"/><rect x="5.5" y="1" width="3" height="3"/><rect x="10" y="1" width="3" height="3"/>
          <rect x="1" y="5.5" width="3" height="3"/><rect x="5.5" y="5.5" width="3" height="3"/><rect x="10" y="5.5" width="3" height="3"/>
          <rect x="1" y="10" width="3" height="3"/><rect x="5.5" y="10" width="3" height="3"/><rect x="10" y="10" width="3" height="3"/>
        </svg>
      </button>
      <button data-val="7" onclick="setDesktopDensity('7')" aria-label="7 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="2.25" height="2.25"/><rect x="4.25" y="1" width="2.25" height="2.25"/><rect x="7.5" y="1" width="2.25" height="2.25"/><rect x="10.75" y="1" width="2.25" height="2.25"/>
          <rect x="1" y="4.25" width="2.25" height="2.25"/><rect x="4.25" y="4.25" width="2.25" height="2.25"/><rect x="7.5" y="4.25" width="2.25" height="2.25"/><rect x="10.75" y="4.25" width="2.25" height="2.25"/>
          <rect x="1" y="7.5" width="2.25" height="2.25"/><rect x="4.25" y="7.5" width="2.25" height="2.25"/><rect x="7.5" y="7.5" width="2.25" height="2.25"/><rect x="10.75" y="7.5" width="2.25" height="2.25"/>
          <rect x="1" y="10.75" width="2.25" height="2.25"/><rect x="4.25" y="10.75" width="2.25" height="2.25"/><rect x="7.5" y="10.75" width="2.25" height="2.25"/><rect x="10.75" y="10.75" width="2.25" height="2.25"/>
        </svg>
      </button>
    </div>
  </div>
  {% endif %}
</div>

{% if files %}
<div class="grid" id="grid">
  {% for f in files %}
  <div class="item" data-file="{{ f }}" data-idx="{{ loop.index0 }}">
    <div class="img-wrap">
      <button class="img-btn" onclick="openViewer(this)">
        <img src="/thumb/{{ f }}" loading="lazy" alt="{{ f }}" draggable="false">
      </button>
      {% if f.lower().endswith('.gif') %}<span class="gif-badge">GIF</span><div class="grid-spin"></div>{% endif %}
      {% if f in audio_set %}<span class="audio-badge" aria-label="Moment"><svg viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg"><path d="M2 7h3l4-3v10l-4-3H2z" fill="#fff"/><path d="M11.5 6.2c1.8 1.5 1.8 4.1 0 5.6M13.7 4.3c3 2.6 3 6.8 0 9.4" stroke="#fff" stroke-width="1.4" stroke-linecap="round" fill="none"/></svg></span>{% endif %}
      {% if f in ai_set %}<span class="ai-badge" aria-label="AI generated">AI</span>{% endif %}
      {% if f in magic_set %}<span class="magic-badge" aria-label="Magic mode photo" title="Magic mode photo"><svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M5 15L15 5M12.5 3.5l4 4M4 3v3M2.5 4.5h3M15.5 13v4M13.5 15h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/></svg></span>{% endif %}
      {% if f in magic_processing_set %}<span class="magic-processing-badge" aria-label="AI processing" title="AI processing"></span>{% endif %}
      <button class="sel-circle" onclick="toggleSel(event, this)"></button>
      <a class="dl-icon" href="/photo/{{ f }}" download="{{ f }}" onclick="downloadFile(event, '{{ f }}')">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  {% endfor %}
</div>
<div class="empty" id="empty-msg" style="display:none">&gt; NONE_</div>
{% else %}
<div class="empty">&gt; NO IMAGES_</div>
{% endif %}

<!-- Viewer -->
<div id="viewer">
  <div class="viewer-header" onclick="event.stopPropagation()">
    <button class="viewer-back" onclick="closeViewer()"><svg viewBox="0 0 16 16" width="13" height="13" xmlns="http://www.w3.org/2000/svg" style="position:relative;top:-2px;"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>BACK</button>
    <div class="viewer-info">
      <span class="viewer-fname"></span>
      <span class="viewer-pos"></span>
    </div>
    <div class="viewer-controls">
      <button class="viewer-del" id="viewer-del" onclick="confirmDeleteCurrent()" aria-label="Delete">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M2 3.5h12M5.5 3.5V1.5h5v2M3.5 3.5l.8 10h7.4l.8-10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </button>
      <button class="viewer-hq" id="viewer-hq" onclick="toggleHQ()">HQ</button>
      <a class="viewer-dl" id="viewer-dl" href="#" download>
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  <div class="viewer-body" id="viewer-body" onclick="if(window.innerWidth>=1200)closeViewer()">
    <div class="spinner" id="spinner"></div>
    <img id="viewer-img" src="" alt="" onload="imgLoaded()" onerror="imgFailed()" onclick="event.stopPropagation()" style="display:none;">
    <span class="audio-badge viewer-audio-badge" id="viewer-audio-badge" aria-label="Moment"><svg viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg"><path d="M2 7h3l4-3v10l-4-3H2z" fill="#fff"/><path d="M11.5 6.2c1.8 1.5 1.8 4.1 0 5.6M13.7 4.3c3 2.6 3 6.8 0 9.4" stroke="#fff" stroke-width="1.4" stroke-linecap="round" fill="none"/></svg></span>
    <audio id="viewer-audio" preload="auto"></audio>
    <button class="side-nav left-nav" onclick="event.stopPropagation();stepViewer(-1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="side-nav right-nav" onclick="event.stopPropagation();stepViewer(1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
  <div class="viewer-info-m">
    <span class="viewer-fname"></span>
    <span class="viewer-pos"></span>
  </div>
  <div class="viewer-nav">
    <button class="nav-btn" onclick="stepViewer(-1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="nav-btn" onclick="stepViewer(1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
</div>

<!-- Footer -->
<footer style="text-align:center; padding: 28px 0 20px; font-size:11px; color:#444; letter-spacing:1px;">Doruk Kumkumo&#287;lu 2026</footer>

<!-- Drag selection box -->
<div id="drag-select"></div>

<!-- Back to top -->
<button id="top-btn" onclick="window.scrollTo({top:0,behavior:'smooth'})"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M3 10l5-5 5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>

<!-- Selection bar -->
<div id="sel-bar">
  <div class="sel-left">
    <button id="desel-btn" onclick="deselectAll()" aria-label="Clear selection">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </button>
    <div class="sel-info">
      <span id="sel-count"></span>
      <span id="sel-size"></span>
    </div>
  </div>
  <div class="sel-bar-btns">
    <button id="dl-all-btn" onclick="downloadSelected()">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </svg>SAVE</button>
    <button id="del-btn" onclick="confirmDelete()">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M2 3.5h12M5.5 3.5V1.5h5v2M3.5 3.5l.8 10h7.4l.8-10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </svg>DELETE</button>
  </div>
</div>

<!-- Confirm delete popup -->
<div id="confirm-overlay" style="display:none">
  <div id="confirm-box">
    <p id="confirm-msg"></p>
    <div class="confirm-btns">
      <button id="confirm-yes" onclick="confirmYes()">DELETE</button>
      <button id="confirm-no" onclick="closeConfirm()">CANCEL</button>
    </div>
  </div>
</div>

<!-- Magic mode editor -->
<div id="magic-overlay">
  <div class="magic-panel">
    <div class="magic-head">
      <h2>MAGIC MODES</h2>
      <button onclick="closeMagicEditor()">CLOSE</button>
    </div>
    <div class="magic-config">
      <label class="magic-field">OPENAI API KEY
        <input id="magic-api-key" type="password" autocomplete="new-password" placeholder="Enter OpenAI API key" onfocus="prepareApiKeyEdit()">
        <span class="magic-key-state" id="magic-key-state"></span>
      </label>
      <label class="magic-clear"><input id="magic-clear-key" type="checkbox"> CLEAR SAVED API KEY</label>
      <label class="magic-field">PROXY
        <input id="magic-proxy" type="url" inputmode="url" placeholder="http://proxy.example:7890">
      </label>
    </div>
    <div id="magic-list"></div>
    <div class="magic-actions">
      <button onclick="addMagicMode()">+ ADD MODE</button>
      <button class="primary" onclick="saveMagicModes()">SAVE</button>
    </div>
    <div id="magic-status"></div>
  </div>
</div>

<!-- Download progress pill -->
<div id="dl-progress"><div id="dl-progress-fill"></div></div>

<script>
// allFiles: every media file, newest-first (the order the grid is rendered in).
// files: the currently visible subset, in display order — what the viewer walks.
let allFiles = {{ files_json | safe }};
let files = allFiles.slice();
const gifs = new Set({{ gifs_json | safe }});
const audioPhotos = new Set({{ audio_json | safe }});
const aiPhotos = new Set({{ ai_json | safe }});
const sizes = {{ sizes_json | safe }};   // filename -> bytes
const selected = new Set();
let magicModes = {{ magic_modes_json | safe }};
let magicSettings = {{ magic_settings_json | safe }};
let magicPhotoStates = {{ magic_photo_states_json | safe }};

function magicStatusIsProcessing(status) {
    return status === 'queued' || status === 'processing' || status === 'retrying';
}

async function pollMagicPhotoStates() {
    if (!Object.values(magicPhotoStates).some(item => magicStatusIsProcessing(item.status))) return;
    try {
        const response = await fetch('/api/magic-photo-status', {cache: 'no-store'});
        if (!response.ok) throw new Error('status request failed');
        const nextStates = (await response.json()).photos || {};
        let completed = false;
        document.querySelectorAll('.item[data-file]').forEach(item => {
            const filename = item.dataset.file;
            const before = magicPhotoStates[filename];
            const after = nextStates[filename];
            if (before && magicStatusIsProcessing(before.status)
                    && after && !magicStatusIsProcessing(after.status)) completed = true;
            const badge = item.querySelector('.magic-processing-badge');
            if (badge && (!after || !magicStatusIsProcessing(after.status))) badge.remove();
        });
        magicPhotoStates = nextStates;
        if (completed && !magicEditorIsOpen()
                && !document.getElementById('viewer').classList.contains('open')) {
            location.reload();
            return;
        }
    } catch (error) {
        // A brief network interruption should not remove the processing icon.
    }
    if (Object.values(magicPhotoStates).some(item => magicStatusIsProcessing(item.status))) {
        setTimeout(pollMagicPhotoStates, 3000);
    }
}
setTimeout(pollMagicPhotoStates, 1500);

function renderMagicSettings() {
    const state = document.getElementById('magic-key-state');
    state.textContent = magicSettings.api_key_configured
        ? 'SAVED: ' + magicSettings.api_key_masked
        : 'NOT CONFIGURED';
    const input = document.getElementById('magic-api-key');
    input.value = magicSettings.api_key_configured ? magicSettings.api_key_masked : '';
    input.dataset.masked = magicSettings.api_key_configured ? 'true' : 'false';
    document.getElementById('magic-clear-key').checked = false;
    document.getElementById('magic-proxy').value = magicSettings.proxy || '';
}

function prepareApiKeyEdit() {
    const input = document.getElementById('magic-api-key');
    if (input.dataset.masked === 'true') {
        input.value = '';
        input.dataset.masked = 'false';
    }
}

function renderMagicModes() {
    const list = document.getElementById('magic-list');
    list.textContent = '';
    magicModes.forEach((mode, index) => {
        const row = document.createElement('div');
        row.className = 'magic-row';
        row.dataset.id = mode.id || '';
        const remove = document.createElement('button');
        remove.className = 'magic-remove';
        remove.textContent = 'REMOVE';
        remove.onclick = () => { magicModes.splice(index, 1); renderMagicModes(); };
        const title = document.createElement('input');
        title.maxLength = 18;
        title.placeholder = 'Mode name, e.g. Cheese';
        title.value = mode.title || '';
        title.oninput = () => { magicModes[index].title = title.value; };
        const prompt = document.createElement('textarea');
        prompt.maxLength = 2000;
        prompt.placeholder = 'Transformation prompt';
        prompt.value = mode.prompt || '';
        prompt.oninput = () => { magicModes[index].prompt = prompt.value; };
        row.append(remove, title, prompt);
        list.appendChild(row);
    });
    if (!magicModes.length) {
        const empty = document.createElement('div');
        empty.className = 'magic-row';
        empty.textContent = '> NO MAGIC MODES_';
        list.appendChild(empty);
    }
}

function openMagicEditor() {
    cancelDragSelection();
    renderMagicModes();
    renderMagicSettings();
    document.getElementById('magic-status').textContent = '';
    document.getElementById('magic-overlay').classList.add('open');
}
function closeMagicEditor() { document.getElementById('magic-overlay').classList.remove('open'); }
function addMagicMode() {
    magicModes.push({id:'', title:'New Magic', prompt:'Describe how the photo should be transformed.'});
    renderMagicModes();
    document.querySelector('#magic-list .magic-row:last-child input')?.focus();
}
async function saveMagicModes() {
    const status = document.getElementById('magic-status');
    status.textContent = 'SAVING...';
    try {
        const apiKeyInput = document.getElementById('magic-api-key');
        const apiKey = apiKeyInput.dataset.masked === 'true' ? '' : apiKeyInput.value;
        const settingsResponse = await fetch('/api/magic-settings', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                api_key: apiKey,
                clear_api_key: document.getElementById('magic-clear-key').checked,
                proxy: document.getElementById('magic-proxy').value
            })
        });
        if (!settingsResponse.ok) throw new Error(await settingsResponse.text());
        magicSettings = await settingsResponse.json();
        renderMagicSettings();
        const response = await fetch('/api/magic-modes', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({modes: magicModes})
        });
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        magicModes = payload.modes;
        renderMagicModes();
        status.textContent = 'SAVED - LONG PRESS THE CAMERA BUTTON TO SELECT';
    } catch (error) {
        status.textContent = 'SAVE FAILED: ' + error.message;
    }
}

function formatSize(bytes) {
    // Decimal units (1 MB = 1,000,000 bytes) to match how macOS/iOS report sizes.
    if (bytes >= 1e9) return Math.round(bytes / 1e9) + ' GB';
    if (bytes >= 1e6) return Math.round(bytes / 1e6) + ' MB';
    if (bytes >= 1e3) return Math.round(bytes / 1e3) + ' KB';
    return bytes + ' B';
}
let viewerIdx = 0;
let hqMode = false;
let viewerScrollY = 0;   // page scroll position saved while the viewer is open

let curFilter = 'all';   // all | photos | gifs
let curOrder = 'newest'; // newest | oldest
let curDensity = '2';          // mobile grid columns: 2 | 3 | 4
let curDesktopDensity = '5';   // desktop grid columns: 5 | 6 | 7

const grid = document.getElementById('grid');
// filename -> grid item element, so we can show/hide and reorder in place.
const itemEls = {};
if (grid) document.querySelectorAll('.item').forEach(el => { itemEls[el.dataset.file] = el; });

function isGif(f) { return gifs.has(f); }
function hasAudio(f) { return audioPhotos.has(f); }

function matchesFilter(f) {
    if (curFilter === 'photos') return !isGif(f);
    if (curFilter === 'gifs') return isGif(f);
    return true;
}

// Recompute the visible subset, reorder the grid to match, and keep `files`
// (used by the viewer) in lockstep so navigation stays correct.
function applyView() {
    let view = allFiles.filter(matchesFilter);
    if (curOrder === 'oldest') view = view.slice().reverse();
    files = view;
    const viewSet = new Set(view);
    if (grid) {
        // Append visible items in view order; appendChild moves existing nodes,
        // so the grid ends up in exactly this order.
        view.forEach(f => { const el = itemEls[f]; if (el) { el.style.display = ''; grid.appendChild(el); } });
        allFiles.forEach(f => { if (!viewSet.has(f)) { const el = itemEls[f]; if (el) el.style.display = 'none'; } });
        grid.style.display = view.length === 0 ? 'none' : '';
    }
    const span = document.querySelector('.meta span');
    if (span) span.textContent = view.length + ' IMAGE' + (view.length !== 1 ? 'S' : '');
    const empty = document.getElementById('empty-msg');
    if (empty) empty.style.display = view.length === 0 ? 'block' : 'none';
}

function setActive(groupId, val) {
    document.querySelectorAll('#' + groupId + ' button')
        .forEach(b => b.classList.toggle('active', b.dataset.val === val));
}

function setFilter(v) {
    if (v === curFilter) return;
    curFilter = v;
    setActive('filter-group', v);
    applyView();
}

function setOrder(v) {
    if (v === curOrder) return;
    curOrder = v;
    setActive('order-group', v);
    applyView();
}

// Grid density: just toggles a CSS class (6-col layout applies on mobile only).
// Independent of filtering/ordering, so it never touches the files arrays.
function setDensity(v) {
    if (v === curDensity) return;
    curDensity = v;
    setActive('density-group', v);
    if (grid) {
        grid.classList.remove('cols-2', 'cols-3', 'cols-4');
        grid.classList.add('cols-' + v);
    }
    try { localStorage.setItem('optocam_density', v); } catch (e) {}
}

// Restore the saved density on load.
try {
    const d = localStorage.getItem('optocam_density');
    if (d && ['2', '3', '4'].includes(d)) setDensity(d);
} catch (e) {}

function setDesktopDensity(v) {
    if (v === curDesktopDensity) return;
    curDesktopDensity = v;
    setActive('desktop-density-group', v);
    if (grid) {
        grid.classList.remove('dcols-5', 'dcols-6', 'dcols-7');
        grid.classList.add('dcols-' + v);
    }
    try { localStorage.setItem('optocam_density_desktop', v); } catch (e) {}
}

// Restore the saved desktop density on load.
try {
    const d = localStorage.getItem('optocam_density_desktop');
    if (d && ['5', '6', '7'].includes(d)) setDesktopDensity(d);
} catch (e) {}


function setItemSelected(item, sel) {
    const f = item.dataset.file;
    if (sel) { selected.add(f); item.classList.add('sel'); }
    else { selected.delete(f); item.classList.remove('sel'); }
}

function refreshSelBar() {
    const bar = document.getElementById('sel-bar');
    if (selected.size > 0) {
        bar.classList.add('open');
        document.getElementById('sel-count').textContent = selected.size + ' SELECTED';
        let bytes = 0;
        selected.forEach(f => { bytes += sizes[f] || 0; });
        document.getElementById('sel-size').textContent = formatSize(bytes);
    } else {
        bar.classList.remove('open');
    }
}

function toggleSel(e, circle) {
    e.stopPropagation();
    const item = circle.closest('.item');
    setItemSelected(item, !item.classList.contains('sel'));
    refreshSelBar();
}

function preloadAhead(idx) {
    const ahead = files.slice(idx + 1, idx + 6);
    if (ahead.length === 0) return;
    fetch('/preload-ahead', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: ahead})
    });
}

function openViewer(el) {
    // Resolve the index from the item's filename against the live `files`
    // array so selection stays correct after deletions reindex the grid.
    const f = el.closest('.item').dataset.file;
    const idx = files.indexOf(f);
    if (idx === -1) return;
    viewerIdx = idx;
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
    updateViewer();
    preloadAhead(idx);
    document.getElementById('viewer').classList.add('open');
    // Fully lock the background so the scrolled grid can't bleed through the
    // overlay on iOS Safari (overflow:hidden alone doesn't lock it).
    viewerScrollY = window.scrollY;
    document.body.style.position = 'fixed';
    document.body.style.top = (-viewerScrollY) + 'px';
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.overflow = 'hidden';
}

function closeViewer() {
    const audio = document.getElementById('viewer-audio');
    audio.pause();
    audio.removeAttribute('src');
    document.getElementById('viewer').classList.remove('open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.overflow = '';
    window.scrollTo(0, viewerScrollY);
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
}

function toggleHQ() {
    if (isGif(files[viewerIdx])) return;   // GIFs have no HQ variant
    hqMode = !hqMode;
    document.getElementById('viewer-hq').classList.toggle('active', hqMode);
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    img.style.display = 'none';
    spinner.classList.add('active');
    img.src = hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200';
}

function updateViewer() {
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    const gif = isGif(f);
    const audio = document.getElementById('viewer-audio');
    const audioBadge = document.getElementById('viewer-audio-badge');
    audio.pause();
    audio.currentTime = 0;
    audioBadge.classList.remove('playing');
    audio.onplay = () => audioBadge.classList.add('playing');
    audio.onpause = () => audioBadge.classList.remove('playing');
    audio.onended = () => audioBadge.classList.remove('playing');
    if (hasAudio(f)) {
        audio.src = '/audio/' + encodeURIComponent(f.replace(/[.]jpg$/i, '.wav'));
        audioBadge.style.display = 'flex';
        audio.play().catch(() => { /* browser may require one more user gesture */ });
    } else {
        audio.removeAttribute('src');
        audioBadge.style.display = 'none';
    }
    resetZoom(false);   // each image opens unzoomed
    img.style.display = 'none';
    spinner.classList.add('active');
    // GIFs always play their full animated original; photos use the sized thumb / HQ
    img.src = gif ? '/gif/' + f : (hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200');
    document.getElementById('viewer-hq').style.display = gif ? 'none' : 'flex';
    const posText = (viewerIdx + 1) + ' / ' + files.length;
    document.querySelectorAll('.viewer-pos').forEach(e => e.textContent = posText);
    document.querySelectorAll('.viewer-fname').forEach(e => e.textContent = f);
    const dl = document.getElementById('viewer-dl');
    dl.href = '/photo/' + f;
    dl.download = f;
    dl.onclick = hasAudio(f) ? (event => downloadFile(event, f)) : null;
}

function imgLoaded() {
    document.getElementById('spinner').classList.remove('active');
    document.getElementById('viewer-img').style.display = 'block';
}

function imgFailed() {
    // Don't leave the viewer spinning forever if a load fails.
    document.getElementById('spinner').classList.remove('active');
}

// --- Progressive GIF previews in the grid ------------------------------
// Grid cells first show a tiny static first-frame JPEG (fast, reliable, never
// a broken "?"). Then, only for GIFs scrolled near the viewport, we fetch the
// full animated original in the background — just a couple at a time so we
// never stampede the Pi the way loading every GIF at once did — and swap it in
// once it has decoded. If the animated fetch fails, the static poster stays.
const GIF_CONCURRENCY = 2;
const gifQueue = [];
const gifQueued = new Set();
let gifActive = 0;

function pumpGifQueue() {
    while (gifActive < GIF_CONCURRENCY && gifQueue.length) {
        const item = gifQueue.shift();
        const gridImg = item.querySelector('img');
        if (!gridImg) continue;
        gifActive++;
        const stopSpin = () => { const sp = item.querySelector('.grid-spin'); if (sp) sp.remove(); };
        const loader = new Image();
        // Animation ready → swap poster for the live GIF and drop the spinner.
        loader.onload = () => { gridImg.src = loader.src; stopSpin(); gifActive--; pumpGifQueue(); };
        // Failed → keep the static poster, but stop spinning (don't hang).
        loader.onerror = () => { stopSpin(); gifActive--; pumpGifQueue(); };
        loader.src = '/gif/' + encodeURIComponent(item.dataset.file);
    }
}

function enqueueGif(item) {
    const f = item.dataset.file;
    if (gifQueued.has(f)) return;
    gifQueued.add(f);
    gifQueue.push(item);
    pumpGifQueue();
}

const gifObserver = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries, obs) => {
        entries.forEach(e => {
            if (!e.isIntersecting) return;
            obs.unobserve(e.target);   // load once, then leave it animating
            enqueueGif(e.target);
        });
      }, { rootMargin: '300px' })
    : null;

// Show the spinner only once the static poster is actually on screen (its
// JPG has loaded); until then there's nothing to overlay. It's removed later
// when the animated GIF swaps in (or fails) via stopSpin().
function revealSpinnerWhenPosterLoads(item) {
    const sp = item.querySelector('.grid-spin');
    const img = item.querySelector('img');
    if (!sp || !img) return;
    if (img.complete && img.naturalWidth > 0) sp.classList.add('show');
    else img.addEventListener('load', () => sp.classList.add('show'), { once: true });
}

(function observeGifs() {
    Object.values(itemEls).forEach(item => {
        if (!isGif(item.dataset.file)) return;
        revealSpinnerWhenPosterLoads(item);
        if (gifObserver) gifObserver.observe(item);   // hidden/filtered items fire when shown
        else enqueueGif(item);                        // no observer: just load all (throttled)
    });
})();

window.addEventListener('load', () => fetch('/preload'));

window.addEventListener('scroll', () => {
    const btn = document.getElementById('top-btn');
    btn.classList.toggle('visible', window.scrollY > 300);
});

function stepViewer(dir) {
    const next = viewerIdx + dir;
    if (next >= 0 && next < files.length) {
        viewerIdx = next;
        hqMode = false;
        document.getElementById('viewer-hq').classList.remove('active');
        updateViewer();
        preloadAhead(viewerIdx);
    }
}

// Keyboard navigation in viewer
document.addEventListener('keydown', e => {
    const isDesktop = window.innerWidth >= 1200;
    const viewerOpen = document.getElementById('viewer').classList.contains('open');
    if (viewerOpen) {
        if (e.key === 'ArrowLeft') stepViewer(-1);
        else if (e.key === 'ArrowRight') stepViewer(1);
        else if (e.key === 'Escape') closeViewer();
    } else if (isDesktop) {
        if (e.key === 'Escape' && selected.size > 0) deselectAll();
        else if (e.key === 'Backspace' && selected.size > 0) { e.preventDefault(); confirmDelete(); }
    }
});

// Viewer: double-tap to zoom, drag to pan while zoomed, swipe to nav/close otherwise.
let touchStartX = 0, touchStartY = 0, multiTouch = false;
let zoomScale = 1, panX = 0, panY = 0;
let panOrigX = 0, panOrigY = 0, panning = false;
let lastTapTime = 0, lastTapX = 0, lastTapY = 0;
let pinching = false, pinchStartDist = 0, pinchStartScale = 1;
let pinchContentX = 0, pinchContentY = 0, pinchEndTime = 0;
const ZOOM = 2.5, MAX_ZOOM = 5;
const vb = document.getElementById('viewer-body');

function applyTransform(animate) {
    const img = document.getElementById('viewer-img');
    img.style.transition = animate ? 'transform 0.2s ease' : 'none';
    img.style.transform = 'translate(' + panX + 'px,' + panY + 'px) scale(' + zoomScale + ')';
    img.style.cursor = zoomScale > 1 ? 'grab' : '';
}

function resetZoom(animate) {
    zoomScale = 1; panX = 0; panY = 0; panning = false;
    applyTransform(animate);
}

function clampPan() {
    const img = document.getElementById('viewer-img');
    const maxX = Math.max(0, (img.offsetWidth * zoomScale - vb.clientWidth) / 2);
    const maxY = Math.max(0, (img.offsetHeight * zoomScale - vb.clientHeight) / 2);
    panX = Math.max(-maxX, Math.min(maxX, panX));
    panY = Math.max(-maxY, Math.min(maxY, panY));
}

// Zoom toward the tapped point; if already zoomed, zoom back out.
function toggleZoomAt(clientX, clientY) {
    if (zoomScale > 1) { resetZoom(true); return; }
    const img = document.getElementById('viewer-img');
    const r = vb.getBoundingClientRect();
    let px = (clientX - r.left) - r.width / 2;
    let py = (clientY - r.top) - r.height / 2;
    px = Math.max(-img.offsetWidth / 2, Math.min(img.offsetWidth / 2, px));
    py = Math.max(-img.offsetHeight / 2, Math.min(img.offsetHeight / 2, py));
    zoomScale = ZOOM;
    panX = -px * (ZOOM - 1);
    panY = -py * (ZOOM - 1);
    clampPan();
    applyTransform(true);
}

vb.addEventListener('touchstart', e => {
    if (e.touches.length >= 2) {           // begin pinch
        multiTouch = true; pinching = true; panning = false;
        const t0 = e.touches[0], t1 = e.touches[1];
        pinchStartDist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY) || 1;
        pinchStartScale = zoomScale;
        const r = vb.getBoundingClientRect();
        const mx = (t0.clientX + t1.clientX) / 2 - r.left - r.width / 2;
        const my = (t0.clientY + t1.clientY) / 2 - r.top - r.height / 2;
        pinchContentX = (mx - panX) / zoomScale;   // content point under the pinch centre
        pinchContentY = (my - panY) / zoomScale;
        return;
    }
    multiTouch = false; pinching = false; panning = false;
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    panOrigX = panX; panOrigY = panY;
}, {passive: true});

vb.addEventListener('touchmove', e => {
    if (pinching && e.touches.length >= 2) {
        e.preventDefault();               // our pinch — not the page's
        const t0 = e.touches[0], t1 = e.touches[1];
        const dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
        zoomScale = Math.max(1, Math.min(MAX_ZOOM, pinchStartScale * (dist / pinchStartDist)));
        const r = vb.getBoundingClientRect();
        const mx = (t0.clientX + t1.clientX) / 2 - r.left - r.width / 2;
        const my = (t0.clientY + t1.clientY) / 2 - r.top - r.height / 2;
        panX = mx - pinchContentX * zoomScale;     // keep that content point under the fingers
        panY = my - pinchContentY * zoomScale;
        clampPan();
        applyTransform(false);
        return;
    }
    if (pinching) return;
    if (zoomScale > 1) {
        const t = e.touches[0];
        const dx = t.clientX - touchStartX, dy = t.clientY - touchStartY;
        if (panning || Math.abs(dx) > 6 || Math.abs(dy) > 6) {
            panning = true;
            e.preventDefault();           // pan the image, don't scroll/swipe
            panX = panOrigX + dx;
            panY = panOrigY + dy;
            clampPan();
            applyTransform(false);
        }
    }
}, {passive: false});

vb.addEventListener('touchend', e => {
    if (pinching) {
        if (e.touches.length >= 2) return;
        pinching = false;
        pinchEndTime = Date.now();
        if (zoomScale <= 1.01) resetZoom(true);
        else { clampPan(); applyTransform(true); }
        if (e.touches.length === 1) {     // a finger remains → continue as a pan
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
            panOrigX = panX; panOrigY = panY;
            multiTouch = false; panning = false;
        }
        return;
    }
    if (multiTouch) { multiTouch = false; panning = false; return; }
    if (panning) { panning = false; return; }
    const ex = e.changedTouches[0].clientX, ey = e.changedTouches[0].clientY;
    const dx = ex - touchStartX, dy = ey - touchStartY;
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
        if (Date.now() - pinchEndTime < 400) return;   // ignore stray taps right after a pinch
        const now = Date.now();
        if (now - lastTapTime < 300 &&
            Math.abs(ex - lastTapX) < 40 && Math.abs(ey - lastTapY) < 40) {
            toggleZoomAt(ex, ey);
            lastTapTime = 0;
            return;
        }
        lastTapTime = now; lastTapX = ex; lastTapY = ey;
        return;
    }
    if (zoomScale > 1) return;             // while zoomed, drags pan — no nav/close
    if (Math.abs(dy) > Math.abs(dx) && dy > 60) { closeViewer(); return; }
    if (Math.abs(dx) > 50) {
        if (dx < 0) stepViewer(1);
        else stepViewer(-1);
    }
}, {passive: true});

// Block Safari's page pinch-zoom in the viewer and over the grid (we handle pinch there).
['gesturestart', 'gesturechange', 'gestureend'].forEach(function (evt) {
    document.addEventListener(evt, function (ev) {
        const viewerOpen = document.getElementById('viewer').classList.contains('open');
        const inGrid = ev.target && ev.target.closest && ev.target.closest('#grid');
        if (viewerOpen || inGrid) ev.preventDefault();
    }, {passive: false});
});

function deselectAll() {
    selected.forEach(f => {
        const el = document.querySelector(`.item[data-file="${f}"]`);
        if (el) el.classList.remove('sel');
    });
    selected.clear();
    document.getElementById('sel-bar').classList.remove('open');
}

function postZip(filenames) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/download-zip';
    filenames.forEach(f => {
        const inp = document.createElement('input');
        inp.type = 'hidden';
        inp.name = 'files';
        inp.value = f;
        form.appendChild(inp);
    });
    document.body.appendChild(form);
    form.submit();
    form.remove();
}

function downloadFile(event, f) {
    event.stopPropagation();
    if (!hasAudio(f)) return true;
    event.preventDefault();
    postZip([f]);
    return false;
}

function downloadSelected() {
    // A single selection downloads the image itself, not a one-file zip.
    if (selected.size === 1 && !hasAudio([...selected][0])) {
        const f = [...selected][0];
        const a = document.createElement('a');
        a.href = '/photo/' + encodeURIComponent(f);
        a.download = f;
        document.body.appendChild(a);
        a.click();
        a.remove();
        return;
    }
    const token = Date.now().toString(36) + Math.random().toString(36).slice(2);
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/download-zip';
    selected.forEach(f => {
        const inp = document.createElement('input');
        inp.type = 'hidden';
        inp.name = 'files';
        inp.value = f;
        form.appendChild(inp);
    });
    const tok = document.createElement('input');
    tok.type = 'hidden';
    tok.name = 'download_token';
    tok.value = token;
    form.appendChild(tok);
    document.body.appendChild(form);
    form.submit();
    form.remove();
    trackDownload(token);
}

// Poll the Pi for how much of the zip it has streamed, and fill the bottom line.
// (Reflects bytes the Pi has sent — runs slightly ahead of bytes received.)
function trackDownload(token) {
    const started = Date.now();
    let shown = false, lastSent = -1, lastChange = Date.now();
    const poll = setInterval(async () => {
        let total = 0, sent = 0, active = false, ok = false;
        try {
            const d = await (await fetch('/download-progress?token=' + token)).json();
            total = d.total; sent = d.sent; active = !!d.active; ok = true;
        } catch (e) { /* network blip — keep polling */ }
        if (sent !== lastSent) { lastSent = sent; lastChange = Date.now(); }

        // Only reveal the bar once the Pi is actually streaming bytes — so a
        // dismissed Safari download prompt (which never streams) shows nothing.
        if (total > 0 && sent > 0) {
            shown = true;
            showProgress(sent / total);
        }

        const now = Date.now();
        const complete = shown && total > 0 && sent >= total;
        const ended = shown && ok && total > 0 && !active;   // server finished/aborted
        // No time cap: keep the bar up as long as the Pi reports the stream is
        // active, however long the download takes.
        if (complete) {
            clearInterval(poll);
            showProgress(1);
            setTimeout(hideProgress, 400);
        } else if (ended) {
            clearInterval(poll);                              // aborted before finishing
            hideProgress();
        } else if ((!shown && now - started > 30000) ||      // prompt dismissed
                   (shown && now - lastChange > 30000)) {     // hard-freeze backstop
            clearInterval(poll);
            hideProgress();
        }
    }, 250);
}

function showProgress(frac) {
    document.getElementById('dl-progress').classList.add('active');
    document.getElementById('dl-progress-fill').style.width =
        (Math.max(0, Math.min(1, frac)) * 100) + '%';
}

function hideProgress() {
    document.getElementById('dl-progress').classList.remove('active');
    document.getElementById('dl-progress-fill').style.width = '0';
}

// null → confirming a selection delete; a filename → confirming that one image
// (from the viewer). The shared confirm popup dispatches on this in confirmYes().
let pendingDeleteFile = null;

function openConfirm(msgHtml) {
    document.getElementById('confirm-msg').innerHTML = msgHtml;
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'flex';
    ov.style.pointerEvents = 'auto';
}

function confirmDelete() {
    pendingDeleteFile = null;
    const n = selected.size;
    openConfirm('Delete ' + n + ' image' + (n !== 1 ? 's' : '') + '?<br>This cannot be undone.');
}

function confirmDeleteCurrent() {
    pendingDeleteFile = files[viewerIdx];
    openConfirm('Delete this image?<br>This cannot be undone.');
}

function confirmYes() {
    if (pendingDeleteFile !== null) deleteCurrent();
    else deleteSelected();
}

function closeConfirm() {
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'none';
    ov.style.pointerEvents = 'none';
}

async function deleteCurrent() {
    closeConfirm();
    const f = pendingDeleteFile;
    pendingDeleteFile = null;
    if (!f) return;
    const res = await fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: [f]})
    });
    if (!res.ok) return;
    const wasIdx = files.indexOf(f);   // slot to land on after removal
    const el = itemEls[f];
    if (el) el.remove();
    delete itemEls[f];
    selected.delete(f);
    audioPhotos.delete(f);
    const aidx = allFiles.indexOf(f);
    if (aidx !== -1) allFiles.splice(aidx, 1);
    refreshSelBar();
    applyView();   // rebuilds `files` (and count / empty state)
    if (files.length === 0) {
        closeViewer();
    } else {
        // Show whatever now occupies the deleted slot, or the new last image.
        viewerIdx = Math.min(Math.max(wasIdx, 0), files.length - 1);
        updateViewer();
    }
}

async function deleteSelected() {
    closeConfirm();
    const filesToDelete = [...selected];
    const res = await fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: filesToDelete})
    });
    if (res.ok) {
        filesToDelete.forEach(f => {
            const el = itemEls[f];
            if (el) el.remove();
            delete itemEls[f];
            selected.delete(f);
            audioPhotos.delete(f);
            let idx = allFiles.indexOf(f);
            if (idx !== -1) allFiles.splice(idx, 1);
        });
        document.getElementById('sel-bar').classList.remove('open');
        // Rebuild the visible view (also refreshes `files`, count and empty state).
        applyView();
    }
}

// Drag-to-select (desktop only)
let dragStart = null, dragMoved = false;
const dragBox = document.getElementById('drag-select');

function magicEditorIsOpen() {
    return document.getElementById('magic-overlay').classList.contains('open');
}

function cancelDragSelection() {
    dragStart = null;
    dragMoved = false;
    dragBox.style.display = 'none';
    document.body.style.userSelect = '';
}

document.addEventListener('mousedown', e => {
    if (window.innerWidth < 1200) return;
    if (magicEditorIsOpen() || e.target.closest('#magic-overlay,#confirm-overlay')) return;
    if (document.getElementById('viewer').classList.contains('open')) return;
    const blocked = e.target.closest('.img-btn,.sel-circle,.dl-icon,#sel-bar,#top-btn,header');
    if (blocked) return;
    dragStart = { x: e.clientX, y: e.clientY };
    dragMoved = false;
    document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', e => {
    if (!dragStart) return;
    if (magicEditorIsOpen()) { cancelDragSelection(); return; }
    const dx = e.clientX - dragStart.x, dy = e.clientY - dragStart.y;
    if (!dragMoved && Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
    dragMoved = true;
    const x1 = Math.min(dragStart.x, e.clientX);
    const y1 = Math.min(dragStart.y, e.clientY);
    const x2 = Math.max(dragStart.x, e.clientX);
    const y2 = Math.max(dragStart.y, e.clientY);
    dragBox.style.cssText = `display:block;left:${x1}px;top:${y1}px;width:${x2-x1}px;height:${y2-y1}px;`;
});

document.addEventListener('mouseup', e => {
    if (!dragStart) return;
    if (magicEditorIsOpen()) { cancelDragSelection(); return; }
    document.body.style.userSelect = '';
    dragBox.style.display = 'none';
    if (dragMoved) {
        const x1 = Math.min(dragStart.x, e.clientX);
        const y1 = Math.min(dragStart.y, e.clientY);
        const x2 = Math.max(dragStart.x, e.clientX);
        const y2 = Math.max(dragStart.y, e.clientY);
        document.querySelectorAll('.item').forEach(item => {
            const r = item.getBoundingClientRect();
            if (r.left < x2 && r.right > x1 && r.top < y2 && r.bottom > y1) {
                const f = item.dataset.file;
                if (!selected.has(f)) { selected.add(f); item.classList.add('sel'); }
            }
        });
        refreshSelBar();
    }
    cancelDragSelection();
});

// ── Touch multi-select (mobile/tablet) ──
// Tap a thumbnail's dot to select it. Once something is selected, glide
// sideways across thumbnails to "paint" more (a glide started on a dot works
// in any direction). Plain taps open the viewer; vertical drags still scroll.
(function () {
    if (!grid) return;
    const INTENT = 10;        // px of movement before we decide scroll vs. paint
    let painting = false;
    let decided = false;      // have we classified this gesture yet?
    let paintMode = true;     // true = select swept items, false = deselect
    let startItem = null;
    let startOnDot = false;
    let startX = 0, startY = 0;
    let suppressClick = false;
    const handled = new Set();

    function paint(item) {
        if (!item) return;
        const f = item.dataset.file;
        if (handled.has(f)) return;
        handled.add(f);
        setItemSelected(item, paintMode);
        refreshSelBar();
    }

    function beginPaint() {
        painting = true;
        suppressClick = true;          // swallow the click that ends the glide
        handled.clear();
        // Glide off an unselected thumb selects the swath; off a selected one
        // deselects it.
        paintMode = !startItem.classList.contains('sel');
        paint(startItem);
        if (navigator.vibrate) { try { navigator.vibrate(10); } catch (e) {} }
    }

    grid.addEventListener('touchstart', e => {
        if (window.innerWidth >= 1200) return;   // desktop uses mouse drag-select
        if (document.getElementById('viewer').classList.contains('open')) return;
        painting = false;
        decided = false;
        if (e.touches.length > 1) { startItem = null; return; }
        startItem = e.target.closest('.item');
        startOnDot = !!e.target.closest('.sel-circle');
        const t = e.touches[0];
        startX = t.clientX;
        startY = t.clientY;
        handled.clear();
    }, {passive: true});

    grid.addEventListener('touchmove', e => {
        if (!startItem) return;
        const t = e.touches[0];
        const dx = t.clientX - startX, dy = t.clientY - startY;
        if (!painting) {
            if (!decided) {
                if (Math.abs(dx) < INTENT && Math.abs(dy) < INTENT) return;
                decided = true;
                const horizontal = Math.abs(dx) > Math.abs(dy);
                const selectionMode = selected.size > 0;
                // Only a sideways glide starts painting — from a dot, or anywhere
                // once a selection exists. A vertical drag always scrolls, even when
                // it begins on a dot, so scrolling is never hijacked.
                if (horizontal && (startOnDot || selectionMode)) {
                    beginPaint();
                } else {
                    startItem = null;      // vertical / not eligible → let it scroll
                    return;
                }
            }
            if (!painting) return;
        }
        e.preventDefault();                // selecting — don't scroll the page
        const el = document.elementFromPoint(t.clientX, t.clientY);
        paint(el ? el.closest('.item') : null);
    }, {passive: false});

    function end() {
        painting = false;
        decided = false;
        startItem = null;
        startOnDot = false;
        setTimeout(() => { suppressClick = false; }, 350);  // fallback reset
    }
    grid.addEventListener('touchend', end);
    grid.addEventListener('touchcancel', end);

    // Swallow the click that fires right after a paint gesture so it doesn't
    // double-toggle the start thumbnail.
    document.addEventListener('click', e => {
        if (suppressClick) {
            e.preventDefault();
            e.stopPropagation();
            suppressClick = false;
        }
    }, true);
})();

// ── Pinch the grid to change density (mobile only): spread = fewer/larger,
// pinch = more/smaller columns. Steps through 2 → 3 → 4. ──
(function () {
    if (!grid) return;
    const STEP = 1.25;          // distance ratio that triggers one density step
    const COLS = ['2', '3', '4'];
    let pinching = false, baseDist = 0;

    function dist(e) {
        const a = e.touches[0], b = e.touches[1];
        return Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY);
    }

    function changeDensity(delta) {   // -1 = fewer columns, +1 = more columns
        let i = COLS.indexOf(curDensity);
        if (i === -1) i = 0;
        const ni = Math.max(0, Math.min(COLS.length - 1, i + delta));
        if (COLS[ni] !== curDensity) {
            setDensity(COLS[ni]);
            if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
        }
    }

    grid.addEventListener('touchstart', e => {
        if (window.innerWidth >= 768) return;   // density is a mobile feature
        if (e.touches.length === 2) { pinching = true; baseDist = dist(e); }
    }, {passive: true});

    grid.addEventListener('touchmove', e => {
        if (!pinching || e.touches.length < 2) return;
        e.preventDefault();                      // don't scroll/zoom the page
        const ratio = dist(e) / baseDist;
        if (ratio > STEP) { changeDensity(-1); baseDist = dist(e); }       // spread → fewer cols
        else if (ratio < 1 / STEP) { changeDensity(1); baseDist = dist(e); } // pinch → more cols
    }, {passive: false});

    grid.addEventListener('touchend', e => {
        if (e.touches.length < 2) pinching = false;
    }, {passive: true});
})();
</script>
</body>
</html>"""


def get_free_space():
    try:
        path = PHOTOS_DIR if os.path.exists(PHOTOS_DIR) else OPTOCAM_HOME
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_bsize
        if free >= 1024 ** 3:
            return f"{free / 1024**3:.1f} GB"
        return f"{free / 1024**2:.0f} MB"
    except:
        return "?"


def json_for_script(value):
    """Serialize user-editable data without allowing a closing script tag."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def get_magic_photo_states(files=None):
    """Return browser-safe Magic state keyed by original photo filename."""
    files = files if files is not None else list_media()
    known_files = set(files)
    states = {}
    for filename in files:
        metadata = read_magic_source_metadata(os.path.join(PHOTOS_DIR, filename))
        if metadata:
            states[filename] = {
                "status": str(metadata.get("status") or "complete"),
                "title": str(metadata.get("magic_mode_title") or "Magic")[:18],
            }

    # Backfill photos made before source-state sidecars were introduced by
    # following the source reference already stored on generated AI photos.
    for filename in files:
        ai_path = os.path.join(PHOTOS_DIR, filename + ".ai.json")
        try:
            with open(ai_path, encoding="utf-8") as handle:
                metadata = json.load(handle)
            source_name = os.path.basename(str(metadata.get("source") or ""))
            if source_name in known_files:
                states.setdefault(source_name, {
                    "status": "complete",
                    "title": str(metadata.get("magic_mode_title") or "Magic")[:18],
                })
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    # Also recognize jobs created by an older app process before its source
    # sidecar was written, so a reboot never makes an active job look ordinary.
    queue_dir = os.path.join(OPTOCAM_HOME, "magic_queue")
    try:
        queue_files = os.listdir(queue_dir)
    except OSError:
        queue_files = []
    for job_name in queue_files:
        if not job_name.endswith(".json"):
            continue
        try:
            with open(os.path.join(queue_dir, job_name), encoding="utf-8") as handle:
                job = json.load(handle)
            filename = os.path.basename(str(job.get("source_path") or ""))
            if filename in known_files:
                states.setdefault(filename, {
                    "status": "queued",
                    "title": str(job.get("title") or "Magic")[:18],
                })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return states


@app.route("/")
def index():
    # All media, newest-first. Filtering and ordering happen client-side so
    # changing a selector never reloads the page.
    files = list_media()
    gif_set = [f for f in files if f.lower().endswith(".gif")]
    audio_set = [f for f in files if paired_audio_filename(f)]
    ai_set = [f for f in files if is_ai_generated(os.path.join(PHOTOS_DIR, f))]
    magic_photo_states = get_magic_photo_states(files)
    magic_modes = MAGIC_MODE_STORE.load()
    magic_settings = read_magic_connection_settings(MAGIC_ENV_PATH)
    sizes = {}
    for f in files:
        try:
            sizes[f] = os.path.getsize(os.path.join(PHOTOS_DIR, f))
            audio_name = paired_audio_filename(f)
            if audio_name:
                sizes[f] += os.path.getsize(os.path.join(PHOTOS_DIR, audio_name))
        except OSError:
            sizes[f] = 0
    html = render_template_string(
        HTML, files=files, audio_set=set(audio_set), ai_set=set(ai_set),
        magic_set=set(magic_photo_states),
        magic_processing_set={
            name for name, state in magic_photo_states.items()
            if state["status"] in {"queued", "processing", "retrying"}
        },
        count=len(files), has_media=bool(files),
        free_space=get_free_space(),
        files_json=json.dumps(files), gifs_json=json.dumps(gif_set),
        audio_json=json.dumps(audio_set),
        ai_json=json.dumps(ai_set), magic_modes_json=json_for_script(magic_modes),
        magic_settings_json=json_for_script(magic_settings),
        magic_photo_states_json=json_for_script(magic_photo_states),
        sizes_json=json.dumps(sizes)
    )
    # The page embeds all CSS/JS inline, so never let the browser serve a stale
    # copy — otherwise UI changes appear not to take effect after a redeploy.
    # (Thumbnails/GIFs keep their own immutable caching, so this costs nothing.)
    resp = Response(html)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/logo")
def logo():
    return send_from_directory(OPTOCAM_HOME, "optocamlogo.svg", mimetype="image/svg+xml")


@app.route("/font/<filename>")
def font(filename):
    return send_from_directory(OPTOCAM_HOME, filename)


@app.route("/api/magic-modes", methods=["GET", "POST"])
def magic_modes_api():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("modes"), list):
            return Response("Invalid modes", status=400, mimetype="text/plain")
        modes = MAGIC_MODE_STORE.save(data["modes"])
    else:
        modes = MAGIC_MODE_STORE.load()
    return Response(
        json.dumps({"modes": modes}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/magic-settings", methods=["GET", "POST"])
def magic_settings_api():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return Response("Invalid settings", status=400, mimetype="text/plain")
        api_key = data.get("api_key")
        proxy = data.get("proxy")
        if not isinstance(api_key, str) or len(api_key) > 512:
            return Response("Invalid API key", status=400, mimetype="text/plain")
        if not isinstance(proxy, str) or len(proxy) > 2048:
            return Response("Invalid proxy", status=400, mimetype="text/plain")
        try:
            settings = save_magic_connection_settings(
                MAGIC_ENV_PATH,
                api_key=api_key,
                clear_api_key=bool(data.get("clear_api_key")),
                proxy=proxy,
            )
        except ValueError as exc:
            return Response(str(exc), status=400, mimetype="text/plain")
    else:
        settings = read_magic_connection_settings(MAGIC_ENV_PATH)
    return Response(
        json.dumps(settings, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/magic-photo-status")
def magic_photo_status_api():
    return Response(
        json.dumps({"photos": get_magic_photo_states()}, ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/photo/<filename>")
def photo(filename):
    return send_from_directory(PHOTOS_DIR, filename, as_attachment=True)


@app.route("/audio/<filename>")
def audio(filename):
    if not filename.lower().endswith(".wav"):
        return "Not found", 404
    return send_from_directory(PHOTOS_DIR, filename, mimetype="audio/wav")


THUMB_DIR = os.path.join(PHOTOS_DIR, ".thumbs")
THUMBNAIL_LOCK = threading.Lock()

def get_thumb_path(filename, size):
    os.makedirs(THUMB_DIR, exist_ok=True)
    return os.path.join(THUMB_DIR, f"{filename}_{size}.jpg")

def ensure_thumbnail(path, cache_path, size, is_gif=False):
    """Create one thumbnail at a time with JPEG decoder downsampling.

    Pi Zero 2 W has very little free RAM while the camera is active. Browser
    grids request many thumbnails concurrently; serializing them and asking
    libjpeg to decode near the target size prevents full-resolution images from
    being resident several times at once.
    """
    with THUMBNAIL_LOCK:
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            return True
        from PIL import Image
        tmp = None
        try:
            with Image.open(path) as source:
                if is_gif:
                    source.seek(0)
                else:
                    source.draft("RGB", (size, size))
                img = source.convert("RGB")
                img.thumbnail((size, size))
                tmp = tempfile.NamedTemporaryFile(
                    dir=THUMB_DIR, delete=False, suffix=".tmp"
                )
                img.save(tmp, "JPEG", quality=75)
                tmp.close()
                os.chmod(tmp.name, 0o644)
                os.replace(tmp.name, cache_path)
            return True
        except Exception:
            if tmp is not None:
                try:
                    tmp.close()
                    os.unlink(tmp.name)
                except Exception:
                    pass
            return False

def _gif_complete(path):
    """A fully-written GIF ends with the trailer byte 0x3B. The camera encodes
    GIFs in place at their final name, so a request can briefly land mid-write;
    this rejects those partial files instead of serving truncated bytes."""
    try:
        if os.path.getsize(path) < 1000:
            return False
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\x3b"
    except OSError:
        return False

@app.route("/thumb/<filename>")
def thumb(filename):
    # Static preview for the grid. For GIFs this is a JPEG of the first frame —
    # the full animation is served separately by /gif so the heavy multi-frame
    # file isn't pulled for every grid cell at once (the cause of dropped
    # transfers / broken "?" previews under load).
    path = os.path.join(PHOTOS_DIR, filename)
    if not os.path.exists(path):
        return "Not found", 404
    is_gif = filename.lower().endswith(".gif")
    if is_gif and not _gif_complete(path):
        resp = Response("GIF not ready", status=503, mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Retry-After"] = "1"
        return resp
    size = min(request.args.get('size', 400, type=int), 1200)
    cache_path = get_thumb_path(filename, size)
    if not ensure_thumbnail(path, cache_path, size, is_gif):
        return "Error", 500
    with open(cache_path, "rb") as f:
        return Response(f.read(), mimetype="image/jpeg")


@app.route("/gif/<filename>")
def gif_full(filename):
    """Full animated GIF, served inline. Used by the viewer and by the grid's
    background loader to swap a static poster for the live animation. Filenames
    are unique and never rewritten once complete, so it caches hard."""
    if not filename.lower().endswith(".gif"):
        return "Not found", 404
    path = os.path.join(PHOTOS_DIR, filename)
    if not os.path.exists(path):
        return "Not found", 404
    if not _gif_complete(path):
        resp = Response("GIF not ready", status=503, mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Retry-After"] = "1"
        return resp
    resp = send_from_directory(PHOTOS_DIR, filename, mimetype="image/gif")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/preload")
def preload():
    if not GALLERY_PRELOAD:
        return "", 204
    def generate_all():
        if not os.path.exists(PHOTOS_DIR):
            return
        # Only photos get pre-generated thumbnails; GIFs are served as-is.
        files = [f for f in list_media() if f.lower().endswith(".jpg")][:10]
        for filename in files:
            cache_path = get_thumb_path(filename, 1200)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                continue
            ensure_thumbnail(
                os.path.join(PHOTOS_DIR, filename), cache_path, 1200
            )
            time.sleep(0.2)  # keep server responsive between generations
    threading.Thread(target=generate_all, daemon=True).start()
    return "", 204


@app.route("/preload-ahead", methods=["POST"])
def preload_ahead():
    if not GALLERY_PRELOAD:
        return "", 204
    data = request.get_json()
    filenames = [f for f in data.get("files", []) if not f.lower().endswith(".gif")]
    def generate():
        for filename in filenames:
            cache_path = get_thumb_path(filename, 1200)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                continue
            path = os.path.join(PHOTOS_DIR, filename)
            if os.path.exists(path):
                ensure_thumbnail(path, cache_path, 1200)
    threading.Thread(target=generate, daemon=True).start()
    return "", 204


@app.route("/delete", methods=["POST"])
def delete_photos():
    data = request.get_json()
    filenames = data.get("files", [])
    for f in filenames:
        base = os.path.basename(f)
        path = os.path.join(PHOTOS_DIR, base)
        if os.path.exists(path):
            os.remove(path)
        for sidecar_suffix in (".ai.json", ".magic.json"):
            try:
                os.remove(path + sidecar_suffix)
            except FileNotFoundError:
                pass
        audio_name = paired_audio_filename(base)
        if audio_name:
            try:
                os.remove(os.path.join(PHOTOS_DIR, audio_name))
            except OSError:
                pass
        for size in [400, 1200]:
            cache = get_thumb_path(os.path.basename(f), size)
            if os.path.exists(cache):
                os.remove(cache)
    return "", 204


@app.route("/download-zip", methods=["POST"])
def download_zip():
    filenames = expand_with_paired_audio(request.form.getlist("files"))
    token = request.form.get("download_token", "")
    paths = []
    for f in filenames:
        base = os.path.basename(f)
        p = os.path.join(PHOTOS_DIR, base)
        if os.path.exists(p):
            paths.append((p, base))

    archive_name = "optocam_photos.zip"
    if len(paths) == 2:
        stems = {os.path.splitext(name)[0] for _, name in paths}
        exts = {os.path.splitext(name)[1].lower() for _, name in paths}
        if len(stems) == 1 and exts == {".jpg", ".wav"}:
            archive_name = next(iter(stems)) + ".zip"

    # Approximate the final ZIP_STORED size as the progress denominator.
    total = 22
    for p, arcname in paths:
        total += 92 + 2 * len(arcname.encode("utf-8")) + os.path.getsize(p)

    if token:
        now = time.time()
        for k, v in list(DOWNLOAD_PROGRESS.items()):   # prune old, finished entries
            if not v.get("active") and now - v["ts"] > 60:
                DOWNLOAD_PROGRESS.pop(k, None)
        DOWNLOAD_PROGRESS[token] = {"sent": 0, "total": total, "ts": now, "active": True}

    class _Stream:
        """Write-only sink. With no seek/tell, zipfile streams using data
        descriptors, so we never buffer more than one file at a time."""
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
            return len(data)
        def flush(self):
            pass
        def drain(self):
            data = bytes(self.buf)
            del self.buf[:]
            return data

    def generate():
        sink = _Stream()
        sent = 0
        completed = False
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
                for p, arcname in paths:
                    zinfo = zipfile.ZipInfo.from_file(p, arcname)
                    zinfo.compress_type = zipfile.ZIP_STORED
                    # Copy each file in small chunks so progress updates continuously
                    # (not in one big jump per file).
                    with zf.open(zinfo, "w") as dest, open(p, "rb") as src:
                        while True:
                            buf = src.read(262144)
                            if not buf:
                                break
                            dest.write(buf)
                            chunk = sink.drain()
                            if chunk:
                                yield chunk
                                sent += len(chunk)
                                if token:
                                    DOWNLOAD_PROGRESS[token] = {"sent": sent, "total": total, "ts": time.time(), "active": True}
                    chunk = sink.drain()       # data descriptor written when the entry closes
                    if chunk:
                        yield chunk
                        sent += len(chunk)
                        if token:
                            DOWNLOAD_PROGRESS[token] = {"sent": sent, "total": total, "ts": time.time(), "active": True}
            tail = sink.drain()                # central directory, written on close
            if tail:
                yield tail
                sent += len(tail)
            completed = True
        finally:
            # Always mark the stream ended (completed or aborted) so the client
            # can stop waiting. A completed download reports the full size.
            if token:
                DOWNLOAD_PROGRESS[token] = {
                    "sent": total if completed else sent,
                    "total": total,
                    "ts": time.time(),
                    "active": False,
                }

    return Response(
        generate(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={archive_name}"}
    )


@app.route("/download-progress")
def download_progress():
    p = DOWNLOAD_PROGRESS.get(request.args.get("token", ""))
    if not p:
        return Response('{"sent":0,"total":0,"active":false}', mimetype="application/json")
    return Response(
        json.dumps({"sent": p["sent"], "total": p["total"], "active": p.get("active", False)}),
        mimetype="application/json")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0", port=GALLERY_PORT, debug=False,
        threaded=GALLERY_THREADED,
    )
