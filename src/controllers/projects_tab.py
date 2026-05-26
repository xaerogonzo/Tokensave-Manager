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
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.detection import _root_label
from helpers.git import (
    _format_git_status_cell,
    _is_local_git_repo,
    _parse_git_status_v2,
)
from helpers.project_discovery import (
    fmt_age,
)
from controllers.codegraph_ctrl import CodeGraphController
from controllers.doctor_ctrl import DoctorController
from controllers.scaffold_ctrl import ScaffoldRetrofitController
from controllers.sync_ctrl import SyncStatusController
from controllers.fileops_ctrl import FileOpsController
from controllers.git_ops_ctrl import GitOpsController
from controllers.shadowlinks_ctrl import ShadowLinksController
from controllers.ai_tasks_ctrl import AITasksController
from dialogs.assign_category import AssignCategoryDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class ProjectsTabController:
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
        self._git_status_refresh_cancel: bool = False
        self._git_status_refresh_running: bool = False

        self._tree: ttk.Treeview | None = None
        self._ctx_menu: tk.Menu | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Projects  ")
        self._build_projects_tab()
        self._build_context_menu()

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
        """Kick off background refresh of the Git status column.

        Called by App.refresh() after rebuild_tree().
        """
        self._kick_off_git_status_refresh(projects)

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

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self._on_refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self._on_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

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
            selectmode="browse",
        )
        self._tree.heading("#0",       text="Project")
        self._tree.heading("active",   text="")
        self._tree.heading("path",     text="Path")
        self._tree.heading("synced",   text="Last Synced")
        self._tree.heading("cg",       text="CG")
        self._tree.heading("git",      text="Git")
        self._tree.heading("scaffold", text="Scaffold")

        self._tree.column("#0",       width=170, stretch=False)
        self._tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self._tree.column("path",     width=220)
        self._tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self._tree.column("cg",       width=36,  stretch=False, anchor=tk.CENTER)
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

    def _build_context_menu(self) -> None:
        m = tk.Menu(self._root, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_separator()
        m.add_command(label="🧠  CodeGraph Init",          command=self.cmd_codegraph_init)
        m.add_command(label="🧠  CodeGraph Sync",          command=self.cmd_codegraph_sync)
        m.add_command(label="🧠  CodeGraph Status",        command=self.cmd_codegraph_status)
        m.add_command(label="🧠  Remove CodeGraph Index…", command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔍  AI Code Review…",    command=self.cmd_ai_code_review)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
        m.add_command(label="🔍  Pre-commit AI Review hook…", command=self.cmd_precommit_hook)
        m.add_command(label="📝  Draft CHANGELOG entry…", command=self.cmd_draft_changelog)
        m.add_command(label="📝  Doc Updates… (CHANGELOG + README)", command=self.cmd_doc_updates)
        m.add_command(label="🔬  Refactor scout…",         command=self.cmd_refactor_scout)
        m.add_command(label="✓  Run checks…",              command=self.cmd_run_checks)
        m.add_command(label="🔄  Integration check",        command=self.cmd_integration_check)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        if not row.startswith("proj:"):
            return
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_tree_select(self, event=None) -> None:
        """Internal handler for <<TreeviewSelect>> — fires the project-select callback."""
        path = self.get_selected_path()
        if path:
            self._on_project_select(path)

    def _insert_pending_row(self, path: str, name: str) -> None:
        """Add a placeholder row while tokensave init is running."""
        self._tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))

    # ── Git status column refresh ─────────────────────────────────────────────

    def _kick_off_git_status_refresh(self, projects: list) -> None:
        """Background-walk every git project and update its Git column cell."""
        if self._git_status_refresh_running:
            self._git_status_refresh_cancel = True
        self._git_status_refresh_cancel  = False
        self._git_status_refresh_running = True

        projects_snapshot = list(projects)

        def worker():
            try:
                for p in projects_snapshot:
                    if self._git_status_refresh_cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue
                    try:
                        out, _rc = self._on_shell(
                            [self._cfg.git_exe, "-C", path,
                             "status", "--porcelain=v2", "--branch"],
                            path)
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]     = status
                    p["_git_idx_mtime"] = idx_mtime
                    piid = f"proj:{path}"
                    self._tab.after(0, self._update_git_status_cell, piid, status)
                    time.sleep(0.05)
            finally:
                self._git_status_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _update_git_status_cell(self, piid: str, status: dict) -> None:
        """Main-thread: update a single row's Git column value + override tag."""
        if not self._tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            self._tree.set(piid, "git", text)
        except tk.TclError:
            return
        existing = list(self._tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in self._GIT_STATUS_TAGS]
        existing.append(tag)
        self._tree.item(piid, tags=tuple(existing))

    # ── Sync / Status commands — delegate to SyncStatusController ───────────

    def cmd_set_active(self) -> None:
        if path := self._selected_path():
            if self._require_tokensave(path):
                self._sync.cmd_set_active(path)

    def cmd_auto(self) -> None:
        self._sync.cmd_auto()

    def cmd_sync(self) -> None:
        path = self._selected_path()
        if path and self._require_tokensave(path):
            self._sync.cmd_sync(path)

    def cmd_sync_all(self) -> None:
        self._sync.cmd_sync_all()

    def cmd_status(self) -> None:
        path = self._selected_path()
        if path and self._require_tokensave(path):
            self._sync.cmd_status(path)

    def cmd_force_sync(self) -> None:
        path = self._selected_path()
        if path and self._require_tokensave(path):
            self._sync.cmd_force_sync(path)

    # ── Doctor command — delegates to DoctorController ───────────────────────

    def cmd_doctor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._doctor.cmd_doctor(path)

    # ── CodeGraph commands ────────────────────────────────────────────────────

    # ── CodeGraph commands — delegate to CodeGraphController ─────────────────

    def cmd_codegraph_init(self) -> None:
        if path := self._selected_path():
            self._codegraph.cmd_init(path)

    def cmd_codegraph_sync(self) -> None:
        if path := self._selected_path():
            self._codegraph.cmd_sync(path)

    def cmd_codegraph_status(self) -> None:
        if path := self._selected_path():
            self._codegraph.cmd_status(path)

    def cmd_codegraph_remove(self) -> None:
        if path := self._selected_path():
            self._codegraph.cmd_remove(path)

    # ── Git commands (Projects-tab variants) ──────────────────────────────────

    # ── Git-feature commands — delegate to GitOpsController ──────────────────

    def cmd_git_log(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_git_log(path)

    def cmd_git_commit(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_git_commit(path)

    def cmd_ai_code_review(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_ai_code_review(path)

    def cmd_git_init(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_git_init(path)

    def cmd_manage_gitignore(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_manage_gitignore(path)

    def cmd_precommit_hook(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_precommit_hook(path)

    def cmd_untrack_ignored(self) -> None:
        if path := self._selected_path():
            self._gitops.cmd_untrack_ignored(path)

    def cmd_draft_changelog(self) -> None:
        if path := self._selected_path():
            self._ai_tasks.cmd_draft_changelog(path)

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

    def cmd_refactor_scout(self) -> None:
        if path := self._selected_path():
            self._ai_tasks.cmd_refactor_scout(path)

    def cmd_run_checks(self) -> None:
        if path := self._selected_path():
            self._ai_tasks.cmd_run_checks(path)

    def cmd_integration_check(self) -> None:
        """Delegate to App.cmd_integration_check (project-independent)."""
        self._root.cmd_integration_check()

    # ── File-ops commands — delegate to FileOpsController ────────────────────

    def cmd_open_folder(self) -> None:
        if path := self._selected_path():
            self._fileops.cmd_open_folder(path)

    def cmd_open_editor(self) -> None:
        if path := self._selected_path():
            self._fileops.cmd_open_editor(path)

    def cmd_copy_path(self) -> None:
        if path := self._selected_path():
            self._fileops.cmd_copy_path(path)

    def cmd_remove(self) -> None:
        if path := self._selected_path():
            self._fileops.cmd_remove(path)

    # ── Shadow Links — delegate to ShadowLinksController ─────────────────────

    def cmd_shadow_links(self) -> None:
        if path := self._selected_path():
            self._shadowlinks.cmd_shadow_links(path)

    # ── Category assignment ───────────────────────────────────────────────────

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

    # ── Scaffold / Retrofit commands — delegate to ScaffoldRetrofitController ──

    def cmd_scaffold(self) -> None:
        self._scaffold.cmd_scaffold()

    def cmd_retrofit(self) -> None:
        self._scaffold.cmd_retrofit()

    def cmd_retrofit_selected(self) -> None:
        if path := self._selected_path():
            self._scaffold.cmd_retrofit_selected(path)
