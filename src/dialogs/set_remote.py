"""SetRemoteDialog — connect a project to a GitHub repository.

Beginner-friendly: guides the user through create-repo → copy-URL → paste.
The actual `git remote add origin <url>` runs in the caller.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class SetRemoteDialog(tk.Toplevel):
    """Connect a project to a GitHub repository by entering its HTTPS URL.

    Guides beginners through the three-step process: create repo on GitHub,
    copy the URL, paste here.
    Callback: callback(path, url)
    """

    def __init__(self, parent, path: str, current_url: str, callback):
        super().__init__(parent)
        self.title(f"Set Remote — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🔗  Connect to GitHub",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # Instructions
        instr = (
            "Steps:\n"
            "  1.  Go to  github.com/new  and create a new repository\n"
            "  2.  Copy the HTTPS URL from the repository page\n"
            "        (looks like: https://github.com/you/repo-name.git)\n"
            "  3.  Paste it in the box below and click Save"
        )
        tk.Label(self, text=instr,
                 font=("Segoe UI", 9), bg=C["base"], fg=C["text"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Remote URL:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._url_var = tk.StringVar(value=current_url)
        ttk.Entry(self, textvariable=self._url_var,
                  width=52).pack(anchor=tk.W, padx=20, pady=(4, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _save(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("URL required",
                "Please enter the GitHub repository URL.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@\n\n"
                "Example:  https://github.com/username/repo.git",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, url)
