"""controllers/output_pane.py — the OUTPUT pane: selectable, read-only, resizable, poppable.

WHY THIS EXISTS. The pane was a ``tk.Text`` created with ``state=DISABLED``.
Tk only moves focus to a Text whose state is ``normal``, so a click never gave
it focus and Ctrl+C went to whatever widget had it: the output could not be
copied. It was also packed at a fixed four lines.

READ-ONLY BY CONSTRUCTION, NOT BY KEY LISTS. The Text stays ``normal`` so every
native selection, navigation and copy binding works, and mutation is refused
at the widget COMMAND: the Tcl command is renamed and the widget path becomes
an ``interp alias`` onto a Tcl proc that drops ``insert`` / ``delete`` /
``replace`` / ``edit undo|redo`` and forwards everything else. That covers
every route -- typed keys, BackSpace, paste, cut, middle-click, a direct
``text.insert()`` -- without enumerating them. The controller writes through
the renamed command, which is private to it.

The proxy is deliberately Tcl, not a Python callback. A Python command that
raises -- and forwarding ``get sel.first sel.last`` with no selection raises,
inside ``tk_textCopy``'s own ``catch`` -- sets _tkinter's pending-error flag,
which ``mainloop()`` re-raises later: an ordinary Ctrl+C with nothing
selected would take the whole Manager down. A Tcl proc keeps errors in Tcl.

A renamed command is still reachable by name from Tcl, so "unreachable" is not
claimed; every public route is blocked, and the tests prove each one.

POP-OUT. One content, two views: the pop-out's Text is a Tk text PEER of the
docked one (content and tags shared; selection, focus and scroll per view), so
nothing is copied and a line logged while popped out is already there when the
pane docks. Docking re-adds the pane at the height it had BEFORE popping out,
never one derived from the pop-out window. Every launch starts docked, and the
pop-out's geometry is session state only.

THREADING. UI-thread only. ``App._log`` is the single cross-thread boundary
(``self._post``); nothing here may be called from a worker.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from constants import C

#: The user's preferred pane height is a preference, clamped on restore only.
MIN_H = 60
DEFAULT_H = 110
_NB_MIN = 200
_SASH = 6
_CFG_KEY = "output_pane_height"

_PROXY_PROC = "::tsm_readonly_text"
_PROXY_BODY = """
    set sub [lindex $args 0]
    if {$sub in {insert delete replace}} { return "" }
    if {$sub eq "edit" && [lindex $args 1] in {undo redo}} { return "" }
    uplevel 1 [list $orig {*}$args]
"""


def install_read_only(text: tk.Text) -> str:
    """Make *text* refuse mutation through its widget command; return the raw name.

    Every caller that must write -- only this module -- calls the returned
    command name directly (``text.tk.call(raw, "insert", ...)``).
    """
    interp = text.tk
    if not interp.call("info", "procs", _PROXY_PROC):
        interp.call("proc", _PROXY_PROC, "orig args", _PROXY_BODY)
    raw = text._w + "__rw"
    interp.call("rename", text._w, raw)
    interp.call("interp", "alias", "", text._w, "", _PROXY_PROC, raw)
    # Tk deletes the renamed command with the widget; the alias it cannot know
    # about, so it goes with the widget's own <Destroy>.
    interp.call("bind", text._w, "<Destroy>",
                "+if {\"%W\" eq \"" + text._w + "\"} "
                "{catch {interp alias {} " + text._w + " {}}}")
    return raw


class PeerText(tk.Text):
    """A Tk text PEER of an existing Text: one content, a second view.

    Content and tags are shared with the source; selection, focus, insert
    mark and scroll position are per view. tkinter has no wrapper for
    ``peer create``, so this sets up the Python side the way
    ``BaseWidget.__init__`` would and lets the source's command create the
    Tcl widget.
    """

    def __init__(self, master, source_raw: str, **kw) -> None:
        tk.BaseWidget._setup(self, master, {})
        self.tk.call(source_raw, "peer", "create", self._w,
                     *self._options(kw))


def clamp_height(value, window_height: int) -> int:
    """A safe pane height from a stored preference. Never written back."""
    try:
        h = int(value)
    except (TypeError, ValueError):
        return DEFAULT_H
    if isinstance(value, bool):
        return DEFAULT_H
    upper = max(MIN_H, int(window_height) - _NB_MIN - _SASH)
    return max(MIN_H, min(h, upper))


def at_bottom(text: tk.Text) -> bool:
    return text.yview()[1] >= 0.999


class OutputPaneController:
    """Owns the OUTPUT pane: header, read-only Text, height, and its pop-out.

    Callbacks are injected; the controller never holds the App.
    ``geometry_ok(geom)`` validates a remembered pop-out position before use,
    so a window last seen on a disconnected monitor comes back on screen.
    """

    def __init__(self, paned: tk.PanedWindow, cfg, *, host, above,
                 on_stop, on_open_savings, on_open_log,
                 geometry_ok=None) -> None:
        self._paned = paned
        self._cfg = cfg
        self._host = host
        self._above = above
        self._on_stop = on_stop
        self._on_open_savings = on_open_savings
        self._on_open_log = on_open_log
        self._geometry_ok = geometry_ok or (lambda geom: False)
        self._running = (False, "")
        self._headers: list = []        # (stop_btn, running_label) per live header
        self._popout = None             # the Toplevel while popped out
        self._peer = None
        self._docked_h = DEFAULT_H
        self._popout_geom = ""          # session only, never persisted

        self.frame = tk.Frame(paned, bg=C["base"], padx=14, pady=8)
        self._build_header(self.frame, docked=True)
        inner = tk.Frame(self.frame, bg=C["mantle"])
        inner.pack(fill=tk.BOTH, expand=True)
        self.text = self._make_text(inner)
        self._raw = install_read_only(self.text)
        self._attach_menu(self.text)

        # The window is not laid out yet, so the screen is the only honest
        # upper bound at construction; the stored preference is not rewritten.
        stored = cfg.raw.get(_CFG_KEY, DEFAULT_H)
        paned.add(self.frame, after=above, stretch="never", minsize=MIN_H,
                  height=clamp_height(stored, host.winfo_screenheight()))
        paned.bind("<ButtonRelease-1>", self._on_sash_release, add="+")
        host.bind("<Unmap>", self._on_host_unmap, add="+")
        host.bind("<Map>", self._on_host_map, add="+")

    @property
    def is_popped_out(self) -> bool:
        return self._popout is not None

    # ── Construction ──────────────────────────────────────────────────────

    def _build_header(self, parent: tk.Frame, *, docked: bool) -> None:
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row, text="OUTPUT", font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)
        ttk.Button(row, text="⧉  Pop out" if docked else "⇲  Dock",
                   command=self.pop_out if docked else self.dock
                   ).pack(side=tk.RIGHT)
        ttk.Button(row, text="View Log",
                   command=self._on_open_log).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(row, text="Savings",
                   command=self._on_open_savings).pack(side=tk.RIGHT, padx=(0, 6))
        stop = ttk.Button(row, text="■  Stop", style="Danger.TButton",
                          command=self._on_stop, state=tk.DISABLED)
        stop.pack(side=tk.RIGHT, padx=(0, 6))
        label = tk.Label(row, text="", font=("Segoe UI", 8),
                         bg=C["base"], fg=C["yellow"])
        label.pack(side=tk.RIGHT, padx=(0, 8))
        self._headers.append((stop, label))
        self._paint_header(stop, label)

    def _make_text(self, parent: tk.Frame, source_raw: str = "") -> tk.Text:
        options = dict(
            height=4, font=("Consolas", 9), bg=C["mantle"],
            fg=C["green"], relief=tk.FLAT, padx=10, pady=6, wrap=tk.WORD,
            state=tk.NORMAL, insertwidth=0,
            selectbackground=C["surface1"], selectforeground=C["text"],
            inactiveselectbackground=C["surface0"])
        text = (PeerText(parent, source_raw, **options) if source_raw
                else tk.Text(parent, **options))
        bar = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        # Tk keeps the TOP line anchored when a Text is resized, so shrinking
        # the pop-out or dragging the sash buried the newest output. Measured
        # live: a pop-out at the bottom showed line 1 after a resize. The last
        # scroll report says whether the view was following, and a resize of
        # a following view keeps it on the end.
        following = {"at_end": True}

        def on_scroll(first, last):
            bar.set(first, last)
            following["at_end"] = float(last) >= 0.999

        def on_resize(_event):
            if following["at_end"]:
                text.after_idle(text.see, "end")

        text.configure(yscrollcommand=on_scroll)
        text.bind("<Configure>", on_resize, add="+")
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        return text

    def _attach_menu(self, view: tk.Text) -> None:
        # No "…": every item acts immediately.
        menu = tk.Menu(view, tearoff=0)
        menu.add_command(label="Copy",
                         command=lambda: self._copy_selection(view))
        menu.add_command(label="Select all",
                         command=lambda: self._select_all(view))
        menu.add_command(label="Copy all", command=self._copy_all)
        menu.add_separator()
        menu.add_command(label="Clear view", command=self.clear_view)
        view.bind("<Button-3>", lambda e: self._popup_menu(e, view, menu))

    def _views(self) -> list:
        views = [self.text]
        if self._peer is not None and self._peer.winfo_exists():
            views.append(self._peer)
        return views

    # ── Writes: the only two mutation paths ───────────────────────────────

    def append(self, msg: str, colour: "str | None" = None) -> None:
        """Add one line; each view scrolls only if it was already at the bottom."""
        following = [view for view in self._views() if at_bottom(view)]
        tag = f"col_{colour}"
        self.text.tag_configure(tag, foreground=colour or C["green"])
        self.text.tk.call(self._raw, "insert", "end", msg + "\n", tag)
        for view in following:
            view.see("end")

    def clear_view(self) -> None:
        """Empty the display. Never stops a job and never touches the log file."""
        self.text.tk.call(self._raw, "delete", "1.0", "end")

    # ── Running state ─────────────────────────────────────────────────────

    def set_running(self, running: bool, label: str = "") -> None:
        self._running = (bool(running), label)
        live = []
        for stop, lbl in self._headers:
            if stop.winfo_exists() and lbl.winfo_exists():
                self._paint_header(stop, lbl)
                live.append((stop, lbl))
        self._headers = live

    def _paint_header(self, stop, label) -> None:
        running, text = self._running
        stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        label.configure(text=f"⏳ running: {text}" if running else "")

    # ── Pop-out ───────────────────────────────────────────────────────────

    def pop_out(self) -> None:
        """Move the pane into its own window; the tabs take the space."""
        if self._popout is not None:
            self._popout.deiconify()
            self._popout.lift()
            return
        self._docked_h = self._current_height()
        self._paned.forget(self.frame)
        win = tk.Toplevel(self._host)
        win.title("TokenSave Manager — Output")
        win.configure(bg=C["base"])
        win.minsize(360, 160)
        if self._popout_geom and self._geometry_ok(self._popout_geom):
            win.geometry(self._popout_geom)
        else:
            win.geometry("%dx%d" % (max(640, self._host.winfo_width() // 2),
                                    max(280, self._docked_h * 3)))
        body = tk.Frame(win, bg=C["base"], padx=14, pady=8)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_header(body, docked=False)
        inner = tk.Frame(body, bg=C["mantle"])
        inner.pack(fill=tk.BOTH, expand=True)
        self._peer = self._make_text(inner, source_raw=self._raw)
        install_read_only(self._peer)
        self._attach_menu(self._peer)
        win.protocol("WM_DELETE_WINDOW", self.dock)
        self._popout = win
        # Lay out first: `see` on a view with no height scrolls nothing, and a
        # view left at the top would then never follow new lines. Measured on
        # the live window: without this the pop-out opened at line 1.
        win.update_idletasks()
        self._peer.see("end")

    def dock(self) -> None:
        """Close the pop-out and put the pane back exactly as it was.

        Idempotent: the Dock button and the window's close box can both
        arrive, and the state flag is cleared before anything is destroyed.
        """
        win = self._popout
        if win is None:
            return
        self._popout = None
        if win.winfo_exists():
            self._popout_geom = win.geometry()
            win.destroy()
        self._peer = None
        follow = at_bottom(self.text)
        height = self._docked_h
        self._paned.add(self.frame, after=self._above, stretch="never",
                        minsize=MIN_H, height=height)
        if follow:
            self.text.see("end")
        self._host.after_idle(lambda: self._restore_sash(height))

    def _current_height(self) -> int:
        if self.frame.winfo_ismapped() and self.frame.winfo_height() >= MIN_H:
            return self.frame.winfo_height()
        return int(self._paned.panecget(self.frame, "height") or DEFAULT_H)

    def _restore_sash(self, height: int) -> None:
        """Pin the sash so the docked pane is `height` tall once laid out."""
        if self._popout is not None or not self._paned.winfo_exists():
            return
        panes = [str(p) for p in self._paned.panes()]
        if str(self.frame) not in panes:
            return
        index = panes.index(str(self.frame))
        total = self._paned.winfo_height()
        if index > 0 and total > height:
            sash = int(self._paned.cget("sashwidth"))
            self._paned.sash_place(index - 1, 0, total - height - sash)

    def _on_host_unmap(self, event) -> None:
        """Hiding the main window (tray, minimise) hides the pop-out with it."""
        if event.widget is self._host and self._popout is not None:
            self._popout.withdraw()

    def _on_host_map(self, event) -> None:
        if event.widget is self._host and self._popout is not None:
            self._popout.deiconify()

    # ── Clipboard ─────────────────────────────────────────────────────────

    def _copy_selection(self, view: "tk.Text | None" = None) -> None:
        (view or self.text).event_generate("<<Copy>>")

    def _select_all(self, view: "tk.Text | None" = None) -> None:
        (view or self.text).tag_add("sel", "1.0", "end-1c")

    def _copy_all(self) -> None:
        self.text.clipboard_clear()
        self.text.clipboard_append(self.text.get("1.0", "end-1c"))

    def _popup_menu(self, event, view: tk.Text, menu: tk.Menu) -> str:
        view.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    # ── Height preference ─────────────────────────────────────────────────

    def _on_sash_release(self, event) -> None:
        """Save the user's height -- the last committed sash position wins."""
        if event.widget is not self._paned or self._popout is not None:
            return
        self.save_height(self.frame.winfo_height())

    def save_height(self, height: int) -> None:
        if height < MIN_H:
            return
        self._cfg.raw[_CFG_KEY] = int(height)
        self._cfg.save()
