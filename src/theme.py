"""Tk-coupled UI primitives shared across dialogs and controllers.

Currently holds the `_Tooltip` widget. Add other low-level UI utilities
here (custom widget subclasses, recurring widget compositions) as they
emerge — but keep this file small. Anything dialog-specific belongs in
its own `src/dialogs/*.py` file.
"""

from __future__ import annotations

import tkinter as tk

from constants import C


class _Tooltip:
    """Hover tooltip for tkinter widgets.

    Shows a small popup after `delay` ms; hides on leave or click.
    Text wraps at ~300 px so multi-line tips stay readable.
    """
    _DELAY = 650   # ms before appearing

    def __init__(self, widget, text: str):
        self._widget  = widget
        self._text    = text
        self._job     = None
        self._tip_win = None
        widget.bind("<Enter>",       self._schedule,  add="+")
        widget.bind("<Leave>",       self._cancel,    add="+")
        widget.bind("<ButtonPress>", self._cancel,    add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self._widget.after(self._DELAY, self._show)

    def _cancel(self, _event=None):
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None

    def _show(self):
        self._job = None
        try:
            x = self._widget.winfo_rootx() + 12
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        except Exception:
            return
        self._tip_win = win = tk.Toplevel(self._widget)
        win.wm_overrideredirect(True)   # no title bar / border
        win.wm_attributes("-topmost", True)
        win.wm_geometry(f"+{x}+{y}")
        win.configure(bg=C["surface1"])
        # Thin border effect via a 1-px frame
        outer = tk.Frame(win, bg=C["overlay0"], padx=1, pady=1)
        outer.pack()
        tk.Label(outer, text=self._text,
                 font=("Segoe UI", 9),
                 bg=C["surface1"], fg=C["text"],
                 padx=10, pady=6,
                 wraplength=300,
                 justify=tk.LEFT).pack()


def bind_mousewheel(canvas: tk.Canvas) -> None:
    """Wire mouse-wheel scrolling to a tk.Canvas.

    Uses <Enter>/<Leave> to activate/deactivate bind_all so the binding
    fires only while the pointer is hovering over this canvas (or its
    child widgets). Safe when multiple scrollable regions are on screen
    simultaneously — each one registers/deregisters on hover.

    Call once after the Canvas is created:
        self._canvas = tk.Canvas(...)
        bind_mousewheel(self._canvas)
    """
    def _scroll(event: tk.Event) -> None:
        # event.delta is ±120 per notch on Windows; divide to get scroll units
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _activate(_event: tk.Event | None = None) -> None:
        canvas.bind_all("<MouseWheel>", _scroll)

    def _deactivate(_event: tk.Event | None = None) -> None:
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _activate)
    canvas.bind("<Leave>", _deactivate)
    # Also activate when a child widget inside the canvas gets the cursor —
    # bind_all means the event propagates up, so no child-by-child wiring needed.
