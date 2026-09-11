"""PushPullController - the push / fetch / pull cluster.

Extracted from GitTabController (2026-09-11), using the same
callback-injection pattern as BranchManagementController and the Round-5
sub-controllers: no parent reference, all parent state reached through
injected callables.

Scope is everything that talks to a REMOTE over git's own transport:

  * `cmd_git_push`, `cmd_git_force_push`, `cmd_push_targets`
  * the multi-remote flow - `_push_targets`, `_confirm_force_push`,
    `_run_multi_push`, `_describe_outcome`, `_after_push`, `_show_auth_help`
  * `cmd_git_fetch`, `cmd_git_pull`

Out of scope, and deliberately left in GitTabController: the PR lifecycle
(open / merge) and release publishing. Those reach the network too, but
through the GitHub API and a wizard rather than git's transport, and merging
them here would have put two domains in one file again.

**Why this cluster and not another.** GitTabController was 1,504 lines with 49
direct methods, over both the 1,500-line and 40-method caps. These eleven
methods put it under both at once without splitting a single flow across two
files - the alternative candidate, the PR cluster, was 6 methods and would
have left the class at 43.

Threading: workers run on daemon background threads and UI touches go through
UiPumpMixin's `_post`. Result lines push to the SHARED `log_queue`, so the
parent's existing drain loop keeps log ordering intact.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, TYPE_CHECKING

from constants import C, _GIT_ENV_NO_PROMPT
from helpers.llm import _is_auth_error
from helpers.multi_remote import PUSH_AUTH, list_remotes, push as mr_push

if TYPE_CHECKING:
    from state import ManagerConfig


class PushPullController:
    """Remote-facing git commands for the Git tab."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        get_git_path: Callable[[], "str | None"],
        on_shell: Callable,
        on_log: Callable,
        post: Callable,
        log_queue: queue.Queue,
        on_begin_op: Callable[[], None],
        on_end_op: Callable[[], None],
        is_op_in_flight: Callable[[], bool],
        on_refresh: Callable[[], None],
    ) -> None:
        self._tab             = tab
        self._cfg             = cfg
        self._get_git_path    = get_git_path
        self._on_shell        = on_shell
        self._on_log          = on_log
        # The parent's UiPumpMixin post. Deliberately NOT a second pump of
        # our own: one tab, one queue, one timer loop -- and a class that
        # mixes the mixin in without calling _start_ui_pump() raises
        # AttributeError on its first post, from a worker thread where
        # nothing is watching. tests/test_no_cross_thread_tk.py guards both.
        self._post            = post
        self._log_queue       = log_queue
        self._on_begin_op     = on_begin_op
        self._on_end_op       = on_end_op
        self._is_op_in_flight = is_op_in_flight
        self._on_refresh      = on_refresh

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    def cmd_git_push(self):
        self._run_multi_push(force=False)

    def cmd_git_force_push(self):
        """Force-push, leasing against each remote's own current tip."""
        path = self._get_git_path()
        if not path or self._is_op_in_flight():
            return
        targets, _upstream = self._push_targets(path)
        if not self._confirm_force_push(targets):
            return
        self._run_multi_push(force=True, confirmed=True)

    def cmd_push_targets(self) -> None:
        """Open the push-target chooser for the active project."""
        path = self._get_git_path()
        if not path:
            return
        from dialogs.remotes_manager import RemotesManagerDialog
        RemotesManagerDialog(self._root, path, self._cfg,
                             on_saved=self._on_refresh)

    # ── multi-remote push ────────────────────────────────────────────────

    def _push_targets(self, path: str) -> tuple:
        """(selected remote names, upstream remote) for *path*.

        Reconciled against `git remote` on every call, so a remote renamed or
        deleted outside the manager drops out rather than failing at push
        time — or worse, silently shrinking the push while still reporting
        success for the remotes that remain.
        """
        from dialogs.remotes_manager import load_selection, load_upstream
        remotes = list_remotes(self._cfg.git_exe, path)
        return (load_selection(self._cfg, path, remotes),
                load_upstream(self._cfg, path, remotes))

    def _confirm_force_push(self, targets) -> bool:
        where = "\n".join("    • %s" % t for t in targets) or "    (none)"
        return messagebox.askyesno(
            "⚠  Force Push — are you sure?",
            "Force-pushing rewrites the remote branch history on:\n\n"
            "%s\n\n"
            "This is safe to use after 'Scrub from History' removed a\n"
            "sensitive file — but anyone who has cloned this repo will need\n"
            "to re-clone afterwards (their history will no longer match).\n\n"
            "Each remote is checked immediately beforehand and skipped if it\n"
            "moved since, so commits someone else pushed are not discarded.\n\n"
            "Force-push now?" % where,
            icon="warning", default="no", parent=self._root)

    def _run_multi_push(self, *, force: bool, confirmed: bool = False):
        path = self._get_git_path()
        if not path or self._is_op_in_flight():
            return
        targets, upstream = self._push_targets(path)
        if not targets:
            messagebox.showinfo(
                "No push targets",
                "No remotes are selected for this project.\n\n"
                "Use \"Push targets…\" to choose which remotes a push goes to.",
                parent=self._root)
            return

        name = os.path.basename(path)
        verb = "Force-pushing" if force else "Pushing"
        self._on_log("[%s] %s to %s…" % (name, verb, ", ".join(targets)),
                     C["peach"])
        self._on_begin_op()

        git_exe = self._cfg.git_exe

        def worker():
            try:
                outcome = mr_push(git_exe, path, targets,
                                  upstream_remote=upstream,
                                  force_with_lease=force)
                for line in self._describe_outcome(outcome):
                    self._log_queue.put(line)
                self._post(lambda: self._after_push(outcome))
            finally:
                self._post(self._on_end_op)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _describe_outcome(outcome) -> list:
        """One log line per remote, plus per-destination detail when it differs.

        A remote with several push URLs gets a line each: reporting the remote
        as a single success would hide one destination having been rejected.
        """
        lines = []
        for result in outcome.results:
            glyph = "✓" if result.ok else "✗"
            colour = C["green"] if result.ok else C["red"]
            lines.append(("  %s %-12s %s" % (glyph, result.remote,
                                             result.detail), colour))
            if len(result.destinations) > 1:
                for dest in result.destinations:
                    lines.append(("      %s %s"
                                  % ("✓" if dest.ok else "✗", dest.url),
                                  C["green"] if dest.ok else C["red"]))
        return lines

    def _after_push(self, outcome) -> None:
        """One dialog, per-remote detail preserved. Tk thread."""
        self._on_refresh()
        if outcome.all_ok:
            return
        if any(r.kind == PUSH_AUTH for r in outcome.failed_remotes):
            self._show_auth_help(outcome)
            return
        detail = "\n".join(
            "  %s %s — %s" % ("✓" if r.ok else "✗", r.remote, r.detail)
            for r in outcome.results)
        messagebox.showwarning(
            "Push %s" % ("partly failed" if outcome.is_partial else "failed"),
            "%s\n\n%s" % (outcome.summary(), detail), parent=self._root)

    def _show_auth_help(self, outcome) -> None:
        failed = [r.remote for r in outcome.failed_remotes
                  if r.kind == PUSH_AUTH]
        messagebox.showinfo(
            "Authentication required",
            "These remotes need to verify your identity:\n\n"
            "%s\n\n"
            "Open a terminal in this project folder and run:\n"
            "    git push %s\n\n"
            "Sign in when prompted; this button will work normally "
            "afterwards." % ("\n".join("    • %s" % r for r in failed),
                             failed[0] if failed else ""),
            parent=self._root)

    def cmd_git_fetch(self):
        """Fetch remote refs (--prune) without merging. Updates remote-tracking branches."""
        path = self._get_git_path()
        if not path or self._is_op_in_flight():
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Fetching…", C["peach"])
        self._on_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "fetch", "--prune"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                lines = out.strip().splitlines()
                if rc == 0 and not lines:
                    self._log_queue.put((f"  [{name}] Already up to date.", col))
                else:
                    for line in lines[-6:]:
                        self._log_queue.put((f"  {line}", col))
                if rc != 0 and _is_auth_error(out):
                    self._post(lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git fetch\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._post(self._on_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_pull(self):
        path = self._get_git_path()
        if not path:
            return
        if self._is_op_in_flight():
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pulling…", C["peach"])
        self._on_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "pull"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0:
                    if _is_auth_error(out):
                        self._post(lambda: messagebox.showinfo(
                            "GitHub Authentication Required",
                            "GitHub needs to verify your identity.\n\n"
                            "Open a terminal in this project folder and run:\n"
                            "    git pull\n\n"
                            "A browser window will open asking you to log in to GitHub.\n"
                            "After that, this button will work normally.",
                            parent=self._root))
                    elif "conflict" in out.lower():
                        self._post(lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            "Pull completed but there are merge conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<<).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.",
                            parent=self._root))
            finally:
                self._post(self._on_end_op)

        threading.Thread(target=worker, daemon=True).start()

