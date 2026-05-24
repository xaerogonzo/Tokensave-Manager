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
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
from typing import TYPE_CHECKING

from constants import C
from theme import _Tooltip
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
        self.transient(parent)

        # ── State ──────────────────────────────────────────────────────────
        self._original_lines: list  = _read_gitignore_lines(path)
        self._removed_indices: set  = set()
        self._additions:      list  = []
        self._row_widgets:    dict  = {}   # idx -> {label, btn, frame}

        self._normal_font = tkfont.Font(family="Consolas", size=9)
        self._strike_font = tkfont.Font(family="Consolas", size=9, overstrike=1)

        # ── Header ─────────────────────────────────────────────────────────
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

        # ── Current entries: scrollable canvas + body Frame ────────────────
        cur_label = tk.Label(self, text="CURRENT ENTRIES",
                             bg=C["base"], fg=C["overlay0"],
                             font=("Segoe UI", 8, "bold"))
        cur_label.pack(anchor=tk.W, padx=18, pady=(0, 4))
        cur_wrap = tk.Frame(self, bg=C["mantle"])
        cur_wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))

        self._cur_canvas = tk.Canvas(cur_wrap, bg=C["mantle"],
                                     highlightthickness=0, height=180)
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

        # ── Template Inject buttons (action, not state — see plan) ────────
        tmpl_label = tk.Label(self,
            text="INJECT TEMPLATE PATTERNS  (one-click — hover to see what each adds)",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8, "bold"))
        tmpl_label.pack(anchor=tk.W, padx=18, pady=(4, 4))
        tmpl_wrap = tk.Frame(self, bg=C["base"])
        tmpl_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))

        # Two-row grid of buttons; up to 6 per row
        per_row = 6
        for i, cat_name in enumerate(_GITIGNORE_TEMPLATES.keys()):
            row, col = divmod(i, per_row)
            btn = ttk.Button(tmpl_wrap, text=f"+ {cat_name}",
                             command=lambda n=cat_name: self._inject_template(n))
            btn.grid(row=row, column=col, padx=(0, 4), pady=(0, 4),
                     sticky=tk.W)
            _Tooltip(btn, self._template_tooltip_text(cat_name))

        # ── Custom entry ──────────────────────────────────────────────────
        custom_wrap = tk.Frame(self, bg=C["base"])
        custom_wrap.pack(fill=tk.X, padx=18, pady=(4, 0))
        tk.Label(custom_wrap, text="Custom entry:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._custom_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_wrap, textvariable=self._custom_var,
                                  font=("Consolas", 9), width=40)
        custom_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                          padx=(0, 6))
        custom_entry.bind("<Return>", lambda e: self._add_custom())
        ttk.Button(custom_wrap, text="+ Add",
                   command=self._add_custom).pack(side=tk.LEFT)

        self._custom_hint = tk.Label(self, text="", bg=C["base"],
                                     fg=C["overlay0"], font=("Segoe UI", 8))
        self._custom_hint.pack(anchor=tk.W, padx=18)

        # ── Pending changes panel ────────────────────────────────────────
        pend_label = tk.Label(self, text="PENDING CHANGES",
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8, "bold"))
        pend_label.pack(anchor=tk.W, padx=18, pady=(10, 2))
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

        # ── Save / Cancel ────────────────────────────────────────────────
        btns = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        btns.pack(fill=tk.X)
        self._save_btn = ttk.Button(btns, text="Save changes",
                                     command=self._on_save)
        self._save_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

        self._update_pending_panel()

        # Centre on parent
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
        return f"Click to add to .gitignore:\n" + "\n".join(f"  {p}" for p in pats)

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
