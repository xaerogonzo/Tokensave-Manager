"""ShadowLinksDialog — configure & run shadow extension link generation.

Lets the user review/edit the extension map before applying. The actual
hardlink creation runs in helpers/shadow_links.py, kicked off by the
caller after this dialog returns the map via callback.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C
from theme import themed_checkbutton
from helpers.shadow_links import (
    DEFAULT_SHADOW_EXT_MAP,
    load_shadow_map,
    save_shadow_map,
)


class ShadowLinksDialog(tk.Toplevel):
    """
    Configure and run shadow extension link generation for a project.
    Lets the user review/edit the extension map before applying.
    """

    def __init__(self, parent, path, callback):
        """
        callback(path, ext_map, run_sync): called on Apply.
        ext_map: dict mapping source extension → shadow suffix (e.g. {'.zsc': '.cpp'})
        """
        super().__init__(parent)
        self.title("Shadow Extension Links")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self._path = path
        self._callback = callback

        pad = dict(padx=20, pady=6)

        tk.Label(self,
                 text="🔗  Shadow Extension Links",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))

        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        tk.Label(self,
            text="Creates NTFS hardlinks with an appended extension so tokensave's\n"
                 "tree-sitter parsers can index non-standard file types. Hardlinks\n"
                 "cost zero extra disk space and update instantly with the source.",
            font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── Extension map editor ──
        tk.Label(self,
                 text="Mapping  (one per line:  .ext = .suffix  or  FILENAME = .suffix)",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        map_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        map_frame.pack(fill=tk.X, padx=20, pady=(0, 4))
        self._map_text = tk.Text(map_frame, height=6, width=36,
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, font=("Consolas", 10),
                                  padx=8, pady=6)
        self._map_text.pack(fill=tk.X)

        # R9-SL1: a previously saved per-project map wins over the default,
        # so custom mappings survive dialog close → reopen.
        saved_map = load_shadow_map(path)
        initial_map = saved_map or DEFAULT_SHADOW_EXT_MAP
        for src_ext, tgt_suf in initial_map.items():
            self._map_text.insert(tk.END, f"{src_ext} = {tgt_suf}\n")

        hint = ("  .ext = .suffix  →  extension match  (e.g. .txt = .cpp for HyperV files)\n"
                "  NAME = .suffix  →  exact filename, case-insensitive  (e.g. DECORATE = .cpp)")
        if saved_map:
            hint += "\n  ✔ Loaded this project's saved map (edits are re-saved on Apply)."
        tk.Label(self,
            text=hint,
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        # ── Status summary ──
        existing = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in initial_map.items())
        )
        status_col = C["green"] if existing else C["overlay0"]
        status_txt = (f"✔  {existing} shadow file(s) already exist in this project."
                      if existing else "No shadow files found — none created yet.")
        tk.Label(self, text=status_txt,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=status_col).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))

        # ── Options ──
        self._var_sync = tk.BooleanVar(value=True)
        themed_checkbutton(self, text="Run tokensave sync after generating links",
                           variable=self._var_sync,
                           bg=C["base"], fg=C["text"],
                           activebackground=C["base"], activeforeground=C["text"],
                           font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(8, 16))

        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _parse_ext_map(self) -> dict:
        """Parse the text widget content into an ext_map dict.

        Two valid line formats:
          .ext = .suffix   → extension-based match (dot-prefixed key)
          NAME = .suffix   → exact filename match, case-insensitive (e.g. DECORATE)
        Lines starting with '#' and blank lines are ignored.
        """
        ext_map = {}
        for line in self._map_text.get("1.0", tk.END).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            src, _, tgt = line.partition("=")
            src = src.strip()
            tgt = tgt.strip()
            # tgt must be a dot-suffix; src can be a dot-extension OR a bare filename
            if tgt.startswith(".") and src:
                ext_map[src] = tgt
        return ext_map

    def _apply(self):
        ext_map = self._parse_ext_map()
        if not ext_map:
            messagebox.showwarning("No mappings",
                "Please define at least one extension mapping.", parent=self)
            return
        # R9-SL1: persist for the next open. save_shadow_map never raises —
        # a failed write must not block the actual generation.
        save_shadow_map(self._path, ext_map)
        run_sync = self._var_sync.get()
        self.destroy()
        self._callback(self._path, ext_map, run_sync)
