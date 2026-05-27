"""Shared IO helpers for the patcher modules.

`_atomic_write` was originally defined per-patcher (and inlined in
architecture / generic via .tmp + os.replace). Multi-section drafting needs
a single read → multiple computes → single write flow, which is cleaner
with a shared helper instead of repeating the inline pattern.

Pure IO — no Tkinter, no UI imports.
"""

from __future__ import annotations

import os


def _atomic_write(path: str, text: str, msg: str) -> tuple[bool, str]:
    """Write `text` to `path` atomically via .tmp + os.replace.

    Returns ``(True, msg)`` on success, ``(False, error_msg)`` on failure.
    The `msg` argument is echoed back on success so callers can pass a
    descriptive action string ("Section X updated", "Roadmap N inserted",
    etc.) for the result dialog / status bar without duplicating
    formatting logic.
    """
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing {os.path.basename(path)}: {e}"
    return True, msg
