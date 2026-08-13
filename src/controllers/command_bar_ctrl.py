"""command_bar_ctrl.py — CommandBarCtrl sub-controller.

Extracted from :class:`~controllers.projects_tab.ProjectsTabController`
(Phase C3). Owns all pure-delegate ``cmd_*`` context-menu commands — the
~30 methods that follow the pattern:

    def cmd_X(self) -> None:
        if path := get_path():
            sub_ctrl.cmd_X(path)

Moving them here reduces ``ProjectsTabController`` from 53 → ~22 methods.
The context menu in ``_build_context_menu`` binds ``command=self._cmd_bar.cmd_X``
rather than ``command=self.cmd_X``.

**Deviant methods that stay on the parent** (they access parent-only state
beyond the allowed set of sub-controllers / get_path / require_tokensave):
  * cmd_doc_updates        — spawns DocDrafterDialog with on_log + on_commit_offer
  * cmd_roadmap_manager    — spawns RoadmapManagerDialog with root
  * cmd_integration_check  — calls self._root.cmd_integration_check()
  * cmd_assign_category    — reads self._cfg.search_roots, opens AssignCategoryDialog
  * _do_assign_category    — mutates self._cfg and calls self._on_refresh()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable


class CommandBarCtrl:
    """Owns all pure-delegate ``cmd_*`` context-menu commands.

    Constructor args (keyword-only after ``get_path`` and ``require_tokensave``):

    ``get_path``
        Zero-arg callable returning the currently selected project path, or
        ``None``.  Wraps ``ProjectsTabController._selected_path()``.
    ``require_tokensave``
        One-arg callable ``(path: str) -> bool`` that shows an info dialog
        and returns ``False`` if the project is not tokensave-indexed.
        Wraps ``ProjectsTabController._require_tokensave()``.

    Remaining keyword args are the 8 sub-controller references.
    """

    def __init__(
        self,
        *,
        get_path: "Callable[[], str | None]",
        require_tokensave: "Callable[[str], bool]",
        sync,
        doctor,
        codegraph,
        gitops,
        fileops,
        shadowlinks,
        scaffold,
        ai_tasks,
        housekeeping=None,
    ) -> None:
        self.get_path          = get_path
        self.require_tokensave = require_tokensave
        self._sync             = sync
        self._doctor           = doctor
        self._codegraph        = codegraph
        self._gitops           = gitops
        self._fileops          = fileops
        self._shadowlinks      = shadowlinks
        self._scaffold         = scaffold
        self._ai_tasks         = ai_tasks
        self._housekeeping     = housekeeping

    # ── Sync / Status commands ────────────────────────────────────────────────

    def cmd_set_active(self) -> None:
        if path := self.get_path():
            if self.require_tokensave(path):
                self._sync.cmd_set_active(path)

    def cmd_auto(self) -> None:
        self._sync.cmd_auto()

    def cmd_sync(self) -> None:
        path = self.get_path()
        if path and self.require_tokensave(path):
            self._sync.cmd_sync(path)

    def cmd_sync_all(self) -> None:
        self._sync.cmd_sync_all()

    def cmd_status(self) -> None:
        path = self.get_path()
        if path and self.require_tokensave(path):
            self._sync.cmd_status(path)

    def cmd_force_sync(self) -> None:
        path = self.get_path()
        if path and self.require_tokensave(path):
            self._sync.cmd_force_sync(path)

    # ── Doctor ────────────────────────────────────────────────────────────────

    def cmd_doctor(self) -> None:
        path = self.get_path()
        if not path:
            return
        if not self.require_tokensave(path):
            return
        self._doctor.cmd_doctor(path)

    def cmd_housekeeping(self) -> None:
        path = self.get_path()
        if not path:
            return
        if not self.require_tokensave(path):
            return
        if self._housekeeping is not None:
            self._housekeeping.cmd_housekeeping(path)

    # ── CodeGraph commands ────────────────────────────────────────────────────

    def cmd_codegraph_init(self) -> None:
        if path := self.get_path():
            self._codegraph.cmd_init(path)

    def cmd_codegraph_sync(self) -> None:
        if path := self.get_path():
            self._codegraph.cmd_sync(path)

    def cmd_codegraph_reindex(self) -> None:
        if path := self.get_path():
            self._codegraph.cmd_reindex(path)

    def cmd_codegraph_status(self) -> None:
        if path := self.get_path():
            self._codegraph.cmd_status(path)

    def cmd_codegraph_remove(self) -> None:
        if path := self.get_path():
            self._codegraph.cmd_remove(path)

    # ── Git commands ──────────────────────────────────────────────────────────

    def cmd_git_log(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_git_log(path)

    def cmd_git_commit(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_git_commit(path)

    def cmd_ai_code_review(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_ai_code_review(path)

    def cmd_git_init(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_git_init(path)

    def cmd_manage_gitignore(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_manage_gitignore(path)

    def cmd_precommit_hook(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_precommit_hook(path)

    def cmd_untrack_ignored(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_untrack_ignored(path)

    def cmd_private_repo(self) -> None:
        if path := self.get_path():
            self._gitops.cmd_private_repo(path)

    # ── AI Tasks commands ─────────────────────────────────────────────────────

    def cmd_draft_changelog(self) -> None:
        if path := self.get_path():
            self._ai_tasks.cmd_draft_changelog(path)

    def cmd_refactor_scout(self) -> None:
        if path := self.get_path():
            self._ai_tasks.cmd_refactor_scout(path)

    def cmd_run_checks(self) -> None:
        if path := self.get_path():
            self._ai_tasks.cmd_run_checks(path)

    # ── File-ops commands ─────────────────────────────────────────────────────

    def cmd_open_folder(self) -> None:
        if path := self.get_path():
            self._fileops.cmd_open_folder(path)

    def cmd_open_editor(self) -> None:
        if path := self.get_path():
            self._fileops.cmd_open_editor(path)

    def cmd_copy_path(self) -> None:
        if path := self.get_path():
            self._fileops.cmd_copy_path(path)

    def cmd_remove(self) -> None:
        if path := self.get_path():
            self._fileops.cmd_remove(path)

    # ── Shadow Links ──────────────────────────────────────────────────────────

    def cmd_shadow_links(self) -> None:
        if path := self.get_path():
            self._shadowlinks.cmd_shadow_links(path)

    # ── Scaffold / Retrofit commands ──────────────────────────────────────────

    def cmd_scaffold(self) -> None:
        self._scaffold.cmd_scaffold()

    def cmd_retrofit(self) -> None:
        self._scaffold.cmd_retrofit()

    def cmd_retrofit_selected(self) -> None:
        if path := self.get_path():
            self._scaffold.cmd_retrofit_selected(path)
