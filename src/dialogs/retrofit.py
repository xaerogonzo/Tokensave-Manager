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
    """Small dialog with checkboxes for the retrofit options."""

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
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))

        # Checkbox: tokensave integration
        self.var_ts = self._opt_row(
            "Add tokensave rules + index the project",
            "  Prepends an @include line so Claude always loads the\n"
            "  tokensave lookup table, and builds the code-graph index if\n"
            "  this project has none. Non-destructive: existing content and\n"
            "  an existing index are both left alone.",
            pad, default=True)

        # Checkbox: BASIC_INSTRUCTIONS.md
        self.var_bi = self._opt_row(
            "Also create BASIC_INSTRUCTIONS.md",
            "  Drops a full project template (overview, architecture,\n"
            "  key files, rules) for Claude to fill in on first use.",
            pad, default=True)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: portable AGENTS.md
        has_agents = os.path.isfile(os.path.join(path, "AGENTS.md"))
        self.var_agents = self._opt_row(
            "Add AGENTS.md (portable agent rules)",
            "  Inlines the same baseline rules CLAUDE.md gets, as plain\n"
            "  markdown. Cursor, Codex, Gemini CLI and opencode read this;\n"
            "  none of them can follow the @include CLAUDE.md uses.\n"
            "  Updates only its own marked block, never your text.\n"
            + ("  (AGENTS.md already exists — its block will be refreshed)"
               if has_agents else "  (creates AGENTS.md)"),
            pad, default=True)

        # Checkbox: Cursor-native rule
        has_mdc = os.path.isfile(
            os.path.join(path, ".cursor", "rules", "tokensave.mdc"))
        self.var_cursor_rule = self._opt_row(
            "Add .cursor/rules/tokensave.mdc",
            "  Cursor's native rule format, marked alwaysApply so it loads\n"
            "  on every request. Cursor ignores plain .md files here, which\n"
            "  is why this is a separate file from AGENTS.md.\n"
            + ("  (already exists — its block will be refreshed)"
               if has_mdc else "  (creates .cursor/rules/tokensave.mdc)"),
            pad, default=True)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = (
            "  Copies build templates from the templates folder.\n"
            "  Edit [ENTRY_SCRIPT] and [OUTPUT_NAME] in build.ps1 before building.\n"
            + ("  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)")
        )
        self.var_nuitka = self._opt_row(
            "Add Nuitka build files", nuitka_note, pad, default=False)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Shadow extension links
        existing_shadows = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        shadow_note = (
            "  Creates NTFS hardlinks (.zs→.cpp, .zsc→.cpp, .acs→.c, DECORATE→.cpp)\n"
            "  so tokensave can parse ZScript/ACS/DECORATE as C++/C. Zero disk cost.\n"
            "  Adds gitignore patterns. Use 🔗 Shadow Links… for custom mappings.\n"
            + (f"  {existing_shadows} shadow file(s) already exist."
               if existing_shadows else
               "  None exist yet — click Apply to create them.")
        )
        self.var_shadow = self._opt_row(
            "Generate shadow extension links", shadow_note, pad, default=False)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Auto-commit Stop hook
        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_note = (
            "  Auto-commits when Claude finishes a session in this project.\n"
            "  Only commits when the working tree has changes. Safe on clean repos.\n"
            + ("  (already present)" if os.path.isfile(hook_settings)
               else "  (.claude/settings.json)")
        )
        self.var_hook = self._opt_row(
            "Add auto-commit Stop hook", hook_note, pad, default=False)

        # Buttons
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 16))
        ttk.Button(btn_frame, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _opt_row(self, label: str, note: str, pad: dict,
                 default: bool = False) -> tk.BooleanVar:
        """Render one ttk.Checkbutton + description label, return its BooleanVar."""
        var = tk.BooleanVar(value=default)
        ttk.Checkbutton(self, text=label, variable=var).pack(anchor=tk.W, **pad)
        tk.Label(self, text=note,
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))
        return var

    def _apply(self):
        # Every var is read BEFORE destroy(): the widgets own them, and
        # reading through a destroyed dialog is a race nobody needs.
        ts      = self.var_ts.get()
        bi      = self.var_bi.get()
        nuitka  = self.var_nuitka.get()
        shadow  = self.var_shadow.get()
        hook    = self.var_hook.get()
        agents  = self.var_agents.get()
        cursor  = self.var_cursor_rule.get()
        self.destroy()
        # The guard lists EVERY flag. Omitting one makes ticking only that box
        # close the dialog and do nothing, which reads as the button being broken.
        if ts or bi or nuitka or shadow or hook or agents or cursor:
            self.callback(self.path, ts, bi, nuitka, shadow,
                          add_git_hook=hook,
                          add_agents=agents,
                          add_cursor_rule=cursor)
