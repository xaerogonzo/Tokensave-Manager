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
import subprocess
import sys
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from constants import (
    AUTO_REFRESH_MS,
    C,
    CREATE_NO_WINDOW,
    LOG_FILE,
    _ANSI,
    _TOKENSAVE_UPDATE_RE,
)
from controllers.ask_tab import AskTabController
from controllers.git_tab import GitTabController
from controllers.help_tab import HelpTabController
from controllers.projects_tab import ProjectsTabController
from controllers.snippets import SnippetsController
from controllers.tasks_tab import TasksController
from controllers.update_poller import UpdatePollerController
from dialogs.git_commit import GitCommitDialog
from dialogs.mcp_config import MCPConfigDialog
from dialogs.settings import SettingsDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog
from helpers.commit_messages import _suggest_commit_message
from helpers.git import _find_tracked_but_ignored, _is_git_repo, _is_local_git_repo
from helpers.mcp import _mcp_configs, _classify_mcp_entry
from helpers.project_discovery import find_projects, get_pinned
from helpers.source_watch import (
    changed_files,
    describe_changes,
    snapshot_sources,
)
from helpers.worktree_health import find_orphaned_worktrees
from helpers.runtime import (
    _acquire_instance_lock,
    _bring_existing_to_front,
    log,
)
from helpers.tray_manager import TrayManager
from state import ManagerConfig


# ── Prompt snippets (Reference tab) ─────────────────────────────────────────

from prompts import PROMPT_SNIPPETS


def _geometry_on_screen(root: "tk.Tk", geom: str) -> bool:
    """Return True if `geom` ("WxH+X+Y") places the window at least partly on screen.

    Uses a conservative single-monitor check via winfo_screen* so we don't
    restore a geometry that's entirely off-screen (e.g. after a monitor was
    disconnected). Allows small negative offsets to tolerate multi-monitor
    left/above arrangements.
    """
    import re as _re
    m = _re.match(r"\d+x\d+\+(-?\d+)\+(-?\d+)$", geom)
    if not m:
        return False
    x, y = int(m.group(1)), int(m.group(2))
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    return x < sw and y < sh and x > -600 and y > -520


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
        saved_geom = self._cfg.raw.get("window_geometry", "")
        if saved_geom and _geometry_on_screen(self, saved_geom):
            self.geometry(saved_geom)
        self._current_proc = None
        self._stop_requested = False
        # Private repo auto-sync concurrency guards (G8)
        self._active_private_syncs: set = set()
        self._pending_private_sync: set = set()
        # Version probe + update-check background loop.
        # _update_poller owns _tokensave_current_version and
        # _tokensave_available_version as properties; App._run() writes
        # them via the controller when it parses opportunistic sync output.
        self._update_poller = UpdatePollerController(
            cfg=self._cfg,
            on_log=self._log,
            on_run=self._run,
            root=self,
        )
        self._update_poller.start()
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {self._cfg.tokensave_exe}")
        log.info(f"  templates: {self._cfg.template_dir}")
        log.info(f"  log file : {LOG_FILE}")
        # ── Worker -> UI channel ──────────────────────────────────────
        # App's background workers used to call self.after() directly. That
        # is a cross-thread Tk call: it usually works on Windows, raises
        # "main thread is not in main loop" when it does not, and on Linux
        # simply BLOCKS with no error and no log line. Workers post here
        # and the pump runs it on the Tk thread. Started before _build so
        # nothing can post into a queue that is not being drained.
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_pump_id = None
        self._style()
        self._build()
        self._start_ui_pump()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray_mgr = TrayManager(self, self._cfg, self._on_tray_quit)
        self._tray_mgr.setup()
        self.protocol("WM_DELETE_WINDOW", self._tray_mgr.hide)
        self.after(300, self._check_config)
        # Staggered after _check_config so the two startup checks' log
        # lines don't interleave mid-write.
        self.after(1200, self._check_worktree_health)
        # Snapshot our own source so an edit made while the manager runs can
        # surface as a banner instead of as "my change did nothing".
        self._src_root = os.path.dirname(os.path.abspath(__file__))
        self._src_baseline = snapshot_sources(self._src_root)
        self._src_banner_dismissed = False
        self.after(self._SRC_CHECK_MS, self._check_source_changed)

    # ── Manager-source change detection ──────────────────────────────────
    #
    # Python does not reload modules, so after editing src/ the running
    # manager keeps executing what it imported at startup. The symptom is
    # never a crash — a feature just behaves like its old self, and the
    # natural conclusion is that the edit did not work.

    _SRC_CHECK_MS = 60_000

    def _check_source_changed(self) -> None:
        try:
            current = snapshot_sources(self._src_root)
            changed = changed_files(self._src_baseline, current)
            if changed and not self._src_banner_dismissed:
                self._show_source_banner(changed)
        finally:
            self.after(self._SRC_CHECK_MS, self._check_source_changed)

    def _show_source_banner(self, changed: list) -> None:
        what = describe_changes(changed, self._src_root)
        self._src_banner_lbl.configure(
            text=f"⚠  Manager source changed since startup ({what}) — "
                 f"restart to load the new code.")
        if not self._src_banner.winfo_ismapped():
            self._src_banner.pack(fill=tk.X, side=tk.TOP, before=self.nb)

    def _dismiss_source_banner(self) -> None:
        """Hide it, and stay hidden.

        Re-raising on every subsequent edit would nag through exactly the
        editing session where the user has already decided to restart later.
        """
        self._src_banner_dismissed = True
        self._src_banner.pack_forget()

    # ── Worker -> UI plumbing ────────────────────────────────────────────

    _UI_PUMP_MS = 50

    def _post(self, fn, *args) -> None:
        """Run *fn(*args)* on the Tk thread. Safe from any thread.

        The one rule for every worker in this file: never touch Tk, post.
        """
        self._ui_queue.put((fn, args))

    def _start_ui_pump(self) -> None:
        self._ui_pump()

    def _ui_pump(self) -> None:
        """Drain whatever the workers posted. Tk thread only."""
        try:
            if not self.winfo_exists():
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
        except queue.Empty:
            pass
        try:
            self._ui_pump_id = self.after(self._UI_PUMP_MS, self._ui_pump)
        except tk.TclError:
            self._ui_pump_id = None

    def report_callback_exception(self, exc, val, tb):
        """Log unhandled exceptions raised inside Tk callbacks.

        Tk's default handler writes to sys.stderr, which is None under
        pythonw.exe / windowed Nuitka builds — so a crash inside a button
        command or `after` callback vanishes silently (e.g. a dialog that
        builds halfway then throws, leaving a blank window). Routing it through
        the manager's logger means every such failure lands in manager.log with
        a full traceback.
        """
        import traceback
        log.error("Unhandled Tk callback exception:\n%s",
                  "".join(traceback.format_exception(exc, val, tb)))

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _on_tray_quit(self) -> None:
        # Release any worker threads waiting on open ProposalDialogs so they
        # don't deadlock when Tk's mainloop ends. Hide-to-tray does NOT
        # trigger this — the user can recover an open proposal via Show.
        try:
            self._ask_ctrl.cancel_all_proposals()
        except AttributeError:
            pass  # ask_ctrl not constructed yet (very early failure path)
        try:
            self._projects.cancel_ai_proposals()
        except AttributeError:
            pass

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

        # ── "source changed, restart" banner ──
        # Packed before the notebook so it appears above the tabs, and stays
        # hidden (pack_forget) until there is something to say.
        self._src_banner = tk.Frame(self, bg=C["peach"])
        self._src_banner_lbl = tk.Label(
            self._src_banner, text="", bg=C["peach"], fg=C["crust"],
            font=("Segoe UI", 9, "bold"), anchor=tk.W, padx=10, pady=4)
        self._src_banner_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(self._src_banner, text="Dismiss", relief=tk.FLAT,
                  bg=C["peach"], fg=C["crust"], bd=0, padx=10,
                  cursor="hand2",
                  command=self._dismiss_source_banner).pack(side=tk.RIGHT)

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
            # Deferred resolution — _ask_ctrl is built two lines below us.
            # The lambda is only invoked when the user clicks Investigate
            # on a refactor scout finding, long after construction.
            on_seed_ask=lambda text, path: self._ask_ctrl.seed_question(text, path),
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
            self.nb, self._cfg, PROMPT_SNIPPETS,
            get_path=self._get_ask_project_path)
        self._help_ctrl = HelpTabController(
            self.nb, self._cfg,
            on_seed_ask=lambda text, path: self._ask_ctrl.seed_question(text, path),
            on_llm_cfg=lambda: self._cfg.raw.get("commit_message_llm", {}),
        )
        from helpers.detection import _root_path
        self._tasks_ctrl = TasksController(
            self.nb, self._cfg,
            get_project_path=lambda: self._projects.get_selected_path(),
            get_known_paths=lambda: [_root_path(r) for r in self._cfg.search_roots],
        )

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        ttk.Button(log_header, text="Cost",
                   command=self._open_cost_viewer).pack(side=tk.RIGHT, padx=(0, 6))

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

    # ── Cost viewer ─────────────────────────────────────────────────────────

    def _open_cost_viewer(self):
        from dialogs.cost_viewer import CostViewerDialog   # lazy import
        CostViewerDialog(self, self._cfg)

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
        if "Tasks" in current_tab_text:
            self._tasks_ctrl.on_tab_selected()
            return
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
    # 🤖 Ask tab     — handled by AskTabController
    # 📚 Reference   — handled by SnippetsController
    # ❓ Help tab    — handled by HelpTabController
    # ═══════════════════════════════════════════════════════════════════

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
        for label, path in _mcp_configs():
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

    def _check_worktree_health(self):
        """Log (never dialog) any git worktree with no tokensave index of its
        own — see helpers/worktree_health.py for why this matters: without
        one, tokensave answers questions asked there using a SIBLING
        checkout's index instead, confidently and about the wrong branch.

        Deliberately quiet — unlike _check_config, this never opens a dialog
        at launch. Real repair is a deliberate action via Doctor (🔍 Doctor →
        one click repairs every orphaned worktree for that project); this
        sweep exists so an orphaned worktree is never silently sitting there
        unnoticed between Doctor runs.
        """
        orphans = find_orphaned_worktrees(
            getattr(self, "projects", None) or [], self._cfg.git_exe)
        if not orphans:
            return
        self._log(
            f"⚠ {len(orphans)} git worktree"
            f"{'s' if len(orphans) != 1 else ''} found with no tokensave "
            "index of its own — run 🔍 Doctor on the parent project to "
            "repair:", C["peach"])
        for o in orphans:
            self._log(
                f"    {o['project_name']}: '{o['branch'] or o['head']}' "
                f"at {o['worktree_path']}", C["overlay0"])

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
            self._post(self._set_running, True, label)
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
                        self._update_poller.current_version = cur_v
                        self._update_poller.available_version = new_v
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
                    elif args and args[0] == "upgrade":
                        # Auto-run integration check immediately after upgrade
                        self._post(self._update_poller.cmd_integration_check)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self._post(self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self._post(self._set_running, False)
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
            _suggestion = _suggest_commit_message(cwd, status_out, self._cfg.raw, self._cfg.git_exe, mc=self._cfg)
            ai_msg = _suggestion.message or "chore: tokensave sync"
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

    # ── Update poller (version probe + GitHub update-check) ─────────────────

    @property
    def _tokensave_current_version(self) -> str | None:
        """Backward-compat accessor for SettingsDialog."""
        return self._update_poller.current_version

    @property
    def _tokensave_available_version(self) -> str | None:
        """Backward-compat accessor for SettingsDialog."""
        return self._update_poller.available_version

    def cmd_upgrade_tokensave(self):
        """Thin wrapper — delegates to UpdatePollerController."""
        self._update_poller.cmd_upgrade()

    def cmd_integration_check(self):
        """Run the tokensave integration check script and show results.

        Delegates to UpdatePollerController which manages the subprocess
        and result dialog. Source-only; shows a friendly info dialog when
        running from the compiled exe (script not shipped in dist/).
        """
        self._update_poller.cmd_integration_check()

    # ── Commit dialog (shared by Projects context menu, Git tab, and
    #    offer-commit-after-change flow) ──────────────────────────────────────

    def _open_commit_dialog(self, path: str, skip_stale_check: bool = False):
        """Open GitCommitDialog for a given project path. Reused by
        `cmd_git_commit` (Projects-tab right-click) AND by the
        offer-commit-after-change flow that runs after Ensure .gitignore,
        Shadow Links, Scaffold, and Retrofit.

        Pre-flight check: if the project has tracked-but-ignored files,
        offer to run Untrack Ignored Files FIRST. Otherwise the commit
        attempt would inevitably hit git's "paths are ignored" error and
        the user would have to come back and untrack anyway.

        ``skip_stale_check=True`` bypasses this pre-flight when the caller
        has just run Untrack Ignored Files — any remaining stale files are
        ones the user deliberately chose to keep tracked, and re-prompting
        would create an infinite loop.
        """
        if not skip_stale_check and _is_local_git_repo(path):
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
                               "(blocks commit until untracked)",
                        on_confirm=self._projects._gitops._do_untrack_ignored)
                    return
        # No conflicts, OR user chose to proceed anyway
        status_out, _ = self._shell_capture(
            [self._cfg.git_exe, "-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path, self._cfg.git_exe)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit, self._cfg)

    def _offer_commit_after_change(self, path: str, summary_label: str,
                                   skip_stale_check: bool = False) -> None:
        """After a manager action (Ensure .gitignore, Scaffold, Retrofit, etc.),
        check whether the working tree is dirty and offer a commit dialog if so.

        Called directly by dialogs that hold a reference to App (e.g.
        GitignoreDialog via self._app._offer_commit_after_change). The
        ProjectsTabController has its own copy for internal flows; this one
        serves external callers that go through App.

        ``skip_stale_check=True`` is forwarded to ``_open_commit_dialog`` to
        prevent an infinite untrack→commit→stale-check→untrack loop when the
        caller has just finished running Untrack Ignored Files.
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
            self._open_commit_dialog(path, skip_stale_check=skip_stale_check)
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

        # Defensive guard: a pathspec commit (`git commit -- <paths>`) silently
        # drops any staged deletion not in <paths>. Fold currently-staged
        # deletions into the pathspec so they're never orphaned. (Ignored-file
        # deletions are handled durably by the untrack flow's immediate commit,
        # so they normally won't reach here.)
        from helpers.git import _staged_deletions
        for _d in _staged_deletions(path, self._cfg.git_exe):
            if _d not in all_paths:
                all_paths.append(_d)

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
                            self._post(lambda: messagebox.showwarning(
                                "Tracked-but-ignored files",
                                "Some of the files you selected are already "
                                "tracked by git AND match a .gitignore rule. "
                                "Git refuses to re-add them in this state.\n\n"
                                "Affected paths:\n  "
                                + "\n  ".join(offending[:10])
                                + ("\n  …" if len(offending) > 10 else "")
                                + "\n\nFix: right-click → "
                                "🧹 Untrack Ignored Files… → untrack those "
                                "paths first. Then commit the result.",
                                parent=self))
                        else:
                            self._post(lambda: self._log(
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
                    self._post(lambda l=line: self._log(f"  {l}", col))
                self._post(self.refresh)
                if crc == 0:
                    # Auto-sync private repo if one is configured (G8: scheduled
                    # on main thread so the dirty-bit logic runs thread-safely)
                    self._post(lambda: self._start_private_sync(path, message))
            finally:
                self._post(self._git._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def _start_private_sync(self, path: str, commit_msg: str = "") -> None:
        """Schedule a private repo sync, queuing if one is already running (G8)."""
        if path in self._active_private_syncs:
            # Don't drop — mark as pending so it runs right after (G8 fix)
            self._pending_private_sync.add(path)
            return
        cfg_entry = self._cfg.raw.get("private_repos", {}).get(path)
        if not cfg_entry:
            return
        self._active_private_syncs.add(path)

        def worker():
            from helpers.private_repo import sync_private_repo
            dest  = cfg_entry.get("dest", "")
            files = cfg_entry.get("files", [])
            result = sync_private_repo(
                self._cfg.git_exe, path, dest, files,
                on_log=self._log,
                commit_msg=commit_msg,
            )
            if not result and result.reason:
                self._log(f"  Detail: {result.reason}", "red")
            # After finishing, check if another sync was queued (G8)
            self._post(lambda: self._finish_private_sync(path))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_private_sync(self, path: str) -> None:
        """Called on main thread after a private sync thread completes (G8)."""
        self._active_private_syncs.discard(path)
        if path in self._pending_private_sync:
            self._pending_private_sync.discard(path)
            # Run the deferred sync (no commit msg — changes already committed)
            self._start_private_sync(path, "")

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
