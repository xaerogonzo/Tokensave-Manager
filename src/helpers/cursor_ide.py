"""Open a folder in the Cursor APP (not its CLI agent).

Why this exists: Cursor's own Claude importer lists a session only when the
folder it ran in is the open workspace, and a worktree is a folder of its own.
So "show me that worktree's imported chats" is "open that worktree in Cursor".

The app's executable is launched directly, never `cursor.cmd`. The shim needs
`cmd.exe /c`, and `cmd.exe` strips the outer quotes of a command line whose exe
path AND argument both contain spaces (this project's `""outer""` rule), which
is exactly what `D:\\Claude Co worker\\...` is.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from constants import CREATE_NO_WINDOW


def find_cursor_exe() -> str:
    """Absolute path to Cursor.exe, or `""` when it cannot be found.

    Looked up at call time (a test redirects `%LOCALAPPDATA%`). The default
    install location first, then wherever the `cursor` shim on PATH says the
    app lives: the shim sits four folders below the app root
    (`<root>\\resources\\app\\bin\\cursor.cmd`).
    """
    candidates = [os.path.join(os.path.expandvars("%LOCALAPPDATA%"),
                               "Programs", "cursor", "Cursor.exe")]
    for name in ("cursor.cmd", "cursor"):
        shim = shutil.which(name)
        if shim:
            root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(shim)))))
            candidates.append(os.path.join(root, "Cursor.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def open_folder(folder: str, exe: str = "") -> "tuple[bool, str]":
    """Open *folder* as a Cursor workspace. Does not wait for it."""
    if not folder or not os.path.isdir(folder):
        return False, f"That folder no longer exists: {folder or '(unknown)'}"
    exe = exe or find_cursor_exe()
    if not exe:
        return False, ("Cursor is not installed where the Manager looks "
                       "(%LOCALAPPDATA%\\Programs\\cursor\\Cursor.exe).")
    try:
        subprocess.Popen([exe, folder], creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        return False, f"Could not start Cursor: {exc}"
    return True, ""
