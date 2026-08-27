"""ProjectsTabController — owns the 📁 Projects tab.

Decoupled from App via 11 explicit callbacks. No back-reference to App.
Owns the project Treeview, the context menu, the per-project commands
(sync, status, doctor, codegraph, retrofit, shadow links, etc.), and the
background git-status column refresh.

Per Round 4 plan rules:
  - `self._cfg.X` properties (tokensave_exe / template_dir / git_exe /
    codegraph_exe / basic_instructions_template / baseline_include_line /
    search_roots) read at execution time inside every method (Rule 3) —
    a Settings save propagates immediately without restart.
  - Raw-dict reads / mutations go through `self._cfg.raw` — keeps the
    only writable surface explicit.  `project_categories` writes call
    `self._cfg.save()` afterward (no refresh_derived() needed — raw
    fields don't back any @property).
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from theme import _Tooltip
from constants import C
from helpers.detection import _root_label
from helpers.git import (
    _format_git_status_cell,
    _is_local_git_repo,
)
from helpers.project_discovery import (
    fmt_age,
)
from controllers.codegraph_ctrl import CodeGraphController
from controllers.doctor_ctrl import DoctorController
from controllers.housekeeping_ctrl import HousekeepingController
from controllers.scaffold_ctrl import ScaffoldRetrofitController
from controllers.sync_ctrl import SyncStatusController
from controllers.fileops_ctrl import FileOpsController
from controllers.git_ops_ctrl import GitOpsController
from controllers.shadowlinks_ctrl import ShadowLinksController
from controllers.ai_tasks_ctrl import AITasksController
from controllers.project_sync_ctrl import ProjectSyncCtrl
from controllers.command_bar_ctrl import CommandBarCtrl
from dialogs.assign_category import AssignCategoryDialog

if TYPE_CHECKING:
    from state import ManagerConfig


#: The two faces of one menu entry. Module-level so the tests can assert
#: against the same strings the menu uses.
_STRICT_TREE_ON_LABEL = "🛡  Enable strict_tree…"
_STRICT_TREE_OFF_LABEL = "🛡  Disable strict_tree…"


class ProjectsTabController:

    #: (submenu, entry index) for the strict_tree toggle, set by
    #: _build_context_menu. None until the menu exists -- callers that
    #: run before it (or against a stubbed menu) have nothing to relabel.
    _strict_tree_entry = None
    """Owns the Projects tab UI and all per-project commands.

    No back-reference to App — all cross-App dependencies flow through the
    explicit callbacks passed at construction time.  At most 10 callbacks;
    if more are needed, revisit with an EventBus or shared-state bus.

    Thread safety rule:
      • Methods named *_worker run on a background daemon thread.
        They MUST NOT call Tkinter directly — use self._tab.after(0, ...).
      • All other methods run on the main thread and may call Tkinter freely.

    Log rule:
      • Worker threads call self._on_log(msg, color) — App._log already
        schedules self.after(0, ...) internally, so it is thread-safe.
      • Direct Tkinter widget calls (tree, menu) must be on the main thread only.

    God-class extraction status (51 methods — Roadmap-8 targets):
      ✅ ScaffoldRetrofitController  (scaffold_ctrl.py)   — extracted Round 5
      ✅ SyncStatusController        (sync_ctrl.py)        — extracted Round 5
      ⬜ GitStatusController         — git-column refresh, _parse_git_status_v2,
                                       _format_git_status_cell, _update_git_status_cell
      ⬜ AiTasksController           (ai_tasks_ctrl.py)   — verify delegation complete
      ⬜ CommandBarController        — right-click menu, _on_right_click, cmd_* actions
    """

    # ── Class-level constants ─────────────────────────────────────────────────

    # Set of tag names used as Git-status overrides on project rows.
    # _update_git_status_cell strips these before applying the new tag.
    _GIT_STATUS_TAGS = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(
        self,
        notebook: ttk.Notebook,
        cfg: "ManagerConfig",
        get_projects,          # () -> list[dict]
        on_run,                # (args, cwd, label) -> None  (App._run, threaded)
        on_run_capture,        # (args, cwd, label) -> (raw, rc, elapsed)  (sync, from thread)
        on_shell,              # (cmd, cwd, env=None) -> (out, rc)  (sync, from thread)
        on_log,                # (msg, colour) -> None  (thread-safe)
        on_commit,             # (path) -> None  (App._open_commit_dialog)
        on_refresh,            # () -> None  (App.refresh — full tree rebuild)
        on_project_select,     # (path) -> None  (fired on row click)
        on_set_running,        # (running: bool, label: str) -> None  (App._set_running)
        on_settings,           # () -> None  (App.cmd_settings)
        on_seed_ask=None,      # (text, path) -> None — refactor-scout Investigate;
                               # passed as a lambda from App because AskTabController
                               # doesn't exist yet at our construction time.
    ):
        self._notebook       = notebook
        self._cfg            = cfg
        self._get_projects   = get_projects
        self._on_run         = on_run
        self._on_run_capture = on_run_capture
        self._on_shell       = on_shell
        self._on_log         = on_log
        self._on_commit      = on_commit
        self._on_refresh     = on_refresh
        self._on_project_select = on_project_select
        self._on_set_running = on_set_running
        self._on_settings    = on_settings
        self._on_seed_ask    = on_seed_ask

        # ── Sub-controllers ───────────────────────────────────────────────────
        # Constructed after self._tab exists (codegraph_ctrl needs the frame).
        # Deferred until after _build_projects_tab() below so _tab is ready.
        self._codegraph: CodeGraphController | None = None  # set in _build_projects_tab

        # Controller-level subprocess tracking (parallel to App._current_proc)
        # Workers managed by THIS controller set/clear this attribute so that
        # App._auto_refresh can see whether the controller is busy.
        self.current_proc: object = None          # subprocess.Popen | None
        # Git-status column refresh — delegated to ProjectSyncCtrl
        self._git_status_refresh_cancel: bool = False   # kept for compat
        self._git_status_refresh_running: bool = False  # kept for compat

        self._tree: ttk.Treeview | None = None
        self._ctx_menu: tk.Menu | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Projects  ")

        self._codegraph = CodeGraphController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_shell=on_shell,
            on_refresh=on_refresh,
            on_commit_offer=self._offer_commit_after_change,
            on_settings=on_settings,
        )
        self._doctor = DoctorController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_set_running=on_set_running,
            on_set_proc=lambda p: setattr(self, "current_proc", p),
        )
        self._scaffold = ScaffoldRetrofitController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_set_running=on_set_running,
            on_set_proc=lambda p: setattr(self, "current_proc", p),
            on_refresh=on_refresh,
            on_commit_offer=self._offer_commit_after_change,
            on_insert_pending=self._insert_pending_row,
        )
        self._sync = SyncStatusController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_set_running=on_set_running,
            on_set_proc=lambda p: setattr(self, "current_proc", p),
            on_refresh=on_refresh,
            on_run=on_run,
            on_run_capture=on_run_capture,
            get_projects=get_projects,
        )
        self._gitops = GitOpsController(
            tab=self._tab,
            notebook=notebook,
            cfg=cfg,
            on_log=on_log,
            on_shell=on_shell,
            on_refresh=on_refresh,
            on_commit_offer=self._offer_commit_after_change,
            on_commit=on_commit,
            on_project_select=on_project_select,
        )
        self._fileops = FileOpsController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_refresh=on_refresh,
        )
        self._shadowlinks = ShadowLinksController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_refresh=on_refresh,
            on_run_capture=on_run_capture,
            on_commit_offer=self._offer_commit_after_change,
        )
        self._ai_tasks = AITasksController(
            tab=self._tab,
            cfg=cfg,
            on_log=on_log,
            on_commit_offer=self._offer_commit_after_change,
            on_seed_ask=self._on_seed_ask,
        )

        # Constructed after DoctorController: housekeeping delegates every
        # tokensave-doctor invocation to it rather than shelling out itself.
        self._housekeeping = HousekeepingController(
            root=self._tab.winfo_toplevel(),
            cfg=cfg,
            doctor=self._doctor,
            on_log=on_log,
        )

        self._git_status = ProjectSyncCtrl(
            tab=self._tab,
            cfg=cfg,
            on_shell=on_shell,
            get_tree=lambda: self._tree,
        )

        self._cmd_bar = CommandBarCtrl(
            get_path=self._selected_path,
            require_tokensave=self._require_tokensave,
            sync=self._sync,
            doctor=self._doctor,
            codegraph=self._codegraph,
            gitops=self._gitops,
            fileops=self._fileops,
            shadowlinks=self._shadowlinks,
            scaffold=self._scaffold,
            ai_tasks=self._ai_tasks,
            housekeeping=self._housekeeping,
        )
        # Tab UI + context menu are built last, after all sub-controllers and
        # CommandBarCtrl exist, so the toolbar buttons and menu items can bind
        # to self._cmd_bar.cmd_* directly. The sub-controllers only need
        # self._tab (already created); ProjectSyncCtrl reads self._tree lazily.
        self._build_projects_tab()
        self._build_context_menu()

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def _root(self) -> tk.Tk:
        """The top-level App window — use for dialog parenting."""
        return self._tab.winfo_toplevel()

    def get_selected_path(self) -> str | None:
        """Return the path of the currently selected project row, or None."""
        if self._tree is None:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("proj:"):
            return iid[5:]
        return None

    def get_selected_paths(self) -> list:
        """Every selected project path, in tree order.

        Category rows and the placeholder rows are skipped, so a shift-select
        spanning a group header yields only the real projects inside it.
        """
        if self._tree is None:
            return []
        return [iid[5:] for iid in self._tree.selection()
                if iid.startswith("proj:")]

    def cancel_ai_proposals(self) -> None:
        """Forward shutdown cancellation to AITasksController."""
        self._ai_tasks.cancel_all_proposals()

    def stop(self):
        """Cancel any in-flight controller worker (called by App._stop_current)."""
        self._sync.request_stop()
        proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # ── Tree population (called by App.refresh) ───────────────────────────────

    def rebuild_tree(self, projects: list, active_path: str | None,
                     pinned: str | None) -> None:
        """Clear and repopulate the Treeview from a fresh project list.

        Called by App.refresh() after it has computed projects / active_path.
        Does NOT kick off the git-status refresh — App.refresh() does that.
        """
        if self._tree is None:
            return
        for item in self._tree.get_children():
            self._tree.delete(item)

        proj_cats = self._cfg.raw.get("project_categories", {})

        groups: dict = {}
        for p in projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self._tree.insert("", tk.END, iid=ciid, text=cat,
                                  open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self._tree.exists(siid):
                    self._tree.insert(parent, tk.END, iid=siid,
                                      text=f"  ↳ {subcat}",
                                      open=True, tags=("subcategory",))
                parent = siid

            for p in projs:
                is_active    = (p["path"] == active_path)
                has_scaffold = self._has_scaffold(p["path"])
                has_ts       = p.get("has_tokensave", True)
                has_git      = p.get("has_git", False)
                has_cg       = p.get("has_codegraph", False)
                if is_active:
                    base_tag = "active"
                elif not has_ts:
                    base_tag = "git_only"
                elif not has_scaffold:
                    base_tag = "scaffold"
                else:
                    base_tag = "normal"
                synced_str = fmt_age(p["mtime"]) if has_ts else "—"
                cg_text = "✓" if has_cg else "—"
                git_text, git_tag = _format_git_status_cell(
                    p.get("git_status"), has_git)
                tags = (base_tag, git_tag) if has_git else (base_tag, "git_none")
                piid = f"proj:{p['path']}"
                self._tree.insert(parent, tk.END, iid=piid,
                                  text=p["name"],
                                  values=("★" if is_active else "",
                                          p["path"],
                                          synced_str,
                                          cg_text,
                                          git_text,
                                          "✔" if has_scaffold else "—"),
                                  tags=tags)

    def refresh_git_status_column(self, projects: list) -> None:
        """Delegate to ProjectSyncCtrl (Phase C3 extraction)."""
        self._git_status.refresh(projects)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _has_scaffold(path: str) -> bool:
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def _selected_path(self) -> str | None:
        """Return the selected project path, or show a warning and return None."""
        sel = self._tree.selection() if self._tree else ()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.",
                                   parent=self._root)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).",
                parent=self._root)
            return None
        return iid[5:]

    def _require_tokensave(self, path: str) -> bool:
        if os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db")):
            return True
        name = os.path.basename(path)
        messagebox.showinfo(
            "Not indexed with tokensave",
            f"'{name}' is a git project but doesn't have a tokensave index yet.\n\n"
            "Tokensave builds a code-graph that lets Claude navigate your project "
            "efficiently without reading every file.\n\n"
            "To add it:  right-click → ⚙ Retrofit…  and tick "
            "'Add tokensave @include + init'.",
            parent=self._root)
        return False

    # _require_codegraph_installed was moved to CodeGraphController._require_installed

    def _offer_commit_after_change(self, path: str, summary_label: str) -> None:
        """After a manager action, check if tree is dirty and offer to commit."""
        if not _is_local_git_repo(path):
            return
        status_out, _ = self._on_shell(
            [self._cfg.git_exe, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._on_log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self._root):
            self._on_commit(path)
        else:
            self._on_log("  Working tree left dirty — commit when you're ready.",
                         C["yellow"])

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_projects_tab(self) -> None:
        tab = self._tab

        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        # F5: the Git tab explains its buttons on hover and this tab did not,
        # which teaches a user that hovering works and then leaves the tabs
        # carrying the unlabelled glyph columns silent.
        btn_scaffold = ttk.Button(btns, text="＋  Scaffold",
                                  style="Action.TButton",
                                  command=self._cmd_bar.cmd_scaffold)
        btn_scaffold.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_scaffold,
                 "Create a NEW project folder from a template, with a\n"
                 "tokensave index and starter files already set up.\n\n"
                 "For a folder you already have, use \u201cAdd tokensave to\n"
                 "a project\u201d instead \u2014 this one starts from nothing.")

        btn_retrofit = ttk.Button(btns, text="⚙  Add tokensave to a project",
                                  command=self._cmd_bar.cmd_retrofit)
        btn_retrofit.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_retrofit,
                 "Point at a folder you already have and give it a tokensave\n"
                 "index, so its code becomes searchable from here and from\n"
                 "your AI agent.\n\n"
                 "Adds a .tokensave/ directory. Your own files are not moved\n"
                 "or edited.")

        btn_sync_all = ttk.Button(btns, text="↺↺  Sync All",
                                  command=self._cmd_bar.cmd_sync_all)
        btn_sync_all.pack(side=tk.LEFT)
        _Tooltip(btn_sync_all,
                 "Re-index EVERY project in the list, one after another.\n\n"
                 "Can take several minutes on a long list. A single project\n"
                 "can be synced from its right-click menu instead.")

        btn_refresh = ttk.Button(btns, text="⟳  Refresh",
                                 command=self._on_refresh)
        btn_refresh.pack(side=tk.RIGHT, padx=(0, 6))
        _Tooltip(btn_refresh,
                 "Re-read this list from disk: git status, index age and\n"
                 "CodeGraph state.\n\n"
                 "Reads only \u2014 it does not sync or change anything.")

        btn_settings = ttk.Button(btns, text="Settings",
                                  command=self._on_settings)
        btn_settings.pack(side=tk.RIGHT, padx=(0, 6))
        _Tooltip(btn_settings,
                 "Paths to git and the tools, AI backends, and which checks\n"
                 "run before a commit or push.")

        # Legend + hint. The Git column encodes eight states as glyphs whose
        # only other distinction is colour, which is invisible to a
        # colour-blind user and unlabelled for everyone else. The hint sits
        # beside it at the same weight, since it was previously the sole
        # pointer to every one of the 32 right-click commands and was styled
        # as the least readable text on the tab.
        legend = tk.Frame(tab, bg=C["base"])
        legend.pack(fill=tk.X, padx=14, pady=(2, 0), side=tk.BOTTOM)
        tk.Label(legend, text="Right-click a project for actions",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 ).pack(side=tk.RIGHT)
        tk.Label(legend,
                 text=("Git:  ✓ clean   ● uncommitted changes   "
                       "↑n to push   ↓n to pull   — no git   … checking"
                       + "\n" +
                       "Dimmed row = no tokensave index yet "
                       "(right-click → Maintenance → Retrofit…)"),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, anchor=tk.W,
                 ).pack(side=tk.LEFT)

        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "cg", "git", "scaffold"),
            show="tree headings",
            # "extended" enables ctrl/shift multi-select so maintenance ops
        # can run across several projects at once. Single-select
        # behaviour is unchanged for every command that needs one path.
        selectmode="extended",
        )
        self._tree.heading("#0",       text="Project")
        self._tree.heading("active",   text="")
        self._tree.heading("path",     text="Path")
        self._tree.heading("synced",   text="Last Synced")
        self._tree.heading("cg",       text="CodeGraph")
        self._tree.heading("git",      text="Git")
        self._tree.heading("scaffold", text="Scaffold")

        self._tree.column("#0",       width=170, stretch=False)
        self._tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self._tree.column("path",     width=220)
        self._tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self._tree.column("cg",       width=72,  stretch=False, anchor=tk.CENTER)
        self._tree.column("git",      width=60,  stretch=False, anchor=tk.CENTER)
        self._tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.tag_configure("active",      foreground=C["green"])
        self._tree.tag_configure("normal",      foreground=C["text"])
        self._tree.tag_configure("scaffold",    foreground=C["peach"])
        self._tree.tag_configure("git_only",    foreground=C["overlay0"])
        self._tree.tag_configure("pending",     foreground=C["yellow"])
        self._tree.tag_configure("git_clean",   foreground=C["green"])
        self._tree.tag_configure("git_dirty",   foreground=C["yellow"])
        self._tree.tag_configure("git_ahead",   foreground=C["sky"])
        self._tree.tag_configure("git_behind",  foreground=C["red"])
        self._tree.tag_configure("git_mixed",   foreground=C["peach"])
        self._tree.tag_configure("git_pending", foreground=C["overlay0"])
        self._tree.tag_configure("git_none",    foreground=C["overlay0"])
        self._tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("subcategory", foreground=C["lavender"])

        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _submenu(self, parent) -> tk.Menu:
        """A themed cascade child, styled like its parent."""
        return tk.Menu(parent, tearoff=0,
                       bg=C["surface0"], fg=C["text"],
                       activebackground=C["surface1"],
                       activeforeground=C["text"],
                       relief=tk.FLAT, bd=0, font=("Segoe UI", 10))

    def _build_context_menu(self) -> None:
        """Group 38 commands into cascades.

        Flat, this menu ran past the bottom of a laptop screen, and finding
        one entry meant reading ~20 unrelated labels. Grouping is presentation
        only — every command is the same delegate it was.

        The four everyday actions stay at the top level, because burying a
        one-click Sync inside a submenu would make the common case worse to
        pay for the rare one. Destructive entries are gathered under
        Maintenance rather than sitting beside read-only ones.
        """
        m = tk.Menu(self._root, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))

        # ── Everyday actions, kept one click away ──────────────────────────
        m.add_command(label="★  Set as Active", command=self._cmd_bar.cmd_set_active)
        m.add_command(label="↺  Sync",          command=self._cmd_bar.cmd_sync)
        m.add_command(label="📊  Status",        command=self._cmd_bar.cmd_status)
        m.add_separator()

        # ── Index ──────────────────────────────────────────────────────────
        index_m = self._submenu(m)
        index_m.add_command(label="⟳  Force Re-sync",
                            command=self._cmd_bar.cmd_force_sync)
        index_m.add_command(label="🔍  Doctor", command=self._cmd_bar.cmd_doctor)
        index_m.add_command(label="🧹  Housekeeping…",
                            command=self._cmd_bar.cmd_housekeeping)
        index_m.add_command(label=_STRICT_TREE_ON_LABEL,
                            command=self._toggle_strict_tree_selected)
        # The label depends on the selected project, and the menu is built
        # once — so keep a handle on the entry and restate it at popup time.
        self._strict_tree_entry = (index_m, index_m.index("end"))
        index_m.add_command(label="🔌  Bind to this project…",
                            command=self._bind_project_selected)
        index_m.add_separator()
        index_m.add_command(label="🔗  Shadow Links…",
                            command=self._cmd_bar.cmd_shadow_links)
        index_m.add_command(label="🔄  Integration check",
                            command=self.cmd_integration_check)
        m.add_cascade(label="🗂  Index", menu=index_m)

        # ── CodeGraph ──────────────────────────────────────────────────────
        cg_m = self._submenu(m)
        cg_m.add_command(label="Init",   command=self._cmd_bar.cmd_codegraph_init)
        cg_m.add_command(label="Sync",   command=self._cmd_bar.cmd_codegraph_sync)
        cg_m.add_command(label="Status", command=self._cmd_bar.cmd_codegraph_status)
        cg_m.add_separator()
        cg_m.add_command(label="Reindex (Full)…",
                         command=self._cmd_bar.cmd_codegraph_reindex)
        cg_m.add_command(label="Remove CodeGraph Index…",
                         command=self._cmd_bar.cmd_codegraph_remove)
        m.add_cascade(label="🧠  CodeGraph", menu=cg_m)

        # ── Git ────────────────────────────────────────────────────────────
        git_m = self._submenu(m)
        git_m.add_command(label="📜  Git Log",   command=self._cmd_bar.cmd_git_log)
        git_m.add_command(label="📝  Commit…",   command=self._cmd_bar.cmd_git_commit)
        git_m.add_command(label="🔧  Git Init",  command=self._cmd_bar.cmd_git_init)
        git_m.add_separator()
        git_m.add_command(label="📋  Manage .gitignore…",
                          command=self._cmd_bar.cmd_manage_gitignore)
        git_m.add_command(label="🧹  Untrack Ignored Files…",
                          command=self._cmd_bar.cmd_untrack_ignored)
        git_m.add_command(label="🔒  Private Repo…",
                          command=self._cmd_bar.cmd_private_repo)
        m.add_cascade(label="🌿  Git", menu=git_m)

        # ── AI & docs ──────────────────────────────────────────────────────
        ai_m = self._submenu(m)
        ai_m.add_command(label="🔍  AI Code Review…",
                         command=self._cmd_bar.cmd_ai_code_review)
        ai_m.add_command(label="🔍  Pre-commit AI Review hook…",
                         command=self._cmd_bar.cmd_precommit_hook)
        ai_m.add_separator()
        ai_m.add_command(label="📝  Doc Updates… (CHANGELOG + README)",
                         command=self.cmd_doc_updates)
        ai_m.add_command(label="📋  Roadmap…", command=self.cmd_roadmap_manager)
        ai_m.add_command(label="🔬  Refactor scout…",
                         command=self._cmd_bar.cmd_refactor_scout)
        ai_m.add_command(label="✓  Run checks…",
                         command=self._cmd_bar.cmd_run_checks)
        m.add_cascade(label="🤖  AI & docs", menu=ai_m)

        # ── Open ───────────────────────────────────────────────────────────
        open_m = self._submenu(m)
        open_m.add_command(label="📂  Open Folder",
                           command=self._cmd_bar.cmd_open_folder)
        open_m.add_command(label="✏   Open in Editor",
                           command=self._cmd_bar.cmd_open_editor)
        open_m.add_command(label="⎘  Copy Path",
                           command=self._cmd_bar.cmd_copy_path)
        open_m.add_separator()
        open_m.add_command(label="⚙   Generate VS Code tasks…",
                           command=self.cmd_generate_vscode_tasks)
        m.add_cascade(label="📂  Open", menu=open_m)

        # ── Maintenance — everything destructive or structural ─────────────
        maint_m = self._submenu(m)
        maint_m.add_command(label="⚙  Retrofit…",
                            command=self._cmd_bar.cmd_retrofit_selected)
        maint_m.add_command(label="📁  Assign Category…",
                            command=self.cmd_assign_category)
        maint_m.add_command(label="Auto-detect", command=self._cmd_bar.cmd_auto)
        maint_m.add_separator()
        maint_m.add_command(label="🗑  Remove Index…",
                            command=self._cmd_bar.cmd_remove)
        m.add_cascade(label="🔧  Maintenance", menu=maint_m)

        self._ctx_menu = m

    def _on_right_click(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        # Preserve an existing multi-selection when right-clicking inside it.
        # Unconditionally calling selection_set() here would collapse the
        # selection to one row, making multi-select unusable via the menu.
        if row not in self._tree.selection():
            self._tree.selection_set(row)
        if not row.startswith("proj:"):
            return
        paths = self.get_selected_paths()
        if len(paths) > 1:
            # A separate menu, deliberately: showing single-project commands
            # over a 3-project selection would invite clicking "Git Commit…"
            # and having it act on exactly one of them, silently.
            self._show_batch_menu(event, paths)
            return
        self._sync_strict_tree_label(paths[0] if paths else "")
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _open_cross_project_search(self, paths: list) -> None:
        """Open the multi-project search over the current selection.

        Lives here rather than in CommandBarCtrl, which is deliberately a pure
        delegate with no Tk import — opening a dialog needs a parent window.
        """
        from dialogs.cross_project_search import CrossProjectSearchDialog
        if len(paths) < 2:
            return          # one project is what the Ask tab is already for
        CrossProjectSearchDialog(self._root, paths, self._cfg)

    def _bind_project_selected(self) -> None:
        """Open the MCP dialog focused on this project's binding.

        Deliberately a navigation action, not a write. `MCPConfigDialog`
        documents itself as the one place that mutates Claude's MCP
        configs -- it owns the diff, the timestamped backup and the
        per-row Apply -- and a second write path here would duplicate all
        three while quietly making that claim false.
        """
        path = self._selected_path()
        if not path:
            return
        from dialogs.mcp_config import MCPConfigDialog
        MCPConfigDialog(self._root, self._cfg, focus_project=path)

    def _sync_strict_tree_label(self, path: str) -> None:
        """Point the entry at whichever direction is actually available.

        Read at popup time rather than when the menu is built: the menu is
        constructed once and reused for every project, so a value captured
        at build time would be both stale and wrong for most rows. One
        small file read per right-click.

        An unreadable or absent config reads as "not on", which offers
        Enable — and the writer then refuses with a specific reason. That
        is the right way round: offering Disable for a project whose state
        we could not determine would be asserting a fact we do not have.
        """
        if not self._strict_tree_entry:
            return
        menu, index = self._strict_tree_entry
        label = _STRICT_TREE_ON_LABEL
        if path:
            try:
                from helpers.tokensave_config import read_strict_tree
                if read_strict_tree(path).is_enabled:
                    label = _STRICT_TREE_OFF_LABEL
            except Exception:                              # noqa: BLE001
                pass          # a mislabelled entry must not eat the menu
        try:
            menu.entryconfigure(index, label=label)
        except tk.TclError:
            pass

    def _toggle_strict_tree_selected(self) -> None:
        """Single-project entry point, in whichever direction applies.

        The batch form was the only way in, which meant a user who never
        multi-selects could not reach it at all -- and the Doctor was
        telling them to turn it on. It is a toggle rather than an enable
        because the confirmation dialog has always said "turn it off again
        if it refuses something it should not", and until now there was no
        way to do that short of hand-editing config.json.
        """
        path = self._selected_path()
        if not path:
            return
        from helpers.tokensave_config import read_strict_tree
        self._set_strict_tree([path], not read_strict_tree(path).is_enabled)

    def _set_strict_tree(self, paths: list, enabled: bool = True) -> None:
        """Write tokensave's strict_tree across the selection.

        Offered as a batch as well as singly: it is per-project and was
        enabled nowhere, and a one-at-a-time toggle across sixteen projects
        does not get used.

        With it on, a tokensave call that would be answered from the wrong
        tree fails with an error naming both roots, instead of prefixing a
        warning to an answer it returns anyway. It covers a worktree
        resolving someone else's index and a server still serving the branch
        it started on, and spares only `tokensave_status` — the one tool a
        refused caller needs in order to understand the refusal.

        Confirmed rather than silent: this writes into each project's own
        `.tokensave/config.json`.
        """
        from helpers.tokensave_config import set_strict_tree
        n = len(paths)
        if enabled:
            title, body = "Enable strict_tree", (
                "Turn on tokensave's strict_tree for %d project%s?\n\n"
                "With it on, a tokensave query that would be answered from "
                "the wrong checkout fails with an error naming both trees, "
                "instead of returning a plausible answer about a project you "
                "are not in.\n\n"
                "It is opt-in upstream because sharing one index across a "
                "family of worktrees is a legitimate setup \u2014 so turn it "
                "off again from this same menu entry if it refuses something "
                "it should not.\n\n"
                "This edits each project's .tokensave/config.json. Projects "
                "without a tokensave index are skipped."
                % (n, "" if n == 1 else "s"))
        else:
            title, body = "Disable strict_tree", (
                "Turn OFF tokensave's strict_tree for %d project%s?\n\n"
                "With it off, a query that resolves an index from another "
                "checkout goes back to returning a plausible answer with a "
                "warning attached, rather than failing outright.\n\n"
                "That is the right choice if it is refusing something "
                "legitimate \u2014 sharing one index across a family of "
                "worktrees is a supported setup, and upstream ships this "
                "off for exactly that reason.\n\n"
                "This edits each project's .tokensave/config.json."
                % (n, "" if n == 1 else "s"))
        if not messagebox.askyesno(title, body, parent=self._root):
            return

        changed = skipped = failed = 0
        verb = "Enabling" if enabled else "Disabling"
        self._on_log("%s strict_tree across %d project%s…"
                     % (verb, n, "" if n == 1 else "s"), C["blue"])
        for path in paths:
            ok, detail = set_strict_tree(path, enabled)
            name = os.path.basename(path) or path
            if not ok:
                # Refusals are expected and informative (no index yet, or an
                # unparseable config the manager must not rewrite), so they
                # are reported per project rather than collapsed into a count.
                failed += 1
                self._on_log("  ✗ %s — %s" % (name, detail), C["peach"])
            elif "already" in detail:
                skipped += 1
            else:
                changed += 1
                self._on_log("  ✓ %s" % name, C["green"])
        self._on_log(
            "  strict_tree: %d %s, %d already %s, %d skipped"
            % (changed, "enabled" if enabled else "disabled",
               skipped, "on" if enabled else "off", failed),
            C["green"] if not failed else C["peach"])
        self._on_refresh()

    def _show_batch_menu(self, event, paths: list) -> None:
        """Context menu for a multi-project selection.

        Only operations that stream to the log and open nothing are offered.
        Status uses the log-only variant — the single-project command shows a
        popup, and one popup per project is not a feature. Doctor is absent on
        purpose: it schedules follow-up dialogs that would stack per project.
        """
        n = len(paths)
        m = tk.Menu(self._root, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label=f"{n} projects selected", state=tk.DISABLED)
        m.add_separator()
        m.add_command(label=f"↺  Sync all {n}",
                      command=lambda: self._cmd_bar.cmd_batch(paths, "sync"))
        m.add_command(label=f"⟳  Force Re-sync all {n}…",
                      command=lambda: self._cmd_bar.cmd_batch(paths, "force"))
        m.add_command(label=f"📊  Status of all {n}",
                      command=lambda: self._cmd_bar.cmd_batch(paths, "status"))
        m.add_separator()
        m.add_command(label="🔍  Search across these projects…",
                      command=lambda: self._open_cross_project_search(paths))
        m.add_command(label=f"🛡  Enable strict_tree on all {n}…",
                      command=lambda: self._set_strict_tree(paths, True))
        m.add_command(label=f"🛡  Disable strict_tree on all {n}…",
                      command=lambda: self._set_strict_tree(paths, False))
        m.add_separator()
        m.add_command(label="Clear selection",
                      command=lambda: self._tree.selection_remove(
                          *self._tree.selection()))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _on_tree_select(self, event=None) -> None:
        """Internal handler for <<TreeviewSelect>> — fires the project-select callback.

        v4.3: also kicks a background codegraph autosync when the index is
        stale, and surfaces a once-per-session reindex prompt when broken.
        """
        path = self.get_selected_path()
        if path:
            self._on_project_select(path)
            self._kick_codegraph_autosync(path)

    def _kick_codegraph_autosync(self, path: str) -> None:
        """Kick a background codegraph autosync for the selected project.

        Two-layer debounced (see codegraph_freshness.kick_autosync).
        After the check/sync completes:
          - Updates the "cg" column glyph (✓/⏳/⚠/—) on the main thread.
          - If the index is broken, fires maybe_prompt_reindex once per session.
        """
        exe = self._cfg.codegraph_exe
        if not exe:
            return
        try:
            from helpers.codegraph_freshness import (
                kick_autosync, maybe_prompt_reindex,
            )
            from helpers.doc_grounding import _codegraph_index_health
        except ImportError:
            return

        def _after_autosync():
            """Called from the autosync worker thread after sync finishes."""
            try:
                status, _detail = _codegraph_index_health(path, exe)
            except Exception:
                return
            # Marshal UI updates to the main thread.
            glyph_map = {
                "healthy": "✓ indexed",
                "stale":   "⏳ stale",
                "broken":  "⚠ under-indexed",
                "missing": "—",
            }
            glyph = glyph_map.get(status, "—")
            piid = f"proj:{path}"

            def _update_ui():
                try:
                    if self._tree.exists(piid):
                        self._tree.set(piid, "cg", glyph)
                except tk.TclError:
                    pass
                if status == "broken":
                    root = self._tab.winfo_toplevel()
                    maybe_prompt_reindex(
                        root, path, exe,
                        on_complete=self._on_refresh,
                    )

            try:
                self._tab.after(0, _update_ui)
            except tk.TclError:
                pass

        kick_autosync(path, exe, on_complete=_after_autosync)

    def _insert_pending_row(self, path: str, name: str) -> None:
        """Add a placeholder row while tokensave init is running."""
        self._tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))


    # ── Sync / Status commands — delegate to SyncStatusController ───────────


    def cmd_doc_updates(self) -> None:
        """Right-click → 📝 Doc Updates… — open the tabbed doc-drafter dialog.

        Roadmap-6 Tier B: drafts CHANGELOG.md [Unreleased] bullets AND
        README.md 'Recent highlights' bullets from a commit range via the
        configured local AI (Ollama / Claude CLI / etc.).  Each apply goes
        through ProposalBridge for the old-vs-new diff review.

        Architecture + Memory tabs are deferred to Roadmap-7.
        """
        path = self._selected_path()
        if not path:
            return
        # Lazy import (Rule 6) — avoids module-load cycle on Toplevel dialogs.
        from dialogs.doc_drafter import DocDrafterDialog
        DocDrafterDialog(
            self._root, path, self._cfg,
            on_log=self._on_log,
            on_commit_offer=self._offer_commit_after_change,
        )

    def cmd_roadmap_manager(self) -> None:
        """Right-click → 📋 Roadmap… — open the Roadmap Manager dialog."""
        path = self._selected_path()
        if not path:
            return
        from dialogs.roadmap_mgr import RoadmapManagerDialog
        RoadmapManagerDialog(self._root, path, self._cfg)



    def cmd_integration_check(self) -> None:
        """Delegate to App.cmd_integration_check (project-independent)."""
        self._root.cmd_integration_check()


    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_generate_vscode_tasks(self) -> None:
        """Write `.vscode/tasks.json` exposing the Manager's CLI to VS Code.

        Confirms first because the file is overwritten wholesale — it is
        generated, and a hand-edited copy would be lost. The runner is derived
        from how this Manager is installed rather than configured; see
        `helpers.vscode_tasks.default_runner`.
        """
        path = self._selected_path()
        if not path:
            return
        from constants import _BASE_DIR
        from helpers.vscode_tasks import (applicable_tasks, default_runner,
                                          write_tasks_json)

        runner = default_runner(
            _BASE_DIR, frozen=bool(os.environ.get("NUITKA_ONEFILE_PARENT")),
            python_exe=sys.executable)
        names = "\n".join(f"  • {t.label}" for t in applicable_tasks(runner))
        target = os.path.join(path, ".vscode", "tasks.json")
        existing = ("\n\nThis OVERWRITES the existing file."
                    if os.path.isfile(target) else "")
        if not messagebox.askyesno(
                "Generate VS Code tasks",
                f"Write .vscode/tasks.json in\n{path}\n\n"
                f"Tasks:\n{names}{existing}",
                parent=self._root):
            return

        ok, message = write_tasks_json(path, runner)
        self._on_log(f"[vscode] {message}", C["green"] if ok else C["peach"])

    def cmd_assign_category(self) -> None:
        path = self._selected_path()
        if not path:
            return
        all_cats: list = []
        all_subs: dict = {}
        for r in self._cfg.search_roots:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in self._cfg.raw.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = self._cfg.raw.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self._root, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path: str, cat, subcat) -> None:
        proj_cats = self._cfg.raw.setdefault("project_categories", {})
        if cat is None:
            proj_cats.pop(path, None)
            self._on_log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._on_log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        self._cfg.save()
        self._on_refresh()

