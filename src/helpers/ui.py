"""UI helpers shared across controllers and dialogs.

Reusable primitives that touch tk widgets but contain no domain logic.
Each function takes a widget explicitly — no `self` coupling, so any
caller (controller or dialog) can compose them.
"""

from __future__ import annotations

import tkinter as tk


def append_text(widget: tk.Text, text: str, tag: str = "") -> None:
    """Append `text` to a tk.Text widget, scroll to end, leave it DISABLED.

    Toggles state NORMAL → insert → DISABLED so widgets that are normally
    read-only (log panes, stream output) can still receive new content.
    Silently no-ops on empty `text`. Pass `tag` to apply a previously
    configured tag (e.g. colour) to the inserted run.
    """
    if not text:
        return
    widget.configure(state=tk.NORMAL)
    if tag:
        widget.insert(tk.END, text, tag)
    else:
        widget.insert(tk.END, text)
    widget.see(tk.END)
    widget.configure(state=tk.DISABLED)
