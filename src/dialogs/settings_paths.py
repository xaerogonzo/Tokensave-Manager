"""PathsSection — the Paths and Git-tools blocks of the Settings dialog.

Extracted verbatim from dialogs/settings.py (Roadmap-8 god-file split).
Builds, in original visual order: Paths (tokensave exe + upgrade row,
template dir, editor command), Git executable, Claude Code CLI row
(path + install + model dropdown), and the GitHub CLI row.

House pattern: the dialog handle is used for Tk plumbing only
(``after()``, dialog parenting, ``master`` for the host App's upgrade
commands). Cross-section actions (Tool Manager launch) arrive as
injected callbacks. ``save_into(raw)`` is this section's slice of the
Save contract — it returns False (and shows a warning) when the
tokensave exe path doesn't exist, aborting the whole save.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import TYPE_CHECKING

from constants import CREATE_NEW_CONSOLE, C, CREATE_NO_WINDOW
from helpers.detection import _detect_git, _detect_gh, _detect_claude_cli

if TYPE_CHECKING:
    from state import ManagerConfig


class PathsSection:
    """Tokensave/template/editor paths + git, Claude CLI, and gh tooling."""

    def __init__(self, dialog: tk.Toplevel, body: tk.Frame,
                 cfg: "ManagerConfig", open_tool_manager) -> None:
        self._dlg = dialog
        self._cfg = cfg
        self._open_tool_manager = open_tool_manager
        raw = cfg.raw
        self._build_paths_section(body, raw)
        self._build_git_tools_section(body, raw)

    def save_into(self, raw: dict) -> bool:
        """Write this section's fields into raw.

        Returns False (save aborts, dialog stays open) when the tokensave
        exe is set but doesn't exist on disk.
        """
        exe = self._exe_var.get().strip()
        if exe and not os.path.isfile(exe):
            messagebox.showwarning("Not found",
                f"tokensave.exe not found at:\n{exe}", parent=self._dlg)
            return False
        raw["tokensave_exe"] = exe
        raw["template_dir"]  = self._tmpl_var.get().strip()
        raw["editor_cmd"]    = self._editor_var.get().strip() or "code"
        # python_exe is intentionally not exposed in the UI (used by the .bat
        # launcher only); preserve whatever value is already in the config.
        raw["git_exe"]          = self._git_exe_var.get().strip()
        raw["claude_cli_exe"]   = self._claude_cli_var.get().strip()
        raw["claude_cli_model"] = self._var_claude_cli_model.get().strip()
        return True

    # ── Section builders (original visual order) ─────────────────────────

    def _build_paths_section(self, body, raw):
        """Tokensave exe, upgrade row, template dir, editor command."""
        def field_row(label, key, is_file=False, is_dir=False, note=""):
            tk.Label(body, text=label, bg=C["base"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(10, 0))
            row = tk.Frame(body, bg=C["base"])
            row.pack(fill=tk.X, padx=20, pady=(2, 0))
            var = tk.StringVar(value=raw.get(key, ""))
            entry = ttk.Entry(row, textvariable=var, width=52)
            entry.pack(side=tk.LEFT, padx=(0, 6))
            def browse(v=var, f=is_file, d=is_dir):
                if f:
                    p = filedialog.askopenfilename(
                        title=f"Select {label}", filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                        initialfile=v.get(), parent=self._dlg)
                elif d:
                    p = filedialog.askdirectory(title=f"Select {label}", parent=self._dlg)
                else:
                    return
                if p:
                    v.set(p)
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT)
            if note:
                tk.Label(row, text=note, bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 0))
            return var

        self._exe_var = field_row("tokensave.exe  —  path to the tokensave binary",
                                  "tokensave_exe", is_file=True)

        # Upgrade tokensave row — ALWAYS shown (idempotent; reports "already
        # on latest" when no update is available). Promoted style when the app
        # has cached an available version from an "Update available" sync line.
        upgrade_row = tk.Frame(body, bg=C["base"])
        upgrade_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        host = self._dlg.master
        cur_ver = getattr(host, "_tokensave_current_version", None)
        new_ver = getattr(host, "_tokensave_available_version", None)
        cur_str = f"v{cur_ver}" if cur_ver else "version unknown"
        if new_ver:
            btn_label = f"🔄  Upgrade tokensave to v{new_ver}"
            btn_style = "Primary.TButton"
            hint = (f"  Current: {cur_str} → available: v{new_ver}.  "
                    "Replaces the binary; restart Claude after upgrading.")
            hint_fg = C["green"]
        else:
            btn_label = "🔄  Upgrade tokensave"
            btn_style = "TButton"
            hint = (f"  Current: {cur_str}.  Runs `tokensave upgrade` — "
                    "no-op if you're already on the latest release. "
                    "Restart Claude after a successful upgrade.")
            hint_fg = C["overlay0"]
        ttk.Button(upgrade_row, text=btn_label, style=btn_style,
                   command=host.cmd_upgrade_tokensave).pack(side=tk.LEFT)
        ttk.Button(upgrade_row, text="🔍  Check integration",
                   command=host.cmd_integration_check).pack(side=tk.LEFT, padx=(8, 0))
        # v4.8: shortcut into the new Tool Manager dialog
        ttk.Button(upgrade_row, text="🛠️  Open Tool Manager…",
                   command=self._open_tool_manager).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(body, text=hint, bg=C["base"], fg=hint_fg,
                 font=("Segoe UI", 8), justify=tk.LEFT,
                 anchor=tk.W).pack(fill=tk.X, padx=20, pady=(0, 4))

        self._tmpl_var = field_row(
            "Template directory  —  folder containing claude-md-template.md and project-baseline.md",
            "template_dir", is_dir=True, note="(leave blank to auto-detect)")
        self._editor_var = field_row(
            "Editor command  —  launched by 'Open in Editor' (e.g. code, code --new-window, notepad)",
            "editor_cmd", note="(flags supported)")

    def _build_git_tools_section(self, body, raw):
        """Git executable path + Claude Code CLI + GitHub CLI install/detect."""
        # ── Git executable ────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(body, text="Git executable  —  path to git.exe",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        git_row = tk.Frame(body, bg=C["base"])
        git_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._git_exe_var = tk.StringVar(value=raw.get("git_exe", ""))
        ttk.Entry(git_row, textvariable=self._git_exe_var, width=44).pack(side=tk.LEFT, padx=(0, 6))
        def _browse_git():
            p = filedialog.askopenfilename(
                title="Select git.exe",
                filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                initialdir=r"C:\Program Files\Git\cmd", parent=self._dlg)
            if p:
                self._git_exe_var.set(p)
                self._verify_git(p)
        def _autodetect_git():
            found = _detect_git()
            self._git_exe_var.set(found)
            self._verify_git(found)
        ttk.Button(git_row, text="Browse…", command=_browse_git).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(git_row, text="Auto-detect", command=_autodetect_git).pack(side=tk.LEFT, padx=(0, 6))
        self._git_status_lbl = tk.Label(git_row, text="", bg=C["base"],
                                        font=("Segoe UI", 8), fg=C["overlay0"])
        self._git_status_lbl.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(body, text="  Leave blank to auto-detect from PATH or common install locations.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        # Verify against the saved-or-live git_exe.
        self._dlg.after(100, lambda: self._verify_git(raw.get("git_exe") or self._cfg.git_exe))

        self._build_claude_cli_row(body, raw)
        self._build_github_cli_row(body)

    def _build_claude_cli_row(self, body, raw):
        """Claude Code CLI path + Install button sub-section."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(body,
                 text="Claude Code CLI  —  path to claude.cmd (npm install -g @anthropic-ai/claude-code)",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        cli_row = tk.Frame(body, bg=C["base"])
        cli_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._claude_cli_var = tk.StringVar(value=raw.get("claude_cli_exe", ""))
        ttk.Entry(cli_row, textvariable=self._claude_cli_var, width=44).pack(
            side=tk.LEFT, padx=(0, 6))

        def _browse_claude():
            p = filedialog.askopenfilename(
                title="Select claude.cmd or claude",
                filetypes=[("All files", "*.*")],
                initialdir=os.path.expandvars(r"%APPDATA%\npm"),
                parent=self._dlg)
            if p:
                self._claude_cli_var.set(p)

        def _autodetect_claude():
            found = _detect_claude_cli()
            if found:
                self._claude_cli_var.set(found)
                self._claude_cli_status.configure(text=f"Found: {found}", fg=C["green"])
                self._claude_install_btn.configure(state=tk.DISABLED)
            else:
                self._claude_cli_status.configure(text="Not found.", fg=C["red"])
                self._claude_install_btn.configure(state=tk.NORMAL)

        def _install_claude():
            # -NoExit keeps the window open so the user can see output and follow prompts.
            try:
                subprocess.Popen(
                    ["powershell", "-NoExit", "-Command",
                     "npm install -g @anthropic-ai/claude-code"],
                    creationflags=CREATE_NEW_CONSOLE,
                )
                self._claude_cli_status.configure(
                    text="PowerShell opened — run it, then click Auto-detect when done.",
                    fg=C["peach"])
            except Exception as ex:
                self._claude_cli_status.configure(
                    text=f"Could not open PowerShell: {ex}", fg=C["red"])

        ttk.Button(cli_row, text="Browse…",     command=_browse_claude).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cli_row, text="Auto-detect", command=_autodetect_claude).pack(side=tk.LEFT, padx=(0, 6))
        self._claude_install_btn = ttk.Button(cli_row, text="Install…", command=_install_claude)
        self._claude_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._claude_cli_status = tk.Label(cli_row, text="", bg=C["base"],
                                           font=("Segoe UI", 8), fg=C["overlay0"])
        self._claude_cli_status.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(body,
                 text="  If auto-detect fails, paste the full path (e.g. %APPDATA%\\npm\\claude.cmd).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        if raw.get("claude_cli_exe"):
            self._claude_install_btn.configure(state=tk.DISABLED)

        # Model dropdown — applies to manager-spawned `claude --print` calls only.
        # Defensive: cfg.claude_cli_model already guards against None; do the
        # same here in case the raw dict has the key with a None value.
        cur_model = raw.get("claude_cli_model")
        if cur_model is None:
            cur_model = "claude-haiku-4-5-20251001"
        self._var_claude_cli_model = tk.StringVar(value=cur_model)
        model_row = tk.Frame(body, bg=C["base"])
        model_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        tk.Label(model_row, text="Model:", width=8, anchor=tk.W,
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
        ttk.Combobox(model_row, textvariable=self._var_claude_cli_model,
            values=[
                "claude-haiku-4-5-20251001",
                "claude-sonnet-4-6",
                "claude-opus-4-7",
                "",
            ],
            state="normal", width=32).pack(side=tk.LEFT)
        tk.Label(body,
            text="  Model used by the manager's automated calls (pre-commit review, commit-message\n"
                 "  Suggest, Draft PR via CLI). Haiku = fast (3–5 s) and cheap. Opus = slow but deeper.\n"
                 "  Empty = use whatever ~/.claude/settings.json defaults to. Does NOT affect interactive\n"
                 "  'claude' sessions you launch from the terminal or Reference tab.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
            anchor=tk.W, padx=20, pady=(2, 0))

    def _build_github_cli_row(self, body):
        """GitHub CLI (gh) detect + install sub-section."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(body, text="GitHub CLI (gh)  —  enables 'Open PR on GitHub' and release creation",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        gh_row = tk.Frame(body, bg=C["base"])
        gh_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._gh_status_lbl = tk.Label(gh_row, text="Checking…",
                                       bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8))
        self._gh_status_lbl.pack(side=tk.LEFT, padx=(0, 12))

        def _check_gh_status():
            found = _detect_gh()
            if found:
                self._gh_status_lbl.config(text=f"✓  {found}", fg=C["green"])
                self._gh_install_btn.configure(state=tk.DISABLED)
            else:
                self._gh_status_lbl.config(text="✗  not installed", fg=C["red"])
                self._gh_install_btn.configure(state=tk.NORMAL)

        def _install_gh():
            self._gh_status_lbl.config(
                text="Installing…  (this may take a minute, a UAC prompt may appear)",
                fg=C["peach"])
            self._gh_install_btn.configure(state=tk.DISABLED)
            def worker():
                try:
                    result = subprocess.run(
                        ["winget", "install", "--id", "GitHub.cli",
                         "--silent", "--accept-package-agreements",
                         "--accept-source-agreements"],
                        capture_output=True, text=True, timeout=180,
                        creationflags=CREATE_NO_WINDOW)
                    if result.returncode == 0:
                        self._dlg.after(0, lambda: self._gh_status_lbl.config(
                            text="✓  Installed!  Restart TokenSave Manager to use gh features.",
                            fg=C["green"]))
                    else:
                        err = (result.stdout + result.stderr).strip()[-120:]
                        self._dlg.after(0, lambda: self._gh_status_lbl.config(
                            text=f"✗  Install failed (code {result.returncode}): {err}",
                            fg=C["red"]))
                        self._dlg.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
                except Exception as ex:
                    err_msg = str(ex)
                    self._dlg.after(0, lambda m=err_msg: self._gh_status_lbl.config(
                        text=f"✗  Error: {m}", fg=C["red"]))
                    self._dlg.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
            threading.Thread(target=worker, daemon=True).start()

        self._gh_install_btn = ttk.Button(gh_row, text="Install via winget", command=_install_gh)
        self._gh_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(gh_row, text="Check again", command=_check_gh_status).pack(side=tk.LEFT)
        tk.Label(body,
                 text="  Once installed, use the Git tab's '🔗 Open PR' button to create pull requests on GitHub.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        self._dlg.after(150, _check_gh_status)

    def _verify_git(self, exe_path: str):
        """Run 'git --version' with the given path and update the status label."""
        if not exe_path:
            self._git_status_lbl.config(text="(will auto-detect on save)", fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW)
            version = result.stdout.strip() or result.stderr.strip()
            if result.returncode == 0:
                self._git_status_lbl.config(text=f"✓  {version}", fg=C["green"])
            else:
                self._git_status_lbl.config(text="✗  not found", fg=C["red"])
        except Exception:
            self._git_status_lbl.config(text="✗  not found", fg=C["red"])
