"""TokenSave Manager — App + entry point.

Phase E of Round 4 — this is the new module-level entry point. The bat
launcher (Launch TokenSave Manager.bat) invokes `python src/app.py`,
which routes through `main()` below.

Architecture (post-Round 4):
  • `src/state.py`         — ManagerConfig dataclass (runtime-mutable settings)
  • `src/constants.py`     — immutable constants (palette, regex, paths)
  • `src/theme.py`         — _Tooltip Tk-coupled UI primitive
  • `src/helpers/`         — 12 modules of pure / IO helpers
  • `src/dialogs/`         — 18 tk.Toplevel dialog classes
  • `src/controllers/`     — 4 tab controllers (Projects / Git / Ask / Snippets)
  • `src/app.py`           — App + main() (THIS file)

App owns the ManagerConfig instance (`self._cfg = ManagerConfig.load()` at
construction). Every controller and dialog receives that same instance via
__init__; they read live values through `self._cfg.X` properties (Rule 3)
so a Settings save propagates immediately without restart.

The legacy module globals (TOKENSAVE, GIT_EXE, …) that lived in the old
monolith are GONE — `App._on_settings_saved` no longer rebinds anything
because nothing reads bare globals anymore.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import pystray

from constants import (
    AUTO_REFRESH_MS,
    C,
    CREATE_NO_WINDOW,
    LOG_FILE,
    _ANSI,
    _BASE_DIR,
    _CONFIG_PATH,
    _TOKENSAVE_UPDATE_RE,
)
from controllers.ask_tab import AskTabController
from controllers.git_tab import GitTabController
from controllers.projects_tab import ProjectsTabController
from controllers.snippets import SnippetsController
from dialogs.git_commit import GitCommitDialog
from dialogs.mcp_config import MCPConfigDialog
from dialogs.settings import SettingsDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog
from helpers.commit_messages import _suggest_commit_message
from helpers.detection import _version_lt
from helpers.git import _find_tracked_but_ignored, _is_git_repo, _is_local_git_repo
from helpers.mcp import _MCP_CONFIGS, _classify_mcp_entry
from helpers.project_discovery import find_projects, get_pinned
from helpers.runtime import (
    _acquire_instance_lock,
    _bring_existing_to_front,
    _make_tray_icon,
    log,
)
from state import ManagerConfig


# ── Prompt snippets (Reference tab) ─────────────────────────────────────────

from prompts import PROMPT_SNIPPETS


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TokenSave Manager")
        self.geometry("760x600")
        self.minsize(600, 520)
        self.configure(bg=C["base"])
        # Hold the canonical ManagerConfig instance. Future controllers and
        # dialogs (extracted in Phases B–E) receive this via __init__ and
        # read live values through cfg.git_exe / cfg.tokensave_exe / etc.
        # During the Phase A transition window, legacy module globals
        # (self._cfg.tokensave_exe, self._cfg.git_exe, …) still exist and get re-bound by
        # _on_settings_saved alongside self._cfg.refresh_derived().
        self._cfg = ManagerConfig.load()
        self._current_proc = None
        self._stop_requested = False
        # Cached tokensave version info.  Current version is populated at
        # App startup via `tokensave --version` (fast, no network).
        # Available-update version is populated by the output parser in
        # `_run` when tokensave emits "Update available: vA → vB" at the
        # end of a sync — that line is opportunistic (tokensave appears
        # to throttle update checks to once per day), so SettingsDialog
        # ALWAYS shows the Upgrade button with the current version, and
        # only labels it with the target version when one is known.
        self._tokensave_current_version: str | None = None
        self._tokensave_available_version: str | None = None
        self._probe_tokensave_version()
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {self._cfg.tokensave_exe}")
        log.info(f"  templates: {self._cfg.template_dir}")
        log.info(f"  log file : {LOG_FILE}")
        self._style()
        self._build()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray = None
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.bind("<Unmap>", self._on_unmap)
        self.after(300, self._check_config)

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.withdraw()
        log.debug("Window hidden to tray")

    def _on_unmap(self, event):
        if event.widget is self:
            self.after(100, self._maybe_hide)

    def _maybe_hide(self):
        if self.state() == "iconic":
            self.withdraw()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None):
        log.info("Quit requested from tray")
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)

    # ── Styles ─────────────────────────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C["base"], foreground=C["text"],
            font=("Segoe UI", 10), borderwidth=0)

        s.configure("Treeview",
            background=C["mantle"], foreground=C["text"],
            fieldbackground=C["mantle"], rowheight=30,
            font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["surface0"], foreground=C["subtext"],
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", C["surface1"])],
            foreground=[("selected", C["text"])])

        s.configure("TButton",
            background=C["surface0"], foreground=C["text"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("TButton",
            background=[("active", C["surface1"]), ("pressed", C["surface1"])])

        s.configure("Primary.TButton",
            background=C["blue"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Primary.TButton",
            background=[("active", C["lavender"]), ("pressed", C["lavender"])])

        s.configure("Action.TButton",
            background=C["peach"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Action.TButton",
            background=[("active", C["yellow"]), ("pressed", C["yellow"])])

        s.configure("Danger.TButton",
            background=C["surface0"], foreground=C["red"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("Danger.TButton",
            background=[("active", C["surface1"])])

        s.configure("TScrollbar",
            background=C["surface0"], troughcolor=C["mantle"],
            bordercolor=C["base"], arrowcolor=C["overlay0"],
            relief="flat")

        s.configure("TSeparator", background=C["surface0"])

        s.configure("TNotebook",
            background=C["base"], borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=C["surface0"], foreground=C["subtext"],
            padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
            background=[("selected", C["base"])],
            foreground=[("selected", C["blue"])])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=C["mantle"], pady=12, padx=16)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="TokenSave Manager",
                 font=("Segoe UI", 15, "bold"),
                 bg=C["mantle"], fg=C["blue"]).pack(side=tk.LEFT)

        self.active_badge = tk.Label(hdr, text="",
            font=("Segoe UI", 9), bg=C["surface0"],
            fg=C["green"], padx=8, pady=3)
        self.active_badge.pack(side=tk.RIGHT)

        # ── Credit bar ──
        tk.Label(self, text="TokenSave Manager  ·  Alexander L Corthell",
                 font=("Segoe UI", 7), bg=C["crust"], fg=C["overlay0"],
                 pady=2).pack(fill=tk.X, side=tk.BOTTOM)

        # ── Separator + Log — packed BEFORE notebook so expand=True doesn't eat it ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, side=tk.BOTTOM)

        log_frame = tk.Frame(self, bg=C["base"], padx=14, pady=8)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Notebook ──
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._projects = ProjectsTabController(
            self.nb, self._cfg,
            get_projects=lambda: self.projects,
            on_run=self._run,
            on_run_capture=self._run_capture,
            on_shell=self._shell_capture,
            on_log=self._log,
            on_commit=self._open_commit_dialog,
            on_refresh=self.refresh,
            on_project_select=self._on_project_selected,
            on_set_running=self._set_running,
            on_settings=self.cmd_settings,
        )
        self._git = GitTabController(
            self.nb, self._cfg,
            get_path=self._get_git_path,
            on_log=self._log,
            on_shell=self._shell_capture,
            on_commit=self._open_commit_dialog,
        )
        self._ask_ctrl = AskTabController(
            self.nb, self._get_ask_project_path, self._cfg)
        self._snippets_ctrl = SnippetsController(
            self.nb, self._cfg, PROMPT_SNIPPETS)
        self._build_help_tab()

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        self._stop_btn = ttk.Button(log_header, text="■  Stop",
                                    style="Danger.TButton",
                                    command=self._stop_current,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._running_label = tk.Label(log_header, text="",
                                       font=("Segoe UI", 8),
                                       bg=C["base"], fg=C["yellow"])
        self._running_label.pack(side=tk.RIGHT, padx=(0, 8))

        log_inner = tk.Frame(log_frame, bg=C["mantle"])
        log_inner.pack(fill=tk.X)

        self.log = tk.Text(log_inner, height=4,
            font=("Consolas", 9), bg=C["mantle"], fg=C["green"],
            insertbackground=C["green"], relief=tk.FLAT,
            padx=10, pady=6, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(log_inner, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Tab / project navigation ────────────────────────────────────────────

    def _on_project_selected(self, path: str) -> None:
        """Fired by ProjectsTabController when the user clicks a project row.

        Routes the new project path to GitTabController and AskTabController.
        This replaces the old event-handler _on_project_select which accessed
        self.tree directly.
        """
        self._git.set_active_path(path)
        if self._git.is_visible():
            self._git.refresh()

    def _on_tab_changed(self, event=None):
        """Fires when the user switches notebook tabs."""
        try:
            current_tab_text = self.nb.tab(self.nb.select(), "text").strip()
        except tk.TclError:
            current_tab_text = ""
        if "Ask" in current_tab_text:
            self._ask_ctrl.on_tab_selected()
            return

        if not self._git.is_visible():
            return
        sel_path = self._projects.get_selected_path()
        if sel_path:
            self._git.set_active_path(sel_path)
        elif not self._git.has_path() and self.active_path:
            self._git.set_active_path(self.active_path)
        if self._git.has_path():
            self._git.refresh()

    def _get_ask_project_path(self) -> str | None:
        """Return the currently focused project path for AskTabController."""
        if hasattr(self, "_projects"):
            path = self._projects.get_selected_path()
            if path:
                return path
        return getattr(self, "active_path", None)

    def _get_git_path(self) -> str | None:
        """Return the currently focused project path for GitTabController."""
        if hasattr(self, "_projects"):
            path = self._projects.get_selected_path()
            if path:
                return path
        return getattr(self, "active_path", None)

    # ═══════════════════════════════════════════════════════════════════
    # 🤖 Ask tab — handled by AskTabController (see above App class)
    # 📚 Reference tab — handled by SnippetsController (see above App class)
    # ═══════════════════════════════════════════════════════════════════

    def _build_help_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  Project Categories",  self._help_categories),
            ("  Git: What & Why",     self._help_git_concepts),
            ("  Git: Daily Workflow", self._help_git_workflow),
            ("  Git Tab Buttons",     self._help_git_tab),
            ("  GitHub Setup",        self._help_github_setup),
            ("  CodeGraph",           self._help_codegraph),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    def _help_show(self, fn):
        """Clear the help text widget, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ──────────────────────────────────────────────────────────

    def _help_switching(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_categories(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Project Categories")
            p("Projects are automatically grouped under the label of the search root "
              "folder they belong to. You can override any project's category — and add "
              "an optional sub-category — without moving any files.")
            br()
            h2("How root labels work")
            p("Each entry in Settings → Search Roots has a Label. That label becomes "
              "the category header for all projects found inside that folder. Edit the "
              "label in Settings to rename the whole group at once.")
            br()
            h2("Overriding a single project")
            ins("  1. Right-click the project row\n", "body")
            ins("  2. Choose  📁 Assign Category…\n", "body")
            ins("  3. Pick or type a Category (and optional Sub-category)\n", "body")
            ins("  4. Click OK — the project moves to the new group immediately\n", "body")
            br()
            p("To remove an override and return the project to its root's group, "
              "open Assign Category… and click Clear Override.")
            br()
            h2("Sub-categories")
            p("Sub-categories appear indented under their parent category (shown as "
              "↳ Sub-category). They work like folders-within-folders. Right-click "
              "any project at any time to move it between groups.")
            br()
            warn("⚠  Category headers and sub-category rows are not selectable — "
                 "right-click and action buttons only work on project rows.")
        self._help_show(_fill)

    def _help_git_concepts(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: What & Why")
            p("Git is a tool that remembers the history of every change you make to "
              "your project. Think of it like infinite undo — but smarter. You decide "
              "when to save a checkpoint, and you can always go back.")
            br()
            h2("Commit — a save point")
            p("A commit is a snapshot of your project at a moment in time. Each one "
              "has a short message you write, like 'fix: typo in README' or "
              "'feat: add dark mode'. Over time, these build up into a history "
              "you can scroll through.")
            br()
            h2("Repository (repo) — the project folder + its history")
            p("When you run Git Init on a project, git creates a hidden .git folder "
              "inside it. That folder stores every commit ever made. The whole thing "
              "— your files plus that history — is called a repository.")
            br()
            h2("Branch — a parallel version")
            p("Imagine photocopying your project so you can experiment on the copy "
              "without touching the original. That's a branch. When you're happy "
              "with the experiment, you can merge it back. The default branch is "
              "usually called 'master' or 'main'.")
            br()
            h2("Remote — a copy on GitHub")
            p("A remote is a second home for your repository, stored on GitHub's "
              "servers. It acts as a backup and lets others see your work. The "
              "remote is usually called 'origin'.")
            br()
            h2("Push — upload to GitHub")
            p("After making commits on your machine, Push sends them to GitHub. "
              "Nothing leaves your computer until you Push — commits are purely local "
              "until then.")
            br()
            h2("Pull — download from GitHub")
            p("Pull fetches any commits from GitHub that you don't have yet and "
              "adds them to your local history. Useful if you work on multiple "
              "machines, or if a collaborator pushed something new.")
            br()
            h2("Working tree — uncommitted changes")
            p("The working tree is the current state of your files right now, before "
              "you've committed them. The Git tab shows a list of files that have "
              "changed since your last commit. An 'M' means modified, '?' means "
              "a new file git hasn't seen before, 'D' means deleted.")
            br()
            h2("Staging — choosing what to commit")
            p("Git lets you pick exactly which changes to include in a commit. "
              "The 'Stage all changes' checkbox in the Commit dialog does this "
              "automatically — it stages everything in the working tree, which is "
              "almost always what you want.")
            br()
            ok("Bottom line: commit often, push when you're done for the day.")
        self._help_show(_fill)

    def _help_git_workflow(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: Daily Workflow")
            p("Here's how a typical coding session looks when using the Git tab.")
            br()
            h2("Starting a session")
            ins("  1. Switch to the Git tab\n", "body")
            ins("  2. Click ⟳ Refresh to see the current state\n", "body")
            ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
                "     changes from GitHub before you start editing\n", "body")
            br()
            h2("While you're working")
            p("Edit your files normally. The Working Tree list updates whenever "
              "you Refresh. Click any file in the list to see exactly what changed "
              "(green = added, red = removed).")
            br()
            h2("Saving your work (committing)")
            ins("  1. Click  📝 Commit…\n", "body")
            ins("  2. The dialog shows what files changed and suggests a message\n", "body")
            ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
            ins("  4. Click Commit\n", "body")
            br()
            p("There's no rule for how often to commit. A good rule of thumb: "
              "commit whenever you finish one thing. Small commits are better than "
              "one huge commit at the end of the day.")
            br()
            h2("Uploading to GitHub (pushing)")
            ins("  1. Click  ⬆ Push\n", "body")
            ins("  2. The output log shows whether it succeeded\n", "body")
            ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
            br()
            h2("Trying out an idea safely (branching)")
            ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
            ins("  2. Check 'Switch to this branch immediately'\n", "body")
            ins("  3. Make your changes and commit as normal\n", "body")
            ins("  4. If you don't like it: 🔀 Switch Branch back to master — the\n"
                "     experiment branch stays there but your main code is untouched\n", "body")
            br()
            h2("Finishing a feature branch (merge & cleanup)")
            p("Once your branch is tested and ready to bring back into master:")
            ins("  1.  🔀 Switch Branch  → master\n", "body")
            ins("  2.  ⬇ Pull            — pick up any new master commits first\n", "body")
            ins("  3.  ⇄ Merge…          → pick your feature branch\n", "body")
            ins("                          Confirmation says 'Merge X INTO master?' — yes\n", "body")
            ins("  4.  ⬆ Push            — master with the merged commits goes to GitHub\n", "body")
            ins("  5.  🗑 Delete Branch  → pick your feature branch → Yes (local)\n", "body")
            ins("                          Then: 'Also delete from GitHub?' → Yes\n", "body")
            br()
            p("If the merge produces conflicts, the manager pops a dialog telling "
              "you what to do (resolve in editor + commit, or run "
              "'git merge --abort' to undo). Conflicts only happen when both "
              "branches changed the same lines.")
            br()
            h2("Undoing mistakes")
            p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
              "as uncommitted edits — you can fix them and recommit, or just discard.")
            br()
            warn("⚠  Undo Last Commit only removes the last commit. To undo older "
                 "commits, use the terminal.")
            br()
            h2("Typical day in one line")
            dim("  Pull → Edit → Commit → Edit → Commit → Push")
        self._help_show(_fill)

    def _help_git_tab(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git Tab")
            p("The Git tab shows live status for whichever project is selected in the "
              "Projects tab. It updates automatically when you switch projects or switch "
              "to this tab.")
            br()
            h2("Working Tree & Diff")
            p("The Working Tree panel lists every modified, added, or deleted file. "
              "Click any file to see its diff below — added lines are green, removed "
              "lines are red.")
            br()
            h2("Committing changes")
            ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
            ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
            ins("  3. Edit the message if you like, then click Commit\n", "body")
            br()
            p("The suggested message is generated from your staged changes, using "
              "a chain of strategies — highest-quality first:")
            ins("    1. CHANGELOG.md bullets (if you've added an entry)\n", "body")
            ins("    2. Diff content — added Python defs/classes, file kinds\n", "body")
            ins("    3. File-name patterns (legacy fallback)\n", "body")
            p("Each result is sanitised (subject ≤ 72 chars, imperative mood, "
              "no filename listings). When AI is enabled in Settings, an "
              "Anthropic / OpenAI / LM Studio / Ollama call runs first — silent "
              "fallback to heuristics on any failure. Click 💡 Suggest at any "
              "time to regenerate.")
            br()
            h2("Undo Last Commit")
            p("Removes the most recent commit but keeps all your changes staged — "
              "nothing is deleted. Safe to use if you committed too early or with "
              "the wrong message.")
            br()
            h2("Branches")
            ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
            ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
            ins("  ⇄ Merge…         — merge another branch INTO the current one\n", "body")
            ins("                     (use after switching to master to pull a finished feature back in)\n", "body")
            ins("  🗑 Delete Branch — safe-delete locally; then offers to also delete from GitHub\n", "body")
            ins("                     (only prompts about GitHub if a remote copy actually exists)\n", "body")
            br()
            warn("⚠  Switching branches with uncommitted changes will fail. "
                 "Commit or undo first.")
            br()
            warn("⚠  Merging with uncommitted changes also fails. Same fix — commit, "
                 "stash, or undo first.")
            br()
            h2("Push & Pull")
            p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
              "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
        self._help_show(_fill)

    def _help_github_setup(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("GitHub Setup")
            p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
              "wizard for getting your project onto GitHub — even if you've never used "
              "GitHub before.")
            br()
            h2("Step 1 — Git identity")
            p("Every commit is stamped with your name and email. The wizard shows your "
              "current global settings and lets you update them. These are stored in "
              "your global git config and apply to every project on this machine.")
            br()
            h2("Step 2 — Create a GitHub account")
            p("Free at github.com. The wizard has a button to open the sign-up page.")
            br()
            h2("Step 3 — Create a repository")
            p("Go to github.com/new. Give it a name, leave it Public. "
              "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
              "Copy the HTTPS URL shown after creation (e.g. "
              "https://github.com/you/my-project.git).")
            br()
            h2("Step 4 — Paste the URL")
            p("Paste the URL into the wizard and click Set. This tells git where to "
              "send your code. The Git tab's Remote label will update immediately.")
            br()
            h2("Step 5 — Push")
            p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
              "you to log in to GitHub — this is Git Credential Manager doing its job. "
              "Log in once and future pushes happen silently.")
            br()
            warn("⚠  If Push fails with an authentication error, open a terminal in "
                 "the project folder and run:  git push\n"
                 "This triggers the browser login. After that, the Push button works normally.")
            br()
            h2("📦 GitHub Releases")
            p("A Release lets anyone download your .exe without needing Python "
              "installed. To create one:")
            ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
            ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
            ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
            ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
            br()
            p("Releases require the GitHub CLI (gh). If it's not installed, "
              "the wizard shows a link to cli.github.com.")
        self._help_show(_fill)

    def _help_codegraph(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("CodeGraph (alternative code-graph tool)")
            p("CodeGraph is a separate MCP server that does what tokensave does — "
              "builds a per-project code-graph index and exposes it to Claude Code. "
              "The two don't conflict; a project can have both at once.")
            br()
            h2("When to use which")
            ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
            ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
            ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
            br()
            warn("⚠  About CodeGraph's auto-sync: the file watcher only runs while "
                 "CodeGraph's MCP server is active inside an open Claude Code session. "
                 "If you edit code with Claude Code closed, those edits won't be "
                 "picked up automatically until the next session — at which point "
                 "the watcher catches up. You can also right-click → 🧠 CodeGraph Sync "
                 "to force an incremental update manually.")
            br()
            h2("Install")
            ins("  Settings → CodeGraph → Install via npm  ", "body")
            ins("(requires Node.js 18+)\n", "dim")
            br()
            h2("Use")
            ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
            ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
            br()
            h2("Why the manager doesn't run `codegraph install`")
            p("CodeGraph registers itself with Claude Code (and Cursor / Codex / "
              "opencode if you use them) via its own one-time installer: "
              "`npx @colbymchenry/codegraph`. The TokenSave Manager intentionally "
              "stays out of that flow — we handle per-project lifecycle only "
              "(init / sync / status / remove). This means tokensave and CodeGraph "
              "can both write their own sections into your global ~/.claude.json "
              "without fighting each other.")
        self._help_show(_fill)

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(self._cfg.template_dir, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(self._cfg.template_dir, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(self._cfg.template_dir, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)

    def refresh(self):
        self.projects = find_projects(self._cfg.search_roots)
        pinned = get_pinned()
        self.active_path = pinned or (self.projects[0]["path"] if self.projects else None)

        # Delegate tree population to the controller
        self._projects.rebuild_tree(self.projects, self.active_path, pinned)

        if self.active_path:
            name = os.path.basename(self.active_path)
            tag  = "pinned" if pinned else "auto"
            self.active_badge.config(text=f"  ★ {name}  ({tag})  ")
        else:
            self.active_badge.config(text="  No project  ")

        # Keep Git tab in sync when it's visible and a project is tracked
        if self._git.is_visible() and self._git.has_path():
            self._git.refresh()

        # Kick off background refresh of the Git status column via controller
        self._projects.refresh_git_status_column(self.projects)

    def _check_config(self):
        problems = []
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            problems.append("tokensave.exe path is missing or invalid")
        if not self._cfg.template_dir or not os.path.isdir(self._cfg.template_dir):
            problems.append("Template directory is missing or invalid")

        # MCP-config drift detection — opens the configurator instead of
        # Settings when there are no other problems, since that's the most
        # actionable thing the user can do.
        skips = (self._cfg.raw.get("mcp_skip_warnings") or []) \
                if isinstance(self._cfg.raw, dict) else []
        mcp_drift = []
        for label, path in _MCP_CONFIGS:
            if path in skips:
                continue
            try:
                info = _classify_mcp_entry(path, self._cfg.raw)
            except Exception:
                # Defensive — never crash startup just because we can't read
                # a Claude config file. The dialog can surface details.
                continue
            if info["state"] != "ok":
                mcp_drift.append((label, info))

        if not problems and not mcp_drift:
            return

        if problems:
            # Existing path: paths broken, open Settings as before.
            note = "Please set the correct paths before using the manager."
            self._log("Config problem: " + " | ".join(problems), C["red"])
            SettingsDialog(
                self, self._cfg, self._cfg.save, self._on_settings_saved,
                startup_note=(note + "\n\n"
                              + "\n".join(f"• {p}" for p in problems)))
            return

        # Pure MCP drift — log it, open the configurator dialog directly.
        # Don't auto-pop in a modal way; the user just launched the manager
        # and wants to see the project list. A log line + a non-modal dialog
        # gives them the choice.
        for label, info in mcp_drift:
            self._log(
                f"MCP: {label} {info['label']} ({info['cfg_path']}). "
                f"Open Settings → MCP integration to fix.",
                C["peach"] if info["state"] in
                ("direct_serve", "wrong_wrapper") else C["red"])

        # Open the configurator after a short delay so the main window has
        # finished laying out — feels less like an interruption.
        self.after(800, lambda: MCPConfigDialog(self, self._cfg))

    def _auto_refresh(self):
        ctrl_idle = (not hasattr(self, "_projects") or self._projects.current_proc is None)
        if self._current_proc is None and ctrl_idle:
            self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _log(self, msg, colour=None):
        def _do():
            self.log.configure(state=tk.NORMAL)
            tag = f"col_{colour}"
            self.log.tag_configure(tag, foreground=colour or C["green"])
            self.log.insert(tk.END, msg + "\n", tag)
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _set_running(self, running, label=""):
        if running:
            self._stop_btn.configure(state=tk.NORMAL)
            self._running_label.configure(text=f"⏳ running: {label}")
        else:
            self._stop_btn.configure(state=tk.DISABLED)
            self._running_label.configure(text="")

    def _stop_current(self):
        self._stop_requested = True
        proc = self._current_proc
        if proc and proc.poll() is None:
            proc.kill()
            self._log("  ■ Stopped by user.", C["red"])
        # Also stop any controller-managed subprocess (e.g. cmd_sync_all, scaffold)
        if hasattr(self, "_projects"):
            self._projects.stop()

    def _open_log(self):
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("No log yet",
                "No log file exists yet — run an operation first.", parent=self)

    def _run(self, args, cwd, label):
        def worker():
            cmd_str = "tokensave " + " ".join(args)
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            log.debug(f"     cwd={cwd}")
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe] + args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                log.debug(f"     pid={proc.pid}")
                # Suppress tokensave's misleading "legacy .codegraph/" warning
                # when CodeGraph is actually an active alternate index in this
                # project — manager treats tokensave and CodeGraph as equal
                # citizens; following tokensave's "safely deleted" advice would
                # wipe the user's CodeGraph index. The warning is only correct
                # for genuinely orphaned .codegraph/ folders (no codegraph.db).
                codegraph_active = os.path.isfile(
                    os.path.join(cwd, ".codegraph", "codegraph.db"))
                _suppressed_codegraph_warning = False
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    if (codegraph_active
                            and "legacy .codegraph" in stripped
                            and "safely deleted" in stripped):
                        # Drop silently, but log once that we did so. The
                        # info-bar message lets the user know we filtered
                        # — never silent fakery without trace.
                        if not _suppressed_codegraph_warning:
                            self._log(
                                "  (suppressed tokensave's '.codegraph/ legacy' "
                                "warning — CodeGraph is active in this project)",
                                C["overlay0"])
                            _suppressed_codegraph_warning = True
                        log.debug(f"  SUPPRESSED {stripped}")
                        continue
                    # Detect tokensave's "Update available: v→v" line and
                    # remember the upgrade target. Settings shows an
                    # "Upgrade tokensave" button when this is set; the
                    # button just runs `tokensave upgrade` via _run.
                    m = _TOKENSAVE_UPDATE_RE.search(stripped)
                    if m:
                        cur_v, new_v = m.group(1), m.group(2)
                        self._tokensave_current_version = cur_v
                        self._tokensave_available_version = new_v
                        self._log(stripped, C["yellow"])
                        self._log(
                            f"  → tokensave {cur_v} → {new_v} ready to "
                            f"install.  Settings → 'Upgrade tokensave to "
                            f"v{new_v}' to apply, or run "
                            f"'tokensave upgrade' from a shell.",
                            C["peach"])
                        log.info(f"UPDATE-AVAILABLE  {cur_v} -> {new_v}")
                        continue
                    self._log(stripped)
                    log.debug(f"  OUT {stripped}")
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                    if (args and args[0] == "sync"
                            and self._cfg.raw.get("auto_commit_after_sync")
                            and _is_git_repo(cwd, self._cfg.git_exe)):
                        self._auto_commit_after_sync(cwd)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)
        threading.Thread(target=worker, daemon=True).start()

    def _auto_commit_after_sync(self, cwd: str) -> None:
        """Commit staged changes after a successful sync, using LLM or amend-stacking."""
        self._log("  Auto-committing sync changes…", C["peach"])
        self._shell_capture([self._cfg.git_exe, "-C", cwd, "add", "-A"], cwd)
        _, staged_rc = self._shell_capture(
            [self._cfg.git_exe, "-C", cwd, "diff", "--cached", "--quiet"], cwd)
        if staged_rc == 0:
            return  # nothing staged

        llm_cfg = self._cfg.raw.get("commit_message_llm") or {}
        use_llm = bool(llm_cfg.get("enabled") and llm_cfg.get("use_for_sync_autocommit"))

        if use_llm:
            # LLM mode: each sync gets a unique message; no amend-stacking.
            self._log("  Composing AI commit message…", C["peach"])
            status_out, _ = self._shell_capture(
                [self._cfg.git_exe, "-C", cwd, "status", "--short"], cwd)
            ai_msg = _suggest_commit_message(cwd, status_out, self._cfg.raw, self._cfg.git_exe) or "chore: tokensave sync"
            commit_cmd = [self._cfg.git_exe, "-C", cwd, "commit", "-m", ai_msg.split("\n", 1)[0]]
            if "\n\n" in ai_msg:
                commit_cmd.extend(["-m", ai_msg.split("\n\n", 1)[1]])
        else:
            # Default amend-stacking: repeated syncs collapse into one commit.
            last_out, _ = self._shell_capture(
                [self._cfg.git_exe, "-C", cwd, "log", "-1", "--format=%s"], cwd)
            if last_out.strip() == "chore: tokensave sync":
                commit_cmd = [self._cfg.git_exe, "-C", cwd, "commit", "--amend", "--no-edit"]
                self._log("  Amending previous sync commit…", C["peach"])
            else:
                commit_cmd = [self._cfg.git_exe, "-C", cwd, "commit", "-m", "chore: tokensave sync"]

        cout, crc = self._shell_capture(commit_cmd, cwd)
        col = C["green"] if crc == 0 else C["red"]
        for line in cout.strip().splitlines()[-3:]:
            self._log(f"  {line}", col)

    def _run_capture(self, args, cwd, label) -> tuple:
        """Run a tokensave command and return (raw_output, returncode, elapsed_s).

        Synchronous — must be called from a background thread.
        Handles _current_proc tracking and _set_running for the duration.
        The caller is responsible for logging and scheduling UI updates.
        """
        cmd_str = "tokensave " + " ".join(args)
        self._log(f"$ {cmd_str}  [{label}]", C["blue"])
        self.after(0, self._set_running, True, label)
        log.info(f"RUN  {cmd_str}")
        log.debug(f"     cwd={cwd}")
        t0 = time.monotonic()
        try:
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            proc = subprocess.Popen(
                [self._cfg.tokensave_exe] + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self._current_proc = proc
            log.debug(f"     pid={proc.pid}")
            raw = proc.stdout.read()
            proc.wait()
            elapsed = time.monotonic() - t0
            log.info(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
            log.debug(f"  OUT {raw[:500]}")
            return raw, proc.returncode, elapsed
        finally:
            self._current_proc = None
            self.after(0, self._set_running, False)

    def _shell_capture(self, cmd: list, cwd: str, env=None) -> tuple:
        """Run any shell command and return (stdout+stderr, returncode).

        Generic helper — cmd[0] is the executable (not tokensave-specific).
        Synchronous — must be called from a background thread.
        Returns ("Error: '<exe>' not found on system PATH.", 1) if the
        executable is missing so callers always get a displayable string.

        Pass env= to override the process environment (e.g. set
        GIT_TERMINAL_PROMPT=0 for network git operations so they fail
        immediately instead of hanging waiting for stdin auth prompts).
        """
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
            out = proc.stdout.read()
            proc.wait()
            return out, proc.returncode
        except FileNotFoundError:
            return (f"Error: '{cmd[0]}' not found on system PATH.", 1)

    def _probe_tokensave_version(self):
        """Best-effort read of the installed tokensave version.

        Runs `tokensave --version` once at App startup (in a background
        thread to avoid blocking the GUI). Output looks like
        "tokensave 5.1.1" — we extract the version string and cache it
        on the instance. Failures (binary missing, weird output) leave
        the cache as None, which the Settings UI handles gracefully.
        """
        def _worker():
            if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
                return
            try:
                r = subprocess.run(
                    [self._cfg.tokensave_exe, "--version"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired):
                return
            out = (r.stdout or "").strip()
            m = re.search(r'(\d+\.\d+\.\d+(?:\.\d+)?)', out)
            if m:
                self._tokensave_current_version = m.group(1)
                log.debug(f"tokensave installed version: "
                          f"{self._tokensave_current_version}")
                # Kick off a single update check right after we know the
                # local version. Subsequent checks fire from the hourly
                # poller below.
                self._check_tokensave_updates()
        threading.Thread(target=_worker, daemon=True,
                         name="tokensave-version-probe").start()
        # Hourly background poller. Cheap (one GitHub API call, no auth);
        # safe to run forever as a daemon thread.
        threading.Thread(target=self._tokensave_update_poll_loop,
                         daemon=True,
                         name="tokensave-update-poll").start()

    # GitHub releases API endpoint for tokensave. Hardcoded since the
    # tokensave repo URL is referenced in README.md and is unlikely to
    # change. If it does, this is a one-line update.
    _TOKENSAVE_RELEASES_API = (
        "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest")

    # Hourly poll cadence — GitHub allows 60 unauthenticated requests/hour
    # per IP, so once an hour is comfortably within the limit and keeps
    # the update notification fresh enough that users won't miss a release
    # for long. Tunable via self._cfg.raw["tokensave_update_poll_hours"] if a user
    # wants to be more or less aggressive.
    def _tokensave_update_poll_interval(self) -> float:
        hours = float(self._cfg.raw.get("tokensave_update_poll_hours", 1.0))
        return max(0.25, hours) * 3600.0  # never poll more than 4x/hour

    def _tokensave_update_poll_loop(self):
        """Daemon: re-check GitHub for new tokensave releases periodically.

        Doesn't trigger any UI prompts — just refreshes the cached
        `_tokensave_available_version` so the Settings dialog reflects
        the current state next time it's opened. The OUTPUT-pane hint
        line is only logged on FRESH discovery (transition from "no
        update known" → "update available"), not on every poll, to avoid
        spamming the log.
        """
        while True:
            time.sleep(self._tokensave_update_poll_interval())
            self._check_tokensave_updates()

    def _check_tokensave_updates(self):
        """Single-shot check against the tokensave releases API.

        Compares against `_tokensave_current_version` (set by the local
        --version probe). When a strictly-newer version is found AND it
        wasn't known before, logs a peach hint to the OUTPUT pane so
        users see the update offer without opening Settings.
        """
        import urllib.request, urllib.error, json as _json
        if not self._tokensave_current_version:
            return  # nothing to compare against yet
        try:
            req = urllib.request.Request(
                self._TOKENSAVE_RELEASES_API,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "tokensave-manager"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, _json.JSONDecodeError, OSError) as e:
            # Common when offline or rate-limited. Silent — try again
            # next interval.
            log.debug(f"tokensave update check failed: {type(e).__name__}: {e}")
            return
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        m = re.match(r'(\d+\.\d+\.\d+(?:\.\d+)?)', tag)
        if not m:
            return
        latest = m.group(1)
        cur = self._tokensave_current_version
        if not _version_lt(cur, latest):
            return  # current is up-to-date or ahead (unlikely but possible)
        prev_known = self._tokensave_available_version
        self._tokensave_available_version = latest
        if prev_known != latest:
            # Fresh discovery — surface it. (Skip the log line if the
            # poller is just re-confirming a version we already knew
            # about.)
            self._log(
                f"  → tokensave {cur} → {latest} ready to install.  "
                f"Settings → 'Upgrade tokensave to v{latest}' to apply, "
                f"or run 'tokensave upgrade' from a shell.",
                C["peach"])
            log.info(f"UPDATE-AVAILABLE  {cur} -> {latest}  (via GitHub API)")

    def cmd_upgrade_tokensave(self):
        """Run `tokensave upgrade` from the manager.

        Streams output to the OUTPUT pane via the existing _run path. cwd
        doesn't matter for upgrade (it operates on the installed binary,
        not any specific project) so we use the tokensave_exe's directory
        as a stable choice. On success, the upgrade replaces the binary
        on disk; future sync / MCP-server spawns pick up the new version
        automatically, but the currently-running MCP wrappers continue
        serving from the old binary until Claude is restarted.

        Clears the cached `_tokensave_available_version` on success so
        the Settings button auto-hides until the next sync re-reports an
        update.
        """
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            messagebox.showwarning(
                "tokensave not found",
                "Set the tokensave.exe path in Settings first.",
                parent=self)
            return
        # Confirm — upgrades replace a binary and we want the user fully
        # aware. Skip the prompt if no version metadata is known (still
        # useful — runs the upgrade command which itself shows what'll
        # happen).
        target = self._tokensave_available_version
        cur = self._tokensave_current_version
        if target:
            msg = (f"Upgrade tokensave from v{cur or '?'} to v{target}?\n\n")
        elif cur:
            msg = (f"Run `tokensave upgrade`?  (Currently installed: "
                   f"v{cur}.)\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        else:
            msg = ("Run `tokensave upgrade`?\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        msg += (
            "This replaces the tokensave binary on disk.  Currently-running\n"
            "MCP wrappers continue serving from the old binary until you\n"
            "restart Claude Desktop / Claude Code.")
        if not messagebox.askyesno("Upgrade tokensave", msg, parent=self):
            return
        # Clear cache so the Settings button hides until the next sync
        # reports a fresh update.  If the upgrade fails, the next sync
        # will re-populate it anyway.
        self._tokensave_available_version = None
        # cwd is the tokensave.exe folder — works for any upgrade flow.
        self._run(["upgrade"], cwd=os.path.dirname(self._cfg.tokensave_exe),
                  label="upgrade")

    # ── Commit dialog (shared by Projects context menu, Git tab, and
    #    offer-commit-after-change flow) ──────────────────────────────────────

    def _open_commit_dialog(self, path: str):
        """Open GitCommitDialog for a given project path. Reused by
        `cmd_git_commit` (Projects-tab right-click) AND by the
        offer-commit-after-change flow that runs after Ensure .gitignore,
        Shadow Links, Scaffold, and Retrofit.

        Pre-flight check: if the project has tracked-but-ignored files,
        offer to run Untrack Ignored Files FIRST. Otherwise the commit
        attempt would inevitably hit git's "paths are ignored" error and
        the user would have to come back and untrack anyway.
        """
        if _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path, self._cfg.git_exe)
            if stale:
                n = len(stale)
                preview = "\n".join(f"  • {f}" for f in stale[:5])
                if n > 5:
                    preview += f"\n  • …and {n - 5} more"
                choice = messagebox.askyesnocancel(
                    "Tracked-but-ignored files detected",
                    f"{n} file{'s' if n != 1 else ''} in this project "
                    f"{'are' if n != 1 else 'is'} tracked by git BUT also "
                    "match a .gitignore rule. Committing in this state "
                    "usually surfaces git's 'paths are ignored' error and "
                    "blocks the commit.\n\n"
                    f"Affected:\n{preview}\n\n"
                    "Yes  → run 🧹 Untrack Ignored Files first (recommended)\n"
                    "No   → open the commit dialog anyway\n"
                    "Cancel → close, do nothing",
                    parent=self)
                if choice is None:   # Cancel
                    return
                if choice:           # Yes — untrack first, that flow then
                                     # offers a fresh commit prompt of its own
                    UntrackIgnoredDialog(self, path, stale,
                        reason="tracked but listed in .gitignore "
                               "(blocks commit until untracked)")
                    return
        # No conflicts, OR user chose to proceed anyway
        status_out, _ = self._shell_capture(
            [self._cfg.git_exe, "-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path, self._cfg.git_exe)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit, self._cfg)

    def _offer_commit_after_change(self, path: str, summary_label: str) -> None:
        """After a manager action (Ensure .gitignore, Scaffold, Retrofit, etc.),
        check whether the working tree is dirty and offer a commit dialog if so.

        Called directly by dialogs that hold a reference to App (e.g.
        GitignoreDialog via self._app._offer_commit_after_change). The
        ProjectsTabController has its own copy for internal flows; this one
        serves external callers that go through App.
        """
        if not _is_local_git_repo(path):
            return
        status_out, _ = self._shell_capture(
            [self._cfg.git_exe, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self):
            self._open_commit_dialog(path)
        else:
            self._log("  Working tree left dirty — commit when you're ready.",
                      C["yellow"])

    def _do_git_commit(self, path: str, message: str, selected: list):
        """Stage and commit the picked files. `selected` is a list of
        (filename, xy) tuples from the GitCommitDialog.
        """
        if not selected:
            return
        # Backward-compat: callers passing legacy list-of-strings still
        # work; treat unknown XY as needs-add.
        if selected and isinstance(selected[0], str):
            selected = [(fname, "??") for fname in selected]

        name = os.path.basename(path)
        all_paths = [fname for fname, _xy in selected]
        # xy[1] == ' ' means "no working-tree change" — file is fully
        # captured in the index already (staged D, A, M, R, etc.).
        files_to_add = [fname for fname, xy in selected
                        if len(xy) >= 2 and xy[1] != ' ']

        self._git._git_begin_op()

        def worker():
            try:
                if files_to_add:
                    out, rc = self._shell_capture(
                        [self._cfg.git_exe, "-C", path, "add", "--"] + files_to_add, path)
                    if rc != 0:
                        if "ignored by one of your .gitignore files" in out:
                            offending = [
                                ln.strip() for ln in out.splitlines()
                                if ln.strip() and not ln.strip().startswith(
                                    ("hint:", "The following", "warning:"))]
                            self.after(0, lambda: messagebox.showwarning(
                                "Tracked-but-ignored files",
                                "Some of the files you selected are already "
                                "tracked by git AND match a .gitignore rule. "
                                "Git refuses to re-add them in this state.\n\n"
                                f"Affected paths:\n  "
                                + "\n  ".join(offending[:10])
                                + ("\n  …" if len(offending) > 10 else "")
                                + "\n\nFix: right-click → "
                                "🧹 Untrack Ignored Files… → untrack those "
                                "paths first. Then commit the result.",
                                parent=self))
                        else:
                            self.after(0, lambda: self._log(
                                f"git add failed: {out.strip()}", C["red"]))
                        return

                self._log(f"[{name}] Committing ({len(all_paths)} file"
                          f"{'s' if len(all_paths) != 1 else ''})…",
                          C["peach"])
                commit_cmd = ([self._cfg.git_exe, "-C", path, "commit", "-m", message,
                               "--"] + all_paths)
                cout, crc = self._shell_capture(commit_cmd, path)
                col = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", col))
                self.after(0, self.refresh)
            finally:
                self.after(0, self._git._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_settings(self):
        SettingsDialog(self, self._cfg, self._cfg.save, self._on_settings_saved)

    def _on_settings_saved(self):
        # Phase E: legacy module globals are gone — only need to recompute
        # the cached derived fields (git_exe / codegraph_exe). All controllers
        # and dialogs hold `self._cfg` (the live ManagerConfig instance), so
        # the new values propagate automatically without re-binding.
        self._cfg.refresh_derived()
        self.refresh()
        self._log("Settings saved and applied.", C["green"])


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    """Module-level entry point. Invoked by `python src/app.py` (via the
    Launch TokenSave Manager.bat) and by the Nuitka-bundled .exe.

    Behaviour matches the legacy monolith's __main__ guard:
      • Single-instance lock via _acquire_instance_lock
      • If already running, bring the existing window to front and exit
      • Otherwise, construct App() and enter the Tk main loop
    """
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
