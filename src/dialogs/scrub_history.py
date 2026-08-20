"""ScrubHistoryDialog — Erase a file from GitHub history (v4.5 / v4.9).

Goal: remove a sensitive file (build script, credential, etc.) from the
remote repository's history so it can no longer be downloaded from GitHub.

How it works (unavoidable, by how git is designed):
  GitHub stores whatever git history you push to it.  To change what
  GitHub has, you must rewrite your LOCAL git history first, then
  force-push so GitHub adopts the rewritten version.  There is no way
  to edit GitHub directly — git filter-repo is the standard tool for the
  local rewrite step.

User flow:
  1. Open from GitignoreDialog → "⚙ Advanced" disclosure button.
  2. Filter-repo availability gate — install via pip if absent.
  3. Pick the file to erase from GitHub history.
  4. Workflow-ordering preamble — untrack + commit FIRST if file still
     in HEAD; gates the Scrub Now button.
  5. Affected-commits display — show what will be rewritten.
  6. Auto backup branch — names + creates `backup/before-scrub-<ts>`.
  7. Confirmation-phrase entry — must type the file's basename.
  8. Scrub Now → runs `git filter-repo --invert-paths --path <file> --force`
     to rewrite local history.
  9. Force Push → pushes the rewritten history to GitHub so the remote
     is also cleaned.  (Step 8 is pointless without this step.)

If you have already run Scrub Now in a previous session, the Force Push
button appears immediately when 0 affected commits are found — no need
to re-run the scrub.  Just re-add your remote via Set Remote in the Git
tab, then open this dialog and force-push.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from constants import C
from theme import UiPumpMixin, bind_mousewheel
from helpers.git import _find_tracked_but_ignored
from helpers.git_scrub import (
    build_backup_branch_name,
    create_backup_branch,
    force_push,
    get_remote_url,
    git_rm_cached,
    install_filter_repo,
    is_tracked_in_head,
    list_affected_commits,
    preflight,
    restore_remote_if_missing,
    run_scrub,
    working_tree_clean,
)

if TYPE_CHECKING:
    from state import ManagerConfig


class ScrubHistoryDialog(UiPumpMixin, tk.Toplevel):
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

        # State
        self._selected_file = tk.StringVar(value=initial_file)
        self._confirm_phrase = tk.StringVar()
        self._scrub_in_flight = False
        self._backup_branch_name = ""
        self._preflight = preflight(self._path, self._cfg.git_exe)
        self._already_scrubbed = False   # True when 0 commits found + not in HEAD
        self._saved_remote_url = ""      # Captured before scrub; filter-repo always removes origin

        # ── Worker -> UI channel ──────────────────────────────────────────
        # Every background worker in this dialog used to call
        # `self.after(0, ...)` directly. That is a cross-thread Tk call, and
        # on Linux it does not raise — it BLOCKS. A CI diagnostic caught a
        # scrub worker alive after 10s having scheduled nothing and raised
        # nothing, which is the worst shape a bug can take: no error, no log
        # line, no way to tell what it was waiting for.
        #
        # Workers now hand callables to UiPumpMixin's queue and a
        # main-thread pump runs them, so no worker touches Tk at all. Same
        # pattern as GitTabController._poll_log_queue.

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

        # Workers post to _ui_queue; nothing runs it until this starts.
        self._start_ui_pump()

    # ── Section builders ──────────────────────────────────────────────────────

    def _build_destructive_banner(self):
        """Top banner — explains the two-step process and destructive nature."""
        banner = tk.Frame(self, bg=C["red"], padx=14, pady=10)
        banner.pack(fill=tk.X)
        tk.Label(banner, text="⚠  Erase from GitHub history — rewrites git commits",
                 bg=C["red"], fg=C["base"],
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        for bullet in (
            "Step 1 (Scrub Now): rewrites your LOCAL history — the file disappears from all commits.",
            "Step 2 (Force Push): pushes rewritten history to GitHub — the file is gone from the remote.",
            "Anyone who cloned the repo will need to re-clone after force-push.",
            "Files on disk are NOT deleted — only the git history is changed.",
        ):
            tk.Label(banner, text=f"  • {bullet}",
                     bg=C["red"], fg=C["base"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W)

    def _build_bottom_action_bar(self):
        """Bottom action row — Scrub Now + (post-scrub) Force Push + Close.

        Packed with ``side=tk.BOTTOM`` BEFORE the scrollable middle sections,
        so it can never be pushed off-screen (same pattern as the fixed
        GitignoreDialog Save bar).

        The Force Push button is created here but not packed — it becomes
        visible in ``_show_postscrub_guidance`` after a successful scrub.
        """
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, side=tk.BOTTOM)
        self._btns_frame = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        self._btns_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # Force-push button — hidden until scrub succeeds
        self._force_push_btn = ttk.Button(
            self._btns_frame, text="⬆  Force Push to GitHub",
            command=self._on_force_push,
        )

        self._scrub_btn = ttk.Button(
            self._btns_frame, text="🧨  Scrub Now",
            command=self._on_scrub_now, state=tk.DISABLED,
        )
        self._scrub_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(self._btns_frame, text="Close",
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
        """Layer 3 — file picker (tracked-but-ignored list + manual entry).

        The candidate list uses a Canvas+Scrollbar so it stays height-capped
        and scrollable no matter how many files are tracked.  A ▼/▶ toggle
        collapses/expands the list so other sections remain reachable.
        """
        wrap = tk.LabelFrame(
            self, text="Step 2 — pick a file to erase",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "bold"),
        )
        wrap.pack(fill=tk.X, padx=18, pady=(4, 4))

        # Manual entry row
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

        # Tracked-but-ignored quick picker — scrollable, collapsible
        tracked = []
        try:
            if self._cfg.git_exe:
                tracked = _find_tracked_but_ignored(
                    self._path, self._cfg.git_exe)
        except Exception:
            tracked = []
        if not tracked:
            return

        # Header row with collapse toggle
        hdr = tk.Frame(wrap, bg=C["base"])
        hdr.pack(fill=tk.X, padx=10, pady=(4, 2))
        tk.Label(hdr, text="Tracked-but-ignored candidates (click to fill):",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self._picker_collapsed = False
        toggle_btn = tk.Label(hdr, text="▼ Hide", bg=C["base"],
                              fg=C["blue"], font=("Segoe UI", 8),
                              cursor="hand2")
        toggle_btn.pack(side=tk.RIGHT)

        # Scrollable canvas list (capped at ~190 px ≈ 8–9 rows)
        list_outer = tk.Frame(wrap, bg=C["mantle"])
        list_outer.pack(fill=tk.X, padx=10, pady=(0, 8))

        canvas = tk.Canvas(list_outer, bg=C["mantle"],
                           highlightthickness=0, height=190)
        vsb = ttk.Scrollbar(list_outer, orient="vertical",
                            command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bind_mousewheel(canvas)

        body = tk.Frame(canvas, bg=C["mantle"])
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        body.bind("<Configure>",
                  lambda e: canvas.configure(
                      scrollregion=canvas.bbox("all")))

        for f in tracked:
            lbl = tk.Label(body, text=f, bg=C["mantle"], fg=C["text"],
                           font=("Consolas", 9), cursor="hand2",
                           anchor=tk.W, padx=6, pady=2)
            lbl.pack(fill=tk.X)
            lbl.bind("<Button-1>", lambda _e, p=f: self._fill_path(p))
            lbl.bind("<Enter>",
                     lambda e: e.widget.configure(bg=C["surface0"]))
            lbl.bind("<Leave>",
                     lambda e: e.widget.configure(bg=C["mantle"]))

        # Collapse/expand toggle wires list_outer visibility
        def _toggle(_evt=None):
            if self._picker_collapsed:
                list_outer.pack(fill=tk.X, padx=10, pady=(0, 8))
                toggle_btn.configure(text="▼ Hide")
                self._picker_collapsed = False
            else:
                list_outer.pack_forget()
                toggle_btn.configure(text="▶ Show")
                self._picker_collapsed = True
        toggle_btn.bind("<Button-1>", _toggle)

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
        rel_file = (self._selected_file.get() or "").strip()
        self._refresh_filter_repo_section()
        self._refresh_workflow_section(rel_file)
        self._refresh_affected_commits(rel_file)
        if rel_file and self._preflight.get("git_exe_present"):
            self._backup_branch_name = build_backup_branch_name()
            self._backup_lbl.configure(
                text=f"Backup branch (created before scrub): "
                     f"{self._backup_branch_name}    "
                     "(use `git reset --hard <branch>` to restore before "
                     "force-push)")
        confirm_ok = self._refresh_confirm_section(rel_file)
        self._refresh_scrub_button(rel_file, confirm_ok)

    def _refresh_filter_repo_section(self):
        """Update the filter-repo installation status label and install button."""
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

    def _refresh_workflow_section(self, rel_file: str):
        """Update the workflow-preamble status and action button for *rel_file*."""
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

    def _refresh_confirm_section(self, rel_file: str) -> bool:
        """Update the confirmation-phrase hint; return True when phrase matches."""
        basename = os.path.basename(rel_file) if rel_file else ""
        typed = self._confirm_phrase.get().strip()
        if not basename:
            self._confirm_hint.configure(
                text="(pick a file first)", fg=C["overlay0"])
            return False
        if typed != basename:
            self._confirm_hint.configure(
                text=f"Type exactly: {basename}",
                fg=C["overlay0"])
            return False
        self._confirm_hint.configure(text="✓", fg=C["green"])
        return True

    def _refresh_scrub_button(self, rel_file: str, confirm_ok: bool):
        """Update Scrub Now enablement and Force Push button visibility."""
        ready = (
            self._preflight.get("filter_repo")
            and rel_file
            and self._preflight.get("git_exe_present")
            and self._preflight.get("is_git_repo")
            and confirm_ok
            and not self._scrub_in_flight
            and not self._already_scrubbed
        )
        # Also require: file no longer tracked + working tree clean.
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

        # Force Push button visibility — driven entirely by _already_scrubbed.
        if self._already_scrubbed:
            self._scrub_btn.configure(state=tk.DISABLED, text="✓ Scrubbed")
            self._force_push_btn.configure(
                state=tk.NORMAL, text="⬆  Force Push to GitHub")
            try:
                self._force_push_btn.pack_info()   # already visible — no-op
            except tk.TclError:
                self._force_push_btn.pack(side=tk.RIGHT, padx=(6, 0))
        else:
            # File not yet scrubbed — reset scrub button label and hide Force Push.
            if self._scrub_btn.cget("text") in ("✓ Scrubbed", "🧨  Scrubbing…"):
                self._scrub_btn.configure(text="🧨  Scrub Now")
            try:
                self._force_push_btn.pack_info()   # visible — hide it
                self._force_push_btn.pack_forget()
            except tk.TclError:
                pass   # already hidden

    def _refresh_affected_commits(self, rel_file: str):
        self._affected_txt.configure(state=tk.NORMAL)
        self._affected_txt.delete("1.0", tk.END)
        self._already_scrubbed = False

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
            else:
                # 0 commits found. Check if file is absent from HEAD too —
                # that's the signature of a completed scrub.
                try:
                    still_tracked = is_tracked_in_head(
                        self._path, self._cfg.git_exe, rel_file)
                except Exception:
                    still_tracked = True
                if not still_tracked:
                    self._already_scrubbed = True
                    self._affected_txt.insert(
                        "1.0",
                        "✓ No commits found touching this file — local history\n"
                        "  is already clean (Scrub Now ran in a previous session).\n\n"
                        "  Next step: click ⬆  Force Push to GitHub below\n"
                        "  to push the rewritten history to the remote.\n\n"
                        "  (If you haven't re-added your remote yet, use\n"
                        "  Set Remote in the Git tab first.)")
                else:
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
            self._post(lambda: _done(ok, log))

        def _done(ok, log):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if not ok:
                self._fr_status_lbl.configure(
                    text="Install failed — see the Output pane below.",
                    fg=C["red"])
                self._fr_install_btn.configure(state=tk.NORMAL)
                return

            # pip exited 0 — re-probe to confirm git can see the script.
            self._preflight = preflight(self._path, self._cfg.git_exe)
            if not self._preflight.get("filter_repo"):
                # pip succeeded but git's subcommand lookup still fails.
                # Most common cause: pip --user Scripts dir not on PATH in
                # the current process snapshot. The user should restart the
                # manager so the new PATH is inherited.  The scrub will also
                # try the standalone script directly, so it may work anyway.
                self._fr_status_lbl.configure(
                    text="pip install succeeded but git can't locate filter-repo yet.\n"
                         "Restart the manager to refresh PATH, then try again —\n"
                         "or click Scrub Now directly; the manager will try the\n"
                         "standalone script as a fallback.",
                    fg=C["peach"])
                self._fr_install_btn.configure(state=tk.DISABLED)
                # Force filter_repo=True so Scrub Now isn't blocked — the
                # run_scrub fallback path will handle actual invocation.
                self._preflight["filter_repo"] = True
            self._refresh_state()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_untrack_and_commit(self):
        """Untrack the file (git rm --cached) then open GitCommitDialog.

        The commit dialog gives the user AI-suggest for the message instead
        of auto-committing with a hardcoded string.
        """
        rel_file = (self._selected_file.get() or "").strip()
        if not rel_file:
            return
        if not messagebox.askyesno(
                "Untrack file?",
                f"Run `git rm --cached -- {rel_file}` then open the Commit\n"
                "dialog so you can compose (and AI-suggest) the message?\n\n"
                "The file stays on disk; only its tracking entry is removed.",
                parent=self, default="no"):
            return
        self._wf_action_btn.configure(state=tk.DISABLED)
        self._log_append(f"--- Untracking {rel_file} ---")

        def _worker():
            ok, log = git_rm_cached(
                self._path, self._cfg.git_exe, rel_file,
                on_log=self._log_append_threadsafe,
            )
            self._post(lambda: _done(ok))

        def _done(ok: bool):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if not ok:
                self._log_append("[git rm --cached FAILED — see output above]")
                self._preflight = preflight(self._path, self._cfg.git_exe)
                self._refresh_state()
                return
            # Open the full commit dialog so the user gets AI-suggest.
            self._open_commit_dialog_after_untrack(rel_file)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_commit_dialog_after_untrack(self, rel_file: str):
        """Open GitCommitDialog after ``git rm --cached`` succeeds.

        The dialog's callback runs the actual ``git commit`` so the user
        can review, AI-suggest, and edit the message before committing.
        """
        import subprocess as _sp

        try:
            from constants import CREATE_NO_WINDOW as _CNW
        except ImportError:
            _CNW = 0

        try:
            status_proc = _sp.run(
                [self._cfg.git_exe, "-C", self._path, "status", "--short"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
                creationflags=_CNW,
            )
            status_out = status_proc.stdout
        except Exception:
            status_out = ""

        def _commit_callback(path: str, message: str, selected: list):
            """Stage-and-commit callback wired into GitCommitDialog."""
            if not selected:
                return
            # selected may be list[str] (legacy) or list[(filename, xy)]
            if selected and isinstance(selected[0], str):
                selected = [(f, "??") for f in selected]

            # xy[1] == ' ' means index-only change — already staged, no add.
            files_to_add = [f for f, xy in selected
                            if len(xy) >= 2 and xy[1] != " "]

            def _worker():
                try:
                    if files_to_add:
                        _sp.run(
                            [self._cfg.git_exe, "-C", path, "add", "--"]
                            + files_to_add,
                            capture_output=True, timeout=15,
                            encoding="utf-8", errors="replace",
                            creationflags=_CNW,
                        )
                    _sp.run(
                        [self._cfg.git_exe, "-C", path, "commit", "-m", message],
                        capture_output=True, timeout=30,
                        encoding="utf-8", errors="replace",
                        creationflags=_CNW,
                    )
                finally:
                    self._post(self._post_commit_refresh)

            threading.Thread(target=_worker, daemon=True).start()

        from dialogs.git_commit import GitCommitDialog
        GitCommitDialog(self, self._path, status_out, True,
                        _commit_callback, self._cfg)

    def _post_commit_refresh(self):
        """Re-probe git state after the commit dialog completes."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._preflight = preflight(self._path, self._cfg.git_exe)
        self._refresh_state()

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
                self._post(lambda: _done(False,
                    "Backup branch creation FAILED — aborting before scrub."))
                return
            # 2. Snapshot remote URL — filter-repo unconditionally removes
            #    all remotes; we restore it automatically before force-push.
            self._saved_remote_url = get_remote_url(
                self._path, self._cfg.git_exe)
            if self._saved_remote_url:
                self._log_append_threadsafe(
                    f"(remote URL saved: {self._saved_remote_url})")

            # 3. Scrub
            self._log_append_threadsafe(
                f"--- Running filter-repo on {rel_file} ---")
            ok_scrub, _ = run_scrub(
                self._path, self._cfg.git_exe, rel_file,
                on_log=self._log_append_threadsafe,
            )
            self._post(lambda: _done(ok_scrub, ""))

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
        """Layer 8 — post-scrub: surface the Force Push button.

        Rather than showing a messagebox with a manual command to copy,
        we reveal the Force Push button in the bottom action bar so the
        user can complete the workflow entirely within the manager.
        """
        head = self._preflight.get("head_branch") or "HEAD"
        self._head_branch = head
        self._log_append(
            f"\n✓ Scrub complete — '{rel_file}' erased from all commits.\n"
            f"\nTo undo (BEFORE force-push only):\n"
            f"    git reset --hard {self._backup_branch_name}\n"
            f"\nWhen ready, click ⬆  Force Push to GitHub below."
        )
        # Swap bottom-bar: dim Scrub Now, reveal Force Push
        self._scrub_btn.configure(state=tk.DISABLED, text="✓ Scrubbed")
        self._force_push_btn.pack(side=tk.RIGHT, padx=(6, 0))

    def _on_force_push(self):
        """Force-push the rewritten history to origin."""
        head = getattr(self, "_head_branch", None) \
               or self._preflight.get("head_branch") or "HEAD"
        if not messagebox.askyesno(
                "⚠  Force Push — overwrite remote history?",
                f"Push rewritten history to origin/{head}.\n\n"
                "Anyone who has cloned this repo will need to re-clone "
                "afterwards — their local history is now divergent.\n\n"
                "Proceed?",
                parent=self, default="no", icon="warning"):
            return

        self._force_push_btn.configure(state=tk.DISABLED, text="⬆  Pushing…")
        self._log_append(f"\n--- Force-pushing to origin/{head} ---")

        def _worker():
            # filter-repo removes 'origin' during the scrub — re-add it
            # automatically so the push doesn't fail with "not a git repository".
            # _saved_remote_url is populated when the scrub ran in this session.
            # If the dialog was re-opened after a previous scrub session the URL
            # may be empty; in that case we try to read it from a preflight hint
            # first, then ask the user if still missing.
            url = self._saved_remote_url
            if not url:
                # Try preflight (reads existing remote config)
                url = self._preflight.get("remote_url", "")
            if not url:
                # Last resort: ask via main thread, block worker until answered
                q: queue.Queue = queue.Queue()
                from tkinter.simpledialog import askstring
                def _ask():
                    val = askstring(
                        "Remote URL needed",
                        "filter-repo removed the origin remote in a previous "
                        "session.\n\nEnter the GitHub URL to re-add it:",
                        parent=self)
                    q.put(val or "")
                self._post(_ask)
                # Bounded. This worker is asking the UI a question and
                # blocking on the reply; without a timeout a dialog that is
                # closing, or a pump that has stopped, wedges the thread
                # permanently with nothing on screen to say why.
                try:
                    url = q.get(timeout=300)
                except queue.Empty:
                    self._log_append_threadsafe(
                        "No remote URL supplied (timed out waiting for the "
                        "prompt) — skipping remote restore.")
                    url = ""
            if url:
                self._saved_remote_url = url  # cache for subsequent pushes
            restore_remote_if_missing(
                self._path, self._cfg.git_exe,
                "origin", url,
                on_log=self._log_append_threadsafe,
            )
            ok, _ = force_push(
                self._path, self._cfg.git_exe, head,
                on_log=self._log_append_threadsafe,
            )
            self._post(lambda: _done(ok))

        def _done(ok: bool):
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if ok:
                self._force_push_btn.configure(
                    state=tk.DISABLED, text="✓ Force-pushed")
                self._log_append(
                    "✓ Force-push succeeded — remote history is now clean.")
                messagebox.showinfo(
                    "Done — remote history rewritten",
                    "Force-push succeeded.\n\n"
                    "The file is gone from the remote repository's history.\n\n"
                    "Anyone who had a clone will need to re-clone to stay\n"
                    "in sync.",
                    parent=self)
            else:
                self._force_push_btn.configure(
                    state=tk.NORMAL, text="⬆  Force Push to GitHub")
                self._log_append(
                    "[Force-push FAILED — check output above. "
                    "If the remote has new commits, run git pull --rebase "
                    "first, then try again.]")

        threading.Thread(target=_worker, daemon=True).start()

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
            self._post(lambda l=line: self._log_append(l))
        except tk.TclError:
            pass
