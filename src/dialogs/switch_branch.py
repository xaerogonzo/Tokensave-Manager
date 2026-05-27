"""SwitchBranchDialog — pick a branch from a list to switch to.

Also exposes a static `pick()` helper for synchronous "choose a branch"
flows (used by cmd_git_delete_branch and the merge-branch picker).

Remote branches (from `git branch -r`) are shown in a second section below
a separator. Selecting one passes the bare name (without `origin/` prefix)
to the callback — git's DWIM checkout auto-creates a local tracking branch.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class SwitchBranchDialog(tk.Toplevel):
    """Select a branch to switch to from a list of local (and optionally remote) branches.

    Also used by cmd_git_delete_branch and cmd_git_merge via the static pick() helper.
    Callback: callback(path, branch_name)

    Remote branches are shown after a separator. Selecting a remote-only branch passes
    its bare name (e.g. 'feature-x', not 'origin/feature-x') — git checkout DWIM will
    auto-create a local tracking branch.
    """

    def __init__(self, parent, path: str, branches: list, current: str,
                 callback, remote_branches: list = []):
        super().__init__(parent)
        self.title(f"Switch Branch — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self._path     = path
        self._callback = callback

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
        lb_wrap.pack(padx=20, pady=(0, 0), fill=tk.X)
        self._lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                               bg=C["mantle"], fg=C["text"],
                               selectbackground=C["surface1"],
                               activestyle="none",
                               relief=tk.FLAT, bd=0,
                               height=max(4, min(12, len(branches) + len(remote_branches) + 1)),
                               width=40)

        # Local branches
        for b in branches:
            self._lb.insert(tk.END, f"  {b}")

        # Remote branches section
        self._remote_start = len(branches)  # index where remote entries start
        if remote_branches:
            self._lb.insert(tk.END, "── remote ──────────────────────")
            self._lb.itemconfig(self._remote_start,
                                fg=C["overlay0"], selectbackground=C["mantle"])
            for b in remote_branches:
                self._lb.insert(tk.END, f"  ↓ {b}")

        self._lb.pack(padx=6, pady=6)
        self._lb.bind("<Double-Button-1>", lambda _: self._switch())
        if remote_branches:
            self._lb.bind("<<ListboxSelect>>", self._on_select)

        # Hint when remote branches are present
        if remote_branches:
            tk.Label(self,
                     text="↓ = remote branch  •  switching creates a local tracking copy",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(2, 0))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 10))

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

    def _on_select(self, _event=None):
        """Prevent selecting the separator row."""
        sel = self._lb.curselection()
        if sel and sel[0] == self._remote_start:
            self._lb.selection_clear(0, tk.END)

    def _switch(self):
        sel = self._lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected",
                "Select a branch first.", parent=self)
            return
        idx = sel[0]
        if idx == self._remote_start:
            return  # separator row
        raw = self._lb.get(idx).strip()
        # Strip the remote arrow prefix if present
        name = raw.lstrip("↓ ").strip()
        self.destroy()
        if self._callback:
            self._callback(self._path, name)

    @staticmethod
    def pick(parent, title: str, branches: list,
             parent_widget=None, remote_branches: list = []) -> str:
        """Synchronous branch picker — returns chosen branch name or ''.

        Used by cmd_git_merge and cmd_git_delete_branch.
        Optional remote_branches shows a second section below a separator.
        """
        result  = [""]
        pw      = parent_widget or parent
        # remote_start tracks the separator row index
        remote_start = len(branches)

        def confirm():
            sel = lb.curselection()
            if sel:
                idx = sel[0]
                if idx == remote_start and remote_branches:
                    return  # separator
                raw = lb.get(idx).strip().lstrip("↓ ").strip()
                result[0] = raw
            dlg.destroy()

        dlg = tk.Toplevel(parent)
        dlg.title(title)
        dlg.configure(bg=C["base"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(parent)

        tk.Label(dlg, text="Select a branch:",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(16, 8))

        lb_wrap = tk.Frame(dlg, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 0), fill=tk.X)
        lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                        bg=C["mantle"], fg=C["text"],
                        selectbackground=C["surface1"],
                        activestyle="none",
                        relief=tk.FLAT, bd=0,
                        height=max(4, min(12, len(branches) + len(remote_branches) + 1)),
                        width=40)
        for b in branches:
            lb.insert(tk.END, f"  {b}")

        if remote_branches:
            lb.insert(tk.END, "── remote ──────────────────────")
            lb.itemconfig(remote_start, fg=C["overlay0"], selectbackground=C["mantle"])
            for b in remote_branches:
                lb.insert(tk.END, f"  ↓ {b}")

            def _guard_sep(_event=None):
                sel = lb.curselection()
                if sel and sel[0] == remote_start:
                    lb.selection_clear(0, tk.END)
            lb.bind("<<ListboxSelect>>", _guard_sep)

        lb.pack(padx=6, pady=6)

        if remote_branches:
            tk.Label(dlg,
                     text="↓ = remote branch",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(2, 0))

        lb.bind("<Double-Button-1>", lambda _: confirm())

        btn_row = tk.Frame(dlg, bg=C["base"])
        btn_row.pack(pady=(8, 16), padx=20, anchor=tk.W)
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
