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
from theme import themed_checkbutton, bind_mousewheel
from helpers.shadow_links import (
    DEFAULT_SHADOW_EXT_MAP,
    ai_suggest_suffixes,
    indexed_extensions,
    load_shadow_map,
    save_shadow_map,
    suggest_shadow_candidates,
)


class ShadowLinksDialog(tk.Toplevel):
    """
    Configure and run shadow extension link generation for a project.
    Lets the user review/edit the extension map before applying.
    """

    def __init__(self, parent, path, callback, cfg=None):
        """
        callback(path, ext_map, run_sync): called on Apply.
        ext_map: dict mapping source extension → shadow suffix (e.g. {'.zsc': '.cpp'})
        cfg: ManagerConfig | None — only needed for the SL5 'Suggest suffixes
             with AI' button; the scanner itself works without it.
        """
        super().__init__(parent)
        self.title("Shadow Extension Links")
        self.configure(bg=C["base"])
        self.resizable(True, False)
        self.grab_set()
        self._path = path
        self._callback = callback
        self._cfg = cfg
        # R9-SL5: populated by _on_scan — (check_var, ext, suffix_var) per row.
        self._candidate_rows: list = []

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
        # Merge: defaults provide the base; saved-map entries override conflicts
        # so user customisations are preserved.  New defaults added in later
        # releases (e.g. SL6 GZDoom lumps) are automatically surfaced for
        # projects whose saved map pre-dates them.
        saved_map = load_shadow_map(path)
        initial_map = {**DEFAULT_SHADOW_EXT_MAP, **(saved_map or {})}
        for src_ext, tgt_suf in initial_map.items():
            self._map_text.insert(tk.END, f"{src_ext} = {tgt_suf}\n")

        hint = ("  .ext = .suffix  →  extension match  (e.g. .txt = .cpp for HyperV files)\n"
                "  NAME = .suffix  →  exact filename, case-insensitive  (e.g. DECORATE = .cpp)")
        if saved_map:
            new_defaults = [k for k in DEFAULT_SHADOW_EXT_MAP if k not in saved_map]
            if new_defaults:
                hint += (f"\n  ✔ Loaded this project's saved map "
                         f"+ {len(new_defaults)} new default(s) merged in.")
            else:
                hint += "\n  ✔ Loaded this project's saved map (edits are re-saved on Apply)."
        tk.Label(self,
            text=hint,
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        # ── R9-SL5: unindexed-file-type scanner ──
        self._build_scanner_section()

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

    def _build_scanner_section(self) -> None:
        """R9-SL5: a Scan button that lists file types tokensave isn't indexing,
        each with a checkbox + editable suffix (prefilled .cpp), plus an
        optional AI suffix-refinement button and an Add-checked action that
        appends the chosen mappings into the map editor above."""
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))
        bar = tk.Frame(self, bg=C["base"])
        bar.pack(fill=tk.X, padx=20, pady=(0, 4))
        ttk.Button(bar, text="🔍 Scan for unindexed file types",
                   command=self._on_scan).pack(side=tk.LEFT)
        # The AI button only does something with a configured LLM; show it only
        # when a cfg was passed (the controller passes it; bare callers don't).
        if self._cfg is not None:
            ttk.Button(bar, text="✨ Suggest suffixes with AI",
                       command=self._on_ai_suggest).pack(side=tk.LEFT, padx=(6, 0))

        self._scan_status = tk.Label(self, text="", font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["overlay0"],
                                     justify=tk.LEFT, anchor=tk.W)
        self._scan_status.pack(anchor=tk.W, padx=20, pady=(0, 2))

        # Bounded, scrollable candidate list (empty until Scan is clicked).
        wrap = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        wrap.pack(fill=tk.X, padx=20, pady=(0, 4))
        self._cand_canvas = tk.Canvas(wrap, bg=C["mantle"], height=110,
                                      highlightthickness=0)
        bind_mousewheel(self._cand_canvas)
        cand_vsb = ttk.Scrollbar(wrap, orient="vertical",
                                 command=self._cand_canvas.yview)
        self._cand_canvas.configure(yscrollcommand=cand_vsb.set)
        cand_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cand_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._cand_inner = tk.Frame(self._cand_canvas, bg=C["mantle"])
        self._cand_win = self._cand_canvas.create_window(
            (0, 0), window=self._cand_inner, anchor="nw")
        self._cand_inner.bind(
            "<Configure>",
            lambda _e: self._cand_canvas.configure(
                scrollregion=self._cand_canvas.bbox("all")))
        self._cand_canvas.bind(
            "<Configure>",
            lambda e: self._cand_canvas.itemconfigure(self._cand_win, width=e.width))

        self._add_checked_btn = ttk.Button(
            self, text="➕ Add checked to map", command=self._on_add_checked,
            state=tk.DISABLED)
        self._add_checked_btn.pack(anchor=tk.W, padx=20, pady=(0, 4))

    def _clear_candidate_rows(self) -> None:
        for child in self._cand_inner.winfo_children():
            child.destroy()
        self._candidate_rows = []

    def _on_scan(self) -> None:
        self._clear_candidate_rows()
        # Distinguish "no index to compare against" from "nothing unindexed".
        if indexed_extensions(self._path) is None:
            self._scan_status.configure(
                text="Run a tokensave sync first — the scanner compares against "
                     "the project's index.", fg=C["peach"])
            self._add_checked_btn.configure(state=tk.DISABLED)
            return
        candidates = suggest_shadow_candidates(self._path, self._parse_ext_map())
        if not candidates:
            self._scan_status.configure(
                text="✔ No unindexed file types found — tokensave already covers "
                     "everything here.", fg=C["green"])
            self._add_checked_btn.configure(state=tk.DISABLED)
            return
        for ext, count in candidates:
            self._add_candidate_row(ext, count)
        self._scan_status.configure(
            text=f"Found {len(candidates)} unindexed type(s). Tick the ones to "
                 "shadow, adjust suffixes, then Add checked to map.",
            fg=C["overlay0"])
        self._add_checked_btn.configure(state=tk.NORMAL)

    def _add_candidate_row(self, ext: str, count: int) -> None:
        row = tk.Frame(self._cand_inner, bg=C["mantle"])
        row.pack(fill=tk.X, padx=4, pady=1)
        chk_var = tk.BooleanVar(value=True)
        themed_checkbutton(row, variable=chk_var,
                           bg=C["mantle"], activebackground=C["mantle"]).pack(side=tk.LEFT)
        tk.Label(row, text=f"{ext}  ({count} file{'s' if count != 1 else ''})",
                 width=22, anchor=tk.W, bg=C["mantle"], fg=C["text"],
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        tk.Label(row, text="→", bg=C["mantle"], fg=C["overlay0"]).pack(
            side=tk.LEFT, padx=4)
        suf_var = tk.StringVar(value=".cpp")
        ttk.Entry(row, textvariable=suf_var, width=8).pack(side=tk.LEFT)
        self._candidate_rows.append((chk_var, ext, suf_var))

    def _on_ai_suggest(self) -> None:
        if not self._candidate_rows:
            self._scan_status.configure(
                text="Scan first, then AI can suggest suffixes for the results.",
                fg=C["peach"])
            return
        exts = [ext for _chk, ext, _suf in self._candidate_rows]
        result = ai_suggest_suffixes(self._cfg, exts)
        if not result:
            self._scan_status.configure(
                text="AI suggestion unavailable (no LLM configured, or it "
                     "returned nothing) — .cpp prefill kept.", fg=C["peach"])
            return
        applied = 0
        for _chk, ext, suf_var in self._candidate_rows:
            if ext in result:
                suf_var.set(result[ext])
                applied += 1
        self._scan_status.configure(
            text=f"AI suggested suffixes for {applied} type(s); edit any before "
                 "adding.", fg=C["green"])

    def _on_add_checked(self) -> None:
        existing = self._map_text.get("1.0", tk.END)
        added = 0
        for chk_var, ext, suf_var in self._candidate_rows:
            if not chk_var.get():
                continue
            suffix = suf_var.get().strip()
            if not suffix.startswith("."):
                continue
            line = f"{ext} = {suffix}"
            if line in existing:
                continue
            if not existing.endswith("\n") and existing.strip():
                self._map_text.insert(tk.END, "\n")
            self._map_text.insert(tk.END, line + "\n")
            existing = self._map_text.get("1.0", tk.END)
            added += 1
        self._scan_status.configure(
            text=(f"Added {added} mapping(s) to the editor above — review, then "
                  "Apply." if added else "Nothing added (already mapped or "
                  "unchecked)."),
            fg=C["green"] if added else C["overlay0"])

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
