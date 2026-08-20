"""TokensaveMCPPickerDialog — wire tokensave into AI agents from the Manager.

Opened from Tool Manager → tokensave row → "🔌 Wire into agents…", and offered
directly from `tokensave doctor` output when it nags about missing integrations.

Wraps ``tokensave install --agent <id> --git-hook no [perm flags]``.

Three things make this NOT a copy of ``dialogs/codegraph_mcp_picker.py``
(verified live from ``tokensave install --help``, v7.8.1):

  1. ``--agent`` is SINGULAR.  codegraph takes ``--target=a,b,c``; tokensave
     takes exactly one agent per invocation, so ``_worker`` loops and runs one
     subprocess per selected agent.  A failure on one agent must not abort the
     rest.
  2. ``--git-hook default`` installs a GLOBAL git ``post-commit`` hook, prompting
     on a TTY and silently skipping otherwise.  We run non-TTY so it would skip,
     but relying on that is fragile — ``--git-hook no`` is always passed
     explicitly, with an opt-in checkbox for users who do want it.
  3. There is no global ``--yes``.  Pinning ``--git-hook`` is what makes the run
     fully non-interactive.

Agent list is two-tier: detected agents render directly and are pre-checked;
the ~18 undetected ones hide behind a "Show all agents" expander so the dialog
doesn't become a wall of greyed-out checkboxes.

Because ``tokensave install`` rewrites agent-control config (``~/.claude.json``,
``~/.claude/settings.json`` — MCP entries, hooks, tool permissions), the exact
argv and every destination file are shown for confirmation before anything runs.

Layout follows the v4.5 sticky-footer pattern: action bar packed ``side=BOTTOM``
BEFORE the expanding middle section so it can never be pushed off-screen.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C, CREATE_NO_WINDOW
from theme import UiPumpMixin, bind_mousewheel
from helpers.mcp import (
    _TOKENSAVE_AGENTS,
    _tokensave_agent_destination_path,
    _tokensave_agent_installed,
)

if TYPE_CHECKING:
    from state import ManagerConfig


# Per-agent subprocess timeout. Generous: `install` can touch several files
# and (on first run for an agent) create directory trees.
_INSTALL_TIMEOUT = 120


def build_install_argv(
    ts_exe: str,
    agent_id: str,
    *,
    wildcard_permissions: bool = False,
    git_hook: bool = False,
) -> list:
    """Build the argv for wiring ONE agent.

    Pure + module-level so tests can assert argv construction without
    standing up Tk.  Note the singular ``--agent`` — see module docstring.
    """
    argv = [ts_exe, "install", "--agent", agent_id,
            "--git-hook", "yes" if git_hook else "no"]
    if wildcard_permissions:
        argv.append("--wildcard-permissions")
    return argv


class TokensaveMCPPickerDialog(UiPumpMixin, tk.Toplevel):
    """Per-agent picker for ``tokensave install``.

    ``preselect`` pre-checks a specific set of agent ids — used by the Doctor
    follow-up so the agents doctor complained about arrive already ticked.
    ``on_done`` fires after a run so the opener can refresh its status row.
    """

    def __init__(
        self,
        parent,
        cfg: "ManagerConfig",
        preselect: "list | None" = None,
        on_done: "Callable[[], None] | None" = None,
    ) -> None:
        super().__init__(parent)
        # Start the worker -> UI channel before anything can post to it.
        self._start_ui_pump()
        self._cfg = cfg
        self._on_done = on_done
        self._preselect = set(preselect or ())
        self.title("tokensave — Wire into agents")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(600, 540)
        self.grab_set()

        # State
        self._agent_vars: dict = {}     # agent_id -> tk.BooleanVar
        self._agent_chks: dict = {}     # agent_id -> ttk.Checkbutton
        self._wildcard_var = tk.BooleanVar(value=False)
        self._git_hook_var = tk.BooleanVar(value=False)
        self._in_flight = False
        self._show_all = False

        # Sticky-footer order: action bar and log pane claim the floor first,
        # then the agent list expands into whatever is left.
        self._build_header()
        self._build_action_bar()
        self._build_log_pane()
        self._build_advanced()
        self._build_agent_list()

        self._centre_on_parent(parent)

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr, text="🔌  Wire tokensave into AI agents",
            bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=(
                "Runs `tokensave install` once per selected agent — registering "
                "the MCP server, hooks, and tool permissions. Agents detected on "
                "this machine are pre-checked. You'll see the exact commands and "
                "the files they touch before anything is written. "
                "Restart an agent after wiring it for the new tools to load."
            ),
            bg=C["surface0"], fg=C["text"],
            font=("Segoe UI", 9),
            wraplength=550, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._install_btn = ttk.Button(
            bar, text="🔌  Wire selected agents",
            command=self._on_install,
        )
        self._install_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    def _build_log_pane(self) -> None:
        wrap = tk.LabelFrame(
            self, text="Output",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        wrap.pack(fill=tk.X, side=tk.BOTTOM, padx=18, pady=(4, 4))
        self._log_txt = tk.Text(
            wrap, height=7, font=("Consolas", 8),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._log_txt.pack(fill=tk.X, padx=8, pady=(6, 8))

    def _build_advanced(self) -> None:
        adv = tk.LabelFrame(
            self, text="Advanced",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        adv.pack(fill=tk.X, side=tk.BOTTOM, padx=18, pady=(0, 4))
        ttk.Checkbutton(
            adv,
            text="Use compact wildcard permissions (--wildcard-permissions)",
            variable=self._wildcard_var,
        ).pack(anchor=tk.W, padx=12, pady=(6, 0))
        tk.Label(
            adv,
            text=("    Claude Code only. Grants tokensave via a single "
                  "mcp__tokensave__* entry instead of listing all ~82 tools."),
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8), wraplength=540, justify=tk.LEFT,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            adv,
            text="Also install the global git post-commit hook (--git-hook yes)",
            variable=self._git_hook_var,
        ).pack(anchor=tk.W, padx=12, pady=(6, 0))
        tk.Label(
            adv,
            text=("    Off by default. This hook is GLOBAL — it runs "
                  "`tokensave sync` after every commit in every repo on this "
                  "machine, not just this project."),
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8), wraplength=540, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))

    def _build_agent_list(self) -> None:
        """Detected agents up front; undetected ones behind an expander."""
        wrap = tk.LabelFrame(
            self, text="Agents",
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

        body = tk.Frame(canvas, bg=C["base"])
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        detected = [(a, l) for a, l in _TOKENSAVE_AGENTS
                    if _tokensave_agent_installed(a)]
        missing = [(a, l) for a, l in _TOKENSAVE_AGENTS
                   if not _tokensave_agent_installed(a)]

        if detected:
            tk.Label(body, text="Detected on this machine",
                     bg=C["base"], fg=C["green"],
                     font=("Segoe UI", 8, "bold")).pack(anchor=tk.W,
                                                        padx=10, pady=(6, 2))
            for agent_id, label in detected:
                self._add_agent_row(body, agent_id, label, detected=True)
        else:
            tk.Label(body,
                     text="  No agents auto-detected — use “Show all agents”.",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Segoe UI", 9, "italic")).pack(anchor=tk.W,
                                                          padx=10, pady=8)

        # Expander for everything not detected.
        self._more_frame = tk.Frame(body, bg=C["base"])
        self._more_btn = ttk.Button(
            body,
            text=f"▸  Show all agents  ({len(missing)} more)",
            command=self._toggle_show_all,
        )
        if missing:
            self._more_btn.pack(anchor=tk.W, padx=10, pady=(8, 4))
            tk.Label(self._more_frame,
                     text="Not detected — tick only if you know you use it",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Segoe UI", 8, "bold")).pack(anchor=tk.W,
                                                        padx=10, pady=(4, 2))
            for agent_id, label in missing:
                self._add_agent_row(self._more_frame, agent_id, label,
                                    detected=False)
        # Doctor may have pre-selected an undetected agent — reveal the list
        # so the user can actually see what's ticked.
        if self._preselect & {a for a, _ in missing}:
            self._toggle_show_all()

    def _add_agent_row(self, parent, agent_id: str, label: str,
                       detected: bool) -> None:
        """One checkbox row + its destination path."""
        checked = detected if not self._preselect else agent_id in self._preselect
        var = tk.BooleanVar(value=checked)
        self._agent_vars[agent_id] = var
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=12, pady=1)
        chk = ttk.Checkbutton(row, text=label, variable=var)
        chk.pack(side=tk.LEFT)
        self._agent_chks[agent_id] = chk
        dest = _tokensave_agent_destination_path(agent_id)
        tk.Label(
            row, text=f"→ {dest}",
            bg=C["base"], fg=C["overlay0"] if detected else C["surface1"],
            font=("Consolas", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _toggle_show_all(self) -> None:
        self._show_all = not self._show_all
        if self._show_all:
            self._more_frame.pack(fill=tk.X, after=self._more_btn)
            self._more_btn.configure(text="▾  Hide undetected agents")
        else:
            self._more_frame.pack_forget()
            missing = sum(1 for a, _ in _TOKENSAVE_AGENTS
                          if not _tokensave_agent_installed(a))
            self._more_btn.configure(
                text=f"▸  Show all agents  ({missing} more)")

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        w, h = 700, 620
        try:
            px = parent.winfo_x() + (parent.winfo_width() - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, line: str) -> None:
        try:
            self._log_txt.configure(state=tk.NORMAL)
            self._log_txt.insert(tk.END, line + "\n")
            self._log_txt.see(tk.END)
            self._log_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _log_threadsafe(self, line: str) -> None:
        try:
            self._post(lambda l=line: self._log(l))
        except tk.TclError:
            pass

    # ── Install action ────────────────────────────────────────────────────────

    def _selected_agents(self) -> list:
        """Selected agent ids, in _TOKENSAVE_AGENTS display order."""
        return [a for a, _ in _TOKENSAVE_AGENTS
                if a in self._agent_vars and self._agent_vars[a].get()]

    def _confirm_text(self, selected: list, ts_exe: str) -> str:
        """Human-reviewable summary: every command, every file touched."""
        lines = ["These commands will run:", ""]
        for agent_id in selected:
            argv = build_install_argv(
                ts_exe, agent_id,
                wildcard_permissions=self._wildcard_var.get(),
                git_hook=self._git_hook_var.get())
            lines.append("  " + " ".join(argv[1:]))
        lines += ["", "Files that may be modified:", ""]
        for agent_id in selected:
            lines.append(f"  {_tokensave_agent_destination_path(agent_id)}")
        if "claude" in selected:
            lines.append(f"  {os.path.expanduser('~')}"
                         f"{os.sep}.claude{os.sep}settings.json"
                         "   (hooks + permissions)")
        if self._git_hook_var.get():
            lines += ["", "⚠ A GLOBAL git post-commit hook will be installed."]
        lines += ["", "Proceed?"]
        return "\n".join(lines)

    def _on_install(self) -> None:
        if self._in_flight:
            return
        selected = self._selected_agents()
        if not selected:
            messagebox.showinfo(
                "No agents selected",
                "Pick at least one agent to wire tokensave into.",
                parent=self)
            return
        ts_exe = self._cfg.tokensave_exe or ""
        if not ts_exe or not os.path.isfile(ts_exe):
            messagebox.showerror(
                "tokensave binary not found",
                "The tokensave executable is not configured. Install it "
                "first from Tool Manager.",
                parent=self)
            return

        # Agent-control config is about to change — show it before it lands.
        if not messagebox.askyesno(
                "Confirm agent wiring",
                self._confirm_text(selected, ts_exe),
                parent=self, default="no"):
            return

        self._in_flight = True
        self._install_btn.configure(state=tk.DISABLED, text="🔌  Wiring…")
        wildcard = self._wildcard_var.get()
        git_hook = self._git_hook_var.get()

        def _worker() -> None:
            ok_n, fail_n = 0, 0
            for agent_id in selected:
                argv = build_install_argv(
                    ts_exe, agent_id,
                    wildcard_permissions=wildcard, git_hook=git_hook)
                self._log_threadsafe(f"$ {' '.join(argv[1:])}")
                ok, detail = _run_install(argv)
                if ok:
                    ok_n += 1
                    self._log_threadsafe(f"  ✓ {agent_id} wired.")
                else:
                    fail_n += 1
                    # One agent failing must never abort the others.
                    self._log_threadsafe(f"  ✗ {agent_id}: {detail}")
            self._post(lambda: self._on_install_done(ok_n, fail_n))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_done(self, ok_n: int, fail_n: int) -> None:
        """Main-thread completion callback."""
        self._in_flight = False
        try:
            if not self.winfo_exists():
                return
            self._install_btn.configure(
                state=tk.NORMAL, text="🔌  Wire selected agents")
            self._log(f"— Done: {ok_n} succeeded, {fail_n} failed —")
            if ok_n and not fail_n:
                self._log("Restart the wired agents to load the new tools.")
        except tk.TclError:
            return
        if self._on_done:
            try:
                self._on_done()
            except Exception:
                pass


def _run_install(argv: list) -> tuple:
    """Run one install invocation. Returns (ok, detail).

    Module-level so the retry/timeout handling stays testable and the dialog
    method above reads as just the loop.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=_INSTALL_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_INSTALL_TIMEOUT}s"
    except (FileNotFoundError, OSError) as exc:
        return False, f"could not launch tokensave: {exc}"
    if proc.returncode == 0:
        return True, ""
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
    return False, f"exit {proc.returncode}: {detail[:200] or '(no output)'}"
