"""GitOpsController — git-feature commands for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab               — the Projects tk.Frame (after() + winfo_toplevel())
  • notebook          — the parent ttk.Notebook (for switching to the Git tab)
  • cfg               — read-only ManagerConfig (.git_exe, .raw)
  • on_log            — thread-safe log callback  (msg: str, colour: str = "")
  • on_shell          — synchronous shell runner  (args, cwd) -> (out: str, rc: int)
  • on_refresh        — () -> None
  • on_commit_offer   — (path: str, label: str) -> None
  • on_commit         — (path: str) -> None  (App._open_commit_dialog)
  • on_project_select — (path: str) -> None  (wires set_active_path on GitTab)
"""

from __future__ import annotations

import os
import threading
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C
from helpers.git import _find_tracked_but_ignored, _find_gitignored_on_disk, _is_git_repo, _is_local_git_repo
from helpers.gitignore import _BASELINE_GITIGNORE
from dialogs.ai_code_review import AICodeReviewDialog
from dialogs.gitignore import GitignoreDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog
from dialogs.private_repo_setup import PrivateRepoSetupDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class GitOpsController:
    """Handles git-log navigation, commit, AI review, git-init, .gitignore, and untrack."""

    def __init__(
        self,
        tab: tk.Frame,
        notebook: ttk.Notebook,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_shell: Callable,
        on_refresh: Callable[[], None],
        on_commit_offer: Callable[[str, str], None],
        on_commit: Callable[[str], None],
        on_project_select: Callable[[str], None],
    ) -> None:
        self._tab               = tab
        self._notebook          = notebook
        self._cfg               = cfg
        self._on_log            = on_log
        self._on_shell          = on_shell
        self._on_refresh        = on_refresh
        self._on_commit_offer   = on_commit_offer
        self._on_commit         = on_commit
        self._on_project_select = on_project_select

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Commands (path acquired by ProjectsTabController before calling) ───────

    def cmd_git_log(self, path: str) -> None:
        """Switch to the Git tab and show live status/log for path."""
        # Switch to Git tab first so is_visible() is True when on_project_select fires.
        try:
            for idx in range(self._notebook.index("end")):
                if self._notebook.tab(idx, "text").strip() == "Git":
                    self._notebook.select(idx)
                    break
        except tk.TclError:
            pass
        # Fire the project-select callback — App._on_project_select wires
        # set_active_path + refresh on GitTabController.
        self._on_project_select(path)

    def cmd_git_commit(self, path: str) -> None:
        """Open the Git Commit dialog for path."""
        self._on_commit(path)

    def cmd_ai_code_review(self, path: str) -> None:
        """Open the AI Code Review dialog for path."""
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

    def cmd_git_init(self, path: str) -> None:
        """Initialise a git repository in path."""
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
            "git init succeeded.\n\nCreate an initial commit now?\n"
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

    def cmd_manage_gitignore(self, path: str) -> None:
        """Open the .gitignore management dialog for path."""
        GitignoreDialog(self._root, path, self._cfg)

    def cmd_untrack_ignored(self, path: str) -> None:
        """Find tracked-but-.gitignored files and offer to untrack them."""
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self._root)
            return
        files = _find_tracked_but_ignored(path, self._cfg.git_exe)
        if not files:
            messagebox.showinfo(
                "Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self._root)
            return
        UntrackIgnoredDialog(self._root, path, files, on_confirm=self._do_untrack_ignored)

    def cmd_create_private_repo(self, path: str) -> None:
        """Open the wizard to create a local-only git repo for gitignored files."""
        _NOISE_EXTS = (".pyc", ".pyo", ".pyd", ".log", ".db-wal", ".db-shm", ".db-wal2")
        _NOISE_DIRS = (
            "__pycache__", ".tokensave", ".codegraph", ".git",
            "node_modules", ".venv", "venv", ".tox", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", ".eggs",
        )
        _FILE_CAP = 300

        raw = _find_gitignored_on_disk(path, self._cfg.git_exe)

        # Filter noise, sort by path depth (root files first — G3)
        filtered = [
            f for f in raw
            if not any(f.endswith(ext) for ext in _NOISE_EXTS)
            and not any(
                part in _NOISE_DIRS
                for part in f.replace("\\", "/").split("/")
            )
        ]
        filtered.sort(key=lambda f: (f.replace("\\", "/").count("/"), f.lower()))

        # Cap after filtering so root-level files are never cut off (G3/G13)
        # The dialog shows the warning banner when capped.
        gitignored = filtered  # dialog handles the cap and warning internally

        PrivateRepoSetupDialog(
            self._root, path, gitignored,
            git_exe=self._cfg.git_exe,
            on_log=self._on_log,
            on_registered=lambda dest, files: self._register_private_repo(
                path, dest, files),
        )

    def _register_private_repo(self, src_path: str, dest: str, files: list) -> None:
        """Save private repo mapping to config (runs on main thread — G11)."""
        self._cfg.raw.setdefault("private_repos", {})[src_path] = {
            "dest":  dest,
            "files": files,
        }
        self._cfg.save()
        self._on_log(
            f"  ✓ Private repo registered — auto-sync enabled for future commits.",
            C["green"])

    def cmd_sync_private_repo(self, path: str) -> None:
        """On-demand sync: copy changed private files and commit to private repo."""
        from helpers.private_repo import sync_private_repo
        cfg_entry = self._cfg.raw.get("private_repos", {}).get(path)
        if not cfg_entry:
            from tkinter import messagebox
            messagebox.showinfo(
                "No private repo configured",
                "No private local repo is linked to this project.\n\n"
                "Right-click → 🔒 Create Private Local Repo… to set one up.",
                parent=self._root)
            return
        dest  = cfg_entry.get("dest", "")
        files = cfg_entry.get("files", [])
        self._on_log(f"Syncing private repo for {os.path.basename(path)}…",
                     C["peach"])

        def worker():
            sync_private_repo(
                self._cfg.git_exe, path, dest, files,
                on_log=self._on_log,
                commit_msg="",
            )
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _do_untrack_ignored(self, path: str, files: list) -> None:
        """Run `git rm --cached -- <files>` in a background thread."""
        if not files:
            return
        name = os.path.basename(path)
        self._on_log(
            f"Untracking {len(files)} file{'s' if len(files) != 1 else ''} in {name}…",
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
                    f"  ✓ Untracked {len(files)} file{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self._tab.after(0, self._on_refresh)
                # skip_stale_check=True prevents an infinite loop: _open_commit_dialog
                # would otherwise re-run the tracked-but-ignored check, find any files
                # the user deliberately left tracked, and prompt for Untrack again.
                # Any remaining stale files after this point are the user's conscious
                # choice — we should open the commit dialog, not re-prompt.
                self._tab.after(0, lambda: self._on_commit_offer(
                    path,
                    f"untrack {len(files)} ignored file{'s' if len(files) != 1 else ''}",
                    skip_stale_check=True))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_precommit_hook(self, path: str) -> None:
        """Install / remove the AI-review pre-commit hook (Phase 5b).

        Single menu entry that flips the install state based on what's
        currently in `.git/hooks/pre-commit`. Surfaces the active
        backend + severity threshold in both confirm messages so the
        user knows what they're (un)installing. Refuses to touch a
        hook the user wrote themselves — symmetric refusal so we
        never destroy their work.

        Backend + threshold UI is intentionally minimal: read-only in
        this dialog, edit via manager-config.json. A Settings-row for
        these is a Roadmap-3 follow-on.
        """
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "a pre-commit hook only makes sense in a git repo.",
                parent=self._root)
            return

        from helpers.precommit_hook import is_pre_commit_hook_installed
        if is_pre_commit_hook_installed(path):
            self._do_precommit_remove(path)
        else:
            self._do_precommit_install(path)

    def _precommit_status_blurbs(self) -> tuple[str, str, str]:
        """Read backend + threshold from cfg; return (backend, threshold, blurb)."""
        backend = (self._cfg.raw.get("precommit_review_backend")
                   or "auto").lower()
        threshold = (self._cfg.raw.get("precommit_severity_threshold")
                     or "none").lower()
        blurb = {
            "none":   "warn-only (never blocks commits)",
            "medium": "blocks on Medium or High findings",
            "high":   "blocks on High findings only",
        }.get(threshold, f"unknown ({threshold!r}) — treated as warn-only")
        return backend, threshold, blurb

    def _do_precommit_remove(self, path: str) -> None:
        """Confirm + remove an existing tokensave-installed hook."""
        from helpers.precommit_hook import remove_pre_commit_hook
        backend, _threshold, blurb = self._precommit_status_blurbs()
        msg = (
            f"Pre-commit AI review hook is INSTALLED on:\n"
            f"  {os.path.basename(path)}\n\n"
            f"Current backend:    {backend}\n"
            f"Current threshold:  {blurb}\n\n"
            "Remove the hook?  (You can re-install any time.)"
        )
        if not messagebox.askyesno(
                "Pre-commit AI Review hook", msg, parent=self._root):
            return
        ok, msg = remove_pre_commit_hook(path)
        if ok:
            self._on_log(
                f"  ✓ Pre-commit AI review hook removed from "
                f"{os.path.basename(path)}", C["green"])
        else:
            messagebox.showerror(
                "Pre-commit AI Review hook",
                f"Could not remove hook:\n\n{msg}",
                parent=self._root)

    def _do_precommit_install(self, path: str) -> None:
        """Validate prerequisites, confirm, then install the hook."""
        from helpers.precommit_hook import (
            install_pre_commit_hook, reviewer_script_path,
        )
        py = self._cfg.raw.get("python_exe", "")
        if not py or not os.path.isfile(py):
            messagebox.showerror(
                "Pre-commit AI Review hook",
                "Cannot install — `python_exe` is not configured in "
                "manager-config.json. Set it via the manager's launcher "
                "or edit the config directly, then retry.",
                parent=self._root)
            return
        rev = reviewer_script_path()
        if not os.path.isfile(rev):
            messagebox.showerror(
                "Pre-commit AI Review hook",
                f"Cannot install — reviewer script not found at:\n"
                f"  {rev}\n\n"
                "The manager source tree is incomplete or moved.",
                parent=self._root)
            return

        backend, threshold, blurb = self._precommit_status_blurbs()
        msg = (
            f"Install pre-commit AI review hook on:\n"
            f"  {os.path.basename(path)}\n\n"
            f"Backend:    {backend}\n"
            f"Threshold:  {blurb}\n\n"
            "On every `git commit`, the hook will read the staged diff "
            "and run an AI code review. With the current threshold it "
            "will surface findings to stderr without blocking the "
            "commit. You can change `precommit_severity_threshold` "
            "(\"none\" / \"medium\" / \"high\") and "
            "`precommit_review_backend` (\"auto\" / \"claude_cli\" / "
            "\"llm\") in manager-config.json.\n\n"
            "Proceed?"
        )
        if not messagebox.askyesno(
                "Install Pre-commit AI Review hook",
                msg, parent=self._root):
            return
        ok, msg = install_pre_commit_hook(path, py, rev)
        if ok:
            self._on_log(
                f"  ✓ Pre-commit AI review hook installed on "
                f"{os.path.basename(path)}  "
                f"(backend={backend}, threshold={threshold})",
                C["green"])
        else:
            messagebox.showerror(
                "Pre-commit AI Review hook",
                f"Could not install hook:\n\n{msg}",
                parent=self._root)
