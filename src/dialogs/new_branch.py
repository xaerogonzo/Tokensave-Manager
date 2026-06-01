"""NewBranchDialog — create a new git branch, optionally switching to it.

Leaf widget: takes a parent + path + callback. Caller (GitTabController)
runs the actual `git branch` / `git switch` commands; this dialog just
gathers the input.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C
from theme import themed_checkbutton


class NewBranchDialog(tk.Toplevel):
    """Create a new git branch, with an option to switch to it immediately.

    Callback: callback(path, branch_name, switch_immediately)
    """

    def __init__(self, parent, path: str, callback):
        super().__init__(parent)
        self.title(f"New Branch — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🌿  New Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["green"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Branch name:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._name_var = tk.StringVar()
        name_entry = ttk.Entry(self, textvariable=self._name_var, width=38)
        name_entry.pack(anchor=tk.W, padx=20, pady=(4, 10))
        name_entry.focus_set()

        self._switch_var = tk.BooleanVar(value=True)
        themed_checkbutton(self,
            text="Switch to this branch immediately",
            variable=self._switch_var,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Create", style="Primary.TButton",
                   command=self._create).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.bind("<Return>", lambda _: self._create())

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _create(self):
        name = self._name_var.get().strip()
        if not name:
            messagebox.showwarning("Name required",
                "Enter a branch name.", parent=self)
            return
        if " " in name:
            messagebox.showwarning("Invalid name",
                "Branch names cannot contain spaces.\n"
                "Try using a hyphen instead, e.g. my-feature",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, name, self._switch_var.get())
