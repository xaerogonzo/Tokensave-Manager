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
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW, _ANSI, _GIT_ENV_NO_PROMPT
from theme import _Tooltip
from helpers.git import _is_local_git_repo
from helpers.llm import _is_auth_error
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
                creationflags=CREATE_NO_WINDOW,
            )
            return proc.returncode, proc.stdout.strip()
        except Exception:
            return 1, ""

    # Step 1: tracked upstream (e.g. "origin/main")
    rc, out = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc == 0 and out:
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
        self._git_path: "str | None"   = None
        self._git_status_files: list   = []
        self._git_all_btns: list       = []
        self._git_push_pull_btns: list = []
        self._git_release_btns: list   = []
        self._git_op_in_flight: bool   = False
        self._log_queue                = queue.Queue()
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

        for btn in (btn_push, btn_pull, btn_fetch, btn_commit, btn_undo,
                    btn_new, btn_switch, btn_merge, btn_del, btn_openpr,
                    btn_mergepr, btn_release, btn_draft_pr):
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
            "Primary click: uses Claude Code CLI if configured, otherwise the API key.\n"
            "Right-click or Shift+click: choose which tool to use.\n\n"
            "CLI mode opens a new terminal window running `claude` with a write\n"
            "instruction — your app stays unblocked while it runs.\n"
            "API mode drafts the description inline and shows it in a dialog.")
        btn_draft_pr.bind("<Button-3>",
            lambda e: self._show_draft_pr_menu(e, btn_draft_pr))
        btn_draft_pr.bind("<Shift-Button-1>",
            lambda e: self._show_draft_pr_menu(e, btn_draft_pr))

        self._git_all_btns       = [self._btn_set_remote, btn_push, btn_pull,
                                     btn_commit, btn_undo, btn_new,
                                     btn_switch, btn_merge, btn_del, btn_openpr,
                                     btn_mergepr, btn_release, btn_draft_pr]
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
          "auto"       — CLI if available, else API key
          "claude_cli" — force Claude Code CLI
          "llm"        — force API key path
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
                    "No API configured",
                    "draft_pr_backend=llm but no API key is configured.\n"
                    "Add a key in Settings → AI commit messages.",
                    parent=self._root)
        else:  # "auto"
            if cli:
                self._draft_pr_via_cli(path)
            elif has_api:
                self._draft_pr_via_api(path)
            else:
                messagebox.showinfo(
                    "No AI configured",
                    "Configure a Claude Code CLI path or an API key in Settings to use Draft PR.",
                    parent=self._root)

    def _show_draft_pr_menu(self, event, btn):
        """Show an override menu for right-click / Shift+click on Draft PR."""
        path = self._git_path
        if not path:
            return
        menu = tk.Menu(self._tab, tearoff=0)
        menu.add_command(label="Use Claude Code CLI",
                         command=lambda: self._draft_pr_via_cli(path))
        menu.add_command(label="Use API key (inline dialog)",
                         command=lambda: self._draft_pr_via_api(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _draft_pr_via_cli(self, path: str):
        from helpers.claude_cli import spawn_claude_cli   # lazy import
        base = _detect_base_branch(path, self._cfg.git_exe)
        if base is None:
            messagebox.showerror(
                "Draft PR — base branch not found",
                "Could not detect the base branch for this PR.\n\n"
                "Push to a remote, or set a tracking branch with:\n"
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
        instruction = (
            f"Draft a PR description for this branch against `{base}`. "
            f"Run `git log {base}..HEAD --oneline` to see the commits, then "
            f"`git diff {base}...HEAD` to see the full diff (triple-dot gives the "
            f"merge-base diff, isolating only this branch's changes — not upstream). "
            f"Write the PR description to PR_DRAFT.md in the current working directory "
            f"(use a relative path — just PR_DRAFT.md, not an absolute path). "
            f"Include: a one-line summary, bullet list of key changes, and a testing checklist. "
            f"{gh_step}"
        )
        ok, err = spawn_claude_cli(
            self._cfg.claude_cli_exe, path, instruction,
            model=self._cfg.claude_cli_model,
        )
        if not ok:
            messagebox.showerror("Claude Code CLI error", err, parent=self._root)

    def _draft_pr_via_api(self, path: str):
        import threading
        self._on_log("  Drafting PR description via API…", C["blue"])

        def _fetch():
            from helpers.pr_draft import generate_pr_draft   # lazy import
            result = generate_pr_draft(self._cfg, path)
            self._tab.after(0, lambda text=result: self._show_pr_draft_dialog(text, path))

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_pr_draft_dialog(self, text: "str | None", path: str):
        if not self._tab.winfo_exists():
            return
        if not text:
            messagebox.showinfo("Draft PR", "No response from AI — check your API settings.",
                                parent=self._root)
            return
        dlg = tk.Toplevel(self._root)
        dlg.title("PR Description Draft")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.minsize(600, 400)
        dlg.transient(self._root)

        txt = tk.Text(dlg, wrap=tk.NONE, bg=C["mantle"], fg=C["text"],
                      font=("Consolas", 9), relief=tk.FLAT, padx=8, pady=6)
        vsb = ttk.Scrollbar(dlg, orient="vertical",   command=txt.yview)
        hsb = ttk.Scrollbar(dlg, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.insert(tk.END, text)
        txt.configure(state=tk.DISABLED)

        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        txt.pack(side=tk.LEFT,   fill=tk.BOTH, expand=True)

        # Title field — pre-filled from generated text; user can edit before creating
        title_row = tk.Frame(dlg, bg=C["base"], padx=12, pady=(6, 0))
        title_row.pack(fill=tk.X)
        tk.Label(title_row, text="PR title:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
        title_var = tk.StringVar(value=_extract_pr_title(text))
        ttk.Entry(title_row, textvariable=title_var, width=60).pack(
            side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

        def _copy():
            dlg.clipboard_clear()
            dlg.clipboard_append(text)

        gh_exe = shutil.which("gh")

        btn_row = tk.Frame(dlg, bg=C["base"], padx=12, pady=8)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Copy to clipboard", command=_copy).pack(side=tk.LEFT)

        # "Create PR" — runs gh pr create directly; body from live text widget content
        create_btn = ttk.Button(
            btn_row, text="Create PR on GitHub",
            command=lambda: self._create_pr_via_gh(
                gh_exe, path, title_var.get(),
                txt.get("1.0", tk.END).rstrip(), dlg))
        create_btn.pack(side=tk.LEFT, padx=(6, 0))
        if not gh_exe:
            create_btn.configure(state=tk.DISABLED)
            _Tooltip(create_btn,
                "GitHub CLI not on PATH. Install gh (cli.github.com) to enable.")
        else:
            _Tooltip(create_btn,
                "Create the PR on GitHub directly. Edit the title above first.")

        # "Open in Browser" — pre-fills gh's web form; user sets title/draft there
        open_btn = ttk.Button(
            btn_row, text="Open in Browser",
            command=lambda: self._open_pr_via_gh(gh_exe, path, text, dlg))
        open_btn.pack(side=tk.LEFT, padx=(6, 0))
        if not gh_exe:
            open_btn.configure(state=tk.DISABLED)
            _Tooltip(open_btn,
                "GitHub CLI not on PATH. Install gh (cli.github.com) "
                "to open a pre-filled PR-create page in your browser.")
        else:
            _Tooltip(open_btn,
                "Open github.com's New PR page with this body pre-filled. "
                "You pick the title, base branch, and draft state there.")

        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        dlg.update_idletasks()
        w, h = 720, 520
        try:
            px = self._root.winfo_x() + (self._root.winfo_width()  - w) // 2
            py = self._root.winfo_y() + (self._root.winfo_height() - h) // 2
            dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            dlg.geometry(f"{w}x{h}")

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

        base = _detect_base_branch(path, self._cfg.git_exe)
        if base is None:
            messagebox.showerror(
                "Create PR",
                "Could not detect base branch.\n"
                "Push the branch and set a tracking upstream first.",
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
                dlg.after(0, lambda: messagebox.showerror("Create PR", str(e), parent=dlg))
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
