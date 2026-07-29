"""GitignoreDialog — view and edit a project's .gitignore through a structured UI.

Layout:
  1. Header: project name + file path + entry count
  2. Scrollable "Current entries" frame — one widget row per non-blank line
     of .gitignore; each pattern row has a × button to mark for removal
     (rendered with strikethrough font); comment rows are visible but
     require a confirm dialog before removal; blank lines are tracked
     by index for layout preservation but not displayed
  3. "Inject template patterns" row — push buttons (not checkboxes; see
     the Gemini critique note in the plan file). One click adds that
     category's missing patterns to the pending additions
  4. Custom entry field + Add button
  5. Pending changes panel (Text widget, read-only) showing + / − diff
  6. Save / Cancel buttons. Save calls _write_gitignore_lines (atomic) and
     then triggers _offer_commit_after_change on the parent App.

After Save, if the new rules match any tracked files, opens
UntrackIgnoredDialog (lazy-imported inside the handler — Rule 6) so the
user can immediately clean up the "tracked-but-ignored" footgun.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW
from theme import _Tooltip, bind_mousewheel
from helpers.gitignore import (
    _read_gitignore_lines, _write_gitignore_lines, _GITIGNORE_TEMPLATES,
)
from helpers.git import _is_local_git_repo, _find_tracked_but_ignored

if TYPE_CHECKING:
    from state import ManagerConfig


class GitignoreDialog(tk.Toplevel):
    """View and edit a project's .gitignore through a structured dialog.

    Layout:
      1. Header: project name + file path + entry count
      2. Scrollable "Current entries" frame — one widget row per non-blank line
         of .gitignore; each pattern row has a × button to mark for removal
         (rendered with strikethrough font); comment rows are visible but
         require a confirm dialog before removal; blank lines are tracked
         by index for layout preservation but not displayed
      3. "Inject template patterns" row — push buttons (not checkboxes; see
         the Gemini critique note in the plan file). One click adds that
         category's missing patterns to the pending additions
      4. Custom entry field + Add button
      5. Pending changes panel (Text widget, read-only) showing + / − diff
      6. Save / Cancel buttons. Save calls _write_gitignore_lines (atomic) and
         then triggers _offer_commit_after_change on the parent App.
    """

    def __init__(self, parent, path: str, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._app  = parent
        self._path = path
        self._cfg  = cfg
        name = os.path.basename(path)
        self.title(f"Manage .gitignore — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 520)
        self.grab_set()

        # ── State ──────────────────────────────────────────────────────────
        self._original_lines: list  = _read_gitignore_lines(path)
        self._removed_indices: set  = set()
        self._additions:      list  = []
        self._row_widgets:    dict  = {}   # idx -> {label, btn, frame}

        self._normal_font = tkfont.Font(family="Consolas", size=9)
        self._strike_font = tkfont.Font(family="Consolas", size=9, overstrike=1)
        self._ai_suggest_stop = threading.Event()

        self._build_header_section(path)
        # Build Save/Cancel bar BEFORE the scrollable/expandable sections so
        # pack(side=BOTTOM) anchors it to the window floor regardless of how
        # much content sits above it.  (Packing it last caused it to be pushed
        # off-screen whenever the combined sections exceeded the dialog height.)
        self._build_save_buttons_section()
        # v4.5: novice-friendly privacy semantics banner — first thing the
        # user reads under the header.
        self._build_privacy_banner_section()
        self._build_current_entries_section()
        self._build_ai_suggest_section()
        self._build_template_buttons_section()
        if _is_local_git_repo(path):
            self._build_untracked_panel()
        self._build_custom_entry_section()
        self._build_pending_changes_section()

        self._update_pending_panel()
        self._centre_on_parent(parent)

        # Wire close button so any in-flight AI call is cancelled immediately
        orig_destroy = self.destroy
        def _safe_destroy():
            self._ai_suggest_stop.set()
            orig_destroy()
        self.protocol("WM_DELETE_WINDOW", _safe_destroy)

    def _build_header_section(self, path: str):
        """Title + .gitignore path + functional-pattern count."""
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📋  .gitignore", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        gi_path = os.path.join(path, ".gitignore")
        sub = tk.Frame(hdr, bg=C["base"])
        sub.pack(fill=tk.X, pady=(4, 0))
        tk.Label(sub, text=gi_path, bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        functional_count = sum(
            1 for ln in self._original_lines
            if ln.strip() and not ln.strip().startswith("#"))
        self._count_lbl = tk.Label(sub,
            text=f"  ({functional_count} pattern"
                 f"{'s' if functional_count != 1 else ''})",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8))
        self._count_lbl.pack(side=tk.LEFT)

    def _build_current_entries_section(self):
        """Scrollable canvas hosting one row widget per non-blank .gitignore line."""
        cur_label = tk.Label(self, text="CURRENT ENTRIES",
                             bg=C["base"], fg=C["overlay0"],
                             font=("Segoe UI", 8, "bold"))
        cur_label.pack(anchor=tk.W, padx=18, pady=(0, 4))
        cur_wrap = tk.Frame(self, bg=C["mantle"])
        cur_wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))

        self._cur_canvas = tk.Canvas(cur_wrap, bg=C["mantle"],
                                     highlightthickness=0, height=180)
        bind_mousewheel(self._cur_canvas)
        cur_vsb = ttk.Scrollbar(cur_wrap, orient="vertical",
                                command=self._cur_canvas.yview)
        self._cur_canvas.configure(yscrollcommand=cur_vsb.set)
        cur_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cur_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._cur_body = tk.Frame(self._cur_canvas, bg=C["mantle"])
        self._cur_body_id = self._cur_canvas.create_window(
            (0, 0), window=self._cur_body, anchor="nw")
        self._cur_canvas.bind("<Configure>",
            lambda e: self._cur_canvas.itemconfigure(
                self._cur_body_id, width=e.width))
        self._cur_body.bind("<Configure>",
            lambda e: self._cur_canvas.configure(
                scrollregion=self._cur_canvas.bbox("all")))

        self._populate_current_entries()

    def _build_template_buttons_section(self):
        """6-per-row grid of `+ <category>` buttons; tooltips show what each adds."""
        tmpl_label = tk.Label(self,
            text="INJECT TEMPLATE PATTERNS  (one-click — hover to see what each adds)",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8, "bold"))
        tmpl_label.pack(anchor=tk.W, padx=18, pady=(4, 4))
        tmpl_wrap = tk.Frame(self, bg=C["base"])
        tmpl_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))

        per_row = 6
        for i, cat_name in enumerate(_GITIGNORE_TEMPLATES.keys()):
            row, col = divmod(i, per_row)
            btn = ttk.Button(tmpl_wrap, text=f"+ {cat_name}",
                             command=lambda n=cat_name: self._inject_template(n))
            btn.grid(row=row, column=col, padx=(0, 4), pady=(0, 4),
                     sticky=tk.W)
            _Tooltip(btn, self._template_tooltip_text(cat_name))

    def _build_ai_suggest_section(self):
        """AI-powered pattern suggestion row — sits above the template buttons."""
        ai_wrap = tk.Frame(self, bg=C["base"])
        ai_wrap.pack(fill=tk.X, padx=18, pady=(0, 4))
        self._ai_suggest_btn = ttk.Button(
            ai_wrap, text="🤖 AI Suggest",
            command=self._ai_suggest,
        )
        self._ai_suggest_btn.pack(side=tk.LEFT)
        self._ai_status_lbl = tk.Label(
            ai_wrap, text="",
            bg=C["base"], fg=C["overlay0"],
            font=("Segoe UI", 8),
        )
        self._ai_status_lbl.pack(side=tk.LEFT, padx=(10, 0))

    def _ai_suggest(self):
        """Kick off AI-powered gitignore pattern suggestions in a background thread.

        Context gathered: CodeGraph file listing (falls back to os.listdir) +
        git untracked files + current ignore state. Two LLM paths: claude_cli or
        _call_llm (Ollama / OpenAI-compatible). All UI mutations via self.after(0,…).
        """
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        llm_cfg = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
        if not llm_cfg or not llm_cfg.get("provider"):
            self._ai_status_lbl.configure(
                text="No AI configured — set a provider in Settings → Ask Tab AI.",
                fg=C["red"])
            return

        # Snapshot current pattern state on main thread before the worker starts
        current_patterns = frozenset(self._current_pattern_state())
        self._ai_suggest_stop.clear()
        self._ai_suggest_btn.configure(state=tk.DISABLED)
        self._ai_status_lbl.configure(text="Scanning project…", fg=C["overlay0"])
        stop_event = self._ai_suggest_stop

        def _worker():
            exts, dirs, source_note = self._collect_project_files(self._path)
            if stop_event.is_set():
                return
            # Novice gotcha #8: disclose which file listing fed the AI —
            # gives a diagnostic path when suggestions come back poor.
            def _show_source(note=source_note):
                try:
                    if self.winfo_exists() and not stop_event.is_set():
                        self._ai_status_lbl.configure(
                            text=f"Asking AI… (context: {note})",
                            fg=C["overlay0"])
                except (tk.TclError, RuntimeError):
                    pass
            self.after(0, _show_source)
            untracked_str = self._collect_untracked_files(
                self._cfg.git_exe, self._path)
            if stop_event.is_set():
                return
            sys_p, user_p = self._build_gitignore_prompts(
                exts, dirs, untracked_str, current_patterns)
            result, error = self._dispatch_gitignore_llm(
                llm_cfg, self._cfg, sys_p, user_p, self._path)
            if stop_event.is_set():
                return
            if error or not result:
                err_msg = error or "AI call returned no result."
                self.after(0, lambda m=err_msg: _on_error(m))
                return
            parsed = _parse_ai_gitignore_patterns(result)
            self.after(0, lambda p=parsed: _on_result(p))

        # ── Callbacks — always guarded against destroyed-window TclError ────

        def _on_error(msg):
            try:
                if not self.winfo_exists() or stop_event.is_set():
                    return
                self._ai_suggest_btn.configure(state=tk.NORMAL)
                self._ai_status_lbl.configure(text=f"Error: {msg}", fg=C["red"])
            except (tk.TclError, RuntimeError):
                return

        def _on_result(patterns):
            try:
                if not self.winfo_exists() or stop_event.is_set():
                    return
                self._ai_suggest_btn.configure(state=tk.NORMAL)
                n = self._inject_patterns_list(patterns)
                if n > 0:
                    self._ai_status_lbl.configure(
                        text=f"Added {n} new pattern(s) — review in Pending Changes",
                        fg=C["green"])
                    self._update_pending_panel()
                else:
                    self._ai_status_lbl.configure(
                        text="✓ Already up to date — no new patterns needed",
                        fg=C["overlay0"])
            except (tk.TclError, RuntimeError):
                return

        threading.Thread(target=_worker, daemon=True).start()

    # ── _ai_suggest helpers ─────────────────────────────────────────────────

    @staticmethod
    def _collect_project_files(path: str) -> tuple:
        """Return (exts, dirs, source_note) — CodeGraph DB, falling back to
        os.listdir. source_note names the data source so the UI can disclose
        which listing fed the AI (suggestion quality differs a lot between
        a full CodeGraph index and a top-level directory listing)."""
        db_path = os.path.join(path, ".codegraph", "codegraph.db")
        all_files: list = []
        source_note = ""
        if os.path.isfile(db_path):
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                rows = con.execute("SELECT path FROM files").fetchall()
                con.close()
                all_files = [r[0] for r in rows]
                source_note = f"CodeGraph file listing ({len(all_files)} files)"
            except Exception:
                try:
                    all_files = os.listdir(path)
                except Exception:
                    all_files = []
                source_note = ("top-level directory listing "
                               "(fallback — CodeGraph index unreadable)")
        else:
            try:
                all_files = os.listdir(path)
            except Exception:
                all_files = []
            source_note = ("top-level directory listing "
                           "(fallback — CodeGraph not initialized)")
        exts = sorted({os.path.splitext(f)[1] for f in all_files
                       if os.path.splitext(f)[1]})
        dirs = sorted({
            f.replace("\\", "/").split("/")[0]
            for f in all_files if "/" in f.replace("\\", "/")
        })
        return exts, dirs, source_note

    @staticmethod
    def _collect_untracked_files(git_exe: str, path: str) -> str:
        """Return newline-joined untracked filenames from git ls-files, or ''."""
        try:
            proc = subprocess.Popen(
                [git_exe, "-C", path,
                 "ls-files", "--others", "--exclude-standard"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
            )
            stdout, _ = proc.communicate(timeout=10)
            if proc.returncode == 0:
                return stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _build_gitignore_prompts(
        exts: list, dirs: list, untracked_str: str, current_patterns: frozenset
    ) -> tuple:
        """Assemble (system_prompt, user_prompt) for the gitignore AI request."""
        sys_p = (
            "You are a .gitignore expert. Analyse the provided project structure "
            "and suggest gitignore patterns that should be excluded from version control.\n\n"
            "Rules:\n"
            "- Output ONLY patterns, one per line. No explanations, no markdown, "
            "no code blocks, no numbering.\n"
            "- Each line must be a valid .gitignore glob "
            "(e.g. __pycache__/, *.log, .env, node_modules/).\n"
            "- Do NOT suggest any pattern already in the 'Already ignored' list.\n"
            "- gitignore patterns without an embedded '/' (or with only a trailing '/') "
            "match at ANY directory depth. If '__pycache__/' is already ignored, do NOT "
            "suggest 'src/__pycache__/' or any other path-scoped variant — it is already "
            "covered. Only suggest a path-scoped pattern (e.g. 'src/vendor/') when the "
            "broader name is NOT already present in the 'Already ignored' list.\n"
            "- Skip binary/database files handled by tool-specific nested .gitignore "
            "files (e.g. CodeGraph writes its own .gitignore inside .codegraph/ — "
            "do not suggest codegraph.db or similar; it is handled already).\n"
            "- Focus on standard language build artifacts, build directories, "
            "local environment configs, and package manager artifacts directly "
            "corresponding to the detected directories/extensions. "
            "Do not guess at frameworks that are not clearly present.\n"
            "- Prefer directory patterns (trailing /) over file patterns when "
            "a whole directory should be excluded."
        )
        user_p = (
            f"Project file extensions detected: "
            f"{', '.join(exts) or '(none detected)'}\n"
            f"Top-level directories: {', '.join(dirs) or '(none detected)'}\n\n"
            f"Untracked files (what git currently sees — may be empty):\n"
            f"{untracked_str or '(none or git unavailable)'}\n\n"
            "Already ignored patterns (do NOT repeat or rephrase these):\n"
            + "\n".join(sorted(current_patterns))
        )
        return sys_p, user_p

    @staticmethod
    def _dispatch_gitignore_llm(
        llm_cfg: dict, cfg, sys_p: str, user_p: str, path: str
    ) -> tuple:
        """Call configured LLM provider; return (result, error) — one is None."""
        provider = llm_cfg.get("provider", "")
        try:
            if provider == "claude_cli":
                from helpers.claude_cli import call_claude_cli_print
                claude_exe = cfg.claude_cli_exe or ""
                if not claude_exe:
                    return None, (
                        "Claude CLI not configured — set path in "
                        "Settings → Claude Code CLI."
                    )
                result = call_claude_cli_print(
                    claude_exe, user_p,
                    system_prompt=sys_p,
                    timeout=60,
                    model=llm_cfg.get("model") or "",
                    cwd=path,
                )
            else:
                from helpers.llm import _call_llm
                result = _call_llm(llm_cfg, sys_p, user_p)
            return result, None
        except Exception as exc:
            return None, str(exc)

    def _inject_patterns_list(self, patterns):
        """Inject a list of patterns with trailing-slash-normalised dedup.

        Two-pass smart merge identical to _inject_template:
          1. Un-remove any existing entries currently marked for removal that
             the pattern list wants present (reverts the removal, no duplicate).
          2. Append genuinely new patterns.

        Comparison is normalised (trailing slash stripped) so '__pycache__/'
        and '__pycache__' are treated as the same pattern.  Uniqueness is seeded
        from both the on-disk state AND in-flight pending additions so the AI
        won't re-suggest what the user just manually added this session.

        Prepends a '# AI suggested patterns' header comment (once) so the
        pending diff is self-documenting.  Returns the count of new patterns
        added (excluding the comment line).
        """
        def _norm(p):
            return p.strip().rstrip("/")

        pattern_norm_set = {_norm(p) for p in patterns}

        # Seed from both on-disk state and any in-flight pending additions
        already_norm = {
            _norm(p)
            for p in list(self._current_pattern_state()) + list(self._additions)
        }

        # First pass: revert pending removals that the pattern list wants kept
        for idx, raw_line in enumerate(self._original_lines):
            if idx not in self._removed_indices:
                continue
            if _norm(raw_line.strip()) in pattern_norm_set:
                self._toggle_removal(idx)   # reverts removal + updates panel
                already_norm.add(_norm(raw_line.strip()))

        # Second pass: collect genuinely new patterns.
        # Two redundancy checks:
        #   1. Exact normalised match — already present verbatim.
        #   2. Path-scoped redundancy — 'src/__pycache__/' is suppressed when
        #      '__pycache__/' is already ignored, because gitignore patterns
        #      without an embedded '/' match at any directory depth.
        def _basename(p):
            return _norm(p).rsplit("/", 1)[-1]

        new_patterns = []
        for p in patterns:
            n = _norm(p)
            if n in already_norm:
                continue                        # exact match — already covered
            if "/" in n and _basename(p) in already_norm:
                continue                        # broader pattern already covers it
            new_patterns.append(p)
            already_norm.add(n)

        if new_patterns:
            header = "# AI suggested patterns"
            if header not in self._additions:
                self._additions.append(header)
            self._additions.extend(new_patterns)

        return len(new_patterns)

    def _build_custom_entry_section(self):
        """Custom-pattern entry + Add / Browse buttons + hint label."""
        custom_wrap = tk.Frame(self, bg=C["base"])
        custom_wrap.pack(fill=tk.X, padx=18, pady=(4, 0))
        tk.Label(custom_wrap, text="Custom entry:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._custom_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_wrap, textvariable=self._custom_var,
                                  font=("Consolas", 9), width=32)
        custom_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                          padx=(0, 6))
        custom_entry.bind("<Return>", lambda e: self._add_custom())
        ttk.Button(custom_wrap, text="+ Add",
                   command=self._add_custom).pack(side=tk.LEFT)
        ttk.Button(custom_wrap, text="Browse file...",
                   command=self._browse_add).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(custom_wrap, text="📁 Add folder...",
                   command=self._add_folder).pack(side=tk.LEFT, padx=(6, 0))

        self._custom_hint = tk.Label(self, text="", bg=C["base"],
                                     fg=C["overlay0"], font=("Segoe UI", 8))
        self._custom_hint.pack(anchor=tk.W, padx=18)

    def _build_pending_changes_section(self):
        """Read-only Text widget showing the +/- diff of pending edits."""
        pend_row = tk.Frame(self, bg=C["base"])
        pend_row.pack(fill=tk.X, padx=18, pady=(10, 2))
        tk.Label(pend_row, text="PENDING CHANGES",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)
        tk.Label(pend_row,
                 text="— click  💾 Save changes  to write to disk",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "italic")).pack(side=tk.LEFT, padx=(6, 0))
        pend_wrap = tk.Frame(self, bg=C["mantle"])
        pend_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))
        self._pend_txt = tk.Text(pend_wrap, height=4,
                                  font=("Consolas", 9),
                                  bg=C["mantle"], fg=C["text"],
                                  relief=tk.FLAT, padx=6, pady=4,
                                  wrap=tk.NONE, state=tk.DISABLED)
        self._pend_txt.pack(fill=tk.X, expand=True)
        self._pend_txt.tag_configure("add",  foreground=C["green"])
        self._pend_txt.tag_configure("rem",  foreground=C["red"])
        self._pend_txt.tag_configure("dim",  foreground=C["overlay0"])

    def _build_save_buttons_section(self):
        """Bottom Save / Cancel button row.

        Packed with side=tk.BOTTOM and built BEFORE the scrollable sections
        so it is always anchored to the window floor — content above expands
        into the remaining space rather than pushing this row off-screen.
        """
        btns = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        # Thin visual separator above the button row
        ttk.Separator(self, orient="horizontal").pack(
            fill=tk.X, side=tk.BOTTOM)
        self._save_btn = ttk.Button(btns, text="💾  Save changes",
                                     command=self._on_save)
        self._save_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)
        # v4.5: advanced "scrub from history" disclosure on the left so it's
        # visually away from the primary Save/Cancel cluster.
        ttk.Button(
            btns, text="⚙  Advanced — Scrub from History…",
            command=self._on_open_scrub_dialog,
        ).pack(side=tk.LEFT)

    def _build_privacy_banner_section(self):
        """v4.5: novice-readable explanation of what 'Save changes' does.

        Single sentence at the top so the user understands the privacy
        semantics: files added here stop being PUSHED from now on, but
        OLDER history may still contain them.  The Advanced button at the
        bottom opens ScrubHistoryDialog for the full-erasure case.
        """
        banner = tk.Frame(self, bg=C["surface0"], padx=12, pady=8)
        banner.pack(fill=tk.X, padx=18, pady=(0, 8))
        tk.Label(banner,
                 text="🔒  Privacy semantics",
                 bg=C["surface0"], fg=C["blue"],
                 font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        tk.Label(
            banner,
            text=(
                "Files added here will stop being pushed from now on. "
                "Older commits on GitHub still contain them — for a "
                "full erase from all history, use ⚙ Advanced below."
            ),
            bg=C["surface0"], fg=C["text"],
            font=("Segoe UI", 8),
            wraplength=580, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))

    def _on_open_scrub_dialog(self):
        """Open the advanced 'Scrub from History' dialog (v4.5)."""
        # Lazy import (Rule 6) — avoids a load-time cycle between gitignore
        # and scrub_history when both end up in the same import graph.
        from dialogs.scrub_history import ScrubHistoryDialog
        # Pre-fill the picker with the first pending addition if any,
        # so the user doesn't have to retype.
        initial = ""
        if self._additions:
            for a in self._additions:
                if not a.startswith("#"):
                    initial = a.strip().lstrip("/")
                    break
        ScrubHistoryDialog(self, self._path, self._cfg,
                           initial_file=initial)

    def _centre_on_parent(self, parent):
        """Position the dialog roughly centred on the parent window."""
        self.update_idletasks()
        w, h = 640, 620
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Row population ─────────────────────────────────────────────────────

    def _populate_current_entries(self):
        """Build a row widget for every non-blank line in _original_lines.

        Blank lines are tracked by index (in _original_lines) but not
        displayed — they get preserved on save by iterating original_lines
        on write and skipping anything in _removed_indices.
        """
        if not self._original_lines:
            empty_lbl = tk.Label(self._cur_body,
                text="(no .gitignore yet — inject a template or add a custom entry below)",
                bg=C["mantle"], fg=C["overlay0"],
                font=("Segoe UI", 9, "italic"), padx=8, pady=12)
            empty_lbl.pack(anchor=tk.W)
            return

        for idx, raw in enumerate(self._original_lines):
            stripped = raw.strip()
            if not stripped:
                continue   # blank, preserved by index but invisible
            row = tk.Frame(self._cur_body, bg=C["mantle"])
            row.pack(fill=tk.X, padx=4, pady=1)

            is_comment = stripped.startswith("#")
            pattern_text = raw  # show raw (keeps any leading indentation)

            lbl = tk.Label(row, text=pattern_text, bg=C["mantle"],
                           fg=(C["peach"] if is_comment else C["text"]),
                           font=self._normal_font, anchor=tk.W)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))

            if is_comment:
                marker = tk.Label(row, text="(comment)",
                                   bg=C["mantle"], fg=C["overlay0"],
                                   font=("Segoe UI", 8, "italic"))
                marker.pack(side=tk.LEFT, padx=(0, 4))

            btn = tk.Label(row, text="×", bg=C["mantle"],
                            fg=C["red"], font=("Segoe UI", 11, "bold"),
                            cursor="hand2", padx=8)
            btn.pack(side=tk.RIGHT)
            btn.bind("<Button-1>",
                lambda _e, i=idx: self._toggle_removal(i))

            self._row_widgets[idx] = {
                "frame":      row,
                "label":      lbl,
                "btn":        btn,
                "is_comment": is_comment,
            }

    # ── Removal toggle ─────────────────────────────────────────────────────

    def _toggle_removal(self, idx: int):
        """Mark or un-mark a row for removal. Confirms before removing comments."""
        widgets = self._row_widgets.get(idx)
        if not widgets:
            return
        if idx in self._removed_indices:
            # Undo removal
            self._removed_indices.discard(idx)
            widgets["label"].configure(font=self._normal_font,
                fg=(C["peach"] if widgets["is_comment"] else C["text"]))
            widgets["btn"].configure(text="×", fg=C["red"])
        else:
            # Adding removal — confirm if it's a comment
            if widgets["is_comment"]:
                ok = messagebox.askyesno(
                    "Remove comment line?",
                    f"Remove this comment line?\n\n  {self._original_lines[idx]}",
                    parent=self)
                if not ok:
                    return
            self._removed_indices.add(idx)
            widgets["label"].configure(font=self._strike_font,
                fg=C["overlay0"])
            widgets["btn"].configure(text="↺", fg=C["green"])
        self._update_pending_panel()

    # ── Template injection (smart-clear conflict aware) ───────────────────

    def _current_pattern_state(self) -> set:
        """Return the set of patterns currently in 'final state' = original
        minus pending-removed plus pending-added. Used to decide what a
        template injection actually needs to add."""
        in_state = set()
        for idx, raw in enumerate(self._original_lines):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if idx in self._removed_indices:
                continue
            in_state.add(s)
        for a in self._additions:
            in_state.add(a.strip())
        return in_state

    def _inject_template(self, cat_name: str):
        """Apply a category's patterns. Smart-resolves conflicts with
        pending removals: if the category contains a pattern currently
        marked for removal, un-remove it instead of appending a duplicate."""
        patterns = _GITIGNORE_TEMPLATES.get(cat_name, [])
        if not patterns:
            return
        already = self._current_pattern_state()
        added_any = False
        # First pass: un-remove any pattern that's currently in _removed_indices
        # AND the category wants it (revert the removal instead of duplicating)
        for idx, raw in enumerate(self._original_lines):
            if idx not in self._removed_indices:
                continue
            s = raw.strip()
            if s in patterns:
                # Revert this row's removal
                self._toggle_removal(idx)   # already updates pending panel
                already.add(s)
                added_any = True
        # Second pass: append patterns that genuinely aren't present
        for p in patterns:
            if p not in already:
                self._additions.append(p)
                already.add(p)
                added_any = True
        if added_any:
            self._update_pending_panel()
        # Don't bother flashing on no-op clicks

    def _template_tooltip_text(self, cat_name: str) -> str:
        """Tooltip text listing the patterns this category contributes."""
        pats = _GITIGNORE_TEMPLATES.get(cat_name, [])
        return "Click to add to .gitignore:\n" + "\n".join(f"  {p}" for p in pats)

    # ── Untracked files panel ─────────────────────────────────────────────

    def _build_untracked_panel(self):
        """Insert a collapsible panel listing untracked files from git ls-files.

        Rows are built in a background thread; the panel is hidden entirely if
        git is unavailable or returns no untracked files.
        """
        self._untracked_frame = tk.LabelFrame(
            self,
            text="UNTRACKED FILES  (click + to add a pattern)",
            fg=C["yellow"], bg=C["base"],
            font=("Segoe UI", 8, "bold"),
            labelanchor="nw",
        )
        self._untracked_frame.pack(fill=tk.X, padx=18, pady=(4, 4))

        self._untracked_body = tk.Frame(self._untracked_frame, bg=C["mantle"])
        self._untracked_body.pack(fill=tk.X)

        self._untracked_status = tk.Label(
            self._untracked_body,
            text="  Scanning…",
            bg=C["mantle"], fg=C["overlay0"],
            font=("Segoe UI", 8, "italic"),
        )
        self._untracked_status.pack(anchor=tk.W, padx=4, pady=4)

        threading.Thread(target=self._fetch_untracked, daemon=True).start()

    def _fetch_untracked(self):
        """Run git ls-files in a background thread and populate the panel."""
        try:
            proc = subprocess.Popen(
                [self._cfg.git_exe, "-C", self._path,
                 "ls-files", "--others", "--exclude-standard"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            stdout, _ = proc.communicate(timeout=10)
            files = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        except Exception:
            files = []
        self.after(0, lambda fs=files: self._populate_untracked(fs))

    def _populate_untracked(self, files: list):
        """Render untracked files, grouping any that share a top-level
        directory into a single "📁 dirname/ (N files)" row. This turns a
        freshly-created build folder like dist/ — which git ls-files reports
        as dozens of individual file paths — into one row with a single
        [+] that ignores the whole folder, instead of burning the 40-row
        cap on that folder's contents alone."""
        if not self.winfo_exists():
            return
        self._untracked_status.destroy()
        if not files:
            tk.Label(
                self._untracked_body,
                text="  (no untracked files)",
                bg=C["mantle"], fg=C["overlay0"],
                font=("Segoe UI", 8, "italic"),
            ).pack(anchor=tk.W, padx=4, pady=4)
            return

        norm = [f.replace("\\", "/") for f in files]
        dir_counts: dict = {}
        loose_files: list = []
        for f in norm:
            if "/" in f:
                top = f.split("/", 1)[0]
                dir_counts[top] = dir_counts.get(top, 0) + 1
            else:
                loose_files.append(f)

        entries = []   # (display_text, pattern)
        for dirname in sorted(dir_counts):
            count = dir_counts[dirname]
            entries.append((
                f"📁 {dirname}/  ({count} untracked file{'s' if count != 1 else ''})",
                f"{dirname}/",
            ))
        for filepath in loose_files:
            entries.append((filepath, filepath))

        MAX_ROWS = 40
        for display, pattern in entries[:MAX_ROWS]:
            row = tk.Frame(self._untracked_body, bg=C["mantle"])
            row.pack(fill=tk.X, padx=4, pady=1)

            btn_lbl = tk.Label(row, text="[+]", bg=C["mantle"],
                               fg=C["green"], font=("Consolas", 9, "bold"),
                               cursor="hand2", padx=6)
            btn_lbl.pack(side=tk.LEFT)

            file_lbl = tk.Label(row, text=display, bg=C["mantle"],
                                fg=C["text"], font=self._normal_font,
                                anchor=tk.W)
            file_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Early binding via default keyword args — prevents late-binding trap
            btn_lbl.bind(
                "<Button-1>",
                lambda _e, p=pattern, b=btn_lbl, l=file_lbl:
                    self._add_from_panel(p, b, l),
            )

        if len(entries) > MAX_ROWS:
            tk.Label(
                self._untracked_body,
                text=f"  … and {len(entries) - MAX_ROWS} more (use custom entry)",
                bg=C["mantle"], fg=C["overlay0"],
                font=("Segoe UI", 8, "italic"),
            ).pack(anchor=tk.W, padx=4, pady=2)

    def _add_from_panel(self, pattern: str, btn_lbl: tk.Label, file_lbl: tk.Label):
        """Add pattern as a gitignore entry and visually strike through the row."""
        self._custom_var.set(pattern)
        self._add_custom()
        # Visual strike-through — no subprocess re-run needed
        btn_lbl.configure(text="[✓]", fg=C["overlay0"], cursor="")
        btn_lbl.unbind("<Button-1>")
        file_lbl.configure(fg=C["overlay0"],
                           font=tkfont.Font(family="Consolas", size=9, overstrike=1))

    # ── Browse button ─────────────────────────────────────────────────────

    def _browse_add(self):
        """Open a file/folder picker, relativize the result, add as a pattern."""
        picked = filedialog.askopenfilename(
            title="Select a file to add to .gitignore",
            initialdir=self._path,
            parent=self,
        )
        if not picked:
            # User might want a directory — offer askdirectory as fallback
            picked = filedialog.askdirectory(
                title="Or select a folder to add to .gitignore",
                initialdir=self._path,
                parent=self,
            )
        if not picked:
            return

        picked = os.path.normpath(picked)
        try:
            rel = os.path.relpath(picked, self._path)
        except ValueError:
            # Different drive on Windows
            messagebox.showwarning(
                "File outside project",
                "The selected file must be on the same drive as the project.",
                parent=self,
            )
            return

        if rel.startswith(".."):
            messagebox.showwarning(
                "File outside project",
                "The selected file must be inside the project directory.",
                parent=self,
            )
            return

        # Normalise to forward slashes; append / for directories
        pattern = rel.replace("\\", "/")
        if os.path.isdir(picked):
            pattern = pattern.rstrip("/") + "/"

        self._custom_var.set(pattern)
        self._add_custom()

    def _add_folder(self):
        """Open a folder picker directly and add the whole folder as a pattern.

        Distinct from _browse_add (file picker with an askdirectory fallback
        on cancel) — this is the direct one-click path for "ignore this
        entire folder" (e.g. a build output dir like dist/) without having
        to cancel a file dialog first.
        """
        picked = filedialog.askdirectory(
            title="Select a folder to add to .gitignore",
            initialdir=self._path,
            parent=self,
        )
        if not picked:
            return

        picked = os.path.normpath(picked)
        try:
            rel = os.path.relpath(picked, self._path)
        except ValueError:
            messagebox.showwarning(
                "Folder outside project",
                "The selected folder must be on the same drive as the project.",
                parent=self,
            )
            return

        if rel == "." or rel.startswith(".."):
            messagebox.showwarning(
                "Folder outside project",
                "The selected folder must be inside the project directory.",
                parent=self,
            )
            return

        pattern = rel.replace("\\", "/").rstrip("/") + "/"
        self._custom_var.set(pattern)
        self._add_custom()

    # ── Custom entry ─────────────────────────────────────────────────────

    def _add_custom(self):
        text = self._custom_var.get().strip()
        if not text:
            return
        # Dedup
        if text in self._current_pattern_state():
            self._custom_hint.configure(text="(already present)", fg=C["overlay0"])
            self.after(2000,
                lambda: self._custom_hint.configure(text=""))
            return
        # Suspicious-looking pattern? Confirm before adding.
        if " " in text or text.startswith("/"):
            # Note: leading slash is actually valid gitignore (anchors to root),
            # but mixed with whitespace it's almost always a typo
            if " " in text:
                if not messagebox.askyesno(
                    "Suspicious pattern",
                    f"This doesn't look like a typical gitignore pattern "
                    f"(contains whitespace):\n\n  {text}\n\nAdd anyway?",
                    parent=self):
                    return
        self._additions.append(text)
        self._custom_var.set("")
        self._custom_hint.configure(text="")
        self._update_pending_panel()

    # ── Pending changes panel ────────────────────────────────────────────

    def _update_pending_panel(self):
        """Refresh the diff display and the Save button's enabled state."""
        self._pend_txt.configure(state=tk.NORMAL)
        self._pend_txt.delete("1.0", tk.END)
        any_changes = False
        for idx in sorted(self._removed_indices):
            line = self._original_lines[idx].strip()
            self._pend_txt.insert(tk.END, f"− {line}\n", "rem")
            any_changes = True
        for line in self._additions:
            self._pend_txt.insert(tk.END, f"+ {line}\n", "add")
            any_changes = True
        if not any_changes:
            self._pend_txt.insert(tk.END,
                "No changes — inject a template, add a custom entry, "
                "or click × on a row to begin.", "dim")
            self._save_btn.configure(state=tk.DISABLED)
        else:
            self._save_btn.configure(state=tk.NORMAL)
        self._pend_txt.configure(state=tk.DISABLED)

    # ── Save ──────────────────────────────────────────────────────────────

    def _on_save(self):
        # Build the final line list: original minus removed, plus additions
        kept_lines = [ln for i, ln in enumerate(self._original_lines)
                      if i not in self._removed_indices]
        final_lines = list(kept_lines)
        if self._additions:
            # Add a blank separator + header before new entries (only if the
            # last existing line isn't already blank — avoids double-blank)
            if final_lines and final_lines[-1].strip() != "":
                final_lines.append("")
            final_lines.append("# Added by TokenSave Manager")
            final_lines.extend(self._additions)
        try:
            _write_gitignore_lines(self._path, final_lines)
        except OSError as e:
            messagebox.showerror("Save failed",
                f"Could not write .gitignore:\n\n{e}", parent=self)
            return
        # Log via the App's log panel
        added_n   = len(self._additions)
        removed_n = len(self._removed_indices)
        bits = []
        if added_n:   bits.append(f"+{added_n}")
        if removed_n: bits.append(f"-{removed_n}")
        change_str = "  ".join(bits) if bits else "(no diff)"
        self._app._log(
            f"  Saved .gitignore  ({change_str})", C["green"])
        path = self._path
        self.destroy()
        # After the .gitignore write, check whether the new rules now match
        # files that were ALREADY tracked. If so, offer to untrack them in
        # the same flow — otherwise the user hits the confusing 'git keeps
        # showing this as modified after I added it to gitignore' problem.
        # Only relevant when at least one addition was made AND this is a
        # local git repo; pure removals or non-git projects skip this.
        if added_n > 0 and _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path, self._cfg.git_exe)
            if stale:
                ask = messagebox.askyesno(
                    "Untrack files that match your new rules?",
                    f"Your .gitignore now matches "
                    f"{len(stale)} file{'s' if len(stale) != 1 else ''} "
                    "that {} already tracked by git:\n\n".format(
                        "are" if len(stale) != 1 else "is")
                    + "\n".join(f"  • {f}" for f in stale[:10])
                    + ("\n  ..." if len(stale) > 10 else "")
                    + "\n\nUntracking removes them from git's index but "
                    "keeps the local files. This is the standard fix for "
                    "'I added it to .gitignore but git keeps showing it.'\n\n"
                    "Open the Untrack Ignored Files dialog now?",
                    parent=self._app)
                if ask:
                    # Lazy import (Rule 6) — avoids any module-load cycle
                    # between two src/dialogs/ files. UntrackIgnoredDialog
                    # is only needed on this confirm-yes path.
                    from dialogs.untrack_ignored import UntrackIgnoredDialog
                    UntrackIgnoredDialog(self._app, path, stale,
                        reason="now matched by your updated .gitignore")
                    return  # untrack flow handles commit prompt itself
        # Otherwise: trigger the existing commit-after-change prompt
        self._app._offer_commit_after_change(path, ".gitignore")


# ── Module-level helpers ───────────────────────────────────────────────────────

def _parse_ai_gitignore_patterns(response):
    """Parse an LLM response into a clean list of .gitignore patterns.

    Handles:
    - Markdown code fence stripping (scans for actual fence positions, not
      index 0/-1, which would miss fences at lines[-2] when the model appends
      trailing prose or blank lines after the closing fence)
    - Numbered / lettered list bullets (``1)`` , ``a)``)
    - Comment lines (``#…``)
    - Residual fence characters (`` ` ``)
    - Prose openers ("Here are…", "I suggest…", etc.)
    - Hard cap of 40 patterns to guard against runaway output
    """
    lines = [ln.strip() for ln in response.splitlines() if ln.strip()]

    # Strip markdown code fences by scanning for actual fence positions
    start_idx = end_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("```"):
            if start_idx is None:
                start_idx = i
            else:
                end_idx = i
    if start_idx is not None and end_idx is not None and end_idx > start_idx:
        lines = lines[start_idx + 1: end_idx]
    elif start_idx is not None:
        lines = lines[start_idx + 1:]   # unclosed block — take everything after fence

    _PROSE_OPENERS = (
        "here ", "here's", "here are",
        "i suggest", "note:", "the following", "these are",
    )
    valid = []
    for line in lines:
        if not line:
            continue
        if line.startswith("#"):        # AI comment
            continue
        if line.startswith("`"):        # residual fence character
            continue
        if re.match(r"^\w\)", line):   # numbered/lettered list bullet (1), a), etc.)
            continue
        if any(line.lower().startswith(p) for p in _PROSE_OPENERS):
            continue
        valid.append(line)
        if len(valid) >= 40:
            break
    return valid
