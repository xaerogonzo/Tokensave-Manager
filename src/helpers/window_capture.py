"""Capture a window to a PNG without it being on screen.

**Why not a screenshot.** Grabbing the screen captures whatever is in front,
so the window has to be raised, focused and unobstructed — which means taking
the machine away from whoever is using it, and means a console stealing focus
mid-run silently captures the wrong thing. `PrintWindow` asks the window to
render ITSELF into a bitmap, so a window sitting behind an editor, or with
nothing on screen at all, captures exactly as it looks.

**Why the PNG encoder is written out longhand.** The manager is
zero-runtime-dependency by design and `tests/test_no_thirdparty_module_imports`
enforces it, so reaching for Pillow would either add the project's first
runtime dependency or add a degradation path that is never exercised. A PNG of
a screenshot is one `zlib` stream and four chunks; that is cheaper than either.

`encode_png` is pure and runs everywhere, so the format work is testable on
Linux CI. Everything touching Win32 is inside a function and reports failure
as a value, so importing this module on a non-Windows box is fine.
"""

from __future__ import annotations

import struct
import sys
import zlib

#: PrintWindow's PW_RENDERFULLCONTENT. Required for windows that render
#: through DWM — without it a composited window prints blank, which looks
#: exactly like the capture working on an empty window.
_PW_RENDERFULLCONTENT = 0x00000002

#: GetAncestor's GA_ROOT. Tk hands out the HWND of its own client window;
#: the decorated frame the user sees is an ancestor of it, and capturing the
#: client alone silently loses the title bar the caller was asking about.
_GA_ROOT = 2


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """PNG bytes for a top-down, 8-bit RGB buffer. Pure — no Win32, no deps.

    `rgb` must hold exactly `width * height * 3` bytes. Each scanline is
    prefixed with filter type 0 ("None"): filtering only buys compression, and
    a screenshot is written once and read once.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(
            "rgb buffer is %d bytes, expected %d for %dx%d"
            % (len(rgb), expected, width, height))

    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                      # filter type: None
        raw += rgb[y * stride:(y + 1) * stride]

    def _chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def bgra_to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    """Drop the alpha and swap to RGB.

    GDI hands back BGRA with the alpha channel undefined for most windows —
    honouring it renders the capture fully transparent, which reads as a
    blank/broken image rather than as an alpha bug.
    """
    out = bytearray(width * height * 3)
    out[0::3] = bgra[2::4]
    out[1::3] = bgra[1::4]
    out[2::3] = bgra[0::4]
    return bytes(out)


def hwnd_for(widget) -> int:
    """The decorated top-level HWND behind a Tk widget, or 0.

    Resolved through GA_ROOT rather than `winfo_toplevel()`: Tk's notion of a
    toplevel is its own client window, and the frame Windows draws around it
    belongs to a different HWND.
    """
    try:
        import ctypes
        raw = int(widget.winfo_id())
        root = ctypes.windll.user32.GetAncestor(raw, _GA_ROOT)
        return int(root) or raw
    except Exception:                                        # noqa: BLE001
        return 0


def _is_uniform(rgb: bytes) -> bool:
    """Is every pixel the same colour?

    The tell for the PW_RENDERFULLCONTENT failure mode: `PrintWindow` returns
    success and fills the bitmap with one colour. Treated as a failed capture
    so the caller can retry with the plain flag rather than write out a
    convincing-looking blank rectangle.
    """
    return len(rgb) >= 3 and rgb.count(rgb[:3]) * 3 >= len(rgb)


def capture_window(hwnd: int, path: str) -> "tuple[bool, str]":
    """Render `hwnd` into `path` as a PNG. Returns `(ok, detail)`.

    Never raises: a diagnostic that takes the application down with it when a
    capture fails is worse than one that reports it could not draw.
    """
    if sys.platform != "win32":
        return False, "window capture is Windows-only (uses PrintWindow)"
    if not hwnd:
        return False, "no window handle"

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:                                 # noqa: BLE001
        return False, "ctypes unavailable: %s" % exc

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return False, "GetWindowRect failed for hwnd %d" % hwnd
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return False, "window has no area (%dx%d)" % (width, height)

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    if not hdc:
        return False, "GetWindowDC failed"
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = gdi32.SelectObject(memdc, bitmap)

    try:
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        # Negative height requests a TOP-DOWN buffer. A bottom-up one would
        # have to be reversed row by row before encoding, and forgetting to is
        # a vertically mirrored screenshot that still looks plausible.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        rgb = b""
        used = ""
        # Try the composited path first, then the plain one. Some windows
        # honour exactly one of the two, and which one is not knowable from
        # here — so the blank result is detected rather than predicted.
        for flag, name in ((_PW_RENDERFULLCONTENT, "PW_RENDERFULLCONTENT"),
                           (0, "PrintWindow(0)")):
            if not user32.PrintWindow(wintypes.HWND(hwnd), memdc, flag):
                continue
            if not gdi32.GetDIBits(memdc, bitmap, 0, height, buffer,
                                   ctypes.byref(header), 0):
                continue
            candidate = bgra_to_rgb(buffer.raw, width, height)
            used = name
            if not _is_uniform(candidate):
                rgb = candidate
                break
            rgb = candidate                  # keep it in case both are blank
        if not rgb:
            return False, "PrintWindow produced no image for hwnd %d" % hwnd

        with open(path, "wb") as handle:
            handle.write(encode_png(width, height, rgb))
        note = "" if not _is_uniform(rgb) else " (image is blank — the window may be minimised)"
        return True, "%dx%d via %s%s" % (width, height, used, note)
    except Exception as exc:                                 # noqa: BLE001
        return False, "capture failed: %s" % exc
    finally:
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
