"""GitCommitDialog — stage and commit changes in a project's git repository.

Shows the working-tree files as a checklist so the user can pick which
files to include in this commit. On commit, only checked files are
staged and committed; un-checked files stay as uncommitted changes.

Smart commit-message suggestion runs via the async orchestrator in
helpers.commit_messages — heuristics inline when LLM is off, daemon
thread + spinner when LLM is on. The user can edit the field at any
time; stale async results are silently dropped when the user has
started typing.

Takes `cfg: ManagerConfig` per Round 4 Phase C. The cfg flows down to
`_suggest_commit_message(path, status, cfg.raw, cfg.git_exe)` — passing
the raw dict for the LLM config lookup AND the live git_exe for the
diff helpers. Reads happen at execution time (Rule 3 — no snapshot
caches), so a Settings → Save propagates without restarting.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
from helpers.commit_messages import _suggest_commit_message
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class GitCommitDialog(tk.Toplevel):
    """
    Stage and commit changes in a project's git repository.
    Shows the working-tree files as a checklist so the user can pick which
    files to include in this commit. On commit, only checked files are staged
    and committed; un-checked files stay as un-committed changes.
    """

    # status-char meanings shown next to filenames
    _STATUS_DESC = {
        "M":  "modified",
        "A":  "added",
        "D":  "deleted",
        "R":  "renamed",
        "C":  "copied",
        "U":  "conflict",
        "?":  "untracked",
        "!":  "ignored",
    }

    def __init__(self, parent, path, status_text: str, is_repo: bool,
                 callback, cfg: "ManagerConfig"):
        """
        callback(path, message, selected_files): called on Commit.
        selected_files is a list of paths (relative to repo root) to stage+commit.
        status_text: output of `git status --short` (may be empty).
        is_repo: False disables the Commit button and shows a warning.
        cfg: ManagerConfig — passed to _suggest_commit_message for LLM config + git_exe.
        """
        super().__init__(parent)
        self.title(f"Git Commit — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 480)
        self.grab_set()
        self.transient(parent)
        self._path       = path
        self._callback   = callback
        self._is_repo    = is_repo
        self._status_raw = status_text
        self._cfg        = cfg
        self._files      = self._parse_status(status_text, is_repo)
        # Async-suggestion infrastructure (see _populate_suggestion docstring).
        self._suggestion_token = 0
        self._user_has_edited  = False

        self._build_header(path)
        self._build_file_list(is_repo)
        self._build_quick_select(is_repo)
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(2, 8))
        self._build_message_area(is_repo, status_text)
        self._build_actions(is_repo)
        self._size_and_centre(parent)

    # ── Construction helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_status(status_text: str, is_repo: bool) -> list:
        """Parse `git status --short` output into [(xy, fname), ...].

        CRITICAL: do NOT call .strip() on status_text BEFORE splitlines().
        `git status --short` lines have a 2-char status (XY) + 1 space +
        path. For working-tree-modified files the first char is a space
        (" M file.py"). Calling strip() on the whole multi-line string
        eats that leading space from the FIRST line only, shifting its
        columns by 1, which caused `line[3:]` to drop the first character
        of the path — e.g. ".claude/settings.json" became
        "claude/settings.json" and git add failed with pathspec error.
        """
        files = []
        if is_repo:
            for line in status_text.splitlines():
                if len(line) >= 4:
                    files.append((line[:2], line[3:]))
        return files

    def _build_header(self, path: str) -> None:
        tk.Label(self,
                 text="📝  Git Commit",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))
        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

    def _build_file_list(self, is_repo: bool) -> None:
        """Build the scrollable file checklist (canvas + body) and populate it."""
        hdr_row = tk.Frame(self, bg=C["base"])
        hdr_row.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(hdr_row,
                 text="Pick files to include in this commit:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        if self._files:
            tk.Label(hdr_row,
                     text=f"  ({len(self._files)} changed)",
                     font=("Segoe UI", 9),
                     bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        # Scrollable checklist via canvas + frame (body parented to self so
        # children render reliably on Windows — same pattern as the wizard).
        list_outer = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        list_outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=160)
        _vsb = ttk.Scrollbar(list_outer, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        # Populate
        self._file_vars: list = []   # parallel to self._files — list of (var, fname, xy)
        if not is_repo:
            tk.Label(self._list_body,
                     text="⚠  Not a git repository — run 🔧 Git Init first.",
                     bg=C["mantle"], fg=C["red"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
            return
        if not self._files:
            tk.Label(self._list_body,
                     text="(nothing to commit — working tree clean)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
            return
        for xy, fname in self._files:
            self._add_file_row(xy, fname)

    def _add_file_row(self, xy: str, fname: str) -> None:
        """Render one checkbox + colour-coded status badge + filename row."""
        var = tk.BooleanVar(value=True)
        self._file_vars.append((var, fname, xy))
        row = tk.Frame(self._list_body, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=1)
        xy_clean = xy.strip() or "?"
        status_char = xy_clean[0]
        color = {
            "M": C["yellow"], "A": C["green"], "D": C["red"],
            "R": C["sky"], "C": C["sky"], "U": C["red"],
            "?": C["blue"], "!": C["overlay0"],
        }.get(status_char, C["text"])
        desc = self._STATUS_DESC.get(status_char, status_char)
        cb = tk.Checkbutton(row, variable=var,
                            bg=C["mantle"], activebackground=C["mantle"],
                            selectcolor=C["surface0"])
        cb.pack(side=tk.LEFT)
        tk.Label(row, text=xy_clean, width=3, anchor=tk.W,
                 font=("Consolas", 9, "bold"),
                 bg=C["mantle"], fg=color).pack(side=tk.LEFT)
        tk.Label(row, text=fname, anchor=tk.W,
                 font=("Consolas", 9),
                 bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(2, 6))
        tk.Label(row, text=f"({desc})", anchor=tk.W,
                 font=("Segoe UI", 8, "italic"),
                 bg=C["mantle"], fg=C["overlay0"]).pack(side=tk.LEFT)

    def _build_quick_select(self, is_repo: bool) -> None:
        if not (is_repo and self._files):
            return
        sel_row = tk.Frame(self, bg=C["base"])
        sel_row.pack(fill=tk.X, padx=20, pady=(0, 8))
        ttk.Button(sel_row, text="Select All",
                   command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(sel_row, text="Select None",
                   command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(sel_row, text="Modified Only  (skip untracked)",
                   command=self._select_modified).pack(side=tk.LEFT)

    def _build_message_area(self, is_repo: bool, status_text: str) -> None:
        """Commit-message field + spinner label + Suggest button + key bindings."""
        msg_hdr = tk.Frame(self, bg=C["base"])
        msg_hdr.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(msg_hdr,
                 text="Commit message:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        # Spinner / status label — empty when idle, "⟳ Generating…" while async.
        self._suggest_status_lbl = tk.Label(
            msg_hdr, text="", font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["peach"])
        self._suggest_status_lbl.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(msg_hdr, text="💡 Suggest",
                   command=self._fill_suggestion).pack(side=tk.RIGHT)

        msg_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        msg_frame.pack(fill=tk.X, padx=20, pady=(0, 12))
        self._msg_txt = tk.Text(msg_frame, height=3,
                                bg=C["mantle"], fg=C["text"],
                                insertbackground=C["text"],
                                relief=tk.FLAT, font=("Segoe UI", 10),
                                padx=8, pady=6, wrap=tk.WORD)
        self._msg_txt.pack(fill=tk.X)

        # Mark field as user-edited on any non-navigation keypress so async
        # results don't overwrite typing.
        def _on_key(event):
            if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                                 "Prior", "Next", "Shift_L", "Shift_R",
                                 "Control_L", "Control_R", "Alt_L", "Alt_R",
                                 "Tab", "Escape", "Caps_Lock"):
                return
            self._user_has_edited = True
        self._msg_txt.bind("<KeyPress>", _on_key, add="+")

        if is_repo:
            self._populate_suggestion(status_text, source="initial")
        self._msg_txt.focus_set()

    def _build_actions(self, is_repo: bool) -> None:
        """Commit / Cancel buttons + Ctrl+Enter binding."""
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 16))
        self._commit_btn = ttk.Button(btn_row, text="Commit Selected",
                                      style="Primary.TButton",
                                      command=self._apply,
                                      state=tk.NORMAL if is_repo and self._files else tk.DISABLED)
        self._commit_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)
        self._msg_txt.bind("<Control-Return>", lambda e: self._apply())

    def _size_and_centre(self, parent) -> None:
        self.update_idletasks()
        content_h = self.winfo_reqheight() + 20
        max_h = max(420, parent.winfo_height() - 60)
        w = 580
        h = min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── Selection helpers ────────────────────────────────────────────────────

    def _set_all(self, value: bool):
        for var, _f, _xy in self._file_vars:
            var.set(value)

    def _select_modified(self):
        """Select all tracked changes; uncheck untracked (??) files."""
        for var, _f, xy in self._file_vars:
            var.set(xy.strip() != "??")

    def _fill_suggestion(self):
        """Replace the commit message field with a fresh suggestion based on
        currently *selected* files only. Resets the user-edited flag so the
        async result is allowed to land — clicking 💡 Suggest is an explicit
        opt-in to overwrite whatever is in the field."""
        selected_lines = []
        for var, fname, xy in self._file_vars:
            if var.get():
                selected_lines.append(f"{xy} {fname}")
        sub_status = "\n".join(selected_lines) if selected_lines else self._status_raw
        # User pressed Suggest — they want to overwrite. Re-arm.
        self._user_has_edited = False
        self._populate_suggestion(sub_status, source="suggest_button")

    def _populate_suggestion(self, status_text: str, source: str = "initial"):
        """Run the commit-message orchestrator and populate the message field.

        Synchronous when LLM is disabled (heuristics are fast).
        Asynchronous when LLM is enabled — a daemon thread calls the
        orchestrator while the dialog stays responsive; the spinner label
        shows progress; the result lands via `self.after(0, …)`.

        `source` is informational ("initial" / "suggest_button") for logging.
        """
        raw = self._cfg.raw
        llm_cfg = (raw.get("commit_message_llm") or {}) if isinstance(raw, dict) else {}
        llm_active = bool(llm_cfg.get("enabled"))

        if not llm_active:
            # Heuristics only — instant, no spinner needed.
            result = _suggest_commit_message(
                self._path, status_text, raw, self._cfg.git_exe
            ) or "chore: update files"
            self._apply_suggestion(result)
            return

        # Async path. Bump token so an in-flight earlier request loses the race.
        self._suggestion_token += 1
        my_token = self._suggestion_token

        # Visible spinner
        self._suggest_status_lbl.configure(
            text="⟳  Generating with AI…  (can take 30–60s on local models)",
            fg=C["peach"])
        # Optional: drop a placeholder in the field if it's currently empty,
        # so the user sees "something is happening" without confusing focus.
        if not self._msg_txt.get("1.0", tk.END).strip() and not self._user_has_edited:
            self._msg_txt.delete("1.0", tk.END)
            self._msg_txt.insert(tk.END, "(generating commit message…)")
            self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)
            # The placeholder counts as "not user-edited"; rebind so any
            # actual keystroke flips the flag.
            self._user_has_edited = False

        def _worker(path=self._path, st=status_text, tok=my_token,
                    raw=raw, git=self._cfg.git_exe):
            try:
                result = _suggest_commit_message(path, st, raw, git)
            except Exception:
                log.exception("Suggestion worker failed")
                result = ""
            # Always come back to the main thread to touch widgets.
            try:
                self.after(0, self._on_suggestion_ready, tok, result)
            except RuntimeError:
                # Dialog was destroyed before result arrived — silent drop.
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="commit-suggestion-worker").start()

    def _on_suggestion_ready(self, token: int, result: str):
        """Main-thread callback: receive the orchestrator's result."""
        # Clear spinner regardless of outcome.
        self._suggest_status_lbl.configure(text="")
        # Stale request (a newer Suggest click already ran) — discard.
        if token != self._suggestion_token:
            return
        # User started editing after we kicked off — respect their input.
        if self._user_has_edited:
            return
        # Empty result → keep placeholder cleared and offer a generic fallback
        if not result:
            result = "chore: update files"
        self._apply_suggestion(result)

    def _apply_suggestion(self, suggestion: str):
        """Insert `suggestion` into the message field, select first line."""
        self._msg_txt.delete("1.0", tk.END)
        self._msg_txt.insert(tk.END, suggestion)
        first_line_end = self._msg_txt.search("\n", "1.0", tk.END)
        if first_line_end:
            self._msg_txt.tag_add(tk.SEL, "1.0", first_line_end)
        else:
            self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)
        # Inserting from code shouldn't count as user editing — keep the flag
        # consistent (it's already False; explicit reset for clarity).
        self._user_has_edited = False
        self._msg_txt.focus_set()

    def _apply(self):
        message = self._msg_txt.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Empty message",
                "Please enter a commit message.", parent=self)
            return
        # Pass xy alongside fname so the worker can distinguish
        # already-staged files (e.g. `git rm --cached` deletions from the
        # 🧹 Untrack flow) from files that still need `git add`. Without
        # this, calling git add on a staged deletion un-does the deletion
        # and re-encounters any matching .gitignore rule, blocking the
        # commit.
        selected = [(fname, xy) for var, fname, xy in self._file_vars
                    if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to include in this commit.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, message, selected)
