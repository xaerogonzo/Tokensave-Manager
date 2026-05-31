"""GitTabController — owns the 🌿 Git tab.

Decoupled from App via four explicit callbacks (get_path / on_log / on_shell /
on_commit). No back-reference to App. Worker threads push log messages to
`_log_queue`; `_poll_log_queue` drains it on the main thread every 100 ms so
Tkinter is never touched from a thread.

Per Round 4 plan rules:
  - `self._cfg.git_exe` read at execution time inside every method (Rule 3) —
    a Settings save that points at a different git binary propagates
    immediately without restart.
  - Dialogs are instantiated lazily where possible, but the heavy ones
    (Release Wizard, GitHub Setup, Merge PR, etc.) are imported up-front
    because there's no module-load cycle risk — none of them know about
    GitTabController.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW, _ANSI, _GIT_ENV_NO_PROMPT
from theme import _Tooltip, themed_checkbutton
from helpers.git import _is_local_git_repo
from helpers.llm import _is_auth_error
from helpers.runtime import log
from dialogs.set_remote import SetRemoteDialog
from dialogs.github_setup import GitHubSetupDialog
from dialogs.merge_pr import MergePRDialog
from dialogs.release_wizard import ReleaseWizardDialog

if TYPE_CHECKING:
    from typing import Callable
    from state import ManagerConfig


def _detect_base_branch(path: str, git_exe: str) -> "str | None":
    """Detect the PR base branch via a 7-step fallback chain.

    Returns a ref string suitable for `git log <base>..HEAD` and
    `git diff <base>...HEAD`, or None if detection fails completely.

    Known limitation: assumes origin-centric workflow. Fork workflows with
    both upstream and origin remotes may resolve to the wrong base.
    """
    _git = git_exe or "git"

    def _run(*args):
        try:
            proc = subprocess.run(
                [_git, "-C", path] + list(args),
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            return proc.returncode, proc.stdout.strip()
        except Exception:
            return 1, ""

    # Step 1: tracked upstream (e.g. "origin/main")
    # Skip if upstream == origin/<current-branch> — that's the branch tracking
    # itself, not a merge target. A feature branch on Roadmap-5 tracking
    # origin/Roadmap-5 should fall through to master/main detection below.
    rc, out = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc == 0 and out:
        _, current = _run("rev-parse", "--abbrev-ref", "HEAD")
        if out != f"origin/{current}":
            return out

    # Step 2: repo default via origin/HEAD pointer (may not exist on all clones)
    rc, out = _run("symbolic-ref", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        return out.replace("refs/remotes/", "")

    # Step 3: check origin/main exists as a remote-tracking branch
    rc, _ = _run("rev-parse", "--verify", "refs/remotes/origin/main")
    if rc == 0:
        return "origin/main"

    # Step 4: check origin/master exists
    rc, _ = _run("rev-parse", "--verify", "refs/remotes/origin/master")
    if rc == 0:
        return "origin/master"

    # Step 5: main as a local branch
    rc, _ = _run("rev-parse", "--verify", "main")
    if rc == 0:
        return "main"

    # Step 6: master as a local branch
    rc, _ = _run("rev-parse", "--verify", "master")
    if rc == 0:
        return "master"

    # Step 7: could not detect
    return None


def _extract_pr_title(text: str) -> str:
    """Pull a one-line title from generated PR markdown.

    Looks for the first non-empty bullet under '## Summary of Changes'.
    Falls back to the first non-empty non-header line in the whole text.
    """
    in_summary = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if stripped.startswith("#"):
                break
            if stripped.startswith(("- ", "* ", "• ")):
                return stripped.lstrip("-*• ").strip()[:120]
            if stripped:
                return stripped[:120]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:120]
    return "PR description"


def _recommended_test_selection(suggestions) -> "list[bool]":
    """Per-suggestion recommended check state for the test-gap panel.

    True for files worth an automated test (pure/subprocess helpers, via
    ``SuggestedTest.requires_automation``); False for Tk-dialog / unclassified
    files (low ROI — AI rarely produces passing GUI tests). Pure function so the
    "✓ Recommend" selection logic is unit-testable without Tk.
    """
    return [bool(getattr(s, "requires_automation", False)) for s in suggestions]


class GitTabController:
    """Owns the Git tab UI and all git operations.

    Decoupled from App via four explicit callbacks. No back-reference to App.
    Worker threads push log messages to _log_queue; _poll_log_queue drains it
    on the main thread every 100 ms so Tkinter is never touched from a thread.
    """

    def __init__(
        self,
        notebook: "ttk.Notebook",
        cfg: "ManagerConfig",
        get_path: "Callable[[], str | None]",
        on_log: "Callable[[str, str | None], None]",
        on_shell: "Callable[..., tuple[str, int]]",
        on_commit: "Callable[[str], None]",
    ):
        self._notebook           = notebook
        self._cfg                = cfg
        self._get_path           = get_path
        self._on_log             = on_log
        self._on_shell           = on_shell
        self._on_commit          = on_commit
        self._git_path: "str | None"      = None
        self._git_status_files: list      = []
        self._git_all_btns: list          = []
        self._git_push_pull_btns: list    = []
        self._git_release_btns: list      = []
        self._git_op_in_flight: bool      = False
        self._log_queue                   = queue.Queue()
        # Weak reference to an open TestManagerDialog — set externally when
        # the dialog is opened so the test-gap panel can refresh coverage.
        # None when the dialog has not been opened or has been destroyed.
        self._test_manager_ref            = None
        # Single live PR-draft dialog (standalone window). Tracked so re-drafts
        # bring it to front / replace it instead of stacking. _pr_draft_dirty is
        # True while a stream is in flight or the user has edited the draft.
        self._pr_draft_dialog             = None
        self._pr_draft_dirty: bool        = False
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Git  ")
        # Phase 4 (Roadmap-2): branch new/switch/merge/delete extracted into a
        # sub-controller (callback injection; no parent reference). The Git tab's
        # branch buttons delegate to self._branch_mgmt.cmd_* below.
        from controllers.branch_mgmt_ctrl import BranchManagementController
        self._branch_mgmt = BranchManagementController(
            tab=self._tab,
            cfg=self._cfg,
            get_git_path=lambda: self._git_path,
            on_shell=self._on_shell,
            log_queue=self._log_queue,
            on_begin_op=self._git_begin_op,
            on_end_op=self._git_end_op,
            is_op_in_flight=lambda: self._git_op_in_flight,
        )
        self._build_git_tab()
        self._poll_log_queue()

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_visible(self) -> bool:
        return self._git_tab_is_visible()

    def refresh(self) -> None:
        self._git_refresh()

    def set_active_path(self, path: str) -> None:
        self._git_path = path

    def has_path(self) -> bool:
        return bool(self._git_path)

    # ── Log queue ─────────────────────────────────────────────────────────────

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg, color = self._log_queue.get_nowait()
                try:
                    self._on_log(msg, color)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._tab.after(100, self._poll_log_queue)

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_git_tab(self):
        """Build the Git tab — shows live git state for the selected project."""
        self._build_git_header()
        mid = tk.Frame(self._tab, bg=C["base"], padx=14, pady=10)
        mid.pack(fill=tk.X)
        mid.columnconfigure(0, weight=1, minsize=200)
        mid.columnconfigure(1, weight=1, minsize=200)
        self._build_git_status_pane(mid)
        self._build_git_action_bar()
        self._build_git_diff_pane()

    def _build_git_header(self) -> None:
        hdr = tk.Frame(self._tab, bg=C["mantle"], padx=14, pady=8)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=C["mantle"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(left,
            text="OPERATING ON",
            font=("Segoe UI", 7, "bold"), bg=C["mantle"], fg=C["overlay0"]
            ).pack(anchor=tk.W)
        self._git_project_lbl = tk.Label(left,
            text="Select a project in the Projects tab",
            font=("Segoe UI", 13, "bold"), bg=C["mantle"], fg=C["blue"])
        self._git_project_lbl.pack(anchor=tk.W)

        info_row = tk.Frame(left, bg=C["mantle"])
        info_row.pack(anchor=tk.W, pady=(2, 0))

        self._git_branch_lbl = tk.Label(info_row,
            text="Branch: —", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"])
        self._git_branch_lbl.pack(side=tk.LEFT, padx=(0, 20))

        self._git_remote_lbl = tk.Label(info_row,
            text="Remote: No remote set", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["overlay0"])
        self._git_remote_lbl.pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=C["mantle"])
        right.pack(side=tk.RIGHT, anchor=tk.N)

        self._btn_set_remote = ttk.Button(right, text="Set Remote",
                                          command=self.cmd_git_set_remote)
        self._btn_set_remote.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(self._btn_set_remote,
            "Connect this project to a GitHub repository.\n"
            "Paste the HTTPS URL from github.com/new.\n"
            "Required before you can Push or Pull.")

        btn_github = ttk.Button(right, text="🐙  GitHub…",
                                command=self.cmd_github_setup)
        btn_github.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_github,
            "Open the GitHub Setup wizard — walks you through creating\n"
            "a GitHub account, connecting this project, pushing your code,\n"
            "and publishing a Release with your built .exe file.")

        btn_refresh = ttk.Button(right, text="⟳  Refresh",
                                 command=self._git_refresh)
        btn_refresh.pack(side=tk.LEFT)
        _Tooltip(btn_refresh, "Re-check the project's current git state and update this tab.")

    def _build_git_status_pane(self, mid: tk.Frame) -> None:
        tk.Label(mid, text="WORKING TREE",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=0, sticky=tk.W, pady=(0, 4))

        tk.Label(mid, text="RECENT COMMITS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))

        status_wrap = tk.Frame(mid, bg=C["mantle"])
        status_wrap.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 4))

        status_vsb = ttk.Scrollbar(status_wrap, orient="vertical")
        self._git_status_lb = tk.Listbox(
            status_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            selectbackground=C["surface1"],
            activestyle="none",
            relief=tk.FLAT, bd=0,
            yscrollcommand=status_vsb.set)
        status_vsb.configure(command=self._git_status_lb.yview)
        self._git_status_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                  padx=6, pady=4)
        status_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._git_status_lb.bind("<<ListboxSelect>>", self._on_git_status_select)

        log_wrap = tk.Frame(mid, bg=C["mantle"])
        log_wrap.grid(row=1, column=1, sticky=tk.NSEW, padx=(4, 0))

        log_vsb = ttk.Scrollbar(log_wrap, orient="vertical")
        self._git_log_txt = tk.Text(
            log_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=log_vsb.set)
        log_vsb.configure(command=self._git_log_txt.yview)
        self._git_log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_git_action_bar(self) -> None:
        acts = tk.Frame(self._tab, bg=C["base"])
        acts.pack(fill=tk.X, padx=14, pady=(6, 4))

        row1 = tk.Frame(acts, bg=C["base"])
        row1.pack(anchor=tk.W, pady=(0, 4))
        row2 = tk.Frame(acts, bg=C["base"])
        row2.pack(anchor=tk.W)

        btn_push    = ttk.Button(row1, text="⬆  Push",
                                 command=self.cmd_git_push)
        btn_pull    = ttk.Button(row1, text="⬇  Pull",
                                 command=self.cmd_git_pull)
        btn_fetch   = ttk.Button(row1, text="📡  Fetch",
                                 command=self.cmd_git_fetch)
        btn_commit  = ttk.Button(row1, text="📝  Commit…",
                                 command=lambda: self._on_commit(self._git_path)
                                         if self._git_path else None)
        btn_undo    = ttk.Button(row1, text="↩  Undo Last Commit",
                                 command=self.cmd_git_undo_commit)
        btn_new     = ttk.Button(row2, text="🌿  New Branch",
                                 command=self._branch_mgmt.cmd_git_new_branch)
        btn_switch  = ttk.Button(row2, text="🔀  Switch Branch…",
                                 command=self._branch_mgmt.cmd_git_switch_branch)
        btn_merge   = ttk.Button(row2, text="⇄  Merge…",
                                 command=self._branch_mgmt.cmd_git_merge)
        btn_del     = ttk.Button(row2, text="🗑  Delete Branch…",
                                 command=self._branch_mgmt.cmd_git_delete_branch)
        btn_openpr  = ttk.Button(row2, text="🔗  Open PR",
                                 command=self.cmd_git_open_pr)
        btn_mergepr = ttk.Button(row2, text="🐙  Merge PR…",
                                 command=self.cmd_git_merge_pr)
        btn_release = ttk.Button(row2, text="📦  Release…",
                                 command=self.cmd_git_release)
        btn_draft_pr = ttk.Button(row2, text="Draft PR…",
                                  command=self.cmd_draft_pr)
        btn_test_gaps = ttk.Button(row2, text="🧪 Test Gaps…",
                                   command=self.cmd_show_test_gaps)

        for btn in (btn_push, btn_pull, btn_fetch, btn_commit, btn_undo,
                    btn_new, btn_switch, btn_merge, btn_del, btn_openpr,
                    btn_mergepr, btn_release, btn_draft_pr, btn_test_gaps):
            btn.pack(side=tk.LEFT, padx=(0, 6))

        _Tooltip(btn_push,
            "Send your saved commits to GitHub.\n"
            "Like uploading a backup — your work is now safe online\n"
            "and others can see it.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_pull,
            "Download any new commits from GitHub to this machine.\n"
            "Use this if you made changes on another computer,\n"
            "or if a collaborator pushed new work.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_fetch,
            "Download the latest list of remote branches without merging any changes.\n"
            "Use this before Switch Branch… to see branches created on other\n"
            "machines or by collaborators.\n\n"
            "Safe to run at any time — it never modifies your local branches.")
        _Tooltip(btn_commit,
            "Save a snapshot of your current changes.\n"
            "Like a save point in a game — you can always come back here.\n\n"
            "You'll write a short message describing what you changed.\n"
            "A suggestion is generated automatically from the file list.")
        _Tooltip(btn_undo,
            "Remove the most recent save point, but keep all your changes.\n"
            "Nothing is deleted — your edits stay exactly as they were.\n\n"
            "Useful if you committed too early or with the wrong message.")
        _Tooltip(btn_new,
            "Create a separate copy of the project to try out an idea.\n"
            "Changes on this branch won't touch your main code\n"
            "until you're ready to merge them in.")
        _Tooltip(btn_switch,
            "Jump to a different branch (version) of the project.\n"
            "For example: switch from an experiment back to 'master'.\n\n"
            "Tip: commit your changes first — switching with\n"
            "unsaved edits will fail.")
        _Tooltip(btn_merge,
            "Merge another branch INTO the branch you're currently on.\n"
            "Use this to bring a finished feature branch back into master.\n\n"
            "Typical workflow: switch to master → pull → merge your feature →\n"
            "push → delete the feature branch.\n\n"
            "Conflicts (if any) must be resolved manually in your editor.")
        _Tooltip(btn_del,
            "Delete a branch you no longer need.\n"
            "Safe by default — warns you if the branch has changes\n"
            "that haven't been saved back to the main branch yet.\n"
            "After local delete, offers to also delete it from GitHub\n"
            "if a remote copy exists.")
        _Tooltip(btn_openpr,
            "Open a Pull Request on GitHub for the current branch.\n\n"
            "A Pull Request is a way to say: 'I made some changes on a\n"
            "separate branch — please review them and merge into main.'\n\n"
            "On master/main: shows you how to create a branch first.\n"
            "On any other branch: opens GitHub's compare page directly.\n\n"
            "Requires a GitHub remote and the branch to be pushed first.")
        _Tooltip(btn_mergepr,
            "Merge an open Pull Request from GitHub.\n\n"
            "Lists every open PR on this repo with its +X/-Y diff size,\n"
            "then lets you pick one and choose a merge strategy:\n"
            "  • Merge commit  — preserves the PR's branch history\n"
            "  • Squash and merge — collapses to a single commit\n"
            "  • Rebase and merge — replays commits linearly\n\n"
            "Shows a confirmation with the title and strategy before\n"
            "doing anything. After a successful merge, the PR is closed\n"
            "and (optionally) its branch is deleted on GitHub.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")
        _Tooltip(btn_release,
            "One-button GitHub release.\n\n"
            "Opens a wizard that auto-drafts release notes from your\n"
            "commits, builds the project, zips dist/, tags locally,\n"
            "pushes, and publishes via `gh release create` — all in one\n"
            "threaded worker. Editable textarea so you can polish the\n"
            "notes before publishing.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")

        _Tooltip(btn_draft_pr,
            "Draft a PR description using AI.\n\n"
            "Primary click: uses Claude Code CLI if configured, otherwise Ollama / API.\n"
            "Right-click or Shift+click: choose which tool to use.\n\n"
            "CLI mode opens a new terminal window running `claude` with a write\n"
            "instruction — your app stays unblocked while it runs.\n"
            "Ollama / API mode drafts the description inline and shows it in a dialog\n"
            "(also shows the 🧪 test-gap panel for changed files with no tests).")
        btn_draft_pr.bind("<Button-3>",
            lambda e: self._show_draft_pr_menu(e, btn_draft_pr))
        btn_draft_pr.bind("<Shift-Button-1>",
            lambda e: self._show_draft_pr_menu(e, btn_draft_pr))

        _Tooltip(btn_test_gaps,
            "Show which changed files on this branch have no tests yet.\n\n"
            "Compares this branch against its base (auto-detected or overridden\n"
            "via the Draft PR right-click menu) and lists any src/ .py files\n"
            "that are missing a tests/test_*.py counterpart.\n\n"
            "From the panel you can generate template stubs or AI-written tests\n"
            "for the flagged files in one click.")

        self._git_all_btns       = [self._btn_set_remote, btn_push, btn_pull,
                                     btn_commit, btn_undo, btn_new,
                                     btn_switch, btn_merge, btn_del, btn_openpr,
                                     btn_mergepr, btn_release, btn_draft_pr,
                                     btn_test_gaps]
        self._git_push_pull_btns = [btn_push, btn_pull, btn_openpr]
        self._git_release_btns   = [btn_release, btn_mergepr]

        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _build_git_diff_pane(self) -> None:
        tk.Label(self._tab, text="DIFF  (click a file above to preview)",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(
                     anchor=tk.W, padx=14, pady=(4, 4))

        diff_wrap = tk.Frame(self._tab, bg=C["mantle"])
        diff_wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        diff_vsb = ttk.Scrollbar(diff_wrap, orient="vertical")
        diff_hsb = ttk.Scrollbar(diff_wrap, orient="horizontal")
        self._git_diff_txt = tk.Text(
            diff_wrap,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=diff_vsb.set,
            xscrollcommand=diff_hsb.set)
        diff_vsb.configure(command=self._git_diff_txt.yview)
        diff_hsb.configure(command=self._git_diff_txt.xview)
        self._git_diff_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        diff_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        diff_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self._git_diff_txt.tag_configure("plus",   foreground=C["green"])
        self._git_diff_txt.tag_configure("minus",  foreground=C["red"])
        self._git_diff_txt.tag_configure("header", foreground=C["blue"])
        self._git_diff_txt.tag_configure("meta",   foreground=C["overlay0"])

    # ── Git tab data methods ──────────────────────────────────────────────────

    def _git_tab_is_visible(self) -> bool:
        try:
            return self._notebook.tab(self._notebook.select(), "text").strip() == "Git"
        except (tk.TclError, AttributeError):
            return False

    def _git_refresh(self):
        """Kick off a background thread that re-reads all git state."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            branch_out, brc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
            is_repo = brc == 0
            branch  = branch_out.strip() if is_repo else "—"

            remote_out, rrc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)
            remote = remote_out.strip() if rrc == 0 else ""

            status_out, _ = self._on_shell(
                [self._cfg.git_exe, "-C", path, "status", "--short"], path)

            log_out, lrc = self._on_shell(
                [self._cfg.git_exe, "-C", path, "log", "--oneline", "-15"], path)
            log_text = log_out.strip() if lrc == 0 else ""

            self._tab.after(0, lambda: self._git_update_ui(
                path, name, is_repo, branch, remote, status_out, log_text))

        threading.Thread(target=worker, daemon=True).start()

    def _git_begin_op(self):
        """Mark a git operation as in flight and disable all Git tab buttons."""
        self._git_op_in_flight = True
        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _git_end_op(self):
        """Clear the in-flight flag and refresh the Git tab to re-enable buttons."""
        self._git_op_in_flight = False
        self._git_refresh()

    def _git_update_ui(self, path, name, is_repo, branch, remote,
                       status_raw, log_text):
        """Main-thread update of all Git tab widgets."""
        self._git_project_lbl.config(text=name)

        if is_repo:
            self._git_branch_lbl.config(text=f"Branch:  {branch}",
                                         fg=C["text"])
        else:
            self._git_branch_lbl.config(
                text="Not a git repository — right-click project → 🔧 Git Init",
                fg=C["peach"])

        if remote:
            disp = remote.replace("https://", "").replace("http://", "")
            disp = disp.rstrip("/")
            if disp.endswith(".git"):
                disp = disp[:-4]
            self._git_remote_lbl.config(text=f"Remote:  {disp}",
                                         fg=C["overlay0"])
        else:
            self._git_remote_lbl.config(text="Remote:  No remote set",
                                         fg=C["overlay0"])

        if self._git_op_in_flight:
            for btn in self._git_all_btns:
                btn.configure(state=tk.DISABLED)
        else:
            repo_state = tk.NORMAL if is_repo else tk.DISABLED
            for btn in self._git_all_btns:
                btn.configure(state=repo_state)
            push_pull_state = tk.NORMAL if (is_repo and remote) else tk.DISABLED
            for btn in self._git_push_pull_btns:
                btn.configure(state=push_pull_state)
            release_ok = bool(is_repo and remote and shutil.which("gh"))
            for btn in self._git_release_btns:
                btn.configure(state=tk.NORMAL if release_ok else tk.DISABLED)

        self._git_status_lb.configure(state=tk.NORMAL)
        self._git_status_lb.delete(0, tk.END)
        self._git_status_files = []
        if is_repo:
            # BUG FIX: do NOT call status_raw.strip() before splitlines() —
            # status lines for working-tree changes start with a leading space
            # (" M file.py"), and strip() eats that leading space from the
            # FIRST line only, shifting column 0..1 (status) and 3+ (path) so
            # the path silently loses its first character. Use splitlines()
            # directly and skip blanks individually.
            has_lines = False
            for line in status_raw.splitlines():
                if len(line) < 4:
                    continue
                has_lines = True
                xy    = line[:2]
                fname = line[3:]
                self._git_status_lb.insert(tk.END, f"  {xy}  {fname}")
                self._git_status_files.append((xy.strip(), fname))
            if not has_lines:
                self._git_status_lb.insert(tk.END, "  (working tree clean)")

        self._git_log_txt.configure(state=tk.NORMAL)
        self._git_log_txt.delete("1.0", tk.END)
        if is_repo:
            self._git_log_txt.insert(tk.END,
                log_text if log_text else "(no commits yet)")
        self._git_log_txt.configure(state=tk.DISABLED)

        self._git_diff_txt.configure(state=tk.NORMAL)
        self._git_diff_txt.delete("1.0", tk.END)
        if not is_repo:
            self._git_diff_txt.insert(tk.END,
                "This project has no git history.\n\n"
                "Right-click the project in the Projects tab\n"
                "→ 🔧 Git Init to set one up.")
        self._git_diff_txt.configure(state=tk.DISABLED)

    def _on_git_status_select(self, event=None):
        """Click a file in the status listbox → show its diff below."""
        sel = self._git_status_lb.curselection()
        if not sel or not self._git_status_files:
            return
        idx = sel[0]
        if idx >= len(self._git_status_files):
            return
        _, fname = self._git_status_files[idx]
        path = self._git_path
        if not path:
            return

        def worker():
            d1, _ = self._on_shell(
                [self._cfg.git_exe, "-C", path, "diff", "--", fname], path)
            d2, _ = self._on_shell(
                [self._cfg.git_exe, "-C", path, "diff", "--cached", "--", fname], path)
            combined = "\n".join(filter(None, [d1.strip(), d2.strip()]))
            if not combined:
                combined = f"(no diff available — {fname} may be untracked or binary)"
            self._tab.after(0, lambda d=combined: self._git_show_diff(d))

        threading.Thread(target=worker, daemon=True).start()

    def _git_show_diff(self, diff_text):
        """Render a diff string into the diff Text widget with colour tags."""
        txt = self._git_diff_txt
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        lines = diff_text.splitlines(keepends=True)
        CAP = 2000
        if len(lines) > CAP:
            lines = lines[:CAP]
            lines.append(f"\n… [diff capped at {CAP} lines — file is very large] …\n")

        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                txt.insert(tk.END, line, "plus")
            elif line.startswith("-") and not line.startswith("---"):
                txt.insert(tk.END, line, "minus")
            elif line.startswith("@@"):
                txt.insert(tk.END, line, "header")
            elif line.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file")):
                txt.insert(tk.END, line, "meta")
            else:
                txt.insert(tk.END, line)

        txt.configure(state=tk.DISABLED)

    # ── Git action commands ────────────────────────────────────────────────────

    def cmd_git_push(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pushing…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "push", "-u", "origin", "HEAD"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0 and _is_auth_error(out):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git push\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_force_push(self):
        """Force-push current branch using --force-with-lease (safe force push)."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        confirmed = messagebox.askyesno(
            "⚠  Force Push — are you sure?",
            "Force-pushing rewrites the remote branch history.\n\n"
            "This is safe to use after 'Scrub from History' removed a\n"
            "sensitive file — but anyone who has cloned this repo will need\n"
            "to re-clone afterwards (their history will no longer match).\n\n"
            "Uses --force-with-lease, which refuses to overwrite commits\n"
            "that someone else pushed since your last fetch.\n\n"
            "Force-push now?",
            icon="warning",
            default="no",
            parent=self._root,
        )
        if not confirmed:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Force-pushing…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path,
                     "push", "--force-with-lease", "origin", "HEAD"],
                    path, env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0 and _is_auth_error(out):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git push\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_fetch(self):
        """Fetch remote refs (--prune) without merging. Updates remote-tracking branches."""
        path = self._git_path
        if not path or self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Fetching…", C["peach"])
        self._git_begin_op()

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
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git fetch\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_pull(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pulling…", C["peach"])
        self._git_begin_op()

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
                        self._tab.after(0, lambda: messagebox.showinfo(
                            "GitHub Authentication Required",
                            "GitHub needs to verify your identity.\n\n"
                            "Open a terminal in this project folder and run:\n"
                            "    git pull\n\n"
                            "A browser window will open asking you to log in to GitHub.\n"
                            "After that, this button will work normally.",
                            parent=self._root))
                    elif "conflict" in out.lower():
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            "Pull completed but there are merge conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<<).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.",
                            parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_open_pr(self):
        """Open a pull-request comparison page on GitHub for the current branch."""
        path = self._git_path
        if not path:
            return

        branch_out, brc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        remote_out, rrc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)

        branch = branch_out.strip() if brc == 0 else ""
        remote = remote_out.strip() if rrc == 0 else ""

        if not remote:
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        base = remote.rstrip("/").removesuffix(".git")
        if base.startswith("git@github.com:"):
            base = "https://github.com/" + base[len("git@github.com:"):]

        is_main = branch in ("master", "main", "")

        if is_main:
            go = messagebox.askyesno(
                "You're on the main branch",
                f"You're on '{branch}' — the main/default branch.\n\n"
                "Pull Requests work like this:\n\n"
                "  1. 🌿 New Branch  →  give it a name (e.g. 'my-feature')\n"
                "  2. Make your changes, then 📝 Commit\n"
                "  3. ⬆ Push  →  sends the branch to GitHub\n"
                "  4. 🔗 Open PR  →  GitHub shows a 'Compare & pull request' button\n"
                "  5. Fill in the description and click 'Create pull request'\n\n"
                "Open the repository page on GitHub now?",
                parent=self._root)
            if go:
                os.startfile(base)
        else:
            pr_url = f"{base}/compare/{branch}"
            self._on_log(
                f"  [{os.path.basename(path)}] Opening PR page for branch '{branch}'…",
                C["peach"])
            os.startfile(pr_url)

    def cmd_git_merge_pr(self):
        """List open PRs on this repo's GitHub origin and let the user pick one to merge."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        remote_out, rrc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        self._on_log(f"$ gh pr list  [{name}]", C["blue"])
        self._git_begin_op()

        def worker():
            try:
                r = subprocess.run(
                    ["gh", "pr", "list", "--state", "open",
                     "--json", "number,title,headRefName,baseRefName,"
                               "additions,deletions,author,url",
                     "--limit", "50"],
                    cwd=path, capture_output=True, text=True,
                    timeout=20, creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    err = (r.stderr or r.stdout or "").strip()
                    self._log_queue.put((f"  ✗ gh pr list failed: {err[:400]}", C["red"]))
                    return
                try:
                    prs = json.loads(r.stdout or "[]")
                except json.JSONDecodeError as e:
                    self._log_queue.put((f"  ✗ Could not parse gh output: {e}", C["red"]))
                    return
                if not prs:
                    self._log_queue.put(("  No open PRs on this repo.", C["overlay0"]))
                    return
                self._log_queue.put((
                    f"  Found {len(prs)} open PR(s).  Opening selection dialog…",
                    C["overlay0"]))
                self._tab.after(0, self._show_merge_pr_dialog, path, prs)
            except (OSError, subprocess.TimeoutExpired) as e:
                self._log_queue.put((f"  ✗ gh pr list error: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-list").start()

    def _show_merge_pr_dialog(self, path: str, prs: list):
        """Open the MergePRDialog with the fetched PR list."""
        MergePRDialog(self._root, path, prs, self._do_merge_pr)

    def _do_merge_pr(self, path: str, pr_number: int, strategy: str,
                     delete_branch: bool, pr_title: str):
        """Run `gh pr merge <N> --<strategy>` and stream output."""
        name = os.path.basename(path)
        flag = f"--{strategy}"
        cmd = ["gh", "pr", "merge", str(pr_number), flag]
        if delete_branch:
            cmd.append("--delete-branch")
        self._on_log(
            f"$ gh pr merge {pr_number} {flag}"
            f"{' --delete-branch' if delete_branch else ''}  [{name}]",
            C["blue"])
        self._git_begin_op()

        def worker():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if stripped:
                        self._log_queue.put((stripped, None))
                proc.wait()
                if proc.returncode == 0:
                    self._log_queue.put((
                        f"  ✓ PR #{pr_number} merged ({strategy}).  "
                        f"Pulling master to sync local…",
                        C["green"]))
                    self._tab.after(0, self._post_merge_pr_sync, path)
                else:
                    self._log_queue.put((
                        f"  ✗ gh pr merge exited with code {proc.returncode}",
                        C["red"]))
            except (OSError, FileNotFoundError) as e:
                self._log_queue.put((f"  ✗ Error running gh: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-merge").start()

    def _post_merge_pr_sync(self, path: str):
        """After a successful PR merge, switch to master and pull."""
        base = "master"
        try:
            r = subprocess.run(
                [self._cfg.git_exe, "-C", path, "symbolic-ref",
                 "refs/remotes/origin/HEAD", "--short"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                head = r.stdout.strip()
                if head.startswith("origin/"):
                    base = head[len("origin/"):]
        except (OSError, subprocess.TimeoutExpired):
            pass

        cur_out, cur_rc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        cur = cur_out.strip() if cur_rc == 0 else ""

        def worker():
            self._git_begin_op()
            try:
                if cur and cur != base:
                    self._log_queue.put((f"  Switching to '{base}'…", C["overlay0"]))
                    out, rc = self._on_shell(
                        [self._cfg.git_exe, "-C", path, "switch", base], path)
                    if rc != 0:
                        self._log_queue.put((
                            f"  ⚠ Could not switch to '{base}': "
                            f"{out.strip()[:200]}.  Pull manually after switching.",
                            C["peach"]))
                        return
                self._log_queue.put((f"  Pulling latest '{base}'…", C["overlay0"]))
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "pull", "--ff-only"], path)
                if rc == 0:
                    self._log_queue.put((f"  ✓ Local '{base}' synced.", C["green"]))
                else:
                    self._log_queue.put((
                        f"  ⚠ Pull failed: {out.strip()[:200]}", C["peach"]))
            finally:
                self._tab.after(0, self._git_end_op)
                self._tab.after(0, self._git_refresh)

        threading.Thread(target=worker, daemon=True, name="post-merge-sync").start()

    def cmd_git_release(self):
        """Open the Release Wizard for the currently loaded Git-tab project."""
        path = self._git_path
        if not path:
            return

        if not shutil.which("gh"):
            messagebox.showwarning("GitHub CLI required",
                "The Release Wizard runs `gh release create` under the hood.\n\n"
                "Install GitHub CLI from https://cli.github.com and re-open\n"
                "this dialog.",
                parent=self._root)
            return

        if not _is_local_git_repo(path):
            messagebox.showwarning("Not a git repo",
                f"{os.path.basename(path)} is not a git repository.\n\n"
                "Right-click the project → 🔧 Git Init first.",
                parent=self._root)
            return

        remote_out, rrc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning("No remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header first.",
                parent=self._root)
            return

        status_out, src = self._on_shell(
            [self._cfg.git_exe, "-C", path, "status", "--porcelain"], path)
        if src == 0 and status_out.strip():
            dirty_files = []
            for line in status_out.splitlines():
                if len(line) < 4:
                    continue
                fname = line[3:].strip()
                if fname and fname != "CHANGELOG.md":
                    dirty_files.append(fname)
            if dirty_files:
                preview = "\n".join(f"  • {f}" for f in dirty_files[:6])
                if len(dirty_files) > 6:
                    preview += f"\n  • …and {len(dirty_files) - 6} more"
                choice = messagebox.askyesnocancel(
                    "Working tree has unrelated changes",
                    f"The Release Wizard needs a clean working tree so the\n"
                    f"release-prep commit only contains the version bump.\n\n"
                    f"Uncommitted files:\n{preview}\n\n"
                    f"Yes  → open the Git Commit dialog now\n"
                    f"No   → cancel, deal with it later\n"
                    f"(Stash flow is on the roadmap.)",
                    parent=self._root)
                if choice is None or choice is False:
                    return
                self._on_commit(path)
                return

        ReleaseWizardDialog(self._root, path, self._cfg)

    def cmd_git_undo_commit(self):
        """Undo the last commit, keeping all changes staged."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        if not messagebox.askyesno(
                "Undo Last Commit",
                "Undo the last commit?\n\n"
                "Your changes will be kept and moved back to 'staged'.\n"
                "Nothing is deleted — you can re-commit at any time.",
                parent=self._root):
            return
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "reset", "--soft", "HEAD~1"], path)
                col = C["green"] if rc == 0 else C["red"]
                msg = "Last commit undone — changes are now staged." if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_set_remote(self):
        """Open the Set Remote dialog to connect this project to GitHub."""
        path = self._git_path
        if not path:
            return
        out, rc = self._on_shell(
            [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)
        current_url = out.strip() if rc == 0 else ""
        SetRemoteDialog(self._root, path, current_url, self._do_git_set_remote)

    def _do_git_set_remote(self, path: str, url: str):
        """Callback from SetRemoteDialog — add or update the origin remote."""
        self._git_begin_op()

        def worker():
            try:
                _, rc_check = self._on_shell(
                    [self._cfg.git_exe, "-C", path, "remote", "get-url", "origin"], path)
                if rc_check == 0:
                    cmd = [self._cfg.git_exe, "-C", path, "remote", "set-url", "origin", url]
                else:
                    cmd = [self._cfg.git_exe, "-C", path, "remote", "add", "origin", url]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = "updated" if rc_check == 0 else "added"
                msg = f"Remote {action}: {url}" if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_github_setup(self):
        """Open the GitHub Setup wizard for the selected/current project."""
        path = self._git_path or self._get_path()
        if not path:
            messagebox.showwarning("No project selected",
                "Select a project first.", parent=self._root)
            return
        GitHubSetupDialog(self._root, path, self._cfg)

    # ── Draft PR description ────────────────────────────────────────────────

    def cmd_draft_pr(self):
        """Draft a PR description using the configured AI backend.

        Backend is resolved from `draft_pr_backend` config key:
          "auto"       — CLI if available, else Ollama / API
          "claude_cli" — force Claude Code CLI
          "llm"        — force Ollama / API path
        Shift-click / right-click shows an override menu.
        """
        path = self._git_path
        if not path:
            return
        backend = self._cfg.draft_pr_backend
        cli = self._cfg.claude_cli_exe
        has_api = bool(self._cfg.raw.get("commit_message_llm", {}).get("api_key", "").strip()
                       or self._cfg.raw.get("commit_message_llm", {}).get("provider", "") == "ollama")
        if backend == "claude_cli":
            if cli:
                self._draft_pr_via_cli(path)
            else:
                messagebox.showinfo(
                    "No CLI configured",
                    "draft_pr_backend=claude_cli but no Claude Code CLI path is set.\n"
                    "Configure it in Settings → Git tools.",
                    parent=self._root)
        elif backend == "llm":
            if has_api:
                self._draft_pr_via_api(path)
            else:
                messagebox.showinfo(
                    "No Ollama / API configured",
                    "draft_pr_backend=llm but no Ollama or API provider is configured.\n"
                    "Add one in Settings → AI commit messages.",
                    parent=self._root)
        else:  # "auto"
            if cli:
                self._draft_pr_via_cli(path)
            elif has_api:
                self._draft_pr_via_api(path)
            else:
                messagebox.showinfo(
                    "No AI configured",
                    "Configure a Claude Code CLI path or an Ollama / API provider in Settings to use Draft PR.",
                    parent=self._root)

    def cmd_show_test_gaps(self):
        """Open a standalone Test Gaps dialog for the current branch.

        Resolves the base branch (override or auto-detect), then runs
        ``suggest_tests_for_diff`` and shows the same panel that appears
        inside the PR draft dialog — without needing to draft a PR first.
        Useful when you draft PRs via the CLI path.
        """
        path = self._git_path
        if not path:
            return
        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Test Gaps — base branch not found",
                "Could not detect the base branch to diff against.\n\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push to a remote and set a tracking branch.",
                parent=self._root)
            return

        self._open_test_gaps_window(path, base)

    def _open_test_gaps_window(self, path: str, base: str,
                               suggestions: "list | None" = None) -> None:
        """Open the standalone 🧪 Test Gaps window for *path* vs *base*.

        Shared by `cmd_show_test_gaps` (async scan) and the Claude CLI draft
        path (passes pre-computed *suggestions* so there's no second scan).
        """
        dlg = tk.Toplevel(self._root)
        dlg.title(f"🧪 Test Gaps — {os.path.basename(path)} vs {base.split('/')[-1]}")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.minsize(560, 200)
        dlg.transient(self._root)

        # Header showing what we're diffing
        hdr_row = tk.Frame(dlg, bg=C["base"])
        hdr_row.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(
            hdr_row,
            text=f"Changed files on this branch vs  {base.split('/')[-1]}",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT)

        # The test gap panel fills the rest of the dialog
        self._build_test_gap_panel(dlg, path, base, suggestions=suggestions)

        ttk.Button(dlg, text="Close",
                   command=dlg.destroy).pack(side=tk.BOTTOM, anchor=tk.E,
                                             padx=12, pady=(4, 10))

        dlg.update_idletasks()
        w, h = 620, 300
        try:
            px = self._root.winfo_x() + (self._root.winfo_width()  - w) // 2
            py = self._root.winfo_y() + (self._root.winfo_height() - h) // 2
            dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            dlg.geometry(f"{w}x{h}")

    # ------------------------------------------------------------------
    # PR base branch override helpers
    # ------------------------------------------------------------------

    def _resolve_pr_base(self, path: str) -> "str | None":
        """Return the base branch for a PR, honouring any per-project override.

        Checks ``raw["pr_base_branch_override"][path]`` first; falls back to
        the automatic 7-step ``_detect_base_branch`` chain when no override is
        stored.
        """
        override = (self._cfg.raw
                    .get("pr_base_branch_override", {})
                    .get(path, ""))
        if override:
            return override
        return _detect_base_branch(path, self._cfg.git_exe)

    def _cmd_set_pr_base(self, path: str) -> None:
        """Prompt the user for a PR base branch override and persist it."""
        from tkinter import simpledialog
        current = (self._cfg.raw
                   .get("pr_base_branch_override", {})
                   .get(path, ""))
        new_val = simpledialog.askstring(
            "Set PR base branch",
            "Enter the base branch for Draft PR (leave blank to reset to auto-detect):\n\n"
            f"Current: {current or '(auto)'}",
            initialvalue=current,
            parent=self._root,
        )
        if new_val is None:   # user cancelled the dialog
            return
        overrides = self._cfg.raw.setdefault("pr_base_branch_override", {})
        if new_val.strip():
            overrides[path] = new_val.strip()
            self._on_log(f"  Draft PR base branch override set to '{new_val.strip()}' "
                         f"for {os.path.basename(path)}", C["green"])
        else:
            overrides.pop(path, None)
            self._on_log(f"  Draft PR base branch override cleared for "
                         f"{os.path.basename(path)} (back to auto-detect)", C["overlay0"])
        self._cfg.save()

    def _show_draft_pr_menu(self, event, btn):
        """Show an override menu for right-click / Shift+click on Draft PR."""
        path = self._git_path
        if not path:
            return
        menu = tk.Menu(self._tab, tearoff=0)
        menu.add_command(label="Use Claude Code CLI",
                         command=lambda: self._draft_pr_via_cli(path))
        menu.add_command(label="Use Ollama / API (inline dialog)",
                         command=lambda: self._draft_pr_via_api(path))
        # Base branch override
        current_base = (self._cfg.raw
                        .get("pr_base_branch_override", {})
                        .get(path, ""))
        base_label = f"Set PR base branch…  (now: {current_base or 'auto'})"
        menu.add_separator()
        menu.add_command(label=base_label,
                         command=lambda: self._cmd_set_pr_base(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _draft_pr_via_cli(self, path: str):
        from helpers.claude_cli import spawn_claude_cli   # lazy import
        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Draft PR — base branch not found",
                "Could not detect the base branch for this PR.\n\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push to a remote and set a tracking branch with:\n"
                "  git branch --set-upstream-to=origin/<base> <branch>",
                parent=self._root)
            return
        # triple-dot `git diff base...HEAD` computes diff(merge-base(base,HEAD), HEAD)
        # — this isolates only this branch's commits, excluding upstream changes that
        # landed on base after branching. Do not change to double-dot.
        gh_available = bool(shutil.which("gh"))
        gh_base = base.split("/")[-1]   # strip "origin/" prefix; gh expects bare branch name
        gh_step = (
            f"Then create the PR on GitHub: push the branch first with "
            f"`git push -u origin HEAD` if it has no remote tracking, then run "
            f"`gh pr create --title <one-line-title> --body-file PR_DRAFT.md "
            f"--base {gh_base}` (replace <one-line-title> with a short descriptive title) "
            f"and print the resulting PR URL. "
            f"If the PR is created successfully, delete PR_DRAFT.md from the project root."
            if gh_available else
            "Note: gh CLI is not on PATH, so the PR_DRAFT.md file is your deliverable — "
            "skip any gh commands."
        )

        # v4.6: pre-build tokensave + codegraph grounding when enabled, AND
        # nudge the CLI to use its own MCP tools if they're wired. Two
        # complementary mechanisms: the grounding block gives the CLI
        # ready-made facts (works regardless of MCP config), the MCP nudge
        # exercises tools the user has already configured for Claude Code.
        #
        # The grounding block CANNOT be inlined into the instruction string
        # because spawn_claude_cli passes it as a cmd.exe command-line
        # argument and cmd.exe interprets `|`, `&`, `(`, `)`, `"` in the
        # markdown content as shell metacharacters — the spawned process
        # crashes immediately.  Write the grounding to a sibling file
        # (`.pr_context.tmp.md` next to PR_DRAFT.md) and tell the CLI to
        # read it as step 1.  The CLI removes the temp file when done.
        grounding_block, grounded = self._build_pr_grounding(path, base)
        if grounded:
            self._on_log("  Draft PR: built grounding from tokensave + codegraph",
                          C["green"])
        else:
            reason = "off in Settings" if not self._cfg.enable_pr_grounding \
                     else "neither tool indexed for this project"
            self._on_log(f"  Draft PR: no grounding attached ({reason})",
                          C["overlay0"])

        # Pre-render the automated checklist block from the test-run cache so
        # Claude can copy it verbatim into PR_DRAFT.md — shell metacharacters in
        # the markdown ([, ], `, |) prevent inlining it into the instruction string,
        # so it lives in the context file alongside the grounding data.
        from helpers.pr_draft import (  # lazy — avoids circular import
            _render_automated_for_pr, _render_coverage_gaps)
        automated_block = _render_automated_for_pr(path)
        # Coverage gaps (changed files lacking tests) — same filtered list the
        # Ollama path injects, so both PR bodies carry it. Reused below to open
        # the test-gap window after the CLI launches.
        try:
            from helpers.test_gap_report import suggest_tests_for_diff
            _gap_suggestions = suggest_tests_for_diff(path, self._cfg.git_exe, base)
        except Exception:
            _gap_suggestions = []
        _gaps_block = _render_coverage_gaps(_gap_suggestions)
        _checklist_tmpl = (
            "## Testing checklist\n"
            "<!-- tokensave-manager:testing-checklist v1 -->\n"
            + automated_block
            + (("\n" + _gaps_block) if _gaps_block else "")
            + "\n### Manual (please verify before merge)\n"
            "- [ ] <one smoke check per meaningful UI flow or changed behaviour>\n"
            "- [ ] <2-5 bullets total>\n"
        )

        context_step = ""
        context_path = ""
        try:
            context_path = os.path.join(path, ".pr_context.tmp.md")
            parts = [
                "# PR context (pre-fetched by manager)\n\n"
                "_Delete this file after PR_DRAFT.md is written._\n",
            ]
            if grounded:
                parts.append(grounding_block + "\n")
            parts.append(
                "\n---\n\n"
                "## Required Testing Checklist Format\n\n"
                "The `## Testing checklist` section in PR_DRAFT.md MUST use this exact "
                "format, including the HTML comment marker — the manager's "
                "'Sync PR Checklist' button depends on it:\n\n"
                + _checklist_tmpl
            )
            with open(context_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("".join(parts))
            _ctx_hint = "project context and " if grounded else ""
            context_step = (
                f"First, read `.pr_context.tmp.md` — it contains {_ctx_hint}"
                f"the required PR checklist format (copy it verbatim into PR_DRAFT.md). "
                f"After PR_DRAFT.md is written, delete `.pr_context.tmp.md`. Then: "
            )
        except OSError as exc:
            self._on_log(
                f"  Draft PR: could not write context file ({exc}); proceeding without it.",
                C["peach"])
            context_step = ""

        mcp_nudge = (
            " Note: if `mcp__tokensave__*` tools are available in this "
            "session, prefer `mcp__tokensave__tokensave_pr_context` and "
            "`mcp__tokensave__tokensave_diff_context` over additional "
            "`git diff` calls — they return branch-scoped structural facts. "
            "Fall back to bash git when those MCP tools are not registered."
            if (grounded or self._cfg.enable_pr_grounding) else ""
        )

        instruction = (
            f"{context_step}"
            f"Draft a PR description for this branch against `{base}`. "
            f"Run `git log {base}..HEAD --oneline` to see the commits, then "
            f"`git diff {base}...HEAD` to see the full diff (triple-dot gives the "
            f"merge-base diff, isolating only this branch's changes — not upstream). "
            f"Write the PR description to PR_DRAFT.md in the current working directory "
            f"(use a relative path — just PR_DRAFT.md, not an absolute path). "
            f"Include: a one-line summary, bullet list of key changes, and a testing checklist "
            f"copied verbatim from the format in .pr_context.tmp.md. "
            f"{gh_step}"
            f"{mcp_nudge}"
        )
        ok, err = spawn_claude_cli(
            self._cfg.claude_cli_exe, path, instruction,
            model=self._cfg.claude_cli_model,
        )
        if not ok:
            # Primary action failed — surface the error and STOP; don't pop the
            # secondary test-gap window into a disjoint "failed but soliciting" state.
            messagebox.showerror("Claude Code CLI error", err, parent=self._root)
            return
        # CLI launched (non-blocking Popen) — open the manager's test-gap window
        # so CLI users get the same coverage visibility the Ollama dialog has.
        if base and _gap_suggestions:
            self._open_test_gaps_window(path, base, suggestions=_gap_suggestions)

    def _build_pr_grounding(self, path: str, base: str) -> "tuple[str, bool]":
        """Pre-build the tokensave + codegraph grounding block for a CLI PR draft.

        Returns ``(block_text, grounded)``. ``grounded`` is True iff at least
        one source returned non-empty content AND the per-feature setting
        is on.  Fail-open: any exception returns ``("", False)`` and the
        Draft PR proceeds without grounding.
        """
        if not self._cfg.enable_pr_grounding:
            return "", False
        try:
            from helpers.doc_grounding import (
                build_grounding_block,
                build_codegraph_block,
                build_combined_grounding,
            )
            # Best-effort changed-files snapshot (triple-dot merge-base diff).
            # If git_exe is missing or the diff fails, the codegraph block
            # falls back to project-scoped queries — still useful.
            changed_files: list = []
            try:
                import subprocess
                from constants import CREATE_NO_WINDOW
                proc = subprocess.run(
                    [self._cfg.git_exe, "-C", path, "diff",
                     "--name-only", f"{base}...HEAD"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                if proc.returncode == 0 and proc.stdout:
                    changed_files = [ln.strip() for ln in
                                     proc.stdout.splitlines() if ln.strip()]
            except Exception:
                pass
            try:
                ts_block = build_grounding_block(
                    path, "roadmap_evidence",
                    tokensave_exe=self._cfg.tokensave_exe,
                )
            except Exception:
                ts_block = ""
            try:
                if self._cfg.codegraph_exe:
                    try:
                        from helpers.codegraph_freshness import ensure_fresh
                        ensure_fresh(path, self._cfg.codegraph_exe)
                    except Exception:
                        pass
                cg_block = build_codegraph_block(
                    path, "roadmap_evidence",
                    changed_files=changed_files,
                    codegraph_exe=self._cfg.codegraph_exe or "",
                )
            except Exception:
                cg_block = ""
            combined = build_combined_grounding(ts_block, cg_block)
            return combined, bool(combined.strip())
        except Exception:
            return "", False

    def _draft_pr_via_api(self, path: str):
        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Draft PR — base branch not found",
                "Could not detect the base branch for this PR.\n\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push to a remote and set a tracking branch with:\n"
                "  git branch --set-upstream-to=origin/<base> <branch>",
                parent=self._root)
            return

        llm_cfg = self._cfg.raw.get("commit_message_llm", {})
        if not llm_cfg.get("enabled"):
            messagebox.showerror(
                "Draft PR — LLM not enabled",
                "The Ollama / API provider is not enabled.\n\n"
                "Go to Settings → Commit Message LLM and enable it,\n"
                "then set a provider and model.",
                parent=self._root)
            return
        if not llm_cfg.get("model"):
            messagebox.showerror(
                "Draft PR — no model configured",
                "No model name is set for the LLM provider.\n\n"
                "Go to Settings → Commit Message LLM and enter\n"
                "a model name (e.g. 'llama3.2' for Ollama).",
                parent=self._root)
            return

        provider = llm_cfg.get("provider", "ollama")
        self._on_log(f"  Drafting PR description via {provider}…", C["blue"])

        # Open the streaming dialog immediately (standalone window). Returns the
        # context dict, or None if the user declined to discard a dirty draft.
        ctx = self._open_pr_draft_dialog(path, base or "", provider)
        if ctx is None:
            return

        _start_time = time.monotonic()
        ctx["start"] = _start_time
        q = ctx["queue"]

        def _fetch():
            from helpers.pr_draft import generate_pr_draft, _render_coverage_gaps
            from helpers.llm import get_last_llm_error
            # Outer guard: ALWAYS enqueue ("done", …) so the poll loop can never
            # spin forever, and log progress so a silent hang is diagnosable from
            # manager.log (Tk-callback / thread exceptions otherwise vanish under
            # pythonw with no console).
            result = None
            err = None
            suggestions: list = []
            try:
                log.info("PR draft worker: start (provider=%s, base=%s)",
                         provider, base or "")
                # Compute test-gap suggestions ONCE — reused for the body
                # checklist AND the panel (no duplicate whole-tree scan).
                try:
                    from helpers.test_gap_report import suggest_tests_for_diff
                    suggestions = suggest_tests_for_diff(path, self._cfg.git_exe, base or "")
                except Exception:
                    log.exception("PR draft: suggest_tests_for_diff failed")
                    suggestions = []
                try:
                    gaps_md = _render_coverage_gaps(suggestions)
                except Exception:
                    gaps_md = ""
                log.info("PR draft worker: %d gap(s); calling generate_pr_draft",
                         len(suggestions))
                result = generate_pr_draft(
                    self._cfg, path, base=base or "",
                    on_token=lambda d: q.put(("token", d)),
                    on_status=lambda p: q.put(("status", p)),
                    coverage_gaps_md=gaps_md,
                )
                log.info("PR draft worker: generate_pr_draft returned %d chars",
                         len(result) if result else 0)
            except Exception as exc:
                log.exception("PR draft worker failed")
                result, err = None, str(exc)
            # Capture on the worker thread — _tls.last_error is thread-local.
            diag = get_last_llm_error() if (result is None and err is None) else None
            q.put(("done", {
                "result": result, "err": err, "diag": diag,
                "suggestions": suggestions,
                "elapsed": int(time.monotonic() - _start_time),
            }))

        threading.Thread(target=_fetch, daemon=True).start()
        self._poll_pr_stream(ctx)

    @staticmethod
    def _pr_status_label(phase: str) -> str:
        """Map a generate_pr_draft on_status phase to a human status line."""
        return {
            "grounding":  "Grounding with tokensave + codegraph…",
            "generating": "Generating draft… (streaming)",
        }.get(phase, phase)

    def _open_pr_draft_dialog(self, path: str, base: str, provider: str = ""):
        """Open the standalone streaming PR-draft window; return its context dict.

        Standalone (no `transient`) → native min/max + its own taskbar entry, so
        it's alt-tab-able and never lost behind the main window. Singleton: a
        prior dialog is brought to front and — if dirty (streaming or user edits)
        — the user is asked before it's discarded. Returns the ctx dict, or None
        if the user declined to discard a dirty draft.
        """
        existing = self._pr_draft_dialog
        if existing is not None:
            try:
                alive = bool(existing.winfo_exists())
            except tk.TclError:
                alive = False
            if alive:
                existing.lift()
                try:
                    existing.focus_force()
                except tk.TclError:
                    pass
                if self._pr_draft_dirty and not messagebox.askyesno(
                        "Unsaved PR draft",
                        "The current PR draft is still generating or has unsaved "
                        "edits.\n\nDiscard it and start a new draft?",
                        parent=existing):
                    return None
                existing.destroy()
            self._pr_draft_dialog = None

        dlg = tk.Toplevel(self._root)
        self._pr_draft_dialog = dlg
        self._pr_draft_dirty = True            # streaming in progress
        dlg.title("PR Description Draft")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.minsize(620, 460)
        # NO transient() → standalone window with native min/max + a taskbar entry.
        dlg.lift()
        try:
            dlg.focus_force()
        except tk.TclError:
            pass

        def _on_destroy(e, _d=dlg):
            if e.widget is _d:
                self._pr_draft_dialog = None
                self._pr_draft_dirty = False
        dlg.bind("<Destroy>", _on_destroy, add="+")

        prog = [False]        # programmatic-insert guard (mutable for closures)
        streamed = [False]    # first real token clears the placeholder
        gh_exe = shutil.which("gh")

        # ── Header: status + grounding badge ──
        hdr = tk.Frame(dlg, bg=C["base"])
        status_var = tk.StringVar(value="Preparing…")
        tk.Label(hdr, textvariable=status_var, bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=12, pady=(8, 0))
        grounded = bool(self._cfg.enable_pr_grounding and
                        (self._cfg.tokensave_exe or self._cfg.codegraph_exe))
        badge_var = tk.StringVar(
            value="✓ Grounded: tokensave + codegraph" if grounded else "not grounded")
        tk.Label(hdr, textvariable=badge_var, bg=C["base"],
                 fg=(C["green"] if grounded else C["overlay0"]),
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=12, pady=(8, 0))

        # ── Body: text + scrollbars in their own grid frame (corner-to-corner) ──
        body = tk.Frame(dlg, bg=C["base"])
        txt = tk.Text(body, wrap=tk.NONE, bg=C["mantle"], fg=C["text"],
                      font=("Consolas", 9), relief=tk.FLAT, padx=8, pady=6)
        vsb = ttk.Scrollbar(body, orient="vertical",   command=txt.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        txt.insert(
            tk.END,
            "⏳  Preparing your PR draft…\n\n"
            "Reading your branch diff and grounding context. On local models the "
            "first tokens can take 30–90s — the draft will stream in here as it "
            "writes. Watch the status line above for progress.\n")
        txt.configure(state=tk.DISABLED)

        # Dirty tracking — genuine user edits only (our inserts set prog[0]).
        def _on_modified(_e=None):
            if prog[0]:
                txt.edit_modified(False)
                return
            self._pr_draft_dirty = True
            txt.edit_modified(False)   # re-arm: <<Modified>> fires only on False→True
        txt.bind("<<Modified>>", _on_modified, add="+")

        # ── Title field ──
        # NB: padding goes on the .pack() call below, NOT here — a tuple pady in
        # a widget constructor raises TclError ("bad screen distance") on strict
        # Tk builds; pack/grid accept the 2-tuple form.
        title_row = tk.Frame(dlg, bg=C["base"])
        tk.Label(title_row, text="PR title:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
        title_var = tk.StringVar(value="")
        ttk.Entry(title_row, textvariable=title_var, width=60).pack(
            side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

        # ── Buttons (disabled until the draft completes) ──
        def _live_body():
            return txt.get("1.0", tk.END).rstrip()

        btn_row = tk.Frame(dlg, bg=C["base"], padx=12, pady=8)
        copy_btn = ttk.Button(btn_row, text="Copy to clipboard", state=tk.DISABLED,
                              command=lambda: (dlg.clipboard_clear(),
                                               dlg.clipboard_append(_live_body())))
        copy_btn.pack(side=tk.LEFT)
        create_btn = ttk.Button(
            btn_row, text="Create PR on GitHub", state=tk.DISABLED,
            command=lambda: self._create_pr_via_gh(
                gh_exe, path, title_var.get(), _live_body(), dlg))
        create_btn.pack(side=tk.LEFT, padx=(6, 0))
        open_btn = ttk.Button(
            btn_row, text="Open in Browser", state=tk.DISABLED,
            command=lambda: self._open_pr_via_gh(gh_exe, path, _live_body(), dlg))
        open_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        # ── Test-gap panel mount point (filled on completion) ──
        gap_frame = tk.Frame(dlg, bg=C["base"])

        # Pack order: buttons + title pinned to bottom (always visible), gap panel
        # above them, header on top, body fills the remaining space.
        btn_row.pack(side=tk.BOTTOM, fill=tk.X)
        title_row.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 6))
        gap_frame.pack(side=tk.BOTTOM, fill=tk.X)
        hdr.pack(side=tk.TOP, fill=tk.X)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(6, 4))

        dlg.update_idletasks()
        w, h = 760, 560
        try:
            px = self._root.winfo_x() + (self._root.winfo_width()  - w) // 2
            py = self._root.winfo_y() + (self._root.winfo_height() - h) // 2
            dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            dlg.geometry(f"{w}x{h}")

        return {
            "dlg": dlg, "queue": queue.Queue(), "txt": txt, "vsb": vsb,
            "status_var": status_var, "badge_var": badge_var,
            "title_var": title_var, "gap_frame": gap_frame,
            "copy_btn": copy_btn, "create_btn": create_btn, "open_btn": open_btn,
            "gh_exe": gh_exe, "prog": prog, "streamed": streamed,
            "path": path, "base": base, "provider": provider,
            "phase": "Preparing…", "start": None,
        }

    def _poll_pr_stream(self, ctx: dict) -> None:
        """Drain streamed tokens from the queue into the dialog (Tk main thread).

        Single recursive after(50) loop (mirrors _poll_log_queue) with a bounded
        drain per tick so a token burst can't lock the event loop. Auto-scroll
        only when the user is already at the bottom.
        """
        dlg = ctx["dlg"]
        try:
            if not dlg.winfo_exists():
                return
        except tk.TclError:
            return
        q = ctx["queue"]
        txt = ctx["txt"]
        chunks: list = []
        budget = 0
        done = None
        try:
            while budget < 8000:        # bounded drain
                kind, data = q.get_nowait()
                if kind == "token":
                    chunks.append(data)
                    budget += len(data)
                elif kind == "status":
                    ctx["phase"] = self._pr_status_label(data)
                elif kind == "done":
                    done = data
                    break
        except queue.Empty:
            pass

        if chunks:
            try:
                at_bottom = ctx["vsb"].get()[1] >= 0.98
            except Exception:
                at_bottom = True
            ctx["prog"][0] = True
            txt.configure(state=tk.NORMAL)
            if not ctx["streamed"][0]:
                txt.delete("1.0", tk.END)        # clear the placeholder
                ctx["streamed"][0] = True
            txt.insert(tk.END, "".join(chunks))
            txt.configure(state=tk.DISABLED)
            txt.edit_modified(False)
            ctx["prog"][0] = False
            if at_bottom:
                txt.see(tk.END)

        # Live elapsed ticker until the first token arrives — local models spend
        # a long "prefill" reading the diff with NO output, which otherwise looks
        # frozen. Show the phase + seconds so the user can see it's alive.
        if done is None and not ctx["streamed"][0]:
            elapsed = int(time.monotonic() - ctx.get("start", time.monotonic()))
            hint = (" — the model is reading your diff; first tokens can take "
                    "30–90s on local models" if elapsed >= 8 else "")
            ctx["status_var"].set(f"{ctx['phase']}  ({elapsed}s){hint}")
        elif done is None:
            ctx["status_var"].set(ctx["phase"])

        if done is not None:
            self._finalize_pr_draft(ctx, done)
            return
        dlg.after(50, lambda: self._poll_pr_stream(ctx))

    def _finalize_pr_draft(self, ctx: dict, payload: dict) -> None:
        """Render the final body, enable actions, and attach the test-gap panel."""
        dlg = ctx["dlg"]
        try:
            if not dlg.winfo_exists():
                return
        except tk.TclError:
            return
        result, elapsed = payload["result"], payload["elapsed"]
        provider, base, path = ctx["provider"], ctx["base"], ctx["path"]

        # Failure modes — log, inform, and close the now-useless window.
        if payload["err"]:
            self._on_log(f"  ✗ PR draft failed ({elapsed}s): {payload['err']}", C["red"])
            messagebox.showerror("Draft PR — error", payload["err"], parent=dlg)
            dlg.destroy()
            return
        if result is None:
            reason = payload["diag"] or "LLM returned no output"
            self._on_log(f"  ✗ PR draft: no LLM response ({elapsed}s)", C["red"])
            messagebox.showerror(
                "Draft PR — no response from LLM",
                f"{reason}\n\nProvider: {provider}\nElapsed: {elapsed}s\n\n"
                "Check Settings → Commit Message LLM and verify:\n"
                "• The service is running\n"
                "• The model name is correct\n"
                "• The model is downloaded (ollama pull <model>)",
                parent=dlg)
            dlg.destroy()
            return
        if result.startswith("Empty diff"):
            self._on_log(f"  ✗ PR draft: empty diff ({elapsed}s)", C["yellow"])
            messagebox.showwarning(
                "Draft PR — no diff found",
                f"{result}\n\nBase branch used: {base!r}\n\n"
                "If this is wrong, right-click → Set PR base branch…\n"
                "to configure a different merge target.",
                parent=dlg)
            dlg.destroy()
            return

        # Success — replace the streamed raw text with the final processed body.
        txt = ctx["txt"]
        ctx["prog"][0] = True
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)
        txt.insert(tk.END, result)
        txt.edit_modified(False)
        ctx["prog"][0] = False
        # Editable now so the user can tweak before Create PR; dirty starts clean.
        self._pr_draft_dirty = False

        ctx["status_var"].set(
            f"✓ Draft ready ({elapsed}s) — review, edit, then Create PR")
        ctx["title_var"].set(_extract_pr_title(result))
        ctx["copy_btn"].configure(state=tk.NORMAL)
        if ctx["gh_exe"]:
            ctx["create_btn"].configure(state=tk.NORMAL)
            ctx["open_btn"].configure(state=tk.NORMAL)
            _Tooltip(ctx["create_btn"],
                     "Create the PR on GitHub directly. Edit the title above first.")
            _Tooltip(ctx["open_btn"],
                     "Open github.com's New PR page with this body pre-filled.")
        else:
            _Tooltip(ctx["create_btn"],
                     "GitHub CLI not on PATH. Install gh (cli.github.com) to enable.")
            _Tooltip(ctx["open_btn"],
                     "GitHub CLI not on PATH. Install gh (cli.github.com) to enable.")
        self._on_log(f"  ✓ PR draft ready ({elapsed}s)", C["green"])

        # Test-gap panel from the already-computed suggestions (no re-scan).
        # Closing a gap in the panel flips its body checklist line to [x].
        if base and payload.get("suggestions"):
            self._build_test_gap_panel(
                ctx["gap_frame"], path, base,
                suggestions=payload["suggestions"],
                on_tests_written=lambda paths: self._apply_gap_progress_to_body(
                    ctx, paths))

    # ------------------------------------------------------------------
    # Test gap panel helpers (Feature 2)
    # ------------------------------------------------------------------

    def _build_test_gap_panel(self, dlg, path: str, base: str,
                              suggestions: "list | None" = None,
                              on_tests_written=None) -> None:
        """Attach a collapsible 🧪 test-gap panel to *dlg* (Toplevel or Frame).

        If *suggestions* is provided (already computed by the caller), the panel
        fills synchronously with no extra coverage scan. Otherwise it runs
        ``suggest_tests_for_diff`` on a background thread and reveals the panel
        only if untested changed files are found.

        *on_tests_written* — optional callback ``(rel_paths: list[str]) -> None``
        invoked with the files that got a test written (AI ✓ or a fresh stub).
        The Draft PR dialog passes this to flip the body's coverage-gaps checklist
        to ``[x]``; the standalone window passes nothing (no-op).
        """
        import threading

        # Placeholder frame — packed after the button row; hidden until populated
        panel = tk.Frame(dlg, bg=C["surface0"], relief=tk.FLAT, bd=1)

        def _fetch():
            from helpers.test_gap_report import suggest_tests_for_diff
            try:
                suggestions = suggest_tests_for_diff(path, self._cfg.git_exe, base)
            except Exception:
                suggestions = []
            if dlg.winfo_exists():
                dlg.after(0, lambda s=suggestions: _populate(s))

        def _populate(suggestions: list) -> None:
            if not dlg.winfo_exists() or not suggestions:
                return
            panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
            _fill_panel(panel, suggestions)

        def _fill_panel(parent: tk.Frame, suggestions: list) -> None:
            """Build the panel widgets inside *parent*."""
            # Header row
            hdr = tk.Frame(parent, bg=C["surface0"])
            hdr.pack(fill=tk.X, padx=8, pady=(6, 2))
            tk.Label(
                hdr,
                text=f"🧪  {len(suggestions)} changed file(s) have no tests",
                font=("Segoe UI", 9, "bold"),
                bg=C["surface0"], fg=C["yellow"],
            ).pack(side=tk.LEFT)

            # AI master switch
            ai_available = bool(
                getattr(self._cfg, "claude_cli_exe", "") or
                self._cfg.raw.get("commit_message_llm", {}).get("provider")
            )
            ai_enabled_var = tk.BooleanVar(value=ai_available)
            ai_chk = themed_checkbutton(
                hdr,
                text="Enable AI generation",
                variable=ai_enabled_var,
                bg=C["surface0"], fg=C["subtext"],
                activebackground=C["surface0"],
                font=("Segoe UI", 9),
                state=tk.NORMAL if ai_available else tk.DISABLED,
            )
            ai_chk.pack(side=tk.RIGHT)

            # AI backend selector (persisted) — Auto / Claude CLI / Ollama. So the
            # user can force Ollama instead of silently getting "auto" → Claude CLI.
            _cli_ok = bool(getattr(self._cfg, "claude_cli_exe", ""))
            _llm_ok = bool(self._cfg.raw.get("commit_message_llm", {}).get("provider"))
            backend_var = tk.StringVar(value=(self._cfg.raw.get("test_gen_backend") or "auto"))
            if backend_var.get() == "claude_cli" and not _cli_ok:
                backend_var.set("auto")
            elif backend_var.get() == "llm" and not _llm_ok:
                backend_var.set("auto")
            backend_var.trace_add(
                "write", lambda *_a: self._cfg.raw.__setitem__(
                    "test_gen_backend", backend_var.get()))
            be_row = tk.Frame(parent, bg=C["surface0"])
            be_row.pack(fill=tk.X, padx=10, pady=(2, 0))
            tk.Label(be_row, text="AI backend:", bg=C["surface0"], fg=C["subtext"],
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
            for _bval, _blabel, _ok in (
                    ("auto", "Auto", _cli_ok or _llm_ok),
                    ("claude_cli", "Claude CLI", _cli_ok),
                    ("llm", "Ollama", _llm_ok)):
                ttk.Radiobutton(
                    be_row, text=_blabel, value=_bval, variable=backend_var,
                    state=(tk.NORMAL if _ok else tk.DISABLED)).pack(side=tk.LEFT, padx=(0, 6))

            # Proactive nudge — make the call-to-action explicit (nothing is
            # forced; the buttons below write files only when clicked).
            tk.Label(
                parent,
                text=(f"{len(suggestions)} changed file(s) lack tests — click "
                      "✓ Recommend (or pick files), then 📝 Generate stubs / "
                      "✨ AI generate to close the gap."),
                font=("Segoe UI", 8), bg=C["surface0"], fg=C["subtext"],
                anchor="w", justify=tk.LEFT, wraplength=560,
            ).pack(fill=tk.X, padx=10, pady=(0, 2))

            # Scrollable checkbox list
            scroll_outer = tk.Frame(parent, bg=C["surface0"])
            scroll_outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=2)

            canvas = tk.Canvas(scroll_outer, bg=C["surface0"],
                               highlightthickness=0, height=160)
            vsb = ttk.Scrollbar(scroll_outer, orient="vertical",
                                command=canvas.yview)
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            list_frame = tk.Frame(canvas, bg=C["surface0"])
            _cw = canvas.create_window((0, 0), window=list_frame, anchor="nw")

            def _sync_scroll(event, _c=canvas):
                _c.configure(scrollregion=_c.bbox("all"))

            def _sync_width(event, _c=canvas, _id=_cw):
                _c.itemconfig(_id, width=event.width)

            list_frame.bind("<Configure>", _sync_scroll)
            canvas.bind("<Configure>", _sync_width)

            def _on_wheel(event, _c=canvas):
                _c.yview_scroll(int(-1 * (event.delta / 120)), "units")

            for _w in (canvas, list_frame):
                _w.bind("<MouseWheel>", _on_wheel)

            # Parallel lists, grown by _add_rows (so the action buttons, which
            # capture these list objects, see appended update rows too). Nothing
            # pre-checked — the user opts in via ✓ Recommend or by hand.
            check_vars: list[tk.BooleanVar] = []
            status_vars: list[tk.StringVar] = []
            panel_suggestions: list = []

            def _add_rows(sugg_list, is_update=False):
                for sg in sugg_list:
                    panel_suggestions.append(sg)
                    var = tk.BooleanVar(value=False)
                    check_vars.append(var)
                    svar = tk.StringVar(value="")   # per-row ⏳ / ✓ / ✗
                    status_vars.append(svar)
                    row = tk.Frame(list_frame, bg=C["surface0"])
                    row.pack(fill=tk.X, pady=1)
                    themed_checkbutton(
                        row, variable=var, text=sg.rel_path,
                        bg=C["surface0"], fg=C["text"],
                        activebackground=C["surface0"],
                        font=("Consolas", 8), anchor="w",
                    ).pack(side=tk.LEFT)
                    _tag = (f"↻ regenerate ({sg.template})" if is_update
                            else f"→ {sg.template}")
                    tk.Label(row, text=_tag, bg=C["surface0"],
                             fg=(C["sky"] if is_update else C["overlay0"]),
                             font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 0))
                    tk.Label(row, textvariable=svar, bg=C["surface0"],
                             fg=C["subtext"], font=("Segoe UI", 9)).pack(
                                 side=tk.LEFT, padx=(6, 0))

            _add_rows(suggestions, is_update=False)

            # Also surface changed-but-tested files (regenerate candidates) on a
            # thread so the initial panel isn't blocked. They render tagged ↻ and
            # are opt-in (unchecked; Recommend does NOT auto-select them).
            def _fetch_updates():
                try:
                    from helpers.test_gap_report import suggest_test_updates_for_diff
                    ups = suggest_test_updates_for_diff(path, self._cfg.git_exe, base)
                except Exception:
                    ups = []
                if ups and dlg.winfo_exists():
                    dlg.after(0, lambda u=ups: _add_rows(u, is_update=True))
            threading.Thread(target=_fetch_updates, daemon=True).start()

            # Status line — shared by the quick-select row and the actions below.
            status_var = tk.StringVar()

            # Quick-select row — makes the "recommended" selection an explicit,
            # explained action instead of silently pre-checking boxes. Mirrors the
            # Git Commit dialog's Select All / None / Modified-Only pattern.
            def _select_recommended():
                # Recommend NEW high-ROI helpers; ↻ existing-test regenerations
                # stay opt-in (the user checks those by hand — regenerate is riskier).
                rec = [bool(sg.requires_automation and not sg.test_exists)
                       for sg in panel_suggestions]
                for v, r in zip(check_vars, rec):
                    v.set(r)
                status_var.set(
                    f"Recommended {sum(rec)} new high-ROI helper(s). "
                    "↻ existing-test regenerations are opt-in — check them by hand.")

            def _select_all():
                for v in check_vars:
                    v.set(True)
                status_var.set(f"Selected all {len(check_vars)}.")

            def _select_none():
                for v in check_vars:
                    v.set(False)
                status_var.set("Selection cleared.")

            qs_row = tk.Frame(parent, bg=C["surface0"])
            qs_row.pack(fill=tk.X, padx=8, pady=(2, 0))
            tk.Label(qs_row, text="Select:", bg=C["surface0"], fg=C["subtext"],
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 4))
            ttk.Button(qs_row, text="✓ Recommend",
                       command=_select_recommended).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Button(qs_row, text="All",
                       command=_select_all).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Button(qs_row, text="None",
                       command=_select_none).pack(side=tk.LEFT, padx=(0, 4))

            # Action buttons
            act_row = tk.Frame(parent, bg=C["surface0"])
            act_row.pack(fill=tk.X, padx=8, pady=(4, 6))

            status_lbl = tk.Label(
                act_row,
                textvariable=status_var,
                bg=C["surface0"], fg=C["subtext"],
                font=("Segoe UI", 8),
            )
            status_lbl.pack(side=tk.LEFT)

            cancel_event = threading.Event()

            stub_btn = ttk.Button(
                act_row, text="📝 Generate stubs",
                command=lambda: self._gap_generate_stubs(
                    panel_suggestions, check_vars, path,
                    status_var, stub_btn, ai_btn, cancel_event, dlg,
                    on_tests_written=on_tests_written),
            )
            stub_btn.pack(side=tk.RIGHT, padx=(4, 0))

            fail_btn = ttk.Button(
                act_row, text="View failures…", state=tk.DISABLED,
                command=lambda: self._show_ai_failures(dlg),
            )
            fail_btn.pack(side=tk.RIGHT, padx=(4, 0))

            ai_btn = ttk.Button(
                act_row, text="✨ AI generate selected",
                command=lambda: self._gap_generate_ai(
                    panel_suggestions, check_vars, status_vars, path,
                    status_var, stub_btn, ai_btn, fail_btn, ai_enabled_var,
                    cancel_event, dlg, backend_var,
                    on_tests_written=on_tests_written),
            )
            ai_btn.pack(side=tk.RIGHT, padx=(4, 0))

            cancel_btn = ttk.Button(
                act_row, text="Cancel",
                command=cancel_event.set,
            )
            cancel_btn.pack(side=tk.RIGHT, padx=(4, 0))

            # Agentic escape hatch: copy a prompt to paste into Claude Code (which
            # writes + verifies the tests itself), then ↻ Re-scan to pick them up.
            copy_cc_btn = ttk.Button(
                act_row, text="📋 Copy Claude Code prompt",
                command=lambda: self._gap_copy_claude_prompt(
                    panel_suggestions, check_vars, path, status_var, dlg),
            )
            copy_cc_btn.pack(side=tk.LEFT, padx=(0, 4))

            def _do_rescan():
                # Disable the action row for the worker's lifetime (no overlapping
                # scans / row-model races); the rebuild replaces these widgets.
                for _b in (stub_btn, ai_btn, fail_btn, cancel_btn,
                           copy_cc_btn, rescan_btn):
                    _b.configure(state=tk.DISABLED)
                status_var.set("Re-scanning gaps…")

                def _work():
                    from helpers.test_gap_report import suggest_tests_for_diff
                    try:
                        fresh = suggest_tests_for_diff(path, self._cfg.git_exe, base)
                    except Exception:
                        fresh = []

                    def _rebuild():
                        if not panel.winfo_exists():
                            return
                        for w in panel.winfo_children():
                            w.destroy()
                        if fresh:
                            _fill_panel(panel, fresh)
                        else:
                            tk.Label(
                                panel,
                                text="✓ No remaining coverage gaps on this branch.",
                                bg=C["surface0"], fg=C["green"],
                                font=("Segoe UI", 9),
                            ).pack(padx=10, pady=10)
                    if dlg.winfo_exists():
                        dlg.after(0, _rebuild)

                threading.Thread(target=_work, daemon=True).start()

            rescan_btn = ttk.Button(
                act_row, text="↻ Re-scan gaps", command=_do_rescan)
            rescan_btn.pack(side=tk.LEFT, padx=(4, 0))

            if not ai_available:
                ai_btn.configure(state=tk.DISABLED)
                _Tooltip(ai_btn,
                    "Configure Claude Code CLI or an LLM provider in Settings "
                    "to enable AI test generation.")

        if suggestions is not None:
            # Caller already computed the suggestions — fill synchronously, no
            # second whole-tree coverage scan.
            _populate(suggestions)
        else:
            threading.Thread(target=_fetch, daemon=True).start()

    def _gap_copy_claude_prompt(self, suggestions: list, check_vars: list,
                                path: str, status_var: tk.StringVar, dlg) -> None:
        """Copy a paste-into-Claude-Code prompt for the checked gaps.

        Claude Code is agentic (writes → runs pytest → fixes to green), unlike the
        manager's one-shot `--print` path — so we hand off and let the user re-scan.
        """
        from helpers.test_gen_llm import build_claude_code_handoff_prompt
        selected = [sg for sg, v in zip(suggestions, check_vars) if v.get()]
        if not selected:
            status_var.set("Nothing selected — check the files you want first.")
            return
        prompt = build_claude_code_handoff_prompt(selected, path)
        try:
            dlg.clipboard_clear()
            dlg.clipboard_append(prompt)
        except tk.TclError:
            status_var.set("Clipboard unavailable.")
            return
        status_var.set(
            f"Copied a Claude Code prompt for {len(selected)} file(s) — paste it into "
            "Claude Code, let it write + verify the tests, then click ↻ Re-scan gaps.")

    def _gap_generate_stubs(
        self, suggestions: list, check_vars: list, path: str,
        status_var: tk.StringVar, stub_btn, ai_btn,
        cancel_event: "threading.Event", dlg: tk.Toplevel,
        on_tests_written=None,
    ) -> None:
        """Generate template stubs for all checked entries."""
        import threading
        from helpers.test_scaffold import generate_test_file

        selected = [sg for sg, v in zip(suggestions, check_vars) if v.get()]
        if not selected:
            status_var.set("Nothing selected.")
            return

        stub_btn.configure(state=tk.DISABLED)
        ai_btn.configure(state=tk.DISABLED)
        status_var.set("Generating…")
        captured_root = path   # snapshot project root at button-press time

        def _run():
            ok, skipped = [], []
            for sg in selected:
                if cancel_event.is_set():
                    break
                try:
                    generate_test_file(captured_root, sg.source_path, sg.template)
                    ok.append(sg.rel_path)
                except FileExistsError:
                    skipped.append(sg.rel_path)
                except Exception as exc:
                    skipped.append(f"{sg.rel_path} ({exc})")
            if dlg.winfo_exists():
                dlg.after(0, lambda: _done(ok, skipped))

        def _done(ok: list, skipped: list) -> None:
            if not dlg.winfo_exists():
                return
            stub_btn.configure(state=tk.NORMAL)
            ai_btn.configure(state=tk.NORMAL)
            msg_parts = []
            if ok:
                msg_parts.append(f"Created: {', '.join(ok)}")
            if skipped:
                msg_parts.append(f"Skipped (already exist): {', '.join(skipped)}")
            status_var.set(" | ".join(msg_parts) or "Done.")
            # A fresh stub file also closes the "no test file" gap.
            if on_tests_written and ok:
                on_tests_written(ok)
            # Refresh Test Manager coverage view if open and same project
            if (self._test_manager_ref is not None
                    and self._test_manager_ref.winfo_exists()):
                try:
                    self._test_manager_ref.refresh_coverage()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _apply_gap_progress_to_body(self, ctx: dict, rel_paths: list) -> None:
        """Flip the Draft PR body's coverage-gap lines to [x] for *rel_paths*.

        Called when the embedded panel writes a passing test / stub. Edits only the
        matching ``- [ ]`` gap lines (see pr_draft._mark_gaps_addressed), guarded so
        it counts as a manager insert (not a user edit → no dirty flag) and can never
        disrupt the generation flow: a closed dialog, missing widget, or any glitch
        is a silent no-op.
        """
        try:
            txt = ctx.get("txt")
            if txt is None or not txt.winfo_exists():
                return
            from helpers.pr_draft import _mark_gaps_addressed
            current = txt.get("1.0", tk.END).rstrip("\n")
            updated = _mark_gaps_addressed(current, rel_paths)
            if updated == current:
                return
            yview = txt.yview()
            ctx["prog"][0] = True          # mark as our insert, not a user edit
            try:
                txt.configure(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                txt.insert("1.0", updated)
                txt.configure(state=tk.DISABLED)
                txt.edit_modified(False)
                txt.yview_moveto(yview[0])
            finally:
                ctx["prog"][0] = False
        except Exception:
            pass                            # never let a body refresh break generation

    def _gap_generate_ai(
        self, suggestions: list, check_vars: list, status_vars: list, path: str,
        status_var: tk.StringVar, stub_btn, ai_btn, fail_btn,
        ai_enabled_var: tk.BooleanVar,
        cancel_event: "threading.Event", dlg: tk.Toplevel,
        backend_var: "tk.StringVar | None" = None,
        on_tests_written=None,
    ) -> None:
        """Generate + VERIFY a test per checked entry; keep only passing tests.

        Each selected file goes through generate_verified_test: generate (valid
        Python guaranteed) → run under pytest → repair once on failure → write
        only if it passes, else discard. Per-row ⏳/✓/✗ + a summary; discarded
        files' pytest output is stashed for the "View failures…" button.
        """
        import threading
        from helpers.test_gen_llm import generate_verified_test

        if not ai_enabled_var.get():
            status_var.set("AI generation is disabled.")
            return

        selected = [(i, sg) for i, (sg, v) in enumerate(zip(suggestions, check_vars))
                    if v.get()]
        if not selected:
            status_var.set("Nothing selected.")
            return

        cancel_event.clear()           # fresh run (Event is reused across clicks)
        backend = backend_var.get() if backend_var is not None else "auto"
        stub_btn.configure(state=tk.DISABLED)
        ai_btn.configure(state=tk.DISABLED)
        fail_btn.configure(state=tk.DISABLED)
        captured_root = path           # snapshot project root at button-press time

        def _set_row(idx: int, glyph: str) -> None:
            if dlg.winfo_exists():
                dlg.after(0, lambda: status_vars[idx].set(glyph))

        def _set_status(text: str) -> None:
            if dlg.winfo_exists():
                dlg.after(0, lambda: status_var.set(text))

        def _make_token_cb(idx: int):
            """Per-row liveness: ⏳ (prefill, no tokens) → ✍ N (generating).

            on_token fires on the worker thread, so marshal to Tk via dlg.after.
            Throttled (every 5th token) so a long local run doesn't flood the
            event loop. The final ✓/✗ glyph is set by _set_row after completion.
            """
            counter = {"n": 0}

            def _cb(_delta: str) -> None:
                counter["n"] += 1
                n_tok = counter["n"]
                if (n_tok == 1 or n_tok % 5 == 0) and dlg.winfo_exists():
                    dlg.after(0, lambda n=n_tok: status_vars[idx].set(f"✍ {n}"))

            return _cb

        def _run():
            n = len(selected)
            passed = failed = 0
            reports: dict = {}
            written_paths: list = []     # rel_paths that now have a passing test
            for k, (idx, sg) in enumerate(selected, 1):
                if cancel_event.is_set():
                    break
                is_update = bool(getattr(sg, "test_exists", False))
                _set_row(idx, "⏳")
                _set_status(f"Verifying {k}/{n}…  (generate → run → repair)")
                res = generate_verified_test(
                    sg.source_path, captured_root,
                    backend=backend, cfg=self._cfg,
                    cancel_event=cancel_event,
                    template=getattr(sg, "template", None),
                    allow_overwrite=is_update,
                    target_path=(getattr(sg, "test_path", "") or None),
                    on_token=_make_token_cb(idx),
                )
                if res.status == "cancelled":
                    break
                if res.status == "pass":
                    passed += 1
                    written_paths.append(sg.rel_path)
                    # Partial pass: per-test pruning kept some, dropped the rest.
                    glyph = (f"✓ ({res.kept}/{res.total})"
                             if res.kept and res.total and res.kept < res.total
                             else "✓")
                    _set_row(idx, glyph)
                    if res.kept and res.kept < res.total:
                        reports[sg.rel_path] = res.report      # show what was dropped
                    self._on_log(
                        f"  ✓ AI test {'updated' if is_update else 'written'} + "
                        f"passing ({glyph}): {sg.rel_path}", C["green"])
                else:
                    failed += 1
                    # Distinguish a failed regenerate (original preserved) from a
                    # failed new-file generate (nothing written).
                    if res.preserved_existing:
                        _set_row(idx, "↻✗")
                        reports[sg.rel_path] = (
                            "[Update failed] the regenerated test failed the runtime "
                            "gate (or dropped coverage); the original test file was "
                            "preserved on disk.\n\n" + (res.report or res.status))
                    else:
                        _set_row(idx, "✗")
                        reports[sg.rel_path] = res.report or res.status
                    self._on_log(
                        f"  ✗ AI test discarded ({res.status}): {sg.rel_path}",
                        C["yellow"])
            self._last_ai_fail_reports = reports
            if dlg.winfo_exists():
                dlg.after(0, lambda: _done(passed, failed, written_paths))

        def _done(passed: int, failed: int, written_paths: list) -> None:
            if not dlg.winfo_exists():
                return
            stub_btn.configure(state=tk.NORMAL)
            ai_btn.configure(state=tk.NORMAL)
            # Enable "View failures…" if anything failed OR a partial pass dropped
            # some tests (those reports are stashed too).
            has_reports = bool(getattr(self, "_last_ai_fail_reports", None))
            fail_btn.configure(state=(tk.NORMAL if (failed or has_reports)
                                      else tk.DISABLED))
            if cancel_event.is_set():
                status_var.set(f"Cancelled — {passed} ✓ / {failed} ✗ so far.")
            else:
                status_var.set(
                    f"Done: {passed} ✓ / {failed} ✗ — passing (incl. pruned-partial) "
                    "tests were written.")
            # Reflect closed gaps in the PR-body checklist (Draft PR dialog only).
            if on_tests_written and written_paths:
                on_tests_written(written_paths)
            # Refresh Test Manager if still open and same project
            if (captured_root == self._git_path
                    and self._test_manager_ref is not None
                    and self._test_manager_ref.winfo_exists()):
                try:
                    self._test_manager_ref.refresh_coverage()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _show_ai_failures(self, parent) -> None:
        """Read-only window with the last AI run's discarded-test pytest output."""
        from tkinter import scrolledtext
        reports = getattr(self, "_last_ai_fail_reports", {}) or {}
        win = tk.Toplevel(parent)
        win.title("AI test generation — failures")
        win.configure(bg=C["base"])
        win.geometry("800x540")
        st = scrolledtext.ScrolledText(
            win, wrap=tk.NONE, bg=C["mantle"], fg=C["text"],
            font=("Consolas", 9), relief=tk.FLAT, padx=8, pady=6)
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        if not reports:
            st.insert(tk.END, "No failures recorded for the last run.")
        else:
            st.insert(tk.END,
                      f"{len(reports)} test(s) were generated but discarded because "
                      "they failed when run.\nFix the source or the test, then "
                      "re-run AI generate.\n\n")
            for rel, rep in reports.items():
                st.insert(tk.END, f"{'=' * 72}\n✗ {rel}\n{'=' * 72}\n{rep}\n\n")
        st.configure(state=tk.DISABLED)            # read-only
        ttk.Button(win, text="Close", command=win.destroy).pack(
            side=tk.BOTTOM, anchor=tk.E, padx=10, pady=(0, 10))

    def _open_pr_via_gh(self, gh_exe: str, path: str, body_text: str, dlg) -> None:
        """Write body to a temp file and spawn `gh pr create --web --body-file`.

        Using a temp file (rather than `--body` with the literal string) avoids
        Windows command-line length limits AND multi-line / quote escaping
        problems entirely. `--web` opens the GitHub New-PR page in the user's
        default browser with the body pre-filled; gh itself exits immediately
        after spawning the browser, so we don't need to capture output.

        Failures (missing remote, no commits to PR, gh auth not set up, etc.)
        surface as a messagebox so the user isn't left wondering why nothing
        happened.
        """
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".md",
                    prefix="pr-body-", delete=False) as f:
                f.write(body_text)
                tmp_path = f.name
        except OSError as e:
            messagebox.showerror(
                "Open PR on GitHub failed",
                f"Could not write temp body file: {e}",
                parent=dlg)
            return
        try:
            # cwd=path so `gh` picks up the project's repo / remote
            subprocess.Popen(
                [gh_exe, "pr", "create", "--web", "--body-file", tmp_path],
                cwd=path, creationflags=CREATE_NO_WINDOW)
            self._on_log(
                "  Opening GitHub New-PR page in your browser…", C["sky"])
        except OSError as e:
            messagebox.showerror(
                "Open PR on GitHub failed",
                f"Could not spawn gh: {e}",
                parent=dlg)
            return
        # NB: tmp_path is left on disk intentionally. gh reads it lazily after
        # the browser opens, so deleting it immediately would race. The OS will
        # clean it up from %TEMP% eventually.

    def _create_pr_via_gh(self, gh_exe: "str | None", path: str, title: str,
                          body_text: str, dlg) -> None:
        """Run `gh pr create` directly — no browser, PR is created immediately.

        Runs on a background thread; all UI callbacks are scheduled via dlg.after().
        """
        import tempfile, webbrowser

        title = title.strip()
        if not title:
            messagebox.showwarning("Create PR", "Enter a PR title first.", parent=dlg)
            return

        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Create PR",
                "Could not detect base branch.\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push the branch and set a tracking upstream first.",
                parent=dlg)
            return

        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".md",
                    prefix="pr-body-", delete=False) as f:
                f.write(body_text)
                tmp_path = f.name
        except OSError as e:
            messagebox.showerror("Create PR", f"Could not write temp file: {e}", parent=dlg)
            return

        gh_base = base.split("/")[-1]  # strip "origin/" prefix; gh needs bare branch name

        def _run():
            try:
                result = subprocess.run(
                    [gh_exe, "pr", "create",
                     "--title", title,
                     "--body-file", tmp_path,
                     "--base", gh_base],
                    capture_output=True, text=True, encoding="utf-8",
                    cwd=path, creationflags=CREATE_NO_WINDOW, timeout=30)
            except subprocess.TimeoutExpired:
                dlg.after(0, lambda: messagebox.showerror(
                    "Create PR", "gh timed out. Check your network / gh auth status.",
                    parent=dlg))
                return
            except OSError as e:
                dlg.after(0, lambda msg=str(e): messagebox.showerror("Create PR", msg, parent=dlg))
                return

            if result.returncode != 0:
                err = (result.stderr or result.stdout or "unknown error").strip()
                dlg.after(0, lambda: messagebox.showerror(
                    "Create PR failed",
                    f"gh pr create exited {result.returncode}:\n\n{err[:600]}",
                    parent=dlg))
                return

            url = (result.stdout or "").strip()
            self._on_log(f"  PR created: {url}", C["green"])
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            dlg.after(0, lambda: _on_success(url))

        def _on_success(url: str):
            if messagebox.askyesno(
                    "PR Created",
                    f"Pull request created:\n{url}\n\nOpen in browser?",
                    parent=dlg):
                webbrowser.open(url)
            dlg.destroy()

        threading.Thread(target=_run, daemon=True).start()
