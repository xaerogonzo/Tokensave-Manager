"""tests/test_window_capture.py — the PNG writer, and refusing to lie about it.

`capture_window` needs Win32, but the part that can be wrong quietly is the
format work: a mirrored image, a transparent one, or a blank one all look like
a plausible screenshot until someone opens it. Those pieces are pure, so they
are tested everywhere including Linux CI, where the Win32 half correctly
declines.

The encoder is longhand rather than Pillow because the manager is
zero-runtime-dependency by design — see `test_no_thirdparty_module_imports`.
That makes these tests the only thing standing behind the format.
"""
from __future__ import annotations

import struct
import sys
import zlib

import pytest

from helpers.window_capture import (
    _is_uniform,
    bgra_to_rgb,
    capture_window,
    encode_png,
    hwnd_for,
)


def _parse_png(blob: bytes) -> "tuple[int, int, bytes]":
    """Decode our own PNG with the stdlib. (width, height, raw RGB)."""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, width, height, idat = 8, 0, 0, b""
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        kind = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        expect = zlib.crc32(kind + payload) & 0xFFFFFFFF
        (stored,) = struct.unpack(">I", blob[pos + 8 + length:pos + 12 + length])
        assert stored == expect, "CRC mismatch in %r chunk" % kind
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
            assert (depth, colour) == (8, 2), "expected 8-bit truecolour"
        elif kind == b"IDAT":
            idat += payload
        pos += 12 + length

    raw = zlib.decompress(idat)
    stride = width * 3
    out = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        assert raw[start] == 0, "expected filter type 0"
        out += raw[start + 1:start + 1 + stride]
    return width, height, bytes(out)


def test_encode_png_round_trips_exactly():
    """Lossless in, identical out — no filtering, no colour management."""
    rgb = bytes([
        255, 0, 0,   0, 255, 0,
        0, 0, 255,   9, 9, 9,
        1, 2, 3,     250, 251, 252,
    ])
    width, height, got = _parse_png(encode_png(2, 3, rgb))
    assert (width, height) == (2, 3)
    assert got == rgb


def test_encode_png_preserves_row_order():
    """A vertically mirrored screenshot still looks like a screenshot.

    Distinct rows, asserted in order, is the only cheap way to catch a
    bottom-up buffer being written as if it were top-down.
    """
    rgb = bytes([1, 1, 1] * 2 + [2, 2, 2] * 2 + [3, 3, 3] * 2)
    _w, _h, got = _parse_png(encode_png(2, 3, rgb))
    assert got[0:3] == b"\x01\x01\x01"
    assert got[-3:] == b"\x03\x03\x03"


def test_encode_png_rejects_a_mismatched_buffer():
    """Silently padding would write a garbage image that still opens."""
    with pytest.raises(ValueError, match="expected"):
        encode_png(4, 4, b"\x00" * 10)


def test_bgra_to_rgb_swaps_channels_and_drops_alpha():
    """GDI's alpha is undefined for most windows.

    Honouring it renders the capture fully transparent, which reads as a
    broken image rather than as an alpha bug.
    """
    # one pixel: B=10 G=20 R=30 A=0  -> RGB 30,20,10
    assert bgra_to_rgb(bytes([10, 20, 30, 0]), 1, 1) == bytes([30, 20, 10])


def test_bgra_to_rgb_handles_several_pixels():
    bgra = bytes([1, 2, 3, 255, 4, 5, 6, 0])
    assert bgra_to_rgb(bgra, 2, 1) == bytes([3, 2, 1, 6, 5, 4])


@pytest.mark.parametrize("rgb,uniform", [
    (bytes([7, 7, 7] * 10), True),
    (bytes([0, 0, 0] * 10), True),
    (bytes([7, 7, 7] * 9 + [8, 8, 8]), False),
    (b"", False),
])
def test_is_uniform_detects_the_blank_printwindow_result(rgb, uniform):
    """PrintWindow can return success and a single-colour bitmap.

    Detected rather than predicted: which of the two flags a given window
    honours is not knowable in advance, so the blank result is what drives
    the retry.
    """
    assert _is_uniform(rgb) is uniform


def test_capture_window_refuses_without_a_handle():
    ok, detail = capture_window(0, "ignored.png")
    assert ok is False
    assert detail


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_capture_window_declines_off_windows(tmp_path):
    """Reported as a value, never raised — this runs inside a live window."""
    ok, detail = capture_window(1234, str(tmp_path / "x.png"))
    assert ok is False
    assert "Windows-only" in detail


def test_hwnd_for_returns_zero_for_a_non_widget():
    """Never raises: it is called while rendering a diagnostic."""
    assert hwnd_for(object()) == 0
