"""TasksController — owns the Tasks tab.

Two views, chosen from the toolbar dropdown:
  - Sessions & worktrees: active git worktrees for the selected project
    (merge / delete / open actions) and recent Claude Code / Cursor sessions
    across all indexed projects. The dot beside a session is transcript-file
    recency, NOT a live process.
  - MCP servers: the tokensave servers and CodeGraph daemons running right
    now (`helpers/mcp_runtime_rows`). Read-only; stopping stays in the daemon
    manager dialogs. Servers are per PROJECT -- nothing here links one to a
    particular session.

Layout: a compact toolbar row above either a ttk.Panedwindow with two
resizable panes, or the MCP server list.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from constants import C, CREATE_NO_WINDOW
from theme import UiPumpMixin, _Tooltip
from helpers.claude_tasks import scan_sessions, scan_worktrees
from helpers.cursor_tasks import scan_cursor_sessions
from helpers.mcp_runtime_rows import collect as collect_mcp_runtime
from helpers.project_discovery import find_projects
from helpers.worktree_cleanup import (
    LOCK_TOKENSAVE_DB,
    LOCK_WORKTREE_DIRECTORY,
    delete_orphan_directory,
    human_size,
    remove_worktree,
)

if TYPE_CHECKING:
    from state import ManagerConfig

AUTO_REFRESH_MS = 60_000  # 60 s between background refreshes

VIEW_SESSIONS = "Sessions & worktrees"
VIEW_MCP = "MCP servers"
_VIEWS = (VIEW_SESSIONS, VIEW_MCP)


class TasksController(UiPumpMixin):
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
        self._start_ui_pump()
        self._tab.after(AUTO_REFRESH_MS, self._maybe_refresh)

    def _ui_host(self):
        return self._tab

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        tab = self._tab

        # Toolbar row
        toolbar = tk.Frame(tab, bg=C["base"])
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 0))
        refresh_btn = ttk.Button(toolbar, text="⟳ Refresh", command=self._refresh)
        refresh_btn.pack(side=tk.RIGHT)
        _Tooltip(refresh_btn,
                 "Re-scan the current view.\n\n"
                 "Reads only — nothing is created, merged, deleted or stopped.")

        tk.Label(toolbar, text="View:", bg=C["base"], fg=C["subtext"]
                 ).pack(side=tk.LEFT)
        self._view_var = tk.StringVar(value=VIEW_SESSIONS)
        self._view_combo = ttk.Combobox(
            toolbar, textvariable=self._view_var, values=_VIEWS,
            state="readonly", width=22)
        self._view_combo.pack(side=tk.LEFT, padx=(4, 0))
        self._view_combo.bind("<<ComboboxSelected>>", self._on_view_changed)
        _Tooltip(self._view_combo,
                 "Sessions & worktrees: what Claude/Cursor have been doing.\n"
                 "MCP servers: which tokensave / CodeGraph servers are\n"
                 "running right now.")

        # Only shown in the MCP view; packed after Refresh so it sits left of it.
        self._manage_btn = ttk.Menubutton(toolbar, text="Manage…")
        manage_menu = tk.Menu(self._manage_btn, tearoff=0)
        manage_menu.add_command(label="tokensave servers…",
                                command=self._open_tokensave_manager)
        manage_menu.add_command(label="CodeGraph daemons…",
                                command=self._open_codegraph_manager)
        self._manage_btn["menu"] = manage_menu

        # Sessions & worktrees view (the default)
        self._sessions_view = ttk.Panedwindow(tab, orient=tk.VERTICAL)
        self._sessions_view.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._wt_frame = ttk.LabelFrame(self._sessions_view, text="Worktrees")
        sess_frame = ttk.LabelFrame(self._sessions_view,
                                    text="Claude Sessions — all projects")
        self._sessions_view.add(self._wt_frame, weight=1)
        self._sessions_view.add(sess_frame, weight=3)

        self._build_worktrees_panel(self._wt_frame)
        self._build_sessions_panel(sess_frame)

        # MCP servers view (built hidden; shown by the dropdown)
        self._mcp_view = ttk.LabelFrame(tab, text="Running MCP servers")
        self._build_mcp_panel(self._mcp_view)
        self._mcp_req_id: int = 0

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
        cols = ("status", "agent", "title", "project", "activity")
        self._sess_tree = ttk.Treeview(parent, columns=cols, show="headings")
        self._sess_tree.heading("status", text="")
        self._sess_tree.heading("agent", text="Agent")
        self._sess_tree.heading("title", text="Title")
        self._sess_tree.heading("project", text="Project")
        self._sess_tree.heading("activity", text="Last Activity")
        self._sess_tree.column("status", width=24, minwidth=24, stretch=False)
        self._sess_tree.column("agent", width=64, minwidth=50, stretch=False)
        self._sess_tree.column("title", width=260, minwidth=100)
        self._sess_tree.column("project", width=180, minwidth=80)
        self._sess_tree.column("activity", width=130, minwidth=80)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self._sess_tree.yview)
        self._sess_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._sess_tree.pack(fill=tk.BOTH, expand=True)

        self._sess_tree.bind("<Double-1>", self._sess_open_folder)

    def _build_mcp_panel(self, parent: tk.Widget) -> None:
        self._mcp_summary = tk.Label(
            parent, text="Not scanned yet.", anchor="w", justify=tk.LEFT,
            bg=C["base"], fg=C["text"], wraplength=1200)
        self._mcp_summary.pack(fill=tk.X, padx=8, pady=(6, 0))
        tk.Label(
            parent, anchor="w", justify=tk.LEFT, bg=C["base"], fg=C["overlay0"],
            wraplength=1200,
            text="Servers belong to a project, not to one session — this view "
                 "cannot say which Claude window started which server. "
                 "'guess' rows are inferred from timing, not confirmed.",
        ).pack(fill=tk.X, padx=8, pady=(0, 4))

        cols = ("server", "project", "pid", "started", "version", "attribution")
        self._mcp_tree = ttk.Treeview(parent, columns=cols, show="headings")
        for col, text, width in (
                ("server", "Server", 80), ("project", "Project", 420),
                ("pid", "PID", 60), ("started", "Started", 110),
                ("version", "Version", 70), ("attribution", "Project is…", 110)):
            self._mcp_tree.heading(col, text=text)
            self._mcp_tree.column(col, width=width, minwidth=50,
                                  stretch=(col == "project"))
        self._mcp_tree.tag_configure("guess", foreground=C["yellow"])
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL,
                            command=self._mcp_tree.yview)
        self._mcp_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._mcp_tree.pack(fill=tk.BOTH, expand=True)

    # ── View switching ─────────────────────────────────────────────────────────

    def _in_mcp_view(self) -> bool:
        return self._view_var.get() == VIEW_MCP

    def _on_view_changed(self, _event=None) -> None:
        if self._in_mcp_view():
            self._sessions_view.pack_forget()
            self._mcp_view.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
            self._manage_btn.pack(side=tk.RIGHT, padx=(0, 6))
        else:
            self._mcp_view.pack_forget()
            self._manage_btn.pack_forget()
            self._sessions_view.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._refresh()

    def _open_tokensave_manager(self) -> None:
        from dialogs.tokensave_daemon_manager import TokensaveDaemonManagerDialog
        TokensaveDaemonManagerDialog(
            self._tab.winfo_toplevel(), self._cfg, on_done=self._refresh)

    def _open_codegraph_manager(self) -> None:
        from dialogs.codegraph_daemon_manager import CodegraphDaemonManagerDialog
        CodegraphDaemonManagerDialog(self._tab.winfo_toplevel(), self._cfg)

    # ── Refresh ────────────────────────────────────────────────────────────────

    def _refresh_mcp(self) -> None:
        self._mcp_req_id += 1
        my_id = self._mcp_req_id
        self._mcp_summary.configure(text="Scanning for running servers…")
        ts_exe = self._cfg.tokensave_exe or ""
        cg_exe = self._cfg.codegraph_exe or ""
        roots = list(self._cfg.search_roots or [])

        def _scan() -> None:
            # Project discovery walks the disk and the process scan spawns
            # PowerShell, so both belong off the Tk thread.
            projects = [p["path"] for p in find_projects(roots)]
            snap = collect_mcp_runtime(ts_exe, cg_exe, projects)

            # A worker never touches Tk: hand the result to the pump.
            self._post(self._apply_mcp_if_current, my_id, snap)

        threading.Thread(target=_scan, daemon=True).start()

    def _apply_mcp_if_current(self, req: int, snap) -> None:
        if self._mcp_req_id == req and self._tab.winfo_exists():
            self._apply_mcp(snap)

    def _apply_mcp(self, snap) -> None:
        self._mcp_summary.configure(text=snap.summary)
        tree = self._mcp_tree
        tree.delete(*tree.get_children())
        for i, r in enumerate(snap.rows):
            tree.insert(
                "", tk.END, iid="srv:%d:%s:%d" % (i, r.server, r.pid),
                values=(r.server, r.project or "(unknown)", r.pid, r.started,
                        r.version, r.attribution),
                tags=("guess",) if r.attribution != "confirmed" else ())

    def _refresh(self) -> None:
        if self._in_mcp_view():
            self._refresh_mcp()
            return
        self._tasks_refresh_id += 1
        my_id = self._tasks_refresh_id
        path = self._get_project_path()
        known = self._get_known_paths()
        threading.Thread(
            target=self._worker, args=(my_id, path, known), daemon=True
        ).start()

    def _worker(self, req_id: int, path: str | None, known: list[str]) -> None:
        wt = scan_worktrees(path, self._cfg.git_exe) if path else []
        # Two scanners, one list. `scan_cursor_sessions` returns the same dict
        # shape plus an "agent" key, so this is a concatenation rather than a
        # second rendering path. It returns [] on a machine without Cursor.
        sess = scan_sessions(known) + scan_cursor_sessions(known)
        sess.sort(key=lambda row: row.get("last_activity") or 0, reverse=True)

        self._post(self._apply_if_current, req_id, wt, sess)

    def _apply_if_current(self, req: int, worktrees: list, sessions: list) -> None:
        if self._tasks_refresh_id == req:
            self._apply_results(worktrees, sessions)

    def _maybe_refresh(self) -> None:
        try:
            wt_open = bool(self._wt_menu.winfo_ismapped())
        except tk.TclError:
            wt_open = False
        # The MCP scan spawns PowerShell: only worth it while it is on screen.
        hidden_mcp = self._in_mcp_view() and not self._tab.winfo_ismapped()
        if not wt_open and not hidden_mcp:
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

    @staticmethod
    def _sess_iid(row: dict) -> str:
        """Row identity is (agent, session_id), never the id alone.

        Two scanners now feed this tree, and nothing guarantees their ids are
        drawn from disjoint spaces. Keying on the id alone would let a Cursor
        chat and a Claude session that happen to share one silently collapse
        into a single row — or have the wrong one selected. Follows
        the project's existing iid-prefix convention (`proj:<path>`).
        """
        return "sess:%s:%s" % (row.get("agent") or "claude", row["session_id"])

    @classmethod
    def _dedupe_sessions(cls, sessions: list[dict]) -> list[dict]:
        """One row per identity, keeping the most recently active.

        A session id is not unique on disk: a worktree's transcript folder and
        its sibling can each hold a file announcing the same `sessionId`
        (measured: 1 of 342 on this machine). The scanner is right to report
        both -- they are two files -- but a Treeview iid must be unique, and
        `insert` raises TclError on a repeat. Order of first appearance is
        kept, so a list already sorted newest-first stays that way.
        """
        best: dict[str, dict] = {}
        for row in sessions:
            iid = cls._sess_iid(row)
            held = best.get(iid)
            if held is None or (row.get("last_activity") or 0) > (held.get("last_activity") or 0):
                best[iid] = row
        return list(best.values())

    def _update_sess_tree(self, sessions: list[dict]) -> None:
        tree = self._sess_tree
        sessions = self._dedupe_sessions(sessions)
        current_iids = set(tree.get_children())
        new_iids = {self._sess_iid(s) for s in sessions}

        for iid in current_iids - new_iids:
            tree.delete(iid)

        for s in sessions:
            iid = self._sess_iid(s)
            status = "🟢" if s["is_recent"] else "⚫"
            agent = "Cursor" if s.get("agent") == "cursor" else "Claude"
            ts = s["last_activity"]
            try:
                import datetime
                dt = datetime.datetime.fromtimestamp(ts)
                activity = dt.strftime("%d %b %H:%M")
            except Exception:
                activity = ""
            vals = (status, agent, s["title"], s["project_display"], activity)
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
                creationflags=CREATE_NO_WINDOW,
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
        project_path = self._get_project_path()
        if not project_path:
            return
        worktree_path, branch = info["path"], info["branch"]

        if not messagebox.askyesno(
                "Delete worktree",
                "Delete worktree for branch '%s'?\n%s" % (branch, worktree_path),
                parent=self._tab):
            return

        res = remove_worktree(self._cfg.git_exe, project_path, worktree_path)
        if res.success:
            self._refresh()
            return

        # Git refused outright and pruned nothing — the classic dirty-worktree
        # case, where --force is genuinely the next step.
        if res.retry_would_help:
            if not self._looks_like_dirty_worktree(res.stderr):
                messagebox.showerror("Delete failed",
                                     res.stderr or "Unknown error",
                                     parent=self._tab)
                return
            if not messagebox.askyesno(
                    "Uncommitted work exists",
                    "Uncommitted work exists in this worktree.\n"
                    "Force delete? (data will be lost)",
                    icon="warning", parent=self._tab):
                return
            res = remove_worktree(self._cfg.git_exe, project_path,
                                  worktree_path, force=True)
            if res.success:
                self._refresh()
                return

        if res.is_half_state:
            self._offer_orphan_cleanup(project_path, worktree_path, res)
            return

        messagebox.showerror("Delete failed", res.stderr or "Unknown error",
                             parent=self._tab)

    @staticmethod
    def _looks_like_dirty_worktree(stderr: str) -> bool:
        text = (stderr or "").lower()
        return any(s in text for s in
                   ("is not empty", "contains untracked", "contains modified"))

    def _offer_orphan_cleanup(self, project_path: str, worktree_path: str,
                              res) -> None:
        """Explain the half-state, then offer the two things that can help.

        `git worktree remove` deregisters even when the delete fails, so at
        this point git is finished and only a directory remains. Telling the
        user to retry — what this tab used to do — sends them round a loop
        with nothing left to execute.

        The directory is NOT offered up for deletion casually: it usually
        holds the uncommitted work that caused the delete to fail, so the
        default action is to open it and look.
        """
        self._refresh()          # git's view changed; the tree should follow

        holder = {
            LOCK_TOKENSAVE_DB:
                "A tokensave server is holding this worktree's index "
                "(.tokensave/tokensave.db).\n"
                "Tool Manager → tokensave → \"Manage servers…\" can "
                "identify and stop it.\n\n"
                "After stopping it the error changes to name the folder "
                "rather than the database — that is how you know the "
                "database lock actually released.",
            LOCK_WORKTREE_DIRECTORY:
                "Something has the folder itself open — usually a "
                "terminal, an editor, or a Claude Code session running "
                "inside it.\n\n"
                "A session cannot release its own working directory; that "
                "clears when the session exits.",
        }.get(res.lock_kind,
              "Something is holding files in the folder open.")

        size = ""
        if res.signature:
            size = "\n\nThe folder still contains %d file%s (%s)." % (
                res.signature.file_count,
                "" if res.signature.file_count == 1 else "s",
                human_size(res.signature.total_bytes))

        if not messagebox.askyesno(
                "Worktree deregistered, folder remains",
                "Git has removed this worktree from its records, but could "
                "not delete the folder:\n\n%s\n\n%s%s\n\n"
                "Retrying the removal will not help — git has nothing "
                "left to do.\n\n"
                "Open the folder now to check what is in it?"
                % (worktree_path, holder, size),
                parent=self._tab):
            self._offer_orphan_delete(project_path, worktree_path, res)
            return

        if os.path.isdir(worktree_path):
            os.startfile(worktree_path)
        self._offer_orphan_delete(project_path, worktree_path, res)

    def _offer_orphan_delete(self, project_path: str, worktree_path: str,
                             res) -> None:
        """Second, separate confirmation before destroying anything.

        Deliberately a distinct prompt from the one above: "git forgot about
        this" is not evidence the contents are disposable, and the delete is
        irreversible.
        """
        if not os.path.isdir(worktree_path):
            return
        if not messagebox.askyesno(
                "Delete the leftover folder?",
                "Permanently delete this folder and everything in it?\n\n%s\n\n"
                "Anything not committed or pushed will be lost. If the lock "
                "is still held, some files may refuse to delete."
                % worktree_path,
                icon="warning", default="no", parent=self._tab):
            return
        ok, detail = delete_orphan_directory(
            self._cfg.git_exe, project_path, worktree_path, res.signature)
        if ok:
            messagebox.showinfo("Folder deleted", detail, parent=self._tab)
        else:
            messagebox.showerror("Not deleted", detail, parent=self._tab)
        self._refresh()

    def _git_is_dirty(self, path: str) -> bool:
        try:
            result = subprocess.run(
                [self._cfg.git_exe, "-C", path, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
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
        # vals: (status, agent, title, project_display, activity)
        # We can't recover the original path from project_display alone — open
        # the first known path whose basename matches project_display.
        proj_display = vals[3] if len(vals) > 3 else ""
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
