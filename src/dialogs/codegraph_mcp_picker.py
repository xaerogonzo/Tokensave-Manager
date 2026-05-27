"""CodegraphMCPPickerDialog — select which AI agents to wire CodeGraph into (v4.7).

Opened from Settings → CodeGraph section → "⚙ Configure MCP — pick agents…".
Wraps ``codegraph install --target=<comma-list> --yes [--no-permissions]``.

Agent surface (verified live from ``codegraph install --help`` on 2026-05-27):

    claude      Claude Code      ~/.claude.json
    cursor      Cursor           ~/.cursor/mcp.json
    codex       Codex CLI        ~/.codex/config.toml
    opencode    opencode         ~/AppData/Roaming/opencode/opencode.jsonc (Win)

The web docs list 4 additional agents (Hermes / Gemini / Antigravity / Kiro);
they are NOT supported by the binary — invoking them produces
``Unknown target "hermes". Known: claude, cursor, codex, opencode.``  Plan
ships only the 4 verified agents.

Detection: per-agent ``_codegraph_agent_installed`` checks the destination
file or parent directory exists. Instant — no subprocess invocations during
dialog open. Not-detected agents render disabled with ``(not installed)``.

Layout mirrors ``dialogs/scrub_history.py``:
  * destructive-action style banner (informational this time)
  * action bar (Install / Cancel) packed ``side=BOTTOM`` BEFORE the agent
    list so it can never be pushed off-screen (v4.5 sticky-footer pattern)
  * agent list expands in the middle
  * read-only log pane at the bottom for subprocess output

No threading.Lock — only the worker thread writes to ``self._in_flight``,
and the main thread reads it under the GIL once per state refresh.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C, CREATE_NO_WINDOW
from helpers.mcp import (
    _CODEGRAPH_AGENTS,
    _claude_code_mcp_has_codegraph,
    _codegraph_agent_destination_path,
    _codegraph_agent_installed,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class CodegraphMCPPickerDialog(tk.Toplevel):
    """Per-agent MCP-wiring picker for the codegraph install command.

    Constructed from SettingsDialog when the user clicks "⚙ Configure
    MCP — pick agents…". Calls ``on_done`` callback (if provided) after
    a successful install so the parent dialog can refresh its status row.
    """

    def __init__(self, parent, cfg: "ManagerConfig",
                 on_done: "Callable[[], None] | None" = None) -> None:
        super().__init__(parent)
        self._parent_dialog = parent
        self._cfg = cfg
        self._on_done = on_done
        self.title("CodeGraph — Configure MCP")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 480)
        self.grab_set()

        # State
        self._agent_vars: dict = {}        # agent_id -> tk.BooleanVar
        self._agent_chks:  dict = {}        # agent_id -> ttk.Checkbutton widget
        self._no_perms_var = tk.BooleanVar(value=False)
        self._in_flight = False

        # Build sections — sticky-footer pattern: action bar FIRST (BOTTOM),
        # then the scrollable middle expands into the remaining space.
        self._build_header()
        self._build_action_bar()
        self._build_log_pane()       # near-bottom; sized small
        self._build_agent_list()      # middle; expands

        self._centre_on_parent(parent)

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_header(self) -> None:
        """Informational top banner."""
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="🔌  Wire CodeGraph as an MCP server",
            bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=(
                "Pick which AI agents should have access to CodeGraph's "
                "MCP tools (codegraph_search / codegraph_context / etc.). "
                "Detected agents are checked by default; agents not "
                "installed on this machine are disabled. "
                "Restart the picked agents after install for the new "
                "tools to load."
            ),
            bg=C["surface0"], fg=C["text"],
            font=("Segoe UI", 9),
            wraplength=520, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        """Install / Cancel buttons — packed BOTTOM before the middle section."""
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._install_btn = ttk.Button(
            bar, text="🔌  Install for selected agents",
            command=self._on_install,
        )
        self._install_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _build_log_pane(self) -> None:
        """Read-only log pane (small, near the bottom)."""
        wrap = tk.LabelFrame(
            self, text="Output",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        wrap.pack(fill=tk.X, side=tk.BOTTOM, padx=18, pady=(4, 4))
        self._log_txt = tk.Text(
            wrap, height=6, font=("Consolas", 8),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._log_txt.pack(fill=tk.X, padx=8, pady=(6, 8))

    def _build_agent_list(self) -> None:
        """Per-agent checkbox row, plus the --no-permissions advanced toggle."""
        wrap = tk.LabelFrame(
            self, text="Agents",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 4))

        for agent_id, label in _CODEGRAPH_AGENTS:
            installed = _codegraph_agent_installed(agent_id)
            var = tk.BooleanVar(value=installed)
            self._agent_vars[agent_id] = var
            row = tk.Frame(wrap, bg=C["base"])
            row.pack(fill=tk.X, padx=12, pady=2)
            chk = ttk.Checkbutton(row, text=label, variable=var)
            chk.pack(side=tk.LEFT)
            self._agent_chks[agent_id] = chk
            # Disable + clear when not installed
            if not installed:
                var.set(False)
                chk.configure(state=tk.DISABLED)
                tk.Label(
                    row, text="(not installed)",
                    bg=C["base"], fg=C["overlay0"],
                    font=("Segoe UI", 8, "italic"),
                ).pack(side=tk.LEFT, padx=(8, 0))
            else:
                # Show the destination path next to each enabled row
                dest = _codegraph_agent_destination_path(agent_id)
                tk.Label(
                    row, text=f"→ {dest}",
                    bg=C["base"], fg=C["overlay0"],
                    font=("Consolas", 8),
                ).pack(side=tk.LEFT, padx=(8, 0))

        # Advanced toggle
        adv = tk.LabelFrame(
            self, text="Advanced",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        adv.pack(fill=tk.X, padx=18, pady=(0, 4))
        ttk.Checkbutton(
            adv,
            text="Skip Claude auto-allow permissions (--no-permissions)",
            variable=self._no_perms_var,
        ).pack(anchor=tk.W, padx=12, pady=(6, 2))
        tk.Label(
            adv,
            text=("    Only meaningful when Claude Code is in the target "
                  "list. Default (off) lets codegraph add codegraph_* tools "
                  "to Claude Code's auto-allow list so you don't have to "
                  "approve each call manually."),
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8),
            wraplength=520, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=12, pady=(0, 6))

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 640, 560
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Install action ────────────────────────────────────────────────────────

    def _on_install(self) -> None:
        """Run `codegraph install --target=<comma-list> --yes [--no-permissions]`."""
        if self._in_flight:
            return
        selected = [aid for aid, var in self._agent_vars.items() if var.get()]
        if not selected:
            messagebox.showinfo(
                "No agents selected",
                "Pick at least one agent to wire CodeGraph into.",
                parent=self)
            return
        exe = self._cfg.codegraph_exe or ""
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(
                "CodeGraph binary not found",
                "The codegraph executable is not configured. "
                "Install it first via Settings → 'Install binary (npm)'.",
                parent=self)
            return

        self._in_flight = True
        self._install_btn.configure(
            state=tk.DISABLED, text="🔌  Installing…")
        target_csv = ",".join(selected)
        cmd = [exe, "install", f"--target={target_csv}", "--yes"]
        if self._no_perms_var.get():
            cmd.append("--no-permissions")
        self._log_append(f"$ {' '.join(cmd)}")

        def _worker() -> None:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=120,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace",
                )
            except subprocess.TimeoutExpired:
                self.after(0, lambda: self._on_install_done(
                    False, "codegraph install timed out after 120 s."))
                return
            except (FileNotFoundError, OSError) as exc:
                self.after(0, lambda e=exc: self._on_install_done(
                    False, f"Could not launch codegraph: {e}"))
                return
            ok = proc.returncode == 0
            log = (proc.stdout or "") + (proc.stderr or "")
            self.after(0, lambda o=ok, l=log: self._on_install_done(o, l))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_done(self, ok: bool, log: str) -> None:
        """Main-thread callback after the codegraph install subprocess returns."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._in_flight = False
        self._install_btn.configure(
            state=tk.NORMAL, text="🔌  Install for selected agents")
        for line in (log or "").splitlines()[-30:]:
            self._log_append(line)
        if ok:
            self._log_append("\n✓ Install complete.")
            # Verify wiring landed for at least Claude Code (the
            # common case). Other agents' verifiers are deferred.
            wired, key = _claude_code_mcp_has_codegraph()
            if wired:
                self._log_append(
                    f"  ✓ Claude Code wired (mcpServers.{key})")
            messagebox.showinfo(
                "CodeGraph MCP installed",
                "✓ MCP server wired. Restart Claude Code (and any other "
                "selected agents) for the new codegraph_* tools to "
                "appear in agent sessions.",
                parent=self)
            if self._on_done is not None:
                try:
                    self._on_done()
                except Exception:
                    pass
            self.destroy()
        else:
            self._log_append("\n✗ Install failed — see output above.")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_append(self, line: str) -> None:
        try:
            self._log_txt.configure(state=tk.NORMAL)
            self._log_txt.insert(tk.END, line + "\n")
            self._log_txt.see(tk.END)
            self._log_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass
