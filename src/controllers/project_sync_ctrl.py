"""project_sync_ctrl.py — ProjectSyncCtrl sub-controller.

Extracted from :class:`~controllers.projects_tab.ProjectsTabController`
(Phase C3). Owns the background git-status column refresh: polling each
project's ``.git/index`` mtime, running ``git status --porcelain=v2``,
and updating the Treeview cell via ``after(0, ...)``.

Corresponds to the ``⬜ GitStatusController`` item in the Roadmap-8 god-class
extraction plan noted in the ProjectsTabController docstring.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from typing import TYPE_CHECKING

from helpers.git import (_format_git_status_cell, _parse_git_status_v2,
                         git_status_argv)

if TYPE_CHECKING:
    from typing import Callable
    from state import ManagerConfig


# Git-status tag names applied to Treeview rows.  Matches the class constant
# on ProjectsTabController._GIT_STATUS_TAGS — kept in sync here so
# _update_git_status_cell does not need to import the parent class.
_GIT_STATUS_TAGS = {
    "git_clean", "git_dirty", "git_ahead", "git_behind",
    "git_mixed", "git_pending", "git_none",
}


class ProjectSyncCtrl:
    """Background git-status column refresh for the Projects Treeview.

    Constructor args:
    * ``tab``       — the Projects ``tk.Frame`` (for ``after()`` scheduling).
    * ``cfg``       — :class:`ManagerConfig` (reads ``git_exe`` at call time).
    * ``on_shell``  — synchronous shell callback ``(cmd, cwd) -> (out, rc)``.
    * ``get_tree``  — zero-arg callable returning the live ``ttk.Treeview``.
    """

    def __init__(
        self,
        tab: "tk.Frame",
        cfg: "ManagerConfig",
        on_shell: "Callable",
        get_tree: "Callable",
    ) -> None:
        self._tab = tab
        self._cfg = cfg
        self._on_shell = on_shell
        self._get_tree = get_tree
        self._cancel:  bool = False
        self._running: bool = False

    # ── Public entry point ────────────────────────────────────────────────────

    def refresh(self, projects: list) -> None:
        """Kick off a background pass over *projects* to refresh Git column cells."""
        self._kick_off(projects)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _kick_off(self, projects: list) -> None:
        """Cancel any in-flight refresh and start a new one."""
        if self._running:
            self._cancel = True
        self._cancel  = False
        self._running = True

        snapshot = list(projects)

        def worker() -> None:
            try:
                for p in snapshot:
                    if self._cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached       = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue
                    try:
                        # Shared argv: the CLI runs the same command
                        # directly, and one builder keeps them honest.
                        out, _rc = self._on_shell(
                            git_status_argv(path, self._cfg.git_exe),
                            path,
                        )
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]      = status
                    p["_git_idx_mtime"]  = idx_mtime
                    piid = f"proj:{path}"
                    if self._tab.winfo_exists():
                        self._tab.after(0, self._update_cell, piid, status)
                    time.sleep(0.05)
            finally:
                self._running = False

        threading.Thread(target=worker, daemon=True).start()

    def _update_cell(self, piid: str, status: dict) -> None:
        """Main-thread: update a single row's Git column value + override tag."""
        tree = self._get_tree()
        if tree is None or not tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            tree.set(piid, "git", text)
        except tk.TclError:
            return
        existing = list(tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in _GIT_STATUS_TAGS]
        existing.append(tag)
        tree.item(piid, tags=tuple(existing))
