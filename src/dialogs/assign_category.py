"""AssignCategoryDialog — assign/override category & sub-category for a project.

Editable comboboxes so the user can type a new category without prior setup.
Caller (ProjectsTabController) persists the override to manager-config.json.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class AssignCategoryDialog(tk.Toplevel):
    """Assign or override the category (and optional sub-category) for a project.

    Categories are sourced from search-root labels and existing overrides.
    Both comboboxes are editable so the user can type a new category/sub-category
    without any prior setup.

    Callback signature: callback(path, cat_or_None, subcat_str)
    Passing cat=None means "clear override" (restore root default).
    """

    def __init__(self, parent, path: str,
                 all_cats: list, subs_by_cat: dict,
                 current: dict, callback):
        super().__init__(parent)
        self.title("Assign Category")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._subs_map = subs_by_cat
        self._callback = callback

        pad = dict(padx=20, pady=4)

        # ── Title ──
        tk.Label(self, text="📁  Assign Category",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Category ──
        tk.Label(self, text="Category:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._cat_var = tk.StringVar(value=current.get("category", ""))
        self._cat_cb  = ttk.Combobox(self, textvariable=self._cat_var,
                                     values=all_cats, width=36)
        self._cat_cb.pack(anchor=tk.W, padx=20, pady=(0, 8))
        self._cat_cb.bind("<<ComboboxSelected>>", self._on_cat_changed)
        self._cat_var.trace_add("write", lambda *_: self._on_cat_changed())

        # ── Sub-category ──
        tk.Label(self, text="Sub-category:  (optional)",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._sub_var = tk.StringVar(value=current.get("subcategory", ""))
        self._sub_cb  = ttk.Combobox(self, textvariable=self._sub_var,
                                     values=subs_by_cat.get(self._cat_var.get(), []),
                                     width=36)
        self._sub_cb.pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Clear Override",
                   command=self._clear).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="OK", style="Primary.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _on_cat_changed(self, *_):
        cat  = self._cat_var.get()
        subs = self._subs_map.get(cat, [])
        self._sub_cb.configure(values=subs)

    def _ok(self):
        cat    = self._cat_var.get().strip()
        subcat = self._sub_var.get().strip()
        if not cat:
            messagebox.showwarning("Category required",
                "Enter a category name, or click 'Clear Override' to restore the default.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, cat, subcat)

    def _clear(self):
        self.destroy()
        self._callback(self._path, None, "")
