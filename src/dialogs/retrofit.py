"""RetrofitDialog — 5 checkboxes for retrofitting an existing project.

Each checkbox toggles one retrofit step (tokensave CLAUDE.md rules,
BASIC_INSTRUCTIONS.md, Nuitka build files, shadow extension links,
auto-commit Stop hook). On Apply, fires the callback with the five
flags if any was checked.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from constants import C
from helpers.shadow_links import DEFAULT_SHADOW_EXT_MAP


class RetrofitDialog(tk.Toplevel):
    """Small dialog with two checkboxes for the retrofit options."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Retrofit Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.callback = callback
        self.path = path

        pad = {"padx": 20, "pady": 8}

        tk.Label(self, text="Retrofit options",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 4))

        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(0, 10))

        # Checkbox: tokensave integration
        self.var_ts = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Add tokensave rules to CLAUDE.md",
            variable=self.var_ts,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Prepends an @include line so Claude always loads the\n"
                 "  tokensave lookup table. Non-destructive — existing content kept.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self.var_bi = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Also create BASIC_INSTRUCTIONS.md",
            variable=self.var_bi,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Drops a full project template (overview, architecture,\n"
                 "  key files, rules) for Claude to fill in on first use.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self.var_nuitka = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add Nuitka build files",
            variable=self.var_nuitka,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text=f"  Copies build templates from the templates folder.{chr(10)}"
                 "  Edit [ENTRY_SCRIPT] and [OUTPUT_NAME] in build.ps1 before building.\n"
                 f"  {nuitka_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Shadow extension links
        self.var_shadow = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Generate shadow extension links",
            variable=self.var_shadow,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # Count existing shadow files
        existing_shadows = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        shadow_note = (f"  {existing_shadows} shadow file(s) already exist."
                       if existing_shadows else
                       "  None exist yet — click Apply to create them.")
        tk.Label(self,
            text="  Creates NTFS hardlinks (.zs→.cpp, .zsc→.cpp, .acs→.c, DECORATE→.cpp)\n"
                 "  so tokensave can parse ZScript/ACS/DECORATE as C++/C. Zero disk cost.\n"
                 "  Adds gitignore patterns. Use 🔗 Shadow Links… for custom mappings.\n"
                 f"  {shadow_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Auto-commit Stop hook
        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_note = "  (already present)" if os.path.isfile(hook_settings) else "  (.claude/settings.json)"
        self.var_hook = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add auto-commit Stop hook",
            variable=self.var_hook,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Auto-commits when Claude finishes a session in this project.\n"
                 "  Only commits when the working tree has changes. Safe on clean repos.\n"
                 f"  {hook_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 12))

        # Buttons
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 16))

        ttk.Button(btn_frame, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        # Centre over parent
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        ts     = self.var_ts.get()
        bi     = self.var_bi.get()
        nuitka = self.var_nuitka.get()
        shadow = self.var_shadow.get()
        hook   = self.var_hook.get()
        self.destroy()
        if ts or bi or nuitka or shadow or hook:
            self.callback(self.path, ts, bi, nuitka, shadow, add_git_hook=hook)
