"""TokensaveDaemonManagerDialog — list + stop running tokensave MCP servers.

Opened from Tool Manager → tokensave row → "🔌 Manage servers…".

The sibling of ``CodegraphDaemonManagerDialog``, for the same class of problem:
a server spawned by Claude Code holds ``.tokensave/tokensave.db`` open, and
until Roadmap-10 the only fix was hunting the OS process by hand. tokensave's
own daemon UI was removed in v6.0.0 along with its daemon — but ``tokensave
serve`` processes still exist and still hold that lock, which is what upstream
issue #421 reported.

## Why this dialog was more cautious than the CodeGraph one

``codegraph daemon`` prints each daemon's project path, so that dialog can
label every row with certainty. ``tokensave serve`` mostly did not: measured
on one machine, two of eight servers declared ``-p <path>`` and six declared
nothing. ``helpers/tokensave_daemon.py`` recovered what it could by
correlating each project's ``tokensave.db-shm`` mtime against process start
times.

**tokensave 7.11.0 fixed #421**, and every server it starts now registers its
own project, so on a current binary the rows are authoritative and the
heuristic never runs. Re-measured on the same machine after the upgrade:
**seven servers, all seven named**, where six would previously have been
unattributed and therefore unstoppable.

The four confidence levels stay regardless, because a server started by an
older tokensave registers nothing and still holds its lock. The Stop control
differs for each:

  * **authoritative** — declared its project. Stop, after the usual confirm.
  * **heuristic** — matched on timing. Stop only after a second confirmation
    that names the project and says plainly that it is a guess.
  * **unattributed / ambiguous** — Stop is disabled, with the reason shown.
    Killing a server whose project we cannot name risks taking down the one
    serving somebody's live session instead of the one holding the lock.

## No "Stop all"

Claude Code restarts its MCP servers, so stopping them all does not converge —
during the incident that prompted this work, PIDs vanished and reappeared
between two checks minutes apart. A "Stop all" button would churn the user's
live sessions to no purpose. Stop the one identified holder.

Layout follows the sticky-footer pattern (action bar ``side=BOTTOM`` before
the expanding middle section), mirroring the CodeGraph dialog.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C
from theme import UiPumpMixin, bind_mousewheel
from helpers.project_discovery import find_projects
from helpers.tokensave_daemon import (
    AMBIGUOUS,
    AUTHORITATIVE,
    HEURISTIC,
    UNATTRIBUTED,
    list_tokensave_servers,
    stop_tokensave_server,
)

if TYPE_CHECKING:
    from state import ManagerConfig


#: Per-attribution presentation. Kept as data next to the states it describes
#: so a new state cannot be added without someone deciding how it looks.
_BADGE = {
    AUTHORITATIVE: ("✓ declared", "green",
                    "This server names its project on its command line."),
    HEURISTIC: ("⚠ heuristic", "peach",
                "Matched by database-open timestamp — good evidence, "
                "but a guess."),
    UNATTRIBUTED: ("? unknown", "overlay0",
                   "No project matched this server's start time."),
    AMBIGUOUS: ("✗ ambiguous", "red",
                "More than one project matches; which one this serves "
                "cannot be determined."),
}


class TokensaveDaemonManagerDialog(UiPumpMixin, tk.Toplevel):
    """List every running tokensave server; stop the ones we can identify."""

    def __init__(self, parent, cfg: "ManagerConfig",
                 on_done: "Callable[[], None] | None" = None) -> None:
        super().__init__(parent)
        # Before anything can post to it.
        self._start_ui_pump()
        self._cfg = cfg
        self._on_done = on_done
        self.title("tokensave — Manage servers")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(680, 560)
        self.grab_set()

        self._servers: list = []
        self._row_widgets: dict = {}
        self._busy_pids: set = set()

        self._build_header()
        self._build_action_bar()
        self._build_list_section()

        self._centre_on_parent(parent)
        self.refresh()

    # ── Section builders ─────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="Running tokensave servers",
            bg=C["surface0"], fg=C["text"], font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=("A server holds its project's index open, which is what "
                  "blocks deleting a worktree or force-syncing. Most servers "
                  "do not report which project they serve, so rows are "
                  "labelled with how confident that identification is — only "
                  "identified servers can be stopped.\n"
                  "Claude Code restarts its servers automatically, so a "
                  "stopped one may reappear. Stop the one holding the lock "
                  "rather than clearing the list."),
            bg=C["surface0"], fg=C["subtext"], font=("Segoe UI", 9),
            wraplength=620, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._refresh_btn = ttk.Button(bar, text="↻  Refresh",
                                       command=self.refresh)
        self._refresh_btn.pack(side=tk.RIGHT, padx=(0, 6))

    def _build_list_section(self) -> None:
        wrap = tk.LabelFrame(
            self, text="Servers", fg=C["subtext"], bg=C["base"],
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
        w, h = 740, 560
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Listing ──────────────────────────────────────────────────────────

    def refresh(self) -> None:
        self._refresh_btn.configure(state=tk.DISABLED, text="↻  Scanning…")
        self._clear_list("Scanning for running servers…")
        exe = self._cfg.tokensave_exe or ""
        roots = list(self._cfg.search_roots or [])

        def _worker() -> None:
            # Project discovery walks the disk, so it belongs off the Tk
            # thread along with the process enumeration it feeds.
            projects = [p["path"] for p in find_projects(roots)]
            servers = list_tokensave_servers(exe, projects)
            self._post(lambda: self._on_refresh_done(servers))

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_list(self, placeholder: str) -> None:
        for child in self._list_body.winfo_children():
            child.destroy()
        self._row_widgets = {}
        tk.Label(self._list_body, text=f"  {placeholder}",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=8)

    def _on_refresh_done(self, servers: list) -> None:
        try:
            if not self.winfo_exists():
                return
            self._refresh_btn.configure(state=tk.NORMAL, text="↻  Refresh")
            self._servers = servers
            self._populate_list(servers)
        except tk.TclError:
            return

    def _populate_list(self, servers: list) -> None:
        for child in self._list_body.winfo_children():
            child.destroy()
        self._row_widgets = {}
        if not servers:
            tk.Label(self._list_body,
                     text="  No tokensave servers are currently running.",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=8)
            return
        for srv in servers:
            self._add_server_row(srv)

    def _add_server_row(self, srv) -> None:
        row = tk.Frame(self._list_body, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=2)

        info = tk.Frame(row, bg=C["mantle"])
        info.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4), pady=6)

        label, colour, _tip = _BADGE.get(srv.attribution,
                                         _BADGE[UNATTRIBUTED])
        title = tk.Frame(info, bg=C["mantle"])
        title.pack(anchor=tk.W, fill=tk.X)
        tk.Label(title, text=srv.project or "(project unidentified)",
                 bg=C["mantle"],
                 fg=C["text"] if srv.project else C["overlay0"],
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        tk.Label(title, text="  " + label, bg=C["mantle"], fg=C[colour],
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)

        # The version is worth a glyph of its own: it is what separates a
        # server that registered itself from one that could only ever be
        # guessed at, which is the difference between the two halves of this
        # dialog's contract.
        meta = f"pid {srv.pid}"
        if srv.version:
            meta += f"  ·  tokensave {srv.version}"
        tk.Label(info, text=meta, bg=C["mantle"],
                 fg=C["overlay0"], font=("Segoe UI", 8)).pack(anchor=tk.W)

        # The index, when the registry named one. This is the row's actual
        # payload for the job people open this dialog to do — finding what
        # holds a directory open so it can be deleted — and it is not
        # derivable from the project path, because a per-branch database does
        # not live at a fixed place beneath it.
        if srv.db_path:
            tk.Label(info, text=srv.db_path, bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 8), wraplength=460,
                     justify=tk.LEFT).pack(anchor=tk.W)
        if srv.detail:
            # The most-recent-index fallback is the one selection worth
            # noticing: it is not a decision anyone made, and it moves the
            # next time another project syncs. Muted grey would file it with
            # the rest of the explanatory text.
            drifting = srv.selection == "most-recent-index"
            tk.Label(info, text=srv.detail, bg=C["mantle"],
                     fg=C["peach"] if drifting else C["overlay0"],
                     font=("Segoe UI", 8), wraplength=460,
                     justify=tk.LEFT).pack(anchor=tk.W)

        stop_btn = ttk.Button(row, text="Stop",
                              command=lambda s=srv: self._on_stop(s))
        stop_btn.pack(side=tk.RIGHT, padx=8)
        if not srv.can_stop:
            # Disabled rather than hidden: the reason is the useful part, and
            # a missing button reads as a rendering bug.
            stop_btn.configure(state=tk.DISABLED)
        self._row_widgets[srv.pid] = {"frame": row, "stop_btn": stop_btn}

    # ── Stop ─────────────────────────────────────────────────────────────

    def _on_stop(self, srv) -> None:
        if srv.pid in self._busy_pids or not srv.can_stop:
            return
        if not self._confirm_stop(srv):
            return

        self._busy_pids.add(srv.pid)
        widgets = self._row_widgets.get(srv.pid)
        if widgets:
            widgets["stop_btn"].configure(state=tk.DISABLED, text="Stopping…")

        def _worker() -> None:
            ok, detail = stop_tokensave_server(srv, confirmed=True)
            self._post(lambda: self._on_stop_done(srv, ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm_stop(self, srv) -> bool:
        """One confirmation for a declared server, a blunter one for a guess."""
        if srv.attribution == HEURISTIC:
            return messagebox.askyesno(
                "Stop a server identified by guesswork?",
                "This server does not say which project it serves.\n\n"
                "It was matched to:\n    %s\n\n"
                "…because that project's index was opened at the same moment "
                "this process started. That is good evidence, not proof — if "
                "the match is wrong, stopping this will interrupt a different "
                "project's session instead.\n\n"
                "Stop process %d anyway?" % (srv.project, srv.pid),
                icon="warning", default="no", parent=self)
        from helpers.mcp_shadow import WRAPPER_SELECTIONS

        if srv.selection in WRAPPER_SELECTIONS:
            # A wrapper-spawned server belongs to Claude Desktop, and Desktop
            # does NOT restart an MCP server that dies -- it reports "Server
            # disconnected" and leaves it (docs/MCP_INTEGRATION_GOTCHAS.md).
            # The generic wording below promises a reconnect that will not
            # happen, which would turn "serving the wrong tree" into "no
            # tokensave at all" with no warning.
            return messagebox.askyesno(
                "Stop Claude Desktop's tokensave server?",
                "Stop the tokensave server for:\n    %s\n\n"
                "(pid %d — started by Claude Desktop)\n\n"
                "Claude Desktop's tokensave tools will disconnect, and "
                "Desktop will NOT start a replacement: it reports the server "
                "as disconnected and leaves it that way until Desktop itself "
                "is restarted.\n\n"
                "This is a cleanup for a server that is serving the wrong "
                "project — not routine maintenance."
                % (srv.project or "(unidentified)", srv.pid),
                icon="warning", default="no", parent=self)

        return messagebox.askyesno(
            "Stop this server?",
            "Stop the tokensave server for:\n    %s\n\n"
            "(pid %d)\n\nAny Claude Code session using it will reconnect, "
            "and may restart the server automatically."
            % (srv.project or "(unidentified)", srv.pid),
            parent=self)

    def _on_stop_done(self, srv, ok: bool, detail: str) -> None:
        self._busy_pids.discard(srv.pid)
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if ok:
            messagebox.showinfo("Server stopped",
                                f"pid {srv.pid}: {detail}", parent=self)
        else:
            messagebox.showerror(
                "Could not stop the server",
                f"pid {srv.pid}: {detail}\n\n"
                "If the process changed since this list was scanned, refresh "
                "and try again — a PID can be reused by an unrelated "
                "process, so it is not acted on without re-checking.",
                parent=self)
        if self._on_done:
            self._on_done()
        self.refresh()
