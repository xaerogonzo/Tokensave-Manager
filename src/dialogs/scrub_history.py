"""ScrubHistoryDialog — Advanced "erase from all history" privacy feature (v4.5).

This dialog is INTENTIONALLY hard to use accidentally. Layered safety
nets — read the v4.5 plan section "Flow B" in
``~/.claude/plans/write-a-comprehensive-plan-elegant-cascade.md`` for the
full design rationale.

User flow:
  1. Open from GitignoreDialog → "⚙ Advanced" disclosure button.
  2. Filter-repo availability gate — install via pip if absent.
  3. Pick the file to scrub (tracked-but-ignored list, or manual entry).
  4. Workflow-ordering preamble — untrack + commit FIRST if file still
     in HEAD; gates the Scrub Now button.
  5. Affected-commits display — show what will be erased.
  6. Auto backup branch — names + creates `backup/before-scrub-<ts>`.
  7. Confirmation-phrase entry — must type the file's basename.
  8. Scrub Now → runs `git filter-repo --invert-paths --path <file> --force`.
  9. Post-scrub guidance — exact force-push command + re-clone warning.

The dialog never auto-pushes. Force-push is left to the user (matches
universal industry guidance).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.git import _find_tracked_but_ignored
from helpers.git_scrub import (
    build_backup_branch_name,
    create_backup_branch,
    install_filter_repo,
    is_tracked_in_head,
    list_affected_commits,
    preflight,
    run_scrub,
    untrack_and_commit,
    working_tree_clean,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class ScrubHistoryDialog(tk.Toplevel):
    """Erase a single file from ALL git history with layered safety nets.

    Constructed from ``GitignoreDialog`` after the user clicks "⚙ Advanced".
    Operates on the same project path as the parent gitignore dialog.
    """

    def __init__(self, parent, path: str, cfg: "ManagerConfig",
                 initial_file: str = ""):
        super().__init__(parent)
        self._parent_dialog = parent
        self._path = path
        self._cfg  = cfg
        self.title(f"Scrub from History — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 620)
        self.grab_set()
        self.transient(parent)

        # State
        self._selected_file = tk.StringVar(value=initial_file)
        self._confirm_phrase = tk.StringVar()
        self._scrub_in_flight = False
        self._backup_branch_name = ""
        self._preflight = preflight(self._path, self._cfg.git_exe)

        # Build sections — Save-style: bottom bar FIRST so it's always visible.
        self._build_destructive_banner()
        self._build_bottom_action_bar()
        self._build_filter_repo_gate()
        self._build_file_picker_section()
        self._build_workflow_ordering_preamble()
        self._build_affected_commits_section()
        self._build_backup_branch_section()
        self._build_confirmation_phrase_section()
        self._build_log_pane()

        self._refresh_state()
        self._centre_on_parent(parent)

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_destructive_banner(self):
        """Layer 9 — hard "this is destructive" red banner."""
        banner = tk.Frame(self, bg=C["red"], padx=14, pady=10)
        banner.pack(fill=tk.X)
        tk.Label(banner, text="⚠  Destructive — rewrites git history",
                 bg=C["red"], fg=C["base"],
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        for bullet in (
            "Every past commit that touched the file will be rewritten.",
            "Force-push is required afterwards (collaborators must re-clone).",
            "Older clones / archived branches on GitHub may still expose the file.",
        ):
            tk.Label(banner, text=f"  • {bullet}",
                     bg=C["red"], fg=C["base"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W)

    def _build_bottom_action_bar(self):
        """Bottom action row — Scrub Now + Close.

        Packed with ``side=tk.BOTTOM`` BEFORE the scrollable middle sections,
        so it can never be pushed off-screen (same pattern as the fixed
        GitignoreDialog Save bar).
        """
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, side=tk.BOTTOM)
        btns = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        self._scrub_btn = ttk.Button(
            btns, text="🧨  Scrub Now",
            command=self._on_scrub_now, state=tk.DISABLED,
        )
        self._scrub_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _build_filter_repo_gate(self):
        """Layer 1 — filter-repo availability gate + install helper."""
        wrap = tk.LabelFrame(
            self, text="Step 1 — `git filter-repo` availability",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.X, padx=18, pady=(10, 4))
        self._fr_status_lbl = tk.Label(
            wrap, text="(checking…)", bg=C["base"], fg=C["text"],
            font=("Segoe UI", 9), anchor=tk.W, justify=tk.LEFT,
        )
        self._fr_status_lbl.pack(anchor=tk.W, padx=10, pady=(6, 2))
        self._fr_install_btn = ttk.Button(
            wrap, text="📥  Install filter-repo (pip install --user git-filter-repo)",
            command=self._on_install_filter_repo,
        )
        self._fr_install_btn.pack(anchor=tk.W, padx=10, pady=(0, 8))

    def _build_file_picker_section(self):
        """Layer 3 — file picker (tracked-but-ignored list + manual entry)."""
        wrap = tk.LabelFrame(
            self, text="Step 2 — pick a file to erase",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.X, padx=18, pady=(4, 4))

        # Manual entry
        row = tk.Frame(wrap, bg=C["base"])
        row.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(row, text="Relative path:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        entry = ttk.Entry(row, textvariable=self._selected_file,
                          font=("Consolas", 9))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind("<KeyRelease>", lambda _e: self._refresh_state())
        ttk.Button(row, text="Browse…",
                   command=self._on_browse).pack(side=tk.LEFT)

        # Tracked-but-ignored quick picker
        tracked = []
        try:
            if self._cfg.git_exe:
                tracked = _find_tracked_but_ignored(
                    self._path, self._cfg.git_exe)
        except Exception:
            tracked = []
        if tracked:
            tk.Label(wrap,
                     text="Tracked-but-ignored candidates (click to fill):",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Segoe UI", 8)).pack(anchor=tk.W, padx=10,
                                                pady=(4, 2))
            list_wrap = tk.Frame(wrap, bg=C["mantle"])
            list_wrap.pack(fill=tk.X, padx=10, pady=(0, 8))
            for f in tracked[:20]:
                row = tk.Frame(list_wrap, bg=C["mantle"])
                row.pack(fill=tk.X, padx=2, pady=1)
                lbl = tk.Label(row, text=f, bg=C["mantle"], fg=C["text"],
                               font=("Consolas", 9), cursor="hand2",
                               anchor=tk.W)
                lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
                lbl.bind("<Button-1>",
                         lambda _e, p=f: self._fill_path(p))

    def _build_workflow_ordering_preamble(self):
        """Layer 2 — untrack + commit preamble (gates Scrub Now)."""
        self._wf_wrap = tk.LabelFrame(
            self, text="Step 3 — untrack and commit first",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        self._wf_wrap.pack(fill=tk.X, padx=18, pady=(4, 4))
        self._wf_status_lbl = tk.Label(
            self._wf_wrap, text="(pick a file first)",
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 9), anchor=tk.W, justify=tk.LEFT,
            wraplength=580,
        )
        self._wf_status_lbl.pack(anchor=tk.W, padx=10, pady=(6, 2))
        self._wf_action_btn = ttk.Button(
            self._wf_wrap, text="Untrack + commit now",
            command=self._on_untrack_and_commit,
            state=tk.DISABLED,
        )
        self._wf_action_btn.pack(anchor=tk.W, padx=10, pady=(0, 8))

    def _build_affected_commits_section(self):
        """Layer 5 — affected-commits display."""
        wrap = tk.LabelFrame(
            self, text="Step 4 — commits that will be rewritten",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(4, 4))
        self._affected_txt = tk.Text(
            wrap, height=5, font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._affected_txt.pack(fill=tk.BOTH, expand=True,
                                 padx=10, pady=(6, 8))

    def _build_backup_branch_section(self):
        """Layer 6 — auto backup branch name display."""
        wrap = tk.Frame(self, bg=C["base"])
        wrap.pack(fill=tk.X, padx=18, pady=(4, 2))
        self._backup_lbl = tk.Label(
            wrap, text="Backup branch (auto-created before scrub): "
                       "backup/before-scrub-<ts>",
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8, "italic"), anchor=tk.W,
        )
        self._backup_lbl.pack(anchor=tk.W)

    def _build_confirmation_phrase_section(self):
        """Layer 7 — confirmation-phrase entry (must type filename)."""
        wrap = tk.LabelFrame(
            self, text="Step 5 — type the filename to confirm",
            fg=C["yellow"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.X, padx=18, pady=(4, 4))
        row = tk.Frame(wrap, bg=C["base"])
        row.pack(fill=tk.X, padx=10, pady=(6, 8))
        tk.Label(row, text="Type the file's basename:",
                 bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        entry = ttk.Entry(row, textvariable=self._confirm_phrase,
                          font=("Consolas", 9))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<KeyRelease>", lambda _e: self._refresh_state())
        self._confirm_hint = tk.Label(
            wrap, text="", bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8))
        self._confirm_hint.pack(anchor=tk.W, padx=10, pady=(0, 6))

    def _build_log_pane(self):
        """Read-only log display for subprocess output."""
        wrap = tk.LabelFrame(
            self, text="Output",
            fg=C["overlay0"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
        )
        wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(4, 4))
        self._log_txt = tk.Text(
            wrap, height=6, font=("Consolas", 8),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._log_txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 8))

    def _centre_on_parent(self, parent):
        self.update_idletasks()
        w, h = 720, 720
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── State refresh — runs after every input change ─────────────────────────

    def _refresh_state(self):
        """Recompute all section states + scrub-button enablement.

        Single source of truth for the dialog's reactive UI.  Reads
        ``self._preflight`` (cached at open) + the current text-entry
        values.  Heavy work (filter-repo install, untrack-commit, scrub)
        runs in worker threads; this method only updates widget state.
        """
        # Filter-repo gate
        if self._preflight.get("filter_repo"):
            self._fr_status_lbl.configure(
                text="✓ filter-repo is installed.",
                fg=C["green"])
            self._fr_install_btn.configure(state=tk.DISABLED)
        else:
            self._fr_status_lbl.configure(
                text="✗ filter-repo is NOT installed. Click below to "
                     "install via pip (one-time, ~5 s).",
                fg=C["red"])
            self._fr_install_btn.configure(state=tk.NORMAL)

        # File picker → workflow preamble
        rel_file = (self._selected_file.get() or "").strip()
        if not rel_file:
            self._wf_status_lbl.configure(
                text="(pick a file first)",
                fg=C["overlay0"])
            self._wf_action_btn.configure(state=tk.DISABLED)
        elif not self._preflight.get("git_exe_present"):
            self._wf_status_lbl.configure(
                text="git executable not configured — set "
                     "Settings → Git tools first.",
                fg=C["red"])
            self._wf_action_btn.configure(state=tk.DISABLED)
        else:
            try:
                tracked = is_tracked_in_head(
                    self._path, self._cfg.git_exe, rel_file)
                clean = working_tree_clean(
                    self._path, self._cfg.git_exe)
            except Exception:
                tracked = False
                clean = False
            if tracked:
                self._wf_status_lbl.configure(
                    text=f"'{rel_file}' is still tracked in HEAD. "
                         "filter-repo will refuse to run until it's "
                         "untracked + committed. Click below to do that "
                         "in one step.",
                    fg=C["peach"])
                self._wf_action_btn.configure(state=tk.NORMAL)
            elif not clean:
                self._wf_status_lbl.configure(
                    text="Working tree has uncommitted changes. "
                         "Commit or stash them before scrubbing.",
                    fg=C["peach"])
                self._wf_action_btn.configure(state=tk.DISABLED)
            else:
                self._wf_status_lbl.configure(
                    text=f"✓ '{rel_file}' is no longer tracked in HEAD "
                         "and the working tree is clean — ready to scrub.",
                    fg=C["green"])
                self._wf_action_btn.configure(state=tk.DISABLED)

        # Affected commits
        self._refresh_affected_commits(rel_file)

        # Backup branch label
        if rel_file and self._preflight.get("git_exe_present"):
            self._backup_branch_name = build_backup_branch_name()
            self._backup_lbl.configure(
                text=f"Backup branch (created before scrub): "
                     f"{self._backup_branch_name}    "
                     "(use `git reset --hard <branch>` to restore before "
                     "force-push)")

        # Confirmation phrase
        basename = os.path.basename(rel_file) if rel_file else ""
        typed = self._confirm_phrase.get().strip()
        if not basename:
            self._confirm_hint.configure(
                text="(pick a file first)", fg=C["overlay0"])
            confirm_ok = False
        elif typed != basename:
            self._confirm_hint.configure(
                text=f"Type exactly: {basename}",
                fg=C["overlay0"])
            confirm_ok = False
        else:
            self._confirm_hint.configure(text="✓", fg=C["green"])
            confirm_ok = True

        # Scrub Now enablement
        ready = (
            self._preflight.get("filter_repo")
            and rel_file
            and self._preflight.get("git_exe_present")
            and self._preflight.get("is_git_repo")
            and confirm_ok
            and not self._scrub_in_flight
        )
        # Also require: file no longer tracked + working tree clean
        if ready and rel_file:
            try:
                still_tracked = is_tracked_in_head(
                    self._path, self._cfg.git_exe, rel_file)
                clean = working_tree_clean(
                    self._path, self._cfg.git_exe)
                ready = (not still_tracked) and clean
            except Exception:
                ready = False
        self._scrub_btn.configure(state=tk.NORMAL if ready else tk.DISABLED)

    def _refresh_affected_commits(self, rel_file: str):
        self._affected_txt.configure(state=tk.NORMAL)
        self._affected_txt.delete("1.0", tk.END)
        if not rel_file or not self._preflight.get("git_exe_present"):
            self._affected_txt.insert(
                "1.0", "(pick a file to see affected commits)")
        else:
            try:
                commits = list_affected_commits(
                    self._path, self._cfg.git_exe, rel_file, max_n=200)
            except Exception as exc:
                commits = []
                self._affected_txt.insert("1.0", f"(error: {exc})")
            if commits:
                for sha, subject in commits:
                    self._affected_txt.insert(
                        tk.END, f"{sha}  {subject}\n")
                self._affected_txt.insert(
                    tk.END,
                    f"\nTotal: {len(commits)} commit"
                    f"{'s' if len(commits) != 1 else ''} touch this file.\n")
            elif not self._affected_txt.get("1.0", tk.END).strip():
                self._affected_txt.insert(
                    "1.0", "(no commits found that touch this file)")
        self._affected_txt.configure(state=tk.DISABLED)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _fill_path(self, p: str):
        self._selected_file.set(p)
        self._refresh_state()

    def _on_browse(self):
        picked = filedialog.askopenfilename(
            title="Pick the file to scrub from history",
            initialdir=self._path, parent=self)
        if not picked:
            return
        try:
            rel = os.path.relpath(picked, self._path).replace("\\", "/")
        except ValueError:
            messagebox.showwarning(
                "File outside project",
                "The file must be inside the project directory.",
                parent=self)
            return
        if rel.startswith(".."):
            messagebox.showwarning(
                "File outside project",
                "The file must be inside the project directory.",
                parent=self)
            return
        self._selected_file.set(rel)
        self._refresh_state()

    def _on_install_filter_repo(self):
        self._fr_install_btn.configure(state=tk.DISABLED)
        self._fr_status_lbl.configure(
            text="Installing filter-repo via pip…", fg=C["peach"])
        self._log_clear()

        def _worker():
            ok, log = install_filter_repo(on_log=self._log_append_threadsafe)
            self.after(0, lambda: _done(ok, log))

        def _done(ok, log):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if ok:
                # Re-probe filter-repo.
                self._preflight = preflight(self._path, self._cfg.git_exe)
            self._refresh_state()
            if not ok:
                self._fr_status_lbl.configure(
                    text="Install failed — see the Output pane.",
                    fg=C["red"])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_untrack_and_commit(self):
        rel_file = (self._selected_file.get() or "").strip()
        if not rel_file:
            return
        if not messagebox.askyesno(
                "Untrack and commit?",
                f"Run `git rm --cached -- {rel_file}` and commit?\n\n"
                "This is the lightweight 'stop tracking from now on' step "
                "that has to happen before the full history scrub. The "
                "file stays on disk; only its index entry is removed.",
                parent=self, default="no"):
            return
        self._wf_action_btn.configure(state=tk.DISABLED)
        self._log_append(f"--- Untracking + committing {rel_file} ---")

        def _worker():
            ok, log = untrack_and_commit(
                self._path, self._cfg.git_exe, rel_file,
                on_log=self._log_append_threadsafe,
            )
            self.after(0, lambda: _done(ok, log))

        def _done(ok, _log):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            # Re-probe working tree state.
            self._preflight = preflight(self._path, self._cfg.git_exe)
            self._refresh_state()
            if not ok:
                self._log_append("[untrack+commit FAILED — see output above]")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scrub_now(self):
        rel_file = (self._selected_file.get() or "").strip()
        if not rel_file:
            return
        # Final confirmation messagebox — last gate before destruction.
        if not messagebox.askyesno(
                "Erase from all history?",
                f"This will REWRITE every commit that touched "
                f"'{rel_file}'.\n\n"
                f"A backup branch '{self._backup_branch_name}' will be "
                "created first so you can recover if something goes "
                "wrong.\n\n"
                "After the scrub, you'll need to force-push manually.\n\n"
                "Proceed?",
                parent=self, default="no", icon="warning"):
            return

        self._scrub_in_flight = True
        self._scrub_btn.configure(state=tk.DISABLED, text="🧨  Scrubbing…")
        self._log_append(f"--- Creating backup branch "
                          f"{self._backup_branch_name} ---")

        def _worker():
            # 1. Backup branch
            ok_backup, log_b = create_backup_branch(
                self._path, self._cfg.git_exe, self._backup_branch_name)
            self._log_append_threadsafe(log_b)
            if not ok_backup:
                self.after(0, lambda: _done(False,
                    "Backup branch creation FAILED — aborting before scrub."))
                return
            # 2. Scrub
            self._log_append_threadsafe(
                f"--- Running filter-repo on {rel_file} ---")
            ok_scrub, _ = run_scrub(
                self._path, self._cfg.git_exe, rel_file,
                on_log=self._log_append_threadsafe,
            )
            self.after(0, lambda: _done(ok_scrub, ""))

        def _done(ok: bool, extra_msg: str):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            self._scrub_in_flight = False
            self._scrub_btn.configure(text="🧨  Scrub Now")
            if extra_msg:
                self._log_append(extra_msg)
            if ok:
                self._show_postscrub_guidance(rel_file)
            else:
                self._log_append(
                    "[SCRUB FAILED — your branch is unchanged. "
                    "Backup branch (if created) is still around.]")
            self._refresh_state()

        threading.Thread(target=_worker, daemon=True).start()

    def _show_postscrub_guidance(self, rel_file: str):
        """Layer 8 — post-scrub force-push guidance."""
        head = self._preflight.get("head_branch") or "<branch>"
        guidance = (
            f"✓ Scrub succeeded. '{rel_file}' is no longer in any commit.\n\n"
            f"Next step (manual — review then run):\n\n"
            f"    git push --force-with-lease origin {head}\n\n"
            f"⚠ Anyone who has cloned this repo will need to re-clone — "
            f"their old history is now divergent.\n\n"
            f"To undo (only works BEFORE you force-push):\n\n"
            f"    git reset --hard {self._backup_branch_name}"
        )
        self._log_append("\n" + guidance)
        messagebox.showinfo(
            "Scrub complete — manual force-push required",
            guidance, parent=self)

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_clear(self):
        self._log_txt.configure(state=tk.NORMAL)
        self._log_txt.delete("1.0", tk.END)
        self._log_txt.configure(state=tk.DISABLED)

    def _log_append(self, line: str):
        try:
            self._log_txt.configure(state=tk.NORMAL)
            self._log_txt.insert(tk.END, line + "\n")
            self._log_txt.see(tk.END)
            self._log_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _log_append_threadsafe(self, line: str):
        try:
            self.after(0, lambda l=line: self._log_append(l))
        except tk.TclError:
            pass
