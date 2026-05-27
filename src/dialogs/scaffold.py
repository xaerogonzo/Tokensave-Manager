"""ScaffoldDialog — options dialog shown before scaffolding a new project.

Four checkboxes (BASIC_INSTRUCTIONS, tokensave init, Nuitka build files,
auto-commit Stop hook), each pre-checked or greyed based on what's
already present in the project folder. On Apply, fires the callback
with the four flags as kwargs.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from constants import C


class ScaffoldDialog(tk.Toplevel):
    """Options dialog shown before scaffolding a new project."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Scaffold Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.path = path
        self.callback = callback
        self._parent = parent

        has_bi = os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))
        has_db = os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db"))

        pad = dict(padx=20, pady=6)

        # Folder display
        tk.Label(self, text="Folder", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=path, bg=C["surface0"], fg=C["text"],
                 font=("Consolas", 9), padx=10, pady=6,
                 wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=20, pady=(2, 10))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self._bi_var = tk.BooleanVar(value=not has_bi)
        bi_text = "Create BASIC_INSTRUCTIONS.md"
        bi_note = "  (already exists — will overwrite)" if has_bi else "  (Claude instruction template)"
        bi_frame = tk.Frame(self, bg=C["base"])
        bi_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(bi_frame, text=bi_text, variable=self._bi_var).pack(side=tk.LEFT)
        tk.Label(bi_frame, text=bi_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Checkbox: tokensave init
        self._init_var = tk.BooleanVar(value=not has_db)
        init_text = "Run tokensave init"
        init_note = "  (already indexed)" if has_db else "  (builds the code graph — ~10–30s)"
        init_frame = tk.Frame(self, bg=C["base"])
        init_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(init_frame, text=init_text, variable=self._init_var).pack(side=tk.LEFT)
        tk.Label(init_frame, text=init_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Info note
        tk.Label(self,
                 text="Project appears in the list immediately while indexing runs in the background.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420).pack(padx=20, pady=(4, 8))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self._nuitka_var = tk.BooleanVar(value=False)
        nuitka_frame = tk.Frame(self, bg=C["base"])
        nuitka_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(nuitka_frame, text="Add Nuitka build files",
                        variable=self._nuitka_var).pack(side=tk.LEFT)
        tk.Label(nuitka_frame, text=nuitka_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        tk.Label(self,
                 text="Copies build.ps1 + build.bat from templates. Edit [ENTRY_SCRIPT] and\n"
                      "[OUTPUT_NAME] in build.ps1 before running your first build.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 8))

        # Checkbox: auto-commit Stop hook
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_exists = os.path.isfile(hook_settings)
        hook_note = "  (already present)" if hook_exists else "  (.claude/settings.json)"
        self._hook_var = tk.BooleanVar(value=False)
        hook_frame = tk.Frame(self, bg=C["base"])
        hook_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(hook_frame, text="Add auto-commit Stop hook",
                        variable=self._hook_var).pack(side=tk.LEFT)
        tk.Label(hook_frame, text=hook_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Label(self,
                 text="  Auto-commits when Claude finishes a session in this project.\n"
                      "  Safe: only commits if the working tree has changes.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 12))

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = self._parent.winfo_x() + (self._parent.winfo_width()  - self.winfo_width())  // 2
        py = self._parent.winfo_y() + (self._parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        create_bi       = self._bi_var.get()
        run_init        = self._init_var.get()
        scaffold_nuitka = self._nuitka_var.get()
        add_git_hook    = self._hook_var.get()
        self.destroy()
        self.callback(self.path, create_bi=create_bi, run_init=run_init,
                      scaffold_nuitka=scaffold_nuitka, add_git_hook=add_git_hook)
