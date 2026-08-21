"""Tk-coupled UI primitives shared across dialogs and controllers.

Currently holds the `_Tooltip` widget. Add other low-level UI utilities
here (custom widget subclasses, recurring widget compositions) as they
emerge — but keep this file small. Anything dialog-specific belongs in
its own `src/dialogs/*.py` file.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk

from constants import C

log = logging.getLogger(__name__)


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


# ── Custom checkbox indicator images ─────────────────────────────────────────
# Cached at module level so they survive for the lifetime of the process
# (PhotoImage objects are garbage-collected if not referenced).

_CHK_OFF: "tk.PhotoImage | None" = None
_CHK_ON:  "tk.PhotoImage | None" = None


def _make_chk_images() -> tuple:
    """Return (off_image, on_image) PhotoImage pair, building lazily on first call.

    14×14 pixel images drawn with tk.PhotoImage.put() — no PIL dependency.
      Off: surface1 fill + overlay0 border  (visible grey box on any bg)
      On:  white fill + overlay0 border + base-colour checkmark pixels
    """
    global _CHK_OFF, _CHK_ON
    if _CHK_OFF is not None:
        return _CHK_OFF, _CHK_ON

    S  = 14
    bd = C["overlay0"]   # #6c7086 — border
    of = C["surface1"]   # #45475a — unchecked fill
    on = "white"         # checked fill
    ck = C["base"]       # #1e1e2e — checkmark ink (max contrast on white)

    def _build(fill: str, checkmark: bool) -> tk.PhotoImage:
        rows = []
        for y in range(S):
            row = [
                bd if (x == 0 or x == S - 1 or y == 0 or y == S - 1) else fill
                for x in range(S)
            ]
            rows.append(row)
        if checkmark:
            # ✓ shape — 2-px wide strokes; clipped to inner 1..12 area
            for x, y in [
                # left downstroke (two columns)
                (2, 8), (3, 9), (4, 10),
                (3, 8), (4, 9), (5, 10),
                # right upstroke (two columns)
                (5, 9), (6, 8), (7, 7), (8, 6), (9, 5), (10, 4), (11, 3),
                (6, 9), (7, 8), (8, 7), (9, 6), (10, 5), (11, 4), (12, 3),
            ]:
                if 0 < x < S - 1 and 0 < y < S - 1:
                    rows[y][x] = ck
        img = tk.PhotoImage(width=S, height=S)
        for y, row in enumerate(rows):
            img.put("{%s}" % " ".join(row), to=(0, y, S, y + 1))
        return img

    _CHK_OFF = _build(of, checkmark=False)
    _CHK_ON  = _build(on, checkmark=True)
    return _CHK_OFF, _CHK_ON


def themed_checkbutton(parent: tk.Widget, **kw) -> tk.Checkbutton:
    """tk.Checkbutton with custom PhotoImage indicator for dark-theme visibility.

    Uses indicatoron=False + two PhotoImage objects so both states are
    explicitly coloured:
      • Unchecked — grey (#45475a) box with visible border
      • Checked   — white box with dark checkmark

    selectcolor is matched to the widget bg so the button background does not
    flash when toggled — only the image swaps. All keyword args are forwarded
    to tk.Checkbutton; any default can be overridden by the caller.
    """
    off_img, on_img = _make_chk_images()
    bg = kw.get("bg", kw.get("background", C["surface0"]))
    kw.pop("selectcolor", None)           # not meaningful with indicatoron=False
    kw.setdefault("indicatoron",   False)
    kw.setdefault("image",         off_img)
    kw.setdefault("selectimage",   on_img)
    kw.setdefault("selectcolor",   bg)    # prevent bg flash on toggle
    kw.setdefault("compound",      tk.LEFT)
    kw.setdefault("relief",        tk.FLAT)
    kw.setdefault("overrelief",    tk.FLAT)
    kw.setdefault("bd",            0)
    kw.setdefault("padx",          2)
    kw.setdefault("activebackground", bg)
    kw.setdefault("activeforeground", C["text"])
    return tk.Checkbutton(parent, **kw)


# ── Worker → UI hand-off ──────────────────────────────────────────────────


class UiPumpMixin:
    """Give a Tk window a thread-safe channel for its background workers.

    Calling ``self.after(...)`` from a worker thread is the most expensive bug
    shape this project has produced, because of HOW it fails:

      * on Windows it usually works, so it survives review and local testing;
      * when it does fail it raises "main thread is not in main loop", which
        the broad ``except`` clauses around such calls routinely swallow;
      * on Linux it does not raise at all — it BLOCKS. A CI diagnostic caught
        a worker alive after 10 seconds having scheduled nothing and raised
        nothing. No error, no log line, no way to tell what it was waiting on.

    The rule this exists to make cheap: a worker never touches Tk. It hands a
    callable to `_post()`, and `_ui_pump()` runs it on the Tk thread.

    Mix in ahead of the Tk base class, and start the pump once the widgets
    exist and before any thread does:

        class MyDialog(UiPumpMixin, tk.Toplevel):
            def __init__(self, parent):
                super().__init__(parent)
                ...build widgets...
                self._start_ui_pump()

    `tests/test_no_cross_thread_tk.py` enforces both halves — that workers
    post rather than call, and that every subclass starts its pump.
    """

    _UI_PUMP_MS = 50

    def _ui_host(self):
        """The widget this pump drives. Override when ``self`` is not one.

        Windows mix this in alongside a Tk base class, so ``self`` *is* the
        widget and the default is right. Controllers are not widgets — they
        own one (``self._tab``) — and would otherwise need a second, parallel
        pump of their own. Overriding this one method lets them share the
        machinery the guard already enforces, instead of growing a bespoke
        copy that nothing checks.
        """
        return self

    def _start_ui_pump(self) -> None:
        """Create the queue and begin draining it. Tk thread, once."""
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_pump_id = None
        self._ui_host().bind("<Destroy>", self._stop_ui_pump, add="+")
        self._ui_pump()

    def _post(self, fn, *args) -> None:
        """Run ``fn(*args)`` on the Tk thread. Safe from any thread."""
        self._ui_queue.put((fn, args))

    def _post_after(self, delay_ms: int, fn, *args) -> None:
        """Run ``fn(*args)`` after ``delay_ms``. Safe from any thread.

        A worker calling ``self.after(2000, ...)`` directly is the same
        cross-thread call as ``after(0, ...)``: the delay changes what runs
        when, not which thread does the scheduling. This posts the *timer
        setup* onto the Tk thread, and the timer itself then behaves normally.
        """
        self._post(self._schedule_after, delay_ms, fn, args)

    def _schedule_after(self, delay_ms: int, fn, args) -> None:
        """Set the timer. Tk thread only — reached via `_post_after`."""
        try:
            self._ui_host().after(delay_ms, lambda: fn(*args))
        except tk.TclError:
            pass          # window closed before the timer was set

    def _ui_pump(self) -> None:
        """Drain whatever the workers posted. Tk thread only."""
        host = self._ui_host()
        try:
            if not host.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:
                    pass          # widget went away between post and run
                except Exception:
                    # One bad callback must not stop the pump: everything
                    # posted afterwards would be dropped, and the window
                    # would freeze in place with no error — the exact
                    # invisible failure this class was written to end.
                    log.exception("posted UI callback raised")
        except queue.Empty:
            pass
        try:
            self._ui_pump_id = host.after(self._UI_PUMP_MS, self._ui_pump)
        except tk.TclError:
            self._ui_pump_id = None

    def _stop_ui_pump(self, evt=None) -> None:
        """Stop rescheduling.

        Bound to ``<Destroy>``, which also fires for every child widget, so
        only the window's own destruction counts.
        """
        if evt is not None and getattr(evt, "widget", None) is not self._ui_host():
            return
        if getattr(self, "_ui_pump_id", None):
            try:
                self._ui_host().after_cancel(self._ui_pump_id)
            except tk.TclError:
                pass
        self._ui_pump_id = None
