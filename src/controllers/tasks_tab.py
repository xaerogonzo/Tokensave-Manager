"""TasksController — owns the Tasks tab.

Shows:
  - Active git worktrees for the selected project (merge / delete / open actions)
  - Recent Claude Code sessions across all indexed projects (title, project, age)

Layout: a compact toolbar row above a ttk.Panedwindow with two resizable panes.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C
from helpers.claude_tasks import scan_sessions, scan_worktrees

if TYPE_CHECKING:
    from state import ManagerConfig

AUTO_REFRESH_MS = 60_000  # 60 s between background refreshes


class TasksController:
    def __init__(
        self,
        notebook: ttk.Notebook,
        cfg: "ManagerConfig",
        *,
        get_project_path: Callable[[], str | None],
        get_known_paths: Callable[[], list[str]],
    ) -> None:
        self._cfg = cfg
        self._get_project_path = get_project_path
        self._get_known_paths = get_known_paths

        self._tasks_refresh_id: int = 0
        self._last_refresh_ts: float = 0.0

        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  📋 Tasks  ")

        self._build()
        self._tab.after(AUTO_REFRESH_MS, self._maybe_refresh)

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        tab = self._tab

        # Toolbar row
        toolbar = tk.Frame(tab, bg=C["base"])
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 0))
        ttk.Button(toolbar, text="⟳ Refresh", command=self._refresh).pack(side=tk.RIGHT)

        # Resizable split
        paned = ttk.Panedwindow(tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._wt_frame = ttk.LabelFrame(paned, text="Worktrees")
        sess_frame = ttk.LabelFrame(paned, text="Claude Sessions — all projects")
        paned.add(self._wt_frame, weight=1)
        paned.add(sess_frame, weight=3)

        self._build_worktrees_panel(self._wt_frame)
        self._build_sessions_panel(sess_frame)

    def _build_worktrees_panel(self, parent: tk.Widget) -> None:
        cols = ("branch", "head", "path")
        self._wt_tree = ttk.Treeview(parent, columns=cols, show="headings", height=4)
        self._wt_tree.heading("branch", text="Branch")
        self._wt_tree.heading("head", text="Head")
        self._wt_tree.heading("path", text="Path")
        self._wt_tree.column("branch", width=160, minwidth=80)
        self._wt_tree.column("head", width=70, minwidth=60)
        self._wt_tree.column("path", width=340, minwidth=120)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self._wt_tree.yview)
        self._wt_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._wt_tree.pack(fill=tk.BOTH, expand=True)

        # Context menu
        self._wt_menu = tk.Menu(self._wt_tree, tearoff=0)
        self._wt_menu.add_command(label="Open Folder", command=self._wt_open_folder)
        self._wt_menu.add_command(label="Merge into current branch", command=self._wt_merge)
        self._wt_menu.add_separator()
        self._wt_menu.add_command(label="Delete Worktree", command=self._wt_delete)

        self._wt_tree.bind("<Button-3>", self._wt_right_click)

    def _build_sessions_panel(self, parent: tk.Widget) -> None:
        cols = ("status", "title", "project", "activity")
        self._sess_tree = ttk.Treeview(parent, columns=cols, show="headings")
        self._sess_tree.heading("status", text="")
        self._sess_tree.heading("title", text="Title")
        self._sess_tree.heading("project", text="Project")
        self._sess_tree.heading("activity", text="Last Activity")
        self._sess_tree.column("status", width=24, minwidth=24, stretch=False)
        self._sess_tree.column("title", width=260, minwidth=100)
        self._sess_tree.column("project", width=180, minwidth=80)
        self._sess_tree.column("activity", width=130, minwidth=80)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self._sess_tree.yview)
        self._sess_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._sess_tree.pack(fill=tk.BOTH, expand=True)

        self._sess_tree.bind("<Double-1>", self._sess_open_folder)

    # ── Refresh ────────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._tasks_refresh_id += 1
        my_id = self._tasks_refresh_id
        path = self._get_project_path()
        known = self._get_known_paths()
        threading.Thread(
            target=self._worker, args=(my_id, path, known), daemon=True
        ).start()

    def _worker(self, req_id: int, path: str | None, known: list[str]) -> None:
        wt = scan_worktrees(path, self._cfg.git_exe) if path else []
        sess = scan_sessions(known)

        def _apply(req=req_id, w=wt, s=sess):
            if self._tasks_refresh_id != req:
                return
            self._apply_results(w, s)

        self._tab.after(0, _apply)

    def _maybe_refresh(self) -> None:
        try:
            wt_open = bool(self._wt_menu.winfo_ismapped())
        except tk.TclError:
            wt_open = False
        if not wt_open:
            self._refresh()
        self._tab.after(AUTO_REFRESH_MS, self._maybe_refresh)

    def on_tab_selected(self) -> None:
        if time.monotonic() - self._last_refresh_ts > 30:
            self._refresh()

    # ── Apply results (differential update) ────────────────────────────────────

    def _apply_results(self, worktrees: list[dict], sessions: list[dict]) -> None:
        path = self._get_project_path()
        proj_name = os.path.basename(path) if path else "(no project selected)"
        self._wt_frame.configure(text=f"Worktrees — {proj_name}")

        self._update_wt_tree(worktrees)
        self._update_sess_tree(sessions)
        self._last_refresh_ts = time.monotonic()

    def _update_wt_tree(self, worktrees: list[dict]) -> None:
        tree = self._wt_tree
        current_iids = set(tree.get_children())
        new_keys = {wt["path"] for wt in worktrees}

        # Remove stale rows (including placeholder)
        for iid in current_iids:
            if iid == "_empty" and worktrees:
                tree.delete(iid)
            elif iid not in new_keys and iid != "_empty":
                tree.delete(iid)

        if not worktrees:
            if "_empty" not in tree.get_children():
                tree.insert(
                    "",
                    tk.END,
                    iid="_empty",
                    values=("(no active worktrees — spawned task branches appear here)", "", ""),
                )
            return

        # Remove placeholder if worktrees exist
        if "_empty" in tree.get_children():
            tree.delete("_empty")

        for wt in worktrees:
            iid = wt["path"]
            vals = (wt["branch"], wt["head"], wt["path"])
            if iid in tree.get_children():
                tree.item(iid, values=vals)
            else:
                tree.insert("", tk.END, iid=iid, values=vals)

    def _update_sess_tree(self, sessions: list[dict]) -> None:
        tree = self._sess_tree
        current_iids = set(tree.get_children())
        new_iids = {s["session_id"] for s in sessions}

        for iid in current_iids - new_iids:
            tree.delete(iid)

        for s in sessions:
            iid = s["session_id"]
            status = "🟢" if s["is_recent"] else "⚫"
            ts = s["last_activity"]
            try:
                import datetime
                dt = datetime.datetime.fromtimestamp(ts)
                activity = dt.strftime("%d %b %H:%M")
            except Exception:
                activity = ""
            vals = (status, s["title"], s["project_display"], activity)
            if iid in current_iids:
                tree.item(iid, values=vals)
            else:
                tree.insert("", tk.END, iid=iid, values=vals)

    # ── Worktree actions ───────────────────────────────────────────────────────

    def _wt_right_click(self, event: tk.Event) -> None:
        row = self._wt_tree.identify_row(event.y)
        if not row or row == "_empty":
            return
        self._wt_tree.selection_set(row)
        try:
            self._wt_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._wt_menu.grab_release()

    def _wt_selected_info(self) -> dict | None:
        sel = self._wt_tree.selection()
        if not sel or sel[0] == "_empty":
            return None
        return dict(zip(("branch", "head", "path"), self._wt_tree.item(sel[0], "values")))

    def _wt_open_folder(self) -> None:
        info = self._wt_selected_info()
        if not info:
            return
        path = info["path"]
        if os.path.isdir(path):
            os.startfile(path)

    def _wt_merge(self) -> None:
        info = self._wt_selected_info()
        if not info:
            return
        project_path = self._get_project_path()
        if not project_path:
            messagebox.showerror("No project", "No project is selected.")
            return

        branch = info["branch"]
        worktree_path = info["path"]

        # Check project root is clean
        dirty_root = self._git_is_dirty(project_path)
        if dirty_root:
            messagebox.showwarning(
                "Working tree dirty",
                "Your working tree has uncommitted changes. Commit or stash them before merging.",
            )
            return

        # Check worktree itself for uncommitted changes
        dirty_wt = self._git_is_dirty(worktree_path)
        if dirty_wt:
            proceed = messagebox.askyesno(
                "Uncommitted changes in task",
                "The task has uncommitted changes in its workspace.\n"
                "Merging now will omit those changes.\n\nContinue?",
            )
            if not proceed:
                return

        confirmed = messagebox.askyesno(
            "Merge worktree branch",
            f"Merge branch '{branch}' into the current branch of:\n{project_path}\n\nContinue?",
        )
        if not confirmed:
            return

        try:
            result = subprocess.run(
                [self._cfg.git_exe, "-C", project_path, "merge", "--no-ff", branch],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                messagebox.showinfo("Merge complete", f"Branch '{branch}' merged successfully.")
                self._refresh()
            else:
                messagebox.showerror("Merge failed", result.stderr or result.stdout)
        except Exception as exc:
            messagebox.showerror("Merge error", str(exc))

    def _wt_delete(self) -> None:
        info = self._wt_selected_info()
        if not info:
            return
        worktree_path = info["path"]
        branch = info["branch"]

        confirmed = messagebox.askyesno(
            "Delete worktree",
            f"Delete worktree for branch '{branch}'?\n{worktree_path}",
        )
        if not confirmed:
            return

        ok, err = self._git_worktree_remove(worktree_path, force=False)
        if ok:
            self._refresh()
            return

        # Dirty worktree — git refuses without --force
        if err and ("is not empty" in err or "contains untracked" in err or "contains modified" in err):
            force_ok = messagebox.askyesno(
                "Uncommitted work exists",
                "Uncommitted work exists in this worktree.\nForce delete? (data will be lost)",
                icon="warning",
            )
            if not force_ok:
                return
            ok2, err2 = self._git_worktree_remove(worktree_path, force=True)
            if ok2:
                self._refresh()
                return
            # --force failed (OS file lock)
            messagebox.showerror(
                "Delete failed",
                "Folder is locked by another process.\n"
                "Close any active terminals or Claude Code sessions running in this worktree, "
                "then try again.",
            )
        else:
            messagebox.showerror("Delete failed", err or "Unknown error")

    def _git_worktree_remove(self, path: str, force: bool) -> tuple[bool, str]:
        cmd = [self._cfg.git_exe, "worktree", "remove", path]
        if force:
            cmd.append("--force")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return True, ""
            return False, result.stderr or result.stdout
        except Exception as exc:
            return False, str(exc)

    def _git_is_dirty(self, path: str) -> bool:
        try:
            result = subprocess.run(
                [self._cfg.git_exe, "-C", path, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ── Session actions ────────────────────────────────────────────────────────

    def _sess_open_folder(self, event: tk.Event) -> None:
        sel = self._sess_tree.selection()
        if not sel:
            return
        vals = self._sess_tree.item(sel[0], "values")
        # vals: (status, title, project_display, activity)
        # We can't recover the original path from project_display alone — open
        # the first known path whose basename matches project_display.
        proj_display = vals[2] if len(vals) > 2 else ""
        known = self._get_known_paths()
        for p in known:
            if os.path.basename(p) == proj_display:
                if os.path.isdir(p):
                    os.startfile(p)
                return
        messagebox.showinfo(
            "Folder not found",
            f"Could not locate the project folder for '{proj_display}'.",
        )
