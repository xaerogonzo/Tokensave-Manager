"""Output helpers shared by the git-hook runners.

GUI git clients — GitHub Desktop, GitKraken, Sourcetree, the VS Code git
panel — swallow a hook's stderr. The user sees a generic "hook rejected"
message with no indication of WHICH check failed, and the only way to find out
is to re-run the operation from a terminal, which is exactly what someone
using a GUI client is not doing.

So when stderr is not a terminal, the hooks also raise a message box. That
began in the pre-push runner (G-K); this module is where it lives so the
pre-commit hook can use it too without the two copies drifting.

Pure module — stdlib only. Tk is imported lazily inside the function, so
importing this on a headless runner costs nothing and cannot fail.
"""

from __future__ import annotations

import sys


def stderr_is_tty() -> bool:
    """Best-effort ``isatty()`` probe on stderr.

    Anything exotic in place of a real stream — pytest's capture, a pipe, a
    closed handle — counts as "not a terminal", which is the side that
    triggers the GUI fallback. Erring that way shows a box to someone who
    might not have needed one; erring the other way hides the reason a hook
    blocked them.
    """
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def show_hook_dialog(title: str, message: str) -> bool:
    """Raise a message box for a hook result. Returns True if one appeared.

    Fails open in every direction: no Tk, no display, a broken install — all
    return False rather than raising. A hook must never fail because it could
    not draw a window; the caller has already written the same information to
    stderr and its exit code is unaffected.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:                                   # noqa: BLE001
        return False
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(title, message)
        return True
    except Exception:                                   # noqa: BLE001
        return False
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:                           # noqa: BLE001
                pass


def maybe_show_hook_dialog(title: str, message: str) -> bool:
    """Show the box only when stderr went somewhere the user cannot read.

    On a real terminal the stderr lines are already visible and a modal would
    just be in the way.
    """
    if stderr_is_tty():
        return False
    return show_hook_dialog(title, message)
