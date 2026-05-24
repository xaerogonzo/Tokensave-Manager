"""SwitchBranchDialog — pick a branch from a list to switch to.

Also exposes a static `pick()` helper for synchronous "choose a branch"
flows (used by cmd_git_delete_branch and the merge-branch picker).
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class SwitchBranchDialog(tk.Toplevel):
    """Select a branch to switch to from a list of local branches.

    Also used by cmd_git_delete_branch via the static pick() helper.
    Callback: callback(path, branch_name)
    """

    def __init__(self, parent, path: str, branches: list, current: str,
                 callback):
        super().__init__(parent)
        self.title(f"Switch Branch — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback
        self._result   = None

        tk.Label(self, text="🔀  Switch Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["lavender"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        if current:
            tk.Label(self, text=f"Current: {current}",
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        lb_wrap = tk.Frame(self, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        self._lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                               bg=C["mantle"], fg=C["text"],
                               selectbackground=C["surface1"],
                               activestyle="none",
                               relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            self._lb.insert(tk.END, f"  {b}")
        self._lb.pack(padx=6, pady=6)
        self._lb.bind("<Double-Button-1>", lambda _: self._switch())

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Switch", style="Primary.TButton",
                   command=self._switch).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _switch(self):
        sel = self._lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected",
                "Select a branch first.", parent=self)
            return
        name = self._lb.get(sel[0]).strip()
        self.destroy()
        if self._callback:
            self._callback(self._path, name)

    @staticmethod
    def pick(parent, title: str, branches: list, parent_widget=None) -> str:
        """Synchronous branch picker — returns chosen branch name or ''."""
        result = [""]
        pw = parent_widget or parent

        def cb(path, name):
            result[0] = name

        dlg = SwitchBranchDialog.__new__(SwitchBranchDialog)
        tk.Toplevel.__init__(dlg, parent)
        dlg.title(title)
        dlg.configure(bg=C["base"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(parent)
        dlg._path     = ""
        dlg._callback = None
        dlg._result   = result

        tk.Label(dlg, text=f"Select a branch:",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(16, 8))
        lb_wrap = tk.Frame(dlg, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                        bg=C["mantle"], fg=C["text"],
                        selectbackground=C["surface1"],
                        activestyle="none",
                        relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            lb.insert(tk.END, f"  {b}")
        lb.pack(padx=6, pady=6)

        def confirm():
            sel = lb.curselection()
            if sel:
                result[0] = lb.get(sel[0]).strip()
            dlg.destroy()

        lb.bind("<Double-Button-1>", lambda _: confirm())
        btn_row = tk.Frame(dlg, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text=title.split()[0], style="Primary.TButton",
                   command=confirm).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px, py = pw.winfo_x(), pw.winfo_y()
        pw2, ph = pw.winfo_width(), pw.winfo_height()
        dlg.geometry(f"{w}x{h}+{px + (pw2 - w) // 2}+{py + (ph - h) // 2}")

        parent.wait_window(dlg)
        return result[0]
