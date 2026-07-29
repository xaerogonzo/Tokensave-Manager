"""CodegraphDaemonManagerDialog — list + stop running CodeGraph MCP daemons.

Opened from Tool Manager → CodeGraph row → "🔌 Manage daemons…".

Built after a user hit `codegraph index --force` failing with an EPERM lock
error — the blocker was a live CodeGraph MCP daemon (spawned by Claude Code)
holding the DB file open, and the only fix was manually hunting down and
killing the OS process. This dialog is that hunt, done once, from the GUI.

Daemon listing is GLOBAL — `codegraph daemon` returns every running daemon
across every project on the machine, not just the currently selected one
(confirmed live: two daemons showed up simultaneously, one per open project).
So this dialog has no "select a project first" precondition, unlike the
per-project CodeGraph Init/Sync/Status commands.

Stopping goes straight to OS-level PID termination
(`helpers.codegraph_daemon.kill_codegraph_daemon`) rather than driving
`codegraph daemon`'s interactive TTY picker — that protocol is undocumented
and getting it wrong risks stopping the wrong daemon. See the module
docstring in `helpers/codegraph_daemon.py` for the full rationale.

Layout follows the v4.5 sticky-footer pattern (action bar `side=BOTTOM`
before the expanding middle section) and mirrors
`dialogs/tokensave_mcp_picker.py`'s shape for visual consistency between the
two Tool Manager pickers shipped in the same cycle.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C
from theme import bind_mousewheel
from helpers.codegraph_daemon import (
    kill_codegraph_daemon,
    list_codegraph_daemons,
    unlock_codegraph_project,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class CodegraphDaemonManagerDialog(tk.Toplevel):
    """List every running CodeGraph daemon; stop any of them by PID."""

    def __init__(self, parent, cfg: "ManagerConfig",
                on_done: "Callable[[], None] | None" = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._on_done = on_done
        self.title("CodeGraph — Manage daemons")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 560)
        self.grab_set()

        self._daemons: list = []
        self._row_widgets: dict = {}   # pid -> {frame, stop_btn, ...}
        self._busy_pids: set = set()

        self._build_header()
        self._build_action_bar()
        self._build_unlock_section()
        self._build_list_section()

        self._centre_on_parent(parent)
        self.refresh()

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="🔌  CodeGraph daemons",
            bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=(
                "CodeGraph runs one background daemon per project it's wired "
                "into, spawned by whichever AI agent connects to it. A daemon "
                "holds an exclusive lock on that project's index — this is "
                "what blocks a manual reindex with an EPERM error. Stopping "
                "one here ends the OS process directly; the agent session "
                "that started it will need to reconnect before CodeGraph "
                "tools work there again."
            ),
            bg=C["surface0"], fg=C["text"], font=("Segoe UI", 9),
            wraplength=580, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._refresh_btn = ttk.Button(
            bar, text="↻  Refresh", command=self.refresh)
        self._refresh_btn.pack(side=tk.RIGHT, padx=(0, 6))

    def _build_unlock_section(self) -> None:
        """Advanced fallback: remove a STALE lock when no daemon shows up."""
        adv = tk.LabelFrame(
            self, text="Advanced — stale lock",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        adv.pack(fill=tk.X, side=tk.BOTTOM, padx=18, pady=(0, 4))
        tk.Label(
            adv,
            text=("If a project is locked but no daemon for it appears "
                  "above, the lock is likely orphaned from a crashed "
                  "process. Enter the project path and remove it directly:"),
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8),
            wraplength=580, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=10, pady=(6, 4))
        row = tk.Frame(adv, bg=C["base"])
        row.pack(fill=tk.X, padx=10, pady=(0, 8))
        self._unlock_path_var = tk.StringVar()
        ttk.Entry(row, textvariable=self._unlock_path_var,
                 font=("Consolas", 9)).pack(side=tk.LEFT, fill=tk.X,
                                            expand=True, padx=(0, 6))
        ttk.Button(row, text="Remove stale lock",
                  command=self._on_unlock).pack(side=tk.LEFT)

    def _build_list_section(self) -> None:
        wrap = tk.LabelFrame(
            self, text="Running daemons",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 4))

        canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        bind_mousewheel(canvas)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(canvas, bg=C["base"])
        body_id = canvas.create_window((0, 0), window=self._list_body,
                                       anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        self._list_body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 700, 560
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Listing ───────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-run the daemon listing in a worker thread."""
        self._refresh_btn.configure(state=tk.DISABLED, text="↻  Refreshing…")
        self._clear_list("Scanning for running daemons…")
        exe = self._cfg.codegraph_exe or ""

        def _worker() -> None:
            daemons = list_codegraph_daemons(exe)
            self.after(0, lambda: self._on_refresh_done(daemons))

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_list(self, placeholder: str) -> None:
        for child in self._list_body.winfo_children():
            child.destroy()
        self._row_widgets = {}
        tk.Label(self._list_body, text=f"  {placeholder}",
                bg=C["base"], fg=C["overlay0"],
                font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=8)

    def _on_refresh_done(self, daemons: list) -> None:
        try:
            if not self.winfo_exists():
                return
            self._refresh_btn.configure(state=tk.NORMAL, text="↻  Refresh")
            self._daemons = daemons
            self._populate_list(daemons)
        except tk.TclError:
            return

    def _populate_list(self, daemons: list) -> None:
        for child in self._list_body.winfo_children():
            child.destroy()
        self._row_widgets = {}
        if not daemons:
            tk.Label(self._list_body,
                    text="  No CodeGraph daemons currently running.",
                    bg=C["base"], fg=C["overlay0"],
                    font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=8)
            return
        for d in daemons:
            self._add_daemon_row(d)

    def _add_daemon_row(self, daemon: dict) -> None:
        pid = daemon["pid"]
        row = tk.Frame(self._list_body, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=2)

        info = tk.Frame(row, bg=C["mantle"])
        info.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4), pady=6)
        tk.Label(info, text=daemon["path"], bg=C["mantle"], fg=C["text"],
                font=("Consolas", 9)).pack(anchor=tk.W)
        tk.Label(
            info,
            text=f"pid {pid}   v{daemon['version']}   up {daemon['uptime']}",
            bg=C["mantle"], fg=C["overlay0"], font=("Segoe UI", 8),
        ).pack(anchor=tk.W)

        stop_btn = ttk.Button(row, text="Stop",
                              command=lambda p=pid: self._on_stop(p))
        stop_btn.pack(side=tk.RIGHT, padx=8)
        self._row_widgets[pid] = {"frame": row, "stop_btn": stop_btn}

    # ── Stop ──────────────────────────────────────────────────────────────────

    def _on_stop(self, pid: int) -> None:
        if pid in self._busy_pids:
            return
        daemon = next((d for d in self._daemons if d["pid"] == pid), None)
        path = daemon["path"] if daemon else "(unknown project)"
        if not messagebox.askyesno(
                "Stop CodeGraph daemon?",
                f"Stop the CodeGraph daemon for:\n\n  {path}\n\n(PID {pid})\n\n"
                "This ends the process directly. Whichever AI agent session "
                "started it will need to reconnect before CodeGraph tools "
                "work there again.",
                parent=self, default="no"):
            return

        self._busy_pids.add(pid)
        widgets = self._row_widgets.get(pid)
        if widgets:
            widgets["stop_btn"].configure(state=tk.DISABLED, text="Stopping…")

        def _worker() -> None:
            ok, detail = kill_codegraph_daemon(pid)
            self.after(0, lambda: self._on_stop_done(pid, ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_stop_done(self, pid: int, ok: bool, detail: str) -> None:
        self._busy_pids.discard(pid)
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if not ok:
            messagebox.showerror("Stop failed",
                                 f"Could not stop PID {pid}:\n\n{detail}",
                                 parent=self)
            widgets = self._row_widgets.get(pid)
            if widgets:
                widgets["stop_btn"].configure(state=tk.NORMAL, text="Stop")
            return
        self.refresh()
        if self._on_done:
            try:
                self._on_done()
            except Exception:
                pass

    # ── Unlock (advanced fallback) ───────────────────────────────────────────

    def _on_unlock(self) -> None:
        path = self._unlock_path_var.get().strip()
        if not path:
            messagebox.showinfo("No path", "Enter a project path first.",
                                parent=self)
            return
        if not messagebox.askyesno(
                "Remove stale lock?",
                f"Remove the CodeGraph lock file for:\n\n  {path}\n\n"
                "Only do this if no daemon for this project is listed above "
                "— removing a lock a live daemon still holds can corrupt "
                "the index.",
                parent=self, default="no"):
            return
        exe = self._cfg.codegraph_exe or ""

        def _worker() -> None:
            ok, detail = unlock_codegraph_project(exe, path)
            self.after(0, lambda: self._on_unlock_done(ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_unlock_done(self, ok: bool, detail: str) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if ok:
            messagebox.showinfo("Lock removed",
                                detail or "Lock removed.", parent=self)
        else:
            messagebox.showerror("Unlock failed",
                                 detail or "Unknown error.", parent=self)
