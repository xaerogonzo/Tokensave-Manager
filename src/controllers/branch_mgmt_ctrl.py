"""BranchManagementController — branch new/switch/merge/delete cluster.

Extracted from GitTabController (Roadmap-2 Phase 4 / W5). Same callback-injection
pattern as the Round-5 sub-controllers under `src/controllers/<feature>_ctrl.py` —
no parent reference, all parent state reached through injected callables.

Scope:
  * `cmd_git_new_branch` + `_do_git_new_branch`
  * `cmd_git_switch_branch` + `_do_git_switch_branch`
  * `cmd_git_merge`        (inline worker, no separate _do_)
  * `cmd_git_delete_branch` + 6 helpers covering the safe-delete → ask-force →
    force-delete → offer-remote → remote-delete flow.

Out of scope (stays in GitTabController): `cmd_git_merge_pr` — GitHub PR merge
flow is a different domain (PR lifecycle, GH API), not branch management.

Threading: every worker method runs on a daemon background thread. UI touches
go through `self._tab.after(0, ...)`. Result lines push to the SHARED
`log_queue` so the parent's existing drain loop keeps log ordering intact.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from constants import C, _GIT_ENV_NO_PROMPT
from dialogs.new_branch import NewBranchDialog
from dialogs.switch_branch import SwitchBranchDialog
from helpers.llm import _is_auth_error

if TYPE_CHECKING:
    from state import ManagerConfig


class BranchManagementController:
    """Branch new / switch / merge / delete operations for the Git tab.

    Constructed once per GitTabController. Methods are bound to the same
    Git-tab buttons that previously called the parent's `cmd_git_*` methods —
    after Phase 4 the parent's button commands delegate here.
    """

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        get_git_path: Callable[[], "str | None"],
        on_shell: Callable,
        log_queue: queue.Queue,
        on_begin_op: Callable[[], None],
        on_end_op: Callable[[], None],
        is_op_in_flight: Callable[[], bool],
    ) -> None:
        self._tab             = tab
        self._cfg             = cfg
        self._get_git_path    = get_git_path
        self._on_shell        = on_shell
        self._log_queue       = log_queue
        self._on_begin_op     = on_begin_op
        self._on_end_op       = on_end_op
        self._is_op_in_flight = is_op_in_flight

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── New branch ──────────────────────────────────────────────────────────

    def cmd_git_new_branch(self):
        """Open New Branch dialog."""
        path = self._get_git_path()
        if not path:
            return
        NewBranchDialog(self._root, path, self._do_git_new_branch)

    def _do_git_new_branch(self, path: str, name: str, switch: bool):
        self._on_begin_op()

        def worker():
            try:
                if switch:
                    cmd = [self._cfg.git_exe, "-C", path, "checkout", "-b", name]
                else:
                    cmd = [self._cfg.git_exe, "-C", path, "branch", name]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = f"Created and switched to '{name}'" if (switch and rc == 0) \
                         else (f"Created '{name}'" if rc == 0 else out.strip())
                self._log_queue.put((f"  [{os.path.basename(path)}] {action}", col))
            finally:
                self._tab.after(0, self._on_end_op)

        threading.Thread(target=worker, daemon=True).start()

    # ── Switch branch ───────────────────────────────────────────────────────

    def cmd_git_switch_branch(self):
        """Open Switch Branch dialog (local + remote-tracking branches)."""
        path = self._get_git_path()
        if not path:
            return
        git = self._cfg.git_exe
        out_loc, rc1 = self._on_shell([git, "-C", path, "branch"], path)
        if rc1 != 0:
            messagebox.showerror("Git Error", out_loc.strip(), parent=self._root)
            return
        local_branches = []
        current = ""
        for line in out_loc.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                local_branches.append(line.strip())

        # Collect remote-tracking branches, skip those already checked out locally
        remote_branches = []
        out_rem, rc2 = self._on_shell([git, "-C", path, "branch", "-r"], path)
        if rc2 == 0:
            local_set = set(local_branches) | {current}
            for line in out_rem.strip().splitlines():
                name = line.strip()
                if "->" in name:          # skip "origin/HEAD -> origin/main"
                    continue
                bare = name.split("/", 1)[-1] if "/" in name else name
                if bare not in local_set:
                    remote_branches.append(bare)

        SwitchBranchDialog(self._root, path, local_branches, current,
                           self._do_git_switch_branch,
                           remote_branches=remote_branches)

    def _do_git_switch_branch(self, path: str, name: str):
        self._on_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "checkout", name], path)
                if rc != 0:
                    self._tab.after(0, lambda: messagebox.showerror(
                        "Switch Failed",
                        "Could not switch branches.\n\n"
                        "You may have uncommitted changes that conflict with the target branch.\n\n"
                        "Please commit or undo your changes before switching.",
                        parent=self._root))
                else:
                    self._log_queue.put((
                        f"  [{os.path.basename(path)}] Switched to branch '{name}'",
                        C["green"]))
            finally:
                self._tab.after(0, self._on_end_op)

        threading.Thread(target=worker, daemon=True).start()

    # ── Merge branch ────────────────────────────────────────────────────────

    def cmd_git_merge(self):
        """Merge another branch INTO the current branch (local + remote-tracking)."""
        path = self._get_git_path()
        if not path:
            return
        if self._is_op_in_flight():
            return
        prepared = self._prepare_merge_sources(path)
        if prepared is None:
            return
        current, non_current, remote_branches = prepared
        if not non_current and not remote_branches:
            messagebox.showinfo("No Other Branches",
                "There are no other branches to merge from.", parent=self._root)
            return
        proj = os.path.basename(path)
        source = SwitchBranchDialog.pick(self._root,
            f"Merge into {current} — {proj}",
            non_current, parent_widget=self._root,
            remote_branches=remote_branches)
        if not source:
            return
        is_remote = source in remote_branches
        merge_ref = f"origin/{source}" if is_remote else source
        display   = f"origin/{source}" if is_remote else source
        if not self._confirm_merge(current, display):
            return
        self._on_begin_op()
        threading.Thread(
            target=self._merge_worker, args=(path, proj, current, merge_ref, display),
            daemon=True).start()

    def _prepare_merge_sources(self, path: str) -> "tuple[str, list[str], list[str]] | None":
        """Run `git branch` + `git branch -r`; return (current, non_current, remote_only).

        Returns None on `git branch` failure (after surfacing the error to the
        user via messagebox). Skips remote-tracking branches that are already
        checked out locally.
        """
        git = self._cfg.git_exe
        out, rc = self._on_shell([git, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return None
        non_current = []
        current = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                non_current.append(line.strip())
        remote_branches = []
        out_rem, rc2 = self._on_shell([git, "-C", path, "branch", "-r"], path)
        if rc2 == 0:
            local_set = set(non_current) | {current}
            for line in out_rem.strip().splitlines():
                name = line.strip()
                if "->" in name:
                    continue
                bare = name.split("/", 1)[-1] if "/" in name else name
                if bare not in local_set:
                    remote_branches.append(bare)
        return current, non_current, remote_branches

    def _confirm_merge(self, current: str, display: str) -> bool:
        return messagebox.askyesno(
            "Merge Branch",
            f"Merge '{display}' INTO '{current}'?\n\n"
            f"This brings commits from '{display}' into '{current}'.\n"
            "Your working tree must be clean.\n\n"
            "If conflicts occur, resolve them in your editor, then\n"
            "Commit the result.",
            parent=self._root)

    def _merge_worker(self, path: str, proj: str, current: str,
                      merge_ref: str, display: str) -> None:
        """Background: run `git merge`; route conflict / unmerged / generic outcomes."""
        try:
            out, rc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "merge", "--no-edit", merge_ref], path)
            col = C["green"] if rc == 0 else C["red"]
            if rc == 0:
                self._log_queue.put((
                    f"  [{proj}] Merged '{display}' into '{current}'", col))
                for line in out.strip().splitlines()[-4:]:
                    self._log_queue.put((f"    {line}", col))
            else:
                self._explain_merge_failure(out, current, display)
                self._log_queue.put((f"  [{proj}] Merge failed", col))
                for line in out.strip().splitlines()[-4:]:
                    self._log_queue.put((f"    {line}", col))
        finally:
            self._tab.after(0, self._on_end_op)

    def _explain_merge_failure(self, out: str, current: str, display: str) -> None:
        """Main-thread dispatch: show the right messagebox for conflict vs dirty-tree."""
        out_l = out.lower()
        if "conflict" in out_l:
            self._tab.after(0, lambda: messagebox.showwarning(
                "Merge Conflicts",
                f"Merging '{display}' into '{current}' produced conflicts.\n\n"
                "Open the project in your editor and look for files\n"
                "marked with conflict markers (<<<<<< / >>>>>>).\n"
                "Resolve them, then use 📝 Commit… to commit the result.\n\n"
                "Or open a terminal in the project folder and run\n"
                "    git merge --abort\n"
                "to undo the merge attempt entirely.",
                parent=self._root))
        elif "unmerged" in out_l or "your local changes" in out_l:
            self._tab.after(0, lambda: messagebox.showwarning(
                "Working Tree Not Clean",
                f"Cannot merge — '{current}' has uncommitted changes.\n\n"
                "Commit or stash them first, then try again.",
                parent=self._root))

    # ── Delete branch (multi-step flow) ─────────────────────────────────────

    def cmd_git_delete_branch(self):
        """Delete a non-current branch with safe/force-delete distinction."""
        path = self._get_git_path()
        if not path:
            return
        branch = self._confirm_branch_delete(path)
        if branch is None:
            return
        self._do_delete_branch(path, branch)

    def _confirm_branch_delete(self, path: str) -> "str | None":
        """List non-current branches, prompt for selection, and confirm."""
        out, rc = self._on_shell([self._cfg.git_exe, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return None
        non_current = [line.strip() for line in out.strip().splitlines()
                       if not line.startswith("* ")]
        if not non_current:
            messagebox.showinfo("No Branches",
                "There are no other branches to delete.", parent=self._root)
            return None
        branch = SwitchBranchDialog.pick(
            self._root, f"Delete Branch — {os.path.basename(path)}",
            non_current, parent_widget=self._root)
        if not branch:
            return None
        if not messagebox.askyesno(
                "Delete Branch",
                f"Delete branch '{branch}'?\n\n"
                "If this branch has been merged, it will be removed safely.",
                parent=self._root):
            return None
        return branch

    def _do_delete_branch(self, path: str, branch: str) -> None:
        """Start the local-branch-delete flow on a background thread."""
        self._on_begin_op()
        threading.Thread(
            target=self._del_branch_worker, args=(path, branch),
            daemon=True).start()

    # ── Branch-delete helpers (one per step; thread boundary in name) ────────
    # Methods named *_worker run on a background thread → use self._tab.after()
    # for any UI touch. Other methods run on the main thread → Tkinter-safe.

    def _del_branch_worker(self, path: str, branch: str) -> None:
        """Thread: attempt safe delete (`git branch -d`). Route to next step."""
        try:
            out, rc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "branch", "-d", branch], path)
            if rc == 0:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] Deleted branch '{branch}'",
                    C["green"]))
                self._tab.after(0, self._del_branch_offer_remote, path, branch)
                return
            out_l = out.lower()
            if "not fully merged" in out_l or "unmerged" in out_l:
                self._tab.after(0, self._del_branch_ask_force, path, branch)
            else:
                self._tab.after(0, lambda: messagebox.showerror(
                    "Delete Failed",
                    f"Could not delete branch '{branch}':\n\n{out.strip()}",
                    parent=self._root))
                self._tab.after(0, self._on_end_op)
        except Exception:
            self._tab.after(0, self._on_end_op)
            raise

    def _del_branch_ask_force(self, path: str, branch: str) -> None:
        """Main thread: ask user whether to force-delete an unmerged branch."""
        if not messagebox.askyesno(
                "Force Delete?",
                f"Branch '{branch}' has unmerged changes.\n\n"
                "Force-delete anyway?\n"
                "This permanently discards those commits.",
                parent=self._root):
            self._on_end_op()
            return
        threading.Thread(
            target=self._del_branch_force_worker, args=(path, branch),
            daemon=True).start()

    def _del_branch_force_worker(self, path: str, branch: str) -> None:
        """Thread: force-delete (`git branch -D`). Route to remote-offer on success."""
        try:
            o2, r2 = self._on_shell(
                [self._cfg.git_exe, "-C", path, "branch", "-D", branch], path)
            col = C["green"] if r2 == 0 else C["red"]
            msg = f"Force-deleted '{branch}'" if r2 == 0 else o2.strip()
            self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            if r2 == 0:
                self._tab.after(0, self._del_branch_offer_remote, path, branch)
                return
        finally:
            self._tab.after(0, self._on_end_op)

    def _del_branch_offer_remote(self, path: str, branch: str) -> None:
        """Main thread: check for a remote copy; ask user whether to delete it too."""
        rbo, rbrc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "branch", "-r"], path)
        has_remote = rbrc == 0 and any(
            line.strip().split(" ", 1)[0] == f"origin/{branch}"
            for line in rbo.strip().splitlines())
        if not has_remote:
            self._on_end_op()
            return
        if not messagebox.askyesno(
                "Delete from GitHub too?",
                f"'{branch}' is deleted locally, but a copy still\n"
                f"exists on GitHub (origin/{branch}).\n\n"
                "Also delete it from GitHub?\n"
                "(This is the same as running\n"
                f"  git push origin --delete {branch})",
                parent=self._root):
            self._on_end_op()
            return
        threading.Thread(
            target=self._del_branch_remote_worker, args=(path, branch),
            daemon=True).start()

    def _del_branch_remote_worker(self, path: str, branch: str) -> None:
        """Thread: `git push origin --delete <branch>`. Log result."""
        try:
            ro, rrc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "push", "origin", "--delete", branch],
                path, env=_GIT_ENV_NO_PROMPT)
            col = C["green"] if rrc == 0 else C["red"]
            if rrc == 0:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] "
                    f"Deleted 'origin/{branch}' from GitHub", col))
            else:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] Remote delete failed", col))
                for line in ro.strip().splitlines()[-4:]:
                    self._log_queue.put((f"    {line}", col))
                if _is_auth_error(ro):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in the project folder and run:\n"
                        f"    git push origin --delete {branch}\n\n"
                        "A browser window will open asking you to log in.",
                        parent=self._root))
        finally:
            self._tab.after(0, self._on_end_op)
