"""UntrackIgnoredDialog — checklist for untracking tracked-but-ignored files.

Shows every file returned by `git ls-files -ci --exclude-standard` with a
checkbox. Untrack Selected runs `git rm --cached -- <files>` (in the
caller) which removes them from the index without touching the working
tree.

Triggered:
  - manually via right-click → 🧹 Untrack Ignored Files…
  - automatically via GitignoreDialog._on_save when new ignore rules
    match files that were already tracked

If `on_confirm` is None, falls back to `parent._do_untrack_ignored(path,
files)` — the legacy App-method path. New callers should pass an explicit
`on_confirm` callback rather than relying on the parent attribute.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C
from theme import bind_mousewheel, themed_checkbutton


class UntrackIgnoredDialog(tk.Toplevel):
    """Checklist dialog for untracking files that are tracked-but-ignored.

    Shows every file returned by `git ls-files -ci --exclude-standard`
    (i.e. files in git's index whose path matches the project's .gitignore)
    with a checkbox. Untrack Selected runs `git rm --cached -- <files>`
    which removes them from the index without touching the working tree,
    so the local copies stay where they are.

    Triggered:
      - manually via right-click → 🧹 Untrack Ignored Files…
      - automatically via GitignoreDialog._on_save when new ignore rules
        match files that were already tracked

    On success, calls _offer_commit_after_change on the parent App so the
    user can immediately commit the untracking as one atomic change.
    """

    def __init__(self, parent, path: str, files: list, *,
                 reason: str = "tracked but listed in .gitignore",
                 on_confirm=None):
        super().__init__(parent)
        self._app      = parent
        self._path     = path
        self._files    = files
        self._on_confirm = on_confirm  # callable(path, files) or None → falls back to self._app._do_untrack_ignored
        name = os.path.basename(path)
        self.title(f"Untrack Ignored Files — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 380)
        self.grab_set()

        self._build_header_section(name)
        self._build_explanation_section(reason)
        self._build_file_list_section(files)
        self._build_action_buttons_section(files)
        self._centre_on_parent(parent)

    def _build_header_section(self, name: str):
        """Dialog title + project name."""
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🧹  Untrack Ignored Files", bg=C["base"],
                 fg=C["blue"], font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=name, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

    def _build_explanation_section(self, reason: str):
        """Wrapping prose explaining what untracking does."""
        expl = tk.Frame(self, bg=C["base"])
        expl.pack(fill=tk.X, padx=18, pady=(0, 6))
        tk.Label(expl,
                 text=(
                   f"The following files are {reason}.\n\n"
                   "👉  Click 'Untrack Selected' — this is almost always the right answer.\n\n"
                   "Your files stay on disk exactly as they are. Git just stops "
                   "watching them for changes, which is what .gitignore intends. "
                   "They will no longer appear in git status or be included in commits."
                 ),
                 bg=C["base"], fg=C["text"], font=("Segoe UI", 9),
                 justify=tk.LEFT, anchor=tk.W,
                 wraplength=500).pack(anchor=tk.W)

    def _build_file_list_section(self, files: list):
        """Scrollable canvas with a per-file checkbox row (or empty-state label)."""
        tk.Label(self, text="FILES TO UNTRACK",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=18, pady=(8, 2))
        list_outer = tk.Frame(self, bg=C["mantle"])
        list_outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=180)
        bind_mousewheel(self._canvas)
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
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        self._file_vars: list = []
        if not files:
            tk.Label(self._list_body,
                     text="  (no tracked-but-ignored files found)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 9, "italic"),
                     padx=10, pady=10).pack(anchor=tk.W)
        else:
            for fname in files:
                row = tk.Frame(self._list_body, bg=C["mantle"])
                row.pack(fill=tk.X, padx=4, pady=1)
                var = tk.BooleanVar(value=True)
                self._file_vars.append((var, fname))
                cb = themed_checkbutton(row, variable=var, bg=C["mantle"],
                                        activebackground=C["mantle"])
                cb.pack(side=tk.LEFT)
                tk.Label(row, text=fname, anchor=tk.W,
                         font=("Consolas", 9),
                         bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(4, 6))

    def _build_action_buttons_section(self, files: list):
        """Quick-select row (only if files exist) + Untrack/Cancel buttons."""
        if files:
            sel_row = tk.Frame(self, bg=C["base"])
            sel_row.pack(fill=tk.X, padx=18, pady=(0, 8))
            ttk.Button(sel_row, text="Select All",
                       command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Select None",
                       command=lambda: self._set_all(False)).pack(side=tk.LEFT)

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._action_btn = ttk.Button(btn_row, text="Untrack Selected",
                                       command=self._apply,
                                       state=tk.NORMAL if files else tk.DISABLED)
        self._action_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

    def _centre_on_parent(self, parent):
        """Position the dialog roughly centred on the parent window."""
        self.update_idletasks()
        w, h = 580, 520
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    def _set_all(self, value: bool):
        for var, _f in self._file_vars:
            var.set(value)

    def _apply(self):
        selected = [fname for var, fname in self._file_vars if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to untrack.", parent=self)
            return
        path = self._path
        self.destroy()
        if self._on_confirm is not None:
            self._on_confirm(path, selected)
        else:
            self._app._do_untrack_ignored(path, selected)
