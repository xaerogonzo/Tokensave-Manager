"""SettingsDialog — edit manager-config.json through the GUI.

Six sections in a scrollable canvas:
  1. Paths            — tokensave_exe, template_dir, editor_cmd
  2. Git tools        — git_exe + GitHub CLI (gh) install/detect
  3. CodeGraph        — codegraph executable path + npm install
  4. Search roots     — Treeview of (label, path) categories
  5. Behavior         — auto-commit toggle + MCP integration + Ollama
  6. AI commit msgs   — provider/model/key/preset/options

The Save handler is the canonical cfg-mutation path. It mutates
`self._cfg.raw[key]` in place, calls the existing `save_fn` callback
(which persists to disk + writes the on-saved hook for the legacy
global rebind), then fires `callback()` so the App can also
`_state.refresh_derived()` and re-bind any module-level globals that
still exist during the Phase C transition window.

Cross-dialog deps (lazy-imported per plan Rule 6 to avoid any future
module-load cycle):
  • MCPConfigDialog (button: 🔌 Manage MCP wiring…)
  • OllamaModelManagerDialog (button: 🦙 Manage Ollama Models…)

Reads `self._cfg.git_exe` at execution time (Rule 3 — no snapshot
caches). The `cfg.get(...)` and `cfg[...] = ...` patterns from the
original become `cfg.raw.get(...)` and `cfg.raw[...] = ...` here.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW
from helpers.detection import (
    _detect_git, _detect_gh, _detect_npm, _detect_codegraph,
    _root_path, _root_label,
)
from helpers.mcp import _MCP_CONFIGS, _classify_mcp_entry

if TYPE_CHECKING:
    from state import ManagerConfig


def _probe_loaded_model(base_url: str) -> str:
    """Query the /v1/models endpoint and return the first non-embedding model id.

    Used by the AI-preset buttons to auto-fill the Model field when the local
    server is reachable. Returns "" on any network or parse failure.
    """
    import urllib.request, urllib.error, json as _json
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, _json.JSONDecodeError):
        return ""
    for m in (data.get("data") or []):
        mid = m.get("id", "")
        lid = mid.lower()
        if mid and "embed" not in lid and "rerank" not in lid and "whisper" not in lid:
            return mid
    return ""


class SettingsDialog(tk.Toplevel):
    """Edit manager-config.json through the GUI."""

    def __init__(self, parent, cfg: "ManagerConfig", save_fn, callback,
                 startup_note: str = ""):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 500)
        self.geometry("760x700")
        self.grab_set()
        self.transient(parent)
        self._cfg = cfg
        self._save_fn = save_fn
        self._callback = callback

        # cfg.raw is a live dict — passed to per-section builders so
        # existing `raw.get("x")` patterns work unchanged. Settings Save
        # mutates raw in place and calls save_fn(raw) which persists to disk.
        raw = cfg.raw

        # ── Scrollable content area ───────────────────────────────────────
        # Save/Cancel buttons stay anchored at the bottom (packed on `self`,
        # NOT on the scrollable body). All section content goes on `body`.
        _scroll_wrap = tk.Frame(self, bg=C["base"])
        _scroll_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        _canvas = tk.Canvas(_scroll_wrap, bg=C["base"], highlightthickness=0, bd=0)
        _vsb = ttk.Scrollbar(_scroll_wrap, orient="vertical", command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(_canvas, bg=C["base"])
        _body_window = _canvas.create_window((0, 0), window=body, anchor="nw")
        def _on_body_configure(event):
            _canvas.configure(scrollregion=_canvas.bbox("all"))
        def _on_canvas_configure(event):
            _canvas.itemconfigure(_body_window, width=event.width)
        body.bind("<Configure>", _on_body_configure)
        _canvas.bind("<Configure>", _on_canvas_configure)
        def _on_mousewheel(event):
            _canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        _canvas.bind("<MouseWheel>", _on_mousewheel)
        body.bind("<MouseWheel>", _on_mousewheel)

        if startup_note:
            tk.Label(body, text=startup_note,
                     bg=C["red"], fg=C["mantle"],
                     font=("Segoe UI", 9, "bold"),
                     justify=tk.LEFT, padx=14, pady=8,
                     wraplength=440).pack(fill=tk.X, pady=(0, 4))

        self._build_paths_section(body, raw)
        self._build_git_tools_section(body, raw)
        self._build_codegraph_section(body, raw)
        self._build_roots_section(body, raw)
        self._build_behavior_section(body, raw)
        self._build_ai_section(body, raw)

        # ── Save/Cancel — anchored outside the scroll area ────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(8, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

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
                        initialfile=v.get(), parent=self)
                elif d:
                    p = filedialog.askdirectory(title=f"Select {label}", parent=self)
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
        host = self.master
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
        """Git executable path + GitHub CLI install/detect."""
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
                initialdir=r"C:\Program Files\Git\cmd", parent=self)
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
        # Verify against the saved-or-live git_exe — self._cfg.git_exe handles
        # the "explicit path or auto-detect" fallback in one property read.
        self.after(100, lambda: self._verify_git(raw.get("git_exe") or self._cfg.git_exe))

        # ── GitHub CLI (gh) ───────────────────────────────────────────────
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
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text="✓  Installed!  Restart TokenSave Manager to use gh features.",
                            fg=C["green"]))
                    else:
                        err = (result.stdout + result.stderr).strip()[-120:]
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text=f"✗  Install failed (code {result.returncode}): {err}",
                            fg=C["red"]))
                        self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
                except Exception as ex:
                    self.after(0, lambda: self._gh_status_lbl.config(
                        text=f"✗  Error: {ex}", fg=C["red"]))
                    self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
            threading.Thread(target=worker, daemon=True).start()

        self._gh_install_btn = ttk.Button(gh_row, text="Install via winget", command=_install_gh)
        self._gh_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(gh_row, text="Check again", command=_check_gh_status).pack(side=tk.LEFT)
        tk.Label(body,
                 text="  Once installed, use the Git tab's '🔗 Open PR' button to create pull requests on GitHub.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        self.after(150, _check_gh_status)

    def _build_codegraph_section(self, body, raw):
        """CodeGraph executable path, install via npm, status check."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._cg_section = tk.Frame(body, bg=C["base"])
        self._cg_section.pack(fill=tk.X)
        tk.Label(self._cg_section,
                 text="CodeGraph (codegraph)  —  optional alternative code-graph tool",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)

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
        self._cg_install_btn = ttk.Button(cg_btn_row, text="Install via npm", command=self._cg_install)
        self._cg_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_btn_row, text="Check again", command=self._cg_check_status).pack(side=tk.LEFT, padx=(0, 6))
        if not _detect_npm():
            self._cg_install_btn.configure(state=tk.DISABLED)

        tk.Label(self._cg_section,
                 text="  npm install -g @colbymchenry/codegraph  —  requires Node.js 18+ on PATH.\n"
                      "  Per-project actions live in the right-click menu (🧠 CodeGraph …).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(4, 0))
        self.after(200, self._cg_check_status)

    # ── CodeGraph section helpers ────────────────────────────────────────────

    def _cg_browse(self):
        """Browse for the codegraph executable."""
        p = filedialog.askopenfilename(
            title="Select codegraph executable",
            filetypes=[("Executable", "*.cmd;*.exe;*.bat"), ("All", "*.*")],
            initialdir=os.path.expandvars(r"%APPDATA%\npm"), parent=self)
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
        """Detect codegraph on PATH and update the status label + install button."""
        found = _detect_codegraph()
        if found:
            self._cg_status_lbl.config(text=f"✓  {found}", fg=C["green"])
            self._cg_install_btn.configure(state=tk.DISABLED)
            if not self._cg_exe_var.get():
                self._cg_exe_var.set(found)
        else:
            self._cg_status_lbl.config(text="✗  not installed", fg=C["red"])
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
                messagebox.showerror("CodeGraph install failed", msg, parent=self)

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
                self.after(0, self._cg_on_install_done, False, "Install timed out after 5 minutes.")
                return
            except FileNotFoundError as e:
                self.after(0, self._cg_on_install_done, False, f"npm not found: {e}")
                return
            if result.returncode == 0:
                self.after(0, self._cg_on_install_done, True, "✓ Installed successfully.")
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
                self.after(0, self._cg_on_install_done, False,
                           f"✗  Install failed (exit {result.returncode}):\n\n{tail}{hint}")

        threading.Thread(target=worker, daemon=True).start()

    def _build_roots_section(self, body, raw):
        """Search roots two-column Treeview (label + path)."""
        tk.Label(body,
                 text="Search roots  —  each root's label becomes a category in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))
        roots_frame = tk.Frame(body, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))
        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._roots_tv = ttk.Treeview(tv_wrap, columns=("label", "path"),
                                      show="headings", height=5, selectmode="browse")
        self._roots_tv.heading("label", text="Label")
        self._roots_tv.heading("path",  text="Path")
        self._roots_tv.column("label", width=130, stretch=False)
        self._roots_tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical", command=self._roots_tv.yview)
        self._roots_tv.configure(yscrollcommand=roots_vsb.set)
        self._roots_tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        for r in raw.get("search_roots", []):
            self._roots_tv.insert("", tk.END, values=(_root_label(r), _root_path(r)))
        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",      command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label", command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",     command=self._remove_root).pack(fill=tk.X)

    def _build_behavior_section(self, body, raw):
        """Auto-commit toggle, MCP integration status, Ollama shortcut."""
        # ── Auto-commit ───────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._var_autocommit = tk.BooleanVar(value=bool(raw.get("auto_commit_after_sync", False)))
        tk.Checkbutton(body,
            text="Auto-commit after sync  (git add -A + git commit)",
            variable=self._var_autocommit,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
            text="  Only fires when the project is a git repo and the working tree has changes.\n"
                 "  Commit message: \"chore: tokensave sync\"  (or AI-generated if enabled below)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── MCP integration ───────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="MCP integration",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        mcp_row = tk.Frame(body, bg=C["base"])
        mcp_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(mcp_row, text="🔌  Manage MCP wiring…",
                   command=self._open_mcp_configurator).pack(side=tk.LEFT)
        try:
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"]
                      for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if states and all(s == "ok" for s in states):
            summary = "✓  Both Claude Desktop and Claude Code route through the wrapper."
            summary_fg = C["green"]
        elif "no_file" in states or "missing" in states:
            summary = "✗  One or more Claude configs need a tokensave entry."
            summary_fg = C["red"]
        elif any(s in ("direct_serve", "wrong_wrapper", "unparseable") for s in states):
            summary = "⚠  One or more Claude configs bypass the wrapper (★ pin won't work for them)."
            summary_fg = C["peach"]
        else:
            summary = ""
            summary_fg = C["overlay0"]
        if summary:
            tk.Label(body, text="  " + summary,
                     font=("Segoe UI", 9), bg=C["base"], fg=summary_fg,
                     justify=tk.LEFT, anchor=tk.W,
                     wraplength=620).pack(anchor=tk.W, padx=36, pady=(0, 2))
        tk.Label(body,
            text="  Routes tokensave through the manager's pin-aware wrapper so\n"
                 "  ★ Set as Active swaps projects live, without restarting Claude.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── Ollama ────────────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="Ollama", font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        ollama_row = tk.Frame(body, bg=C["base"])
        ollama_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(ollama_row, text="🦙  Manage Ollama Models…",
                   command=self._open_ollama_manager).pack(side=tk.LEFT)
        tk.Label(body,
            text="  Browse installed models, pull new ones, see context windows.\n"
                 "  Uses Ollama's native REST API at the base URL configured below.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _build_ai_section(self, body, raw):
        """AI commit messages — provider, model, key env, presets, options."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="AI commit messages",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))

        llm_cfg = raw.get("commit_message_llm") or {}
        self._var_llm_enabled = tk.BooleanVar(value=bool(llm_cfg.get("enabled", False)))
        tk.Checkbutton(body,
            text="Use AI to generate commit message suggestions",
            variable=self._var_llm_enabled,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 4))

        self._build_ai_provider_grid(body, llm_cfg)
        self._build_ai_presets(body)
        self._build_ai_options(body, llm_cfg)

    # ── AI-section sub-builders ──────────────────────────────────────────────

    def _build_ai_provider_grid(self, body, llm_cfg):
        """Provider / model / key / base-URL 4-row grid."""
        llm_grid = tk.Frame(body, bg=C["base"])
        llm_grid.pack(fill=tk.X, padx=36, pady=(0, 6))

        def _row(label_txt, widget):
            row = tk.Frame(llm_grid, bg=C["base"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label_txt, width=18, anchor=tk.W,
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["subtext"]).pack(side=tk.LEFT)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._var_llm_provider = tk.StringVar(value=llm_cfg.get("provider", "anthropic"))
        provider_box = ttk.Combobox(llm_grid, textvariable=self._var_llm_provider,
            values=["ollama", "anthropic", "openai", "openai_compatible"],
            state="readonly", width=22)
        _row("Provider:", provider_box)

        self._var_llm_model = tk.StringVar(value=llm_cfg.get("model", "claude-haiku-4-5"))
        _row("Model:", ttk.Entry(llm_grid, textvariable=self._var_llm_model))

        self._var_llm_keyenv = tk.StringVar(value=llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        _row("API key env var:", ttk.Entry(llm_grid, textvariable=self._var_llm_keyenv))

        self._var_llm_base_url = tk.StringVar(value=llm_cfg.get("base_url", ""))
        _row("Base URL:", ttk.Entry(llm_grid, textvariable=self._var_llm_base_url))

    def _build_ai_presets(self, body):
        """Anthropic / LM Studio / Ollama quick-preset buttons + feedback label."""
        preset_row = tk.Frame(body, bg=C["base"])
        preset_row.pack(anchor=tk.W, padx=36, pady=(0, 4))
        tk.Label(preset_row, text="Quick presets:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 8))

        # Hint label must exist BEFORE the preset callbacks reference it.
        self._llm_preset_hint = tk.Label(body, text="", font=("Segoe UI", 8),
                                         bg=C["base"], fg=C["overlay0"],
                                         justify=tk.LEFT, wraplength=620, anchor=tk.W)
        self._llm_preset_hint.pack(anchor=tk.W, padx=36, pady=(2, 0), fill=tk.X)

        def _apply_lm_studio():
            self._var_llm_provider.set("openai_compatible")
            base = "http://localhost:1234"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            detected = _probe_loaded_model(base)
            if detected:
                self._var_llm_model.set(detected)
                self._llm_preset_hint.configure(text=f"✓  Using loaded model: {detected}", fg=C["green"])
            else:
                self._llm_preset_hint.configure(
                    text="⚠  LM Studio server not reachable at http://localhost:1234 — "
                         "start the Local Server in LM Studio's '</>' panel and load a model, "
                         "then click this preset again.", fg=C["peach"])

        def _apply_ollama():
            self._var_llm_provider.set("ollama")
            base = "http://localhost:11434"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            detected = _probe_loaded_model(base)
            if detected:
                self._var_llm_model.set(detected)
                self._llm_preset_hint.configure(text=f"✓  Using Ollama model: {detected}", fg=C["green"])
            else:
                if not self._var_llm_model.get() or "claude" in self._var_llm_model.get():
                    self._var_llm_model.set("qwen2.5-coder:14b")
                self._llm_preset_hint.configure(
                    text="⚠  Ollama not reachable at http://localhost:11434 — "
                         "make sure the Ollama service is running and run "
                         "`ollama pull qwen2.5-coder:14b` (or any chat model), "
                         "then click this preset again.", fg=C["peach"])

        def _apply_anthropic():
            self._var_llm_provider.set("anthropic")
            self._var_llm_base_url.set("")
            self._var_llm_keyenv.set("ANTHROPIC_API_KEY")
            if not self._var_llm_model.get() or "/" in self._var_llm_model.get():
                self._var_llm_model.set("claude-haiku-4-5")
            self._llm_preset_hint.configure(
                text="ℹ  Set the ANTHROPIC_API_KEY environment variable (get a "
                     "key at console.anthropic.com).  Haiku is cheapest "
                     "(~$0.0005/commit); Sonnet/Opus are higher-fidelity.", fg=C["blue"])

        ttk.Button(preset_row, text="Anthropic", command=_apply_anthropic).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="LM Studio", command=_apply_lm_studio).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Ollama",    command=_apply_ollama).pack(side=tk.LEFT)

    def _build_ai_options(self, body, llm_cfg):
        """Min-diff-lines spinner, sync auto-commit toggle, disclaimer."""
        min_row = tk.Frame(body, bg=C["base"])
        min_row.pack(anchor=tk.W, padx=36, pady=(2, 0))
        self._var_llm_min_diff = tk.StringVar(value=str(llm_cfg.get("min_diff_lines", 30)))
        tk.Label(min_row, text="Min diff lines (smaller commits skip AI):",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(min_row, textvariable=self._var_llm_min_diff, width=6).pack(side=tk.LEFT)

        self._var_llm_for_sync = tk.BooleanVar(value=bool(llm_cfg.get("use_for_sync_autocommit", False)))
        tk.Checkbutton(body,
            text="Also use AI for sync auto-commit messages (disables amend-stacking)",
            variable=self._var_llm_for_sync,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(6, 2))
        tk.Label(body,
            text="  AI runs only when toggled ON. Silent fallback on any error\n"
                 "  (missing key, network failure, timeout). Anthropic Claude Haiku\n"
                 "  costs ~$0.0005 per commit.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    # ── Cross-dialog launchers (lazy-imported per Rule 6) ───────────────────

    def _open_mcp_configurator(self):
        """Launch the MCP integration configurator.

        Lazy import (Rule 6) — avoids any module-load cycle between
        dialogs/settings.py and dialogs/mcp_config.py. MCPConfigDialog
        is only needed when the user clicks this button, so the
        in-handler import has no performance cost.
        """
        from dialogs.mcp_config import MCPConfigDialog
        MCPConfigDialog(self, cfg=self._cfg)

    def _open_ollama_manager(self):
        """Launch the Ollama Model Manager dialog.

        Uses whatever base URL is currently typed in the AI commit messages
        section (so editing the URL takes effect without saving Settings
        first). Falls back to http://localhost:11434 if blank. When the
        user clicks "Use for AI features" on a model in the dialog, the
        callback updates the provider/model/base-url fields in this very
        Settings dialog — they still have to click Save to persist.

        Lazy import (Rule 6) for the same reason as _open_mcp_configurator.
        """
        from dialogs.ollama_model_mgr import OllamaModelManagerDialog
        base_url = self._var_llm_base_url.get().strip() \
                   or "http://localhost:11434"

        def _on_use(model_name: str, server_url: str):
            self._var_llm_provider.set("ollama")
            self._var_llm_model.set(model_name)
            self._var_llm_base_url.set(server_url)
            self._var_llm_keyenv.set("")
            # Auto-enable AI features when the user explicitly picks a model.
            self._var_llm_enabled.set(True)
            if hasattr(self, "_llm_preset_hint"):
                self._llm_preset_hint.configure(
                    text=f"✓  Using Ollama model: {model_name}.  "
                         f"Click Save to persist.",
                    fg=C["green"])

        OllamaModelManagerDialog(
            self, base_url=base_url, on_use_for_ai=_on_use)

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            f"Label for this category:\n(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self,
        )
        if lbl is None:
            return   # user cancelled
        self._roots_tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))

    def _edit_root_label(self):
        sel = self._roots_tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl  = self._roots_tv.set(iid, "label")
        new_lbl  = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self)
        if new_lbl is not None:
            self._roots_tv.set(iid, "label", new_lbl.strip() or cur_lbl)

    def _remove_root(self):
        sel = self._roots_tv.selection()
        if sel:
            self._roots_tv.delete(sel[0])

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

    def _scroll_to_codegraph(self):
        """Pull the CodeGraph section into view + focus its path entry.

        SettingsDialog is non-scrollable (resizable=False, no canvas wrapper),
        so all sections are always rendered. focus_set on the path entry is
        enough — no yview math needed. Wrapped in try/except so any future
        layout change that introduces scrolling fails gracefully rather than
        crashing the install-nudge flow.
        """
        try:
            self._cg_exe_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _save(self):
        """Persist the dialog's pending changes via the cfg-mutation contract.

        The flow:
          1. Mutate self._cfg.raw[key] = value for each edited field
          2. Call self._save_fn(self._cfg.raw) — writes to disk via
             helpers.config._save_config
          3. Call self._callback() — App._on_settings_saved, which
             refreshes _state.refresh_derived() AND re-binds the
             legacy module globals (TOKENSAVE, GIT_EXE, etc.) so any
             remaining global reader sees the new values

        Plan Rule 5 holds throughout: we mutate raw (the writable
        surface), NEVER assign a derived @property — those would
        raise AttributeError on assignment.
        """
        exe = self._exe_var.get().strip()
        if exe and not os.path.isfile(exe):
            messagebox.showwarning("Not found",
                f"tokensave.exe not found at:\n{exe}", parent=self)
            return
        raw = self._cfg.raw
        raw["tokensave_exe"] = exe
        raw["template_dir"]  = self._tmpl_var.get().strip()
        raw["editor_cmd"]    = self._editor_var.get().strip() or "code"
        # python_exe is intentionally not exposed in the UI (used by the .bat
        # launcher only); preserve whatever value is already in the config.
        raw["search_roots"] = [
            {"path": self._roots_tv.set(iid, "path"),
             "label": self._roots_tv.set(iid, "label")}
            for iid in self._roots_tv.get_children()
        ]
        raw["auto_commit_after_sync"] = self._var_autocommit.get()
        # Persist AI commit-message settings (preserves any unknown keys
        # the user may have added manually via JSON edit).
        existing_llm = raw.get("commit_message_llm") or {}
        try:
            min_diff_lines = int(self._var_llm_min_diff.get())
        except ValueError:
            min_diff_lines = 30
        existing_llm.update({
            "enabled":     self._var_llm_enabled.get(),
            "provider":    self._var_llm_provider.get().strip() or "anthropic",
            "model":       self._var_llm_model.get().strip(),
            "api_key_env": self._var_llm_keyenv.get().strip(),
            "base_url":    self._var_llm_base_url.get().strip(),
            "min_diff_lines": max(0, min_diff_lines),
            "use_for_sync_autocommit": self._var_llm_for_sync.get(),
        })
        # Fill in defaults that other helpers expect
        existing_llm.setdefault("max_diff_chars", 24000)
        existing_llm.setdefault("timeout_seconds", 90)
        raw["commit_message_llm"] = existing_llm
        raw["git_exe"]       = self._git_exe_var.get().strip()
        raw["codegraph_exe"] = self._cg_exe_var.get().strip()
        self._save_fn(raw)
        self.destroy()
        self._callback()
