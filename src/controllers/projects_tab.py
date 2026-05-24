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

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers.detection import _is_codegraph_project, _root_label
from helpers.git import (
    _find_tracked_but_ignored,
    _format_git_status_cell,
    _is_git_repo,
    _is_local_git_repo,
    _parse_git_status_v2,
)
from helpers.gitignore import _BASELINE_GITIGNORE
from helpers.mcp import _MCP_CONFIGS, _classify_mcp_entry
from helpers.project_discovery import (
    clear_pinned,
    fmt_age,
    load_basic_instructions_template,
    set_pinned,
)
from helpers.runtime import log
from helpers.scaffold import _scaffold_git_hook
from helpers.shadow_links import (
    DEFAULT_SHADOW_EXT_MAP,
    generate_shadow_links,
    update_gitignore_for_shadows,
)
from controllers.codegraph_ctrl import CodeGraphController
from controllers.doctor_ctrl import DoctorController
from dialogs.ai_code_review import AICodeReviewDialog
from dialogs.assign_category import AssignCategoryDialog
from dialogs.gitignore import GitignoreDialog
from dialogs.retrofit import RetrofitDialog
from dialogs.scaffold import ScaffoldDialog
from dialogs.shadow_links import ShadowLinksDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog

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

        # ── Sub-controllers ───────────────────────────────────────────────────
        # Constructed after self._tab exists (codegraph_ctrl needs the frame).
        # Deferred until after _build_projects_tab() below so _tab is ready.
        self._codegraph: CodeGraphController | None = None  # set in _build_projects_tab

        # Controller-level subprocess tracking (parallel to App._current_proc)
        # Workers managed by THIS controller set/clear this attribute so that
        # App._auto_refresh can see whether the controller is busy.
        self.current_proc: object = None          # subprocess.Popen | None
        self._ctrl_stop_requested: bool = False   # checked by batch workers
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

    def stop(self):
        """Cancel any in-flight controller worker (called by App._stop_current)."""
        self._ctrl_stop_requested = True
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
            self._on_log(f"  Working tree left dirty — commit when you're ready.",
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
        m.add_command(label="🧠  Remove CodeGraph Index",  command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔍  AI Code Review…",    command=self.cmd_ai_code_review)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
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

    # ── Scaffold helpers ──────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path: str) -> list:
        """Copy Nuitka build templates into path.  Returns list of action strings."""
        name     = os.path.basename(path)
        src_ps1  = os.path.join(self._cfg.template_dir, "nuitka-build.ps1.template")
        src_bat  = os.path.join(self._cfg.template_dir, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._on_log("  [WARN] Nuitka templates not found in template directory — skipped",
                         C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {self._cfg.template_dir}")
            return actions

        if os.path.isfile(dst_ps1):
            self._on_log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._on_log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._on_log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        if os.path.isfile(dst_bat):
            self._on_log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._on_log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._on_log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._on_log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])
        return actions

    def _scaffold_project(self, path: str, create_bi: bool = True,
                          run_init: bool = True, scaffold_nuitka: bool = False,
                          add_git_hook: bool = False) -> None:
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template(
                    self._cfg.basic_instructions_template, self._cfg.baseline_include_line)
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._on_log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._on_log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._on_log(f"  {action}", C["green"])

        if run_init:
            self._insert_pending_row(path, name)
            self._on_log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self._tab.after(0, self._on_set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [self._cfg.tokensave_exe, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._on_log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._on_log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._on_log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self.current_proc = None
                    self._tab.after(0, self._on_set_running, False, "")
                    self._tab.after(0, self._on_refresh)
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._on_refresh()
            self._offer_commit_after_change(path, "scaffold files")

    # ── Tokensave commands ────────────────────────────────────────────────────

    def cmd_set_active(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        set_pinned(path)
        self._on_log(f"Pinned → {path}", C["green"])
        try:
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"] for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if "ok" in states and not all(s == "ok" for s in states):
            bad = [lbl for (lbl, p), s in zip(_MCP_CONFIGS, states) if s != "ok"]
            self._on_log(
                f"  Pin will take effect at next Claude restart.  "
                f"Note: {', '.join(bad)} also still needs its MCP wiring "
                f"fixed (Settings → 🔌 Manage MCP wiring).",
                C["peach"])
        elif "ok" not in states:
            self._on_log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
        else:
            self._on_log(
                "  Pin will take effect at next Claude Desktop / Claude Code "
                "restart.  (Live in-session reload is deferred — see the "
                "wrapper script's docstring for context.)",
                C["overlay0"])
        self._on_refresh()

    def cmd_auto(self) -> None:
        clear_pinned()
        self._on_log("Auto-detect enabled — wrapper picks the most-recently-synced project at next launch.", C["sky"])
        self._on_log(
            "  Restart Claude Desktop / Claude Code to trigger a fresh "
            "auto-detect.",
            C["overlay0"])
        self._on_refresh()

    def cmd_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._on_run(["sync"], cwd=path, label=os.path.basename(path))

    def cmd_sync_all(self) -> None:
        projects = self._get_projects()
        if not projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self._root)
            return
        ts_projects = [p for p in projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self._root)
            return
        count = len(ts_projects)
        skipped = len(projects) - count
        skip_note = (f"\n({skipped} git-only project{'s' if skipped != 1 else ''} "
                     f"will be skipped)") if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self._root,
        ):
            return

        projects_snapshot = list(ts_projects)

        def worker():
            self._ctrl_stop_requested = False
            self._on_log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self._tab.after(0, self._on_set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._ctrl_stop_requested:
                    self._on_log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._on_log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [self._cfg.tokensave_exe, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._on_log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._on_log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self.current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._on_log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self._tab.after(0, self._on_set_running, False, "")
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._on_run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._on_log(f"  Status OK — {data.get('node_count')} nodes, "
                                 f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self._tab.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._on_log(line)
                self._on_log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self._tab.after(0, self._on_refresh)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name: str, msg: str) -> None:
        win = tk.Toplevel(self._root)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self._root)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_force_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self._root,
        ):
            self._on_run(["sync", "--force"], cwd=path, label=os.path.basename(path))

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

    def cmd_git_log(self) -> None:
        """Navigate to the Git tab and show live status/log for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Switch to Git tab first so is_visible() is True when on_project_select fires.
        try:
            for idx in range(self._notebook.index("end")):
                if self._notebook.tab(idx, "text").strip() == "Git":
                    self._notebook.select(idx)
                    break
        except tk.TclError:
            pass
        # Now fire the project-select callback — App._on_project_select wires
        # set_active_path + refresh on GitTabController.
        self._on_project_select(path)

    def cmd_git_commit(self) -> None:
        """Open the Git Commit dialog for the selected project."""
        path = self._selected_path()
        if path:
            self._on_commit(path)

    def cmd_ai_code_review(self) -> None:
        """Open the AI Code Review dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} doesn't have a .git folder, "
                "so there's no pending diff to review.",
                parent=self._root)
            return
        llm_cfg = self._cfg.raw.get("commit_message_llm") or {}
        if not llm_cfg.get("enabled"):
            messagebox.showinfo(
                "AI is not enabled",
                "Open Settings → 'AI commit messages' and enable AI to use "
                "this feature.",
                parent=self._root)
            return
        AICodeReviewDialog(self._root, path, llm_cfg, self._cfg)

    def cmd_git_init(self) -> None:
        """Initialise a git repository in the selected project folder."""
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        if _is_git_repo(path, self._cfg.git_exe):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self._root,
            )
            return
        self._on_log(f"Running git init in {name}…", C["peach"])
        out, rc = self._on_shell([self._cfg.git_exe, "-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._on_log(f"  {line}", col)
        if rc != 0:
            self._on_refresh()
            return
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._on_log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._on_log(f"  Warning: could not write .gitignore: {e}", C["yellow"])
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self._root,
        ):
            def do_commit():
                self._on_shell([self._cfg.git_exe, "-C", path, "add", "-A"], path)
                cout, crc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self._tab.after(0, lambda l=line: self._on_log(f"  {l}", ccol))
                self._tab.after(0, self._on_refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self._on_refresh()

    def cmd_manage_gitignore(self) -> None:
        path = self._selected_path()
        if not path:
            return
        GitignoreDialog(self._root, path, self._cfg)

    def cmd_untrack_ignored(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo("Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self._root)
            return
        files = _find_tracked_but_ignored(path, self._cfg.git_exe)
        if not files:
            messagebox.showinfo("Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self._root)
            return
        UntrackIgnoredDialog(self._root, path, files,
                             on_confirm=self._do_untrack_ignored)

    def _do_untrack_ignored(self, path: str, files: list) -> None:
        """Worker: run `git rm --cached -- <files>` in a background thread."""
        if not files:
            return
        name = os.path.basename(path)
        self._on_log(f"Untracking {len(files)} file"
                     f"{'s' if len(files) != 1 else ''} in {name}…",
                     C["peach"])

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "rm", "-r", "--cached", "--"] + files,
                    path)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._on_log(f"  {line}", col)
                if rc != 0:
                    return
                self._on_log(
                    f"  ✓ Untracked {len(files)} file"
                    f"{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path,
                    f"untrack {len(files)} ignored file"
                    f"{'s' if len(files) != 1 else ''}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── File / editor / path commands ─────────────────────────────────────────

    def cmd_open_folder(self) -> None:
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        editor_str = self._cfg.raw.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self._root,
            )

    def cmd_copy_path(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(path)
        self._on_log(f"Copied: {path}", C["sky"])

    def cmd_remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self._root)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self._root,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._on_log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self._on_refresh()
        except Exception as e:
            self._on_log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self._root)

    # ── Shadow Links ──────────────────────────────────────────────────────────

    def cmd_shadow_links(self) -> None:
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self._root, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict,
                         run_sync: bool = True) -> None:
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            rc = 0
            try:
                self._on_log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._on_log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and self._cfg.tokensave_exe and created > 0:
                    self._on_log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._on_run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._on_log(f"    {line}", col)

                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}."
                       if run_sync and created > 0 else ""),
                    parent=self._root))
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()

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

    # ── Scaffold / Retrofit ───────────────────────────────────────────────────

    def cmd_scaffold(self) -> None:
        folder = filedialog.askdirectory(title="Select folder to scaffold",
                                         parent=self._root)
        if not folder:
            return
        ScaffoldDialog(self._root, folder, self._scaffold_project)

    def cmd_retrofit(self) -> None:
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self._root)
        if not folder:
            return
        RetrofitDialog(self._root, folder, self._do_retrofit)

    def cmd_retrofit_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self._root, path, self._do_retrofit)

    def _do_retrofit(self, path: str, add_tokensave: bool,
                     add_basic_instructions: bool,
                     add_nuitka: bool = False,
                     add_shadow_links: bool = False,
                     shadow_ext_map: dict | None = None,
                     add_git_hook: bool = False) -> None:
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} "
                         f"nuitka={add_nuitka}")
                self._on_log(f"Retrofitting {name}…", C["peach"])
                actions: list[str] = []

                if add_tokensave:
                    actions.extend(self._retrofit_add_tokensave(path, name))
                if add_basic_instructions:
                    actions.extend(self._retrofit_add_basic_instructions(path))
                if add_nuitka:
                    actions.extend(self._scaffold_nuitka_build(path))
                if add_shadow_links:
                    actions.extend(self._retrofit_add_shadow_links(
                        path, shadow_ext_map or DEFAULT_SHADOW_EXT_MAP))
                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._on_log(f"  {action}", C["green"])
                    actions.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions or 'nothing changed'}")
                self._on_log(f"Retrofit complete: {path}", C["green"])
                self._tab.after(0, self._on_refresh)

                if actions:
                    summary = "\n".join(f"  ✔ {a}" for a in actions)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions):
                        msg += ("\n\nNext step: open build.ps1 and replace "
                                "[ENTRY_SCRIPT] and [OUTPUT_NAME] before building.")
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Retrofit complete", msg, parent=self._root))
                if actions:
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "retrofit additions"))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Retrofit failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()

    # ── Retrofit step helpers (called from _do_retrofit's worker thread) ──

    def _retrofit_add_tokensave(self, path: str, name: str) -> list[str]:
        """Prepend the @include line to CLAUDE.md (or create it). Returns actions taken."""
        claude_md = os.path.join(path, "CLAUDE.md")
        include_line = self._cfg.baseline_include_line
        if os.path.isfile(claude_md):
            content = open(claude_md, encoding="utf-8", errors="ignore").read()
            if "project-baseline.md" in content:
                log.info("  CLAUDE.md already has @include — skipped")
                self._on_log("  Tokensave already integrated in CLAUDE.md — skipped", C["overlay0"])
                return []
            with open(claude_md, "r+", encoding="utf-8") as f:
                existing = f.read()
                f.seek(0)
                f.write(include_line + "\n\n" + existing)
            log.info("  prepended @include to CLAUDE.md")
            self._on_log("  Added tokensave @include to CLAUDE.md", C["green"])
            return ["Added tokensave rules to CLAUDE.md"]
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(f"# {name} — Claude Instructions\n\n{include_line}\n")
        log.info("  created CLAUDE.md with @include")
        self._on_log("  Created CLAUDE.md with tokensave @include", C["green"])
        return ["Created CLAUDE.md with tokensave rules"]

    def _retrofit_add_basic_instructions(self, path: str) -> list[str]:
        """Write BASIC_INSTRUCTIONS.md from the template. Returns actions taken."""
        basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
        if os.path.isfile(basic_md):
            log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
            self._on_log("  BASIC_INSTRUCTIONS.md already exists — skipped", C["overlay0"])
            return []
        template = load_basic_instructions_template(
            self._cfg.basic_instructions_template, self._cfg.baseline_include_line)
        with open(basic_md, "w", encoding="utf-8") as f:
            f.write(template)
        log.info("  created BASIC_INSTRUCTIONS.md")
        self._on_log("  Created BASIC_INSTRUCTIONS.md", C["green"])
        return ["Created BASIC_INSTRUCTIONS.md"]

    def _retrofit_add_shadow_links(self, path: str, ext_map: dict) -> list[str]:
        """Generate shadow extension links and update .gitignore. Returns actions taken."""
        self._on_log("  Generating shadow extension links…", C["peach"])
        created, skipped, failed = generate_shadow_links(path, ext_map)
        update_gitignore_for_shadows(path, ext_map)
        msg = f"Shadow links: created {created}"
        if skipped:
            msg += f", {skipped} already existed"
        if failed:
            msg += f", {failed} failed"
        self._on_log(f"  {msg}", C["green"])
        log.info(f"  {msg}")
        return [msg] if created > 0 else []
