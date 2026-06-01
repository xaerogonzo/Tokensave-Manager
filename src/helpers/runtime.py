"""Runtime process plumbing: logger, single-instance lock, tray icon.

`log` is initialised at module load and shared via `from helpers.runtime
import log`. The whole manager (~72 call sites) writes to the same
RotatingFileHandler instance, so log output lands in one file regardless
of which module logged it.

`_acquire_instance_lock` mutates the module-level `_mutex_handle` global.
This is the one piece of mutable cross-module state in helpers/ — but
it's set exactly once on app startup and never read by anyone else.
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import math
import os

from constants import LOG_DIR, LOG_FILE


# ── Logger ───────────────────────────────────────────────────────────────────

def _setup_logger():
    """Build the manager's rotating file logger ('tsm') and return it."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=500_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger = logging.getLogger("tsm")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger


log = _setup_logger()


# ── Single-instance lock (Windows kernel32 mutex) ────────────────────────────

_MUTEX_NAME = "TokenSaveManager_SingleInstance"
_mutex_handle = None


def _acquire_instance_lock():
    """Return True if this is the first instance, False if another is running."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS


def _bring_existing_to_front():
    """Find the existing window by title and restore it."""
    hwnd = ctypes.windll.user32.FindWindowW(None, "TokenSave Manager")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)


# ── Tray icon image ──────────────────────────────────────────────────────────

def _make_tray_icon():
    """Generate a 64×64 tray icon: dark circle with a white star."""
    from PIL import Image, ImageDraw  # lazy — only needed in the live app, not tests
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (30, 30, 46, 255)   # Catppuccin Mocha base
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)
    # Simple 5-point star approximation using a polygon
    cx, cy, r_out, r_in = size / 2, size / 2, 26, 11
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=(137, 180, 250, 255))  # Catppuccin blue
    return img
