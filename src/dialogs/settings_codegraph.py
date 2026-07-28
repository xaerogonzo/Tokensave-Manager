"""CodegraphSection — the CodeGraph block of the Settings dialog.

Extracted verbatim from dialogs/settings.py (Roadmap-8 god-file split).
Owns the codegraph executable path entry, npm install worker, MCP
configure/uninstall workers, and the restart-required info bar.

House pattern (Pre-Roadmap-8 extractions): the section receives the
dialog handle for Tk plumbing only — ``after()`` scheduling, messagebox
``parent=``, and child-dialog parenting. No back-references to dialog
attributes; cross-section actions (Tool Manager launch) arrive as
injected callbacks. ``save_into(raw)`` is the section's slice of the
Save contract.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW
from helpers.detection import _detect_codegraph, _detect_npm

if TYPE_CHECKING:
    from state import ManagerConfig


class CodegraphSection:
    """CodeGraph executable path, install via npm, status check, MCP wiring."""

    def __init__(self, dialog: tk.Toplevel, body: tk.Frame,
                 cfg: "ManagerConfig", open_tool_manager) -> None:
        self._dlg = dialog
        self._cfg = cfg
        self._open_tool_manager = open_tool_manager
        self._build(body, cfg.raw)

    def save_into(self, raw: dict) -> bool:
        """Write this section's fields into raw. Always succeeds."""
        raw["codegraph_exe"] = self._cg_exe_var.get().strip()
        return True

    def focus_path_entry(self) -> None:
        """Pull the CodeGraph section into view + focus its path entry.

        All sections are always rendered (the canvas wrapper scrolls);
        focus_set on the path entry is enough — no yview math needed.
        Wrapped in try/except so any future layout change fails
        gracefully rather than crashing the install-nudge flow.
        """
        try:
            self._cg_exe_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    # ── Section construction ─────────────────────────────────────────────

    def _build(self, body, raw):
        """CodeGraph executable path, install via npm, status check."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._cg_section = tk.Frame(body, bg=C["base"])
        self._cg_section.pack(fill=tk.X)
        cg_header = tk.Frame(self._cg_section, bg=C["base"])
        cg_header.pack(fill=tk.X, padx=20)
        tk.Label(cg_header,
                 text="CodeGraph (codegraph)  —  optional alternative code-graph tool",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        # v4.8: discovery shortcut into the new Tool Manager dialog
        ttk.Button(cg_header, text="🛠️  Open Tool Manager…",
                   command=self._open_tool_manager).pack(side=tk.RIGHT)

        # Path entry + Browse/Auto-detect buttons
        cg_path_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_path_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._cg_exe_var = tk.StringVar(value=raw.get("codegraph_exe", ""))
        self._cg_exe_entry = ttk.Entry(cg_path_row, textvariable=self._cg_exe_var, width=44)
        self._cg_exe_entry.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_path_row, text="Browse…",      command=self._cg_browse).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_path_row, text="Auto-detect",  command=self._cg_autodetect).pack(side=tk.LEFT, padx=(0, 6))

        # Status label
        cg_install_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_install_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        self._cg_status_lbl = tk.Label(cg_install_row, text="Checking…",
                                       bg=C["base"], fg=C["overlay0"],
                                       font=("Segoe UI", 8), justify=tk.LEFT,
                                       wraplength=420, anchor=tk.W)
        self._cg_status_lbl.pack(side=tk.LEFT, padx=(0, 12), fill=tk.X, expand=True)

        # Install / Check-again buttons
        cg_btn_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_btn_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        # v4.7: relabeled — disambiguates from the new MCP-config buttons below
        self._cg_install_btn = ttk.Button(cg_btn_row, text="Install binary (npm)", command=self._cg_install)
        self._cg_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_btn_row, text="Check again", command=self._cg_check_status).pack(side=tk.LEFT, padx=(0, 6))
        if not _detect_npm():
            self._cg_install_btn.configure(state=tk.DISABLED)

        # v4.7: MCP-config buttons.  Step 2 of the codegraph setup flow.
        # Step 1 is installing the binary (above); Step 2 is wiring it as
        # an MCP server for whichever AI agents the user uses.
        cg_mcp_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_mcp_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        self._cg_mcp_auto_btn = ttk.Button(
            cg_mcp_row, text="🔌  Configure MCP (auto)",
            command=self._cg_mcp_configure_auto,
        )
        self._cg_mcp_auto_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cg_mcp_picker_btn = ttk.Button(
            cg_mcp_row, text="⚙  Configure MCP — pick agents…",
            command=self._cg_mcp_open_picker,
        )
        self._cg_mcp_picker_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cg_mcp_uninstall_btn = ttk.Button(
            cg_mcp_row, text="🧹  Uninstall MCP",
            command=self._cg_mcp_uninstall,
        )
        self._cg_mcp_uninstall_btn.pack(side=tk.LEFT, padx=(0, 6))

        # Non-modal restart-required info bar — hidden by default, shown
        # after a successful Configure MCP (auto) run.
        self._cg_mcp_restart_bar = tk.Label(
            self._cg_section,
            text="",  # populated when shown
            bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 8),
            wraplength=520, justify=tk.LEFT,
            padx=10, pady=6,
        )
        # NOT packed yet — _cg_show_restart_bar packs it on demand.

        tk.Label(self._cg_section,
                 text="  Step 1: install the binary.  Step 2: wire it as an MCP server for the agents you use.\n"
                      "  Per-project actions live in the right-click menu (🧠 CodeGraph …).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(4, 0))
        self._dlg.after(200, self._cg_check_status)

    # ── CodeGraph section helpers ────────────────────────────────────────

    def _cg_browse(self):
        """Browse for the codegraph executable."""
        p = filedialog.askopenfilename(
            title="Select codegraph executable",
            filetypes=[("Executable", "*.cmd;*.exe;*.bat"), ("All", "*.*")],
            initialdir=os.path.expandvars(r"%APPDATA%\npm"), parent=self._dlg)
        if p:
            self._cg_exe_var.set(p)
            self._verify_codegraph(p)

    def _cg_autodetect(self):
        """Auto-detect the codegraph executable from PATH."""
        found = _detect_codegraph()
        if found:
            self._cg_exe_var.set(found)
            self._verify_codegraph(found)
        else:
            self._cg_status_lbl.config(text="✗  not installed", fg=C["red"])

    def _cg_check_status(self):
        """Detect codegraph on PATH and update the status label + install button.

        v4.7: also reports MCP wiring state for Claude Code via
        ``helpers/mcp.py:_claude_code_mcp_has_codegraph``.  The status label
        renders two lines when the binary is present — one for the binary,
        one for the MCP wiring.  Buttons enable/disable based on both
        states (e.g. "Uninstall MCP" only enables when MCP is currently
        wired).
        """
        found = _detect_codegraph()
        if found:
            # v4.7: dual-line status — binary + MCP wiring
            try:
                from helpers.mcp import _claude_code_mcp_has_codegraph
                mcp_wired, mcp_key = _claude_code_mcp_has_codegraph()
            except Exception:
                mcp_wired, mcp_key = False, ""
            mcp_line = (
                f"✓  MCP wired for Claude Code (mcpServers.{mcp_key})"
                if mcp_wired else
                "✗  MCP not wired for Claude Code"
            )
            mcp_colour = C["green"] if mcp_wired else C["red"]
            # Use a multi-line label: foreground colour reflects the
            # OVERALL state (green only when both binary + MCP are good).
            overall = C["green"] if mcp_wired else C["yellow"]
            self._cg_status_lbl.config(
                text=f"✓  {found}\n{mcp_line}",
                fg=overall)
            self._cg_install_btn.configure(state=tk.DISABLED)
            # MCP buttons gated on binary presence + per-button state
            self._cg_mcp_auto_btn.configure(state=tk.NORMAL)
            self._cg_mcp_picker_btn.configure(state=tk.NORMAL)
            self._cg_mcp_uninstall_btn.configure(
                state=tk.NORMAL if mcp_wired else tk.DISABLED)
            # Mute the colour cue: the per-line text now carries the signal
            _ = mcp_colour
            if not self._cg_exe_var.get():
                self._cg_exe_var.set(found)
        else:
            self._cg_status_lbl.config(text="✗  not installed", fg=C["red"])
            # No binary → all MCP buttons disabled
            self._cg_mcp_auto_btn.configure(state=tk.DISABLED)
            self._cg_mcp_picker_btn.configure(state=tk.DISABLED)
            self._cg_mcp_uninstall_btn.configure(state=tk.DISABLED)
            state = tk.NORMAL if _detect_npm() else tk.DISABLED
            self._cg_install_btn.configure(state=state)

    def _cg_on_install_done(self, ok: bool, msg: str):
        """Main-thread callback: update UI after the npm install worker finishes."""
        if ok:
            path = _detect_codegraph()
            if path:
                self._cg_exe_var.set(path)
                self._cg_status_lbl.config(text=f"✓  Installed — {path}", fg=C["green"])
            else:
                self._cg_status_lbl.config(
                    text="✓  Installed.  Click 'Check again' to confirm.", fg=C["green"])
            self._cg_install_btn.configure(state=tk.NORMAL)
        else:
            self._cg_status_lbl.config(text=msg, fg=C["red"])
            self._cg_install_btn.configure(state=tk.NORMAL)
            if "\n" in msg:
                messagebox.showerror("CodeGraph install failed", msg, parent=self._dlg)

    def _cg_install(self):
        """Install codegraph via npm in a background thread."""
        npm = _detect_npm()
        if not npm:
            self._cg_status_lbl.config(
                text="✗  npm not found — install Node.js 18+ first (https://nodejs.org)",
                fg=C["red"])
            return
        self._cg_install_btn.configure(state=tk.DISABLED)
        self._cg_status_lbl.config(
            text="Installing…  (this may take a couple of minutes)", fg=C["yellow"])

        def worker():
            try:
                result = subprocess.run(
                    [npm, "install", "-g", "@colbymchenry/codegraph"],
                    capture_output=True, text=True, timeout=300,
                    creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                self._dlg.after(0, self._cg_on_install_done, False, "Install timed out after 5 minutes.")
                return
            except FileNotFoundError as e:
                self._dlg.after(0, self._cg_on_install_done, False, f"npm not found: {e}")
                return
            if result.returncode == 0:
                self._dlg.after(0, self._cg_on_install_done, True, "✓ Installed successfully.")
            else:
                err_text = (result.stderr or result.stdout or "").strip()
                hint = ""
                if "EPERM" in err_text or "EACCES" in err_text:
                    hint = ("\n\nThis usually happens when Node.js was "
                            "installed system-wide. Either run TokenSave "
                            "Manager as administrator OR reinstall "
                            "Node.js as a per-user install (the Node "
                            "installer offers this option).")
                tail = "\n".join(err_text.splitlines()[-8:]) or "(no output)"
                self._dlg.after(0, self._cg_on_install_done, False,
                                f"✗  Install failed (exit {result.returncode}):\n\n{tail}{hint}")

        threading.Thread(target=worker, daemon=True).start()

    # ── v4.7: CodeGraph MCP configure / uninstall handlers ────────────────

    def _cg_show_restart_bar(self, message: str) -> None:
        """Show the non-modal restart-required info bar after a successful
        Configure MCP run.  Re-shows on every install (idempotent pack)."""
        self._cg_mcp_restart_bar.configure(text=message)
        try:
            # Pack just below the MCP button row if not already packed.
            # Catches the case where it was already shown earlier in the session.
            info = self._cg_mcp_restart_bar.pack_info()
            if not info:  # never packed
                raise tk.TclError("not packed")
        except tk.TclError:
            self._cg_mcp_restart_bar.pack(
                fill=tk.X, padx=20, pady=(6, 0))

    def _cg_mcp_configure_auto(self) -> None:
        """🔌 Configure MCP (auto) — runs `codegraph install --yes`.

        Auto-detects installed agents and wires them all.  Logs progress to
        the status label (subprocess output is short).  On success, surfaces
        a non-modal restart-required info bar; on failure, the status label
        shows the error tail.
        """
        exe = self._cg_exe_var.get().strip() or _detect_codegraph()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(
                "CodeGraph binary not found",
                "The codegraph executable is not configured. "
                "Install it first via 'Install binary (npm)'.",
                parent=self._dlg)
            return
        self._cg_mcp_auto_btn.configure(state=tk.DISABLED)
        self._cg_mcp_picker_btn.configure(state=tk.DISABLED)
        self._cg_mcp_uninstall_btn.configure(state=tk.DISABLED)
        self._cg_status_lbl.config(
            text="Configuring MCP…  (codegraph install --yes)",
            fg=C["yellow"])

        def worker():
            try:
                result = subprocess.run(
                    [exe, "install", "--yes"],
                    capture_output=True, text=True, timeout=120,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                self._dlg.after(0, self._cg_mcp_done, False,
                                "Configure MCP timed out after 120 s.")
                return
            except (FileNotFoundError, OSError) as e:
                self._dlg.after(0, self._cg_mcp_done, False,
                                f"Could not launch codegraph: {e}")
                return
            ok = result.returncode == 0
            log = (result.stdout or "") + (result.stderr or "")
            self._dlg.after(0, self._cg_mcp_done, ok, log)

        threading.Thread(target=worker, daemon=True).start()

    def _cg_mcp_done(self, ok: bool, log: str) -> None:
        """Main-thread callback for both Configure MCP (auto) and Uninstall MCP."""
        # Refresh status row — this re-enables buttons based on current state.
        self._cg_check_status()
        if ok:
            tail = "\n".join((log or "").splitlines()[-6:])
            self._cg_show_restart_bar(
                "✓  MCP server wired. Restart Claude Code (and any other "
                "agents you wired) for the new codegraph_* tools to appear "
                "in their sessions."
            )
            # Brief status banner in the status label too — fades back to
            # the regular detail line on the next Check again click.
            self._cg_status_lbl.config(
                text=self._cg_status_lbl.cget("text") + "\n(just updated)",
                fg=C["green"])
            _ = tail
        else:
            tail = "\n".join((log or "").splitlines()[-8:]) or "(no output)"
            messagebox.showerror(
                "CodeGraph MCP — install failed",
                f"codegraph install failed:\n\n{tail}",
                parent=self._dlg)

    def _cg_mcp_open_picker(self) -> None:
        """⚙ Configure MCP — pick agents… — opens the picker dialog."""
        exe = self._cg_exe_var.get().strip() or _detect_codegraph()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(
                "CodeGraph binary not found",
                "The codegraph executable is not configured. "
                "Install it first via 'Install binary (npm)'.",
                parent=self._dlg)
            return
        # Lazy import — avoids any module-load cycle and keeps the
        # picker out of the main import graph until first use.
        from dialogs.codegraph_mcp_picker import CodegraphMCPPickerDialog
        CodegraphMCPPickerDialog(self._dlg, self._cfg,
                                 on_done=self._cg_check_status)

    def _cg_mcp_uninstall(self) -> None:
        """🧹 Uninstall MCP — strip codegraph from all wired agents."""
        exe = self._cg_exe_var.get().strip() or _detect_codegraph()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror(
                "CodeGraph binary not found",
                "The codegraph executable is not configured.",
                parent=self._dlg)
            return
        if not messagebox.askyesno(
                "Uninstall CodeGraph from AI agents?",
                "Remove CodeGraph from your AI agents' MCP servers? "
                "They'll lose access to codegraph_* tools until you "
                "re-configure.\n\n"
                "This does NOT delete the binary or any project "
                "indexes — only the MCP registrations in Claude Code "
                "(and any other agents that have it wired).",
                parent=self._dlg, default="no"):
            return
        self._cg_mcp_auto_btn.configure(state=tk.DISABLED)
        self._cg_mcp_picker_btn.configure(state=tk.DISABLED)
        self._cg_mcp_uninstall_btn.configure(state=tk.DISABLED)
        self._cg_status_lbl.config(
            text="Uninstalling MCP…  (codegraph uninstall)",
            fg=C["yellow"])

        def worker():
            try:
                result = subprocess.run(
                    [exe, "uninstall"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                self._dlg.after(0, self._cg_mcp_done, False,
                                "codegraph uninstall timed out after 60 s.")
                return
            except (FileNotFoundError, OSError) as e:
                self._dlg.after(0, self._cg_mcp_done, False,
                                f"Could not launch codegraph: {e}")
                return
            ok = result.returncode == 0
            log = (result.stdout or "") + (result.stderr or "")
            self._dlg.after(0, self._cg_mcp_done, ok, log)

        threading.Thread(target=worker, daemon=True).start()

    def _verify_codegraph(self, exe_path: str):
        """Run 'codegraph --version' with the given path; update status label."""
        if not exe_path:
            self._cg_status_lbl.config(text="(will auto-detect on save)",
                                       fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW)
            version = (result.stdout or result.stderr).strip()
            if result.returncode == 0:
                self._cg_status_lbl.config(text=f"✓  {version or 'OK'}",
                                           fg=C["green"])
                self._cg_install_btn.configure(state=tk.DISABLED)
            else:
                self._cg_status_lbl.config(text="✗  not found at that path",
                                           fg=C["red"])
        except Exception:
            self._cg_status_lbl.config(text="✗  not found at that path",
                                       fg=C["red"])
