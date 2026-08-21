"""SettingsDialog — edit manager-config.json through the GUI.

Six sections in a scrollable canvas, composed from section modules
(Roadmap-8 god-file split — each owns its widgets, vars, and its slice
of the Save contract via ``save_into(raw)``):

  1. Paths + Git tools — dialogs/settings_paths.py   (PathsSection)
  2. CodeGraph         — dialogs/settings_codegraph.py (CodegraphSection)
  3. Search roots      — here (Treeview of label/path categories)
  4. Behavior toggles  — here (auto-commit, pre-commit hook, MCP row)
  5. AI sections       — dialogs/settings_ai.py      (AISection)

The Save handler is the canonical cfg-mutation path. It mutates
`self._cfg.raw[key]` in place (each section writes its own keys; a
section returning False aborts the save), calls the existing `save_fn`
callback (which persists to disk + writes the on-saved hook for the
legacy global rebind), then fires `callback()` so the App can also
`_state.refresh_derived()` and re-bind any module-level globals that
still exist during the Phase C transition window.

Cross-dialog deps (lazy-imported per plan Rule 6 to avoid any future
module-load cycle):
  • MCPConfigDialog (button: 🔌 Manage MCP wiring…)
  • ToolManagerDialog (buttons in the Paths + CodeGraph sections)

Reads `self._cfg.git_exe` at execution time (Rule 3 — no snapshot
caches). The `cfg.get(...)` and `cfg[...] = ...` patterns from the
original become `cfg.raw.get(...)` and `cfg.raw[...] = ...` here.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog
from typing import TYPE_CHECKING

from constants import C
from theme import bind_mousewheel, themed_checkbutton
from helpers.detection import _root_path, _root_label
from helpers.mcp import _mcp_configs, _classify_mcp_entry
from dialogs.settings_paths import PathsSection
from dialogs.settings_codegraph import CodegraphSection
from dialogs.settings_ai import AISection

if TYPE_CHECKING:
    from state import ManagerConfig


class SettingsDialog(tk.Toplevel):
    """Edit manager-config.json through the GUI."""

    def __init__(self, parent, cfg: "ManagerConfig", save_fn, callback,
                 startup_note: str = ""):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 500)
        self.geometry("760x700")
        self.grab_set()
        self._cfg = cfg
        self._save_fn = save_fn
        self._callback = callback

        # cfg.raw is a live dict — passed to per-section builders so
        # existing `raw.get("x")` patterns work unchanged. Settings Save
        # mutates raw in place and calls save_fn(raw) which persists to disk.
        raw = cfg.raw

        # ── Scrollable content area ───────────────────────────────────────
        # Save/Cancel buttons stay anchored at the bottom (packed on `self`,
        # NOT on the scrollable body). All section content goes on `body`.
        _scroll_wrap = tk.Frame(self, bg=C["base"])
        _scroll_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        _canvas = tk.Canvas(_scroll_wrap, bg=C["base"], highlightthickness=0, bd=0)
        bind_mousewheel(_canvas)
        _vsb = ttk.Scrollbar(_scroll_wrap, orient="vertical", command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(_canvas, bg=C["base"])
        _body_window = _canvas.create_window((0, 0), window=body, anchor="nw")
        def _on_body_configure(event):
            _canvas.configure(scrollregion=_canvas.bbox("all"))
        def _on_canvas_configure(event):
            _canvas.itemconfigure(_body_window, width=event.width)
        body.bind("<Configure>", _on_body_configure)
        _canvas.bind("<Configure>", _on_canvas_configure)
        def _on_mousewheel(event):
            _canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        _canvas.bind("<MouseWheel>", _on_mousewheel)
        body.bind("<MouseWheel>", _on_mousewheel)

        if startup_note:
            tk.Label(body, text=startup_note,
                     bg=C["red"], fg=C["mantle"],
                     font=("Segoe UI", 9, "bold"),
                     justify=tk.LEFT, padx=14, pady=8,
                     wraplength=440).pack(fill=tk.X, pady=(0, 4))

        # Sections in original visual order. Each section owns its vars and
        # writes them back via save_into(raw) in _save.
        self._paths = PathsSection(
            self, body, cfg, open_tool_manager=self._open_tool_manager)
        self._codegraph = CodegraphSection(
            self, body, cfg, open_tool_manager=self._open_tool_manager)
        self._build_roots_section(body, raw)
        self._build_git_toggles_section(body, raw)
        self._build_mcp_section(body)
        self._ai = AISection(self, body, cfg)
        self._sections = [self._paths, self._codegraph, self._ai]

        # ── Save/Cancel — anchored outside the scroll area ────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(8, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _build_roots_section(self, body, raw):
        """Search roots two-column Treeview (label + path)."""
        tk.Label(body,
                 text="Search roots  —  each root's label becomes a category in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))
        roots_frame = tk.Frame(body, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))
        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._roots_tv = ttk.Treeview(tv_wrap, columns=("label", "path"),
                                      show="headings", height=5, selectmode="browse")
        self._roots_tv.heading("label", text="Label")
        self._roots_tv.heading("path",  text="Path")
        self._roots_tv.column("label", width=130, stretch=False)
        self._roots_tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical", command=self._roots_tv.yview)
        self._roots_tv.configure(yscrollcommand=roots_vsb.set)
        self._roots_tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        for r in raw.get("search_roots", []):
            self._roots_tv.insert("", tk.END, values=(_root_label(r), _root_path(r)))
        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",      command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label", command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",     command=self._remove_root).pack(fill=tk.X)

    def _build_git_toggles_section(self, body, raw):
        """Auto-commit after sync + pre-commit smoke-test hook."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._var_autocommit = tk.BooleanVar(value=bool(raw.get("auto_commit_after_sync", False)))
        themed_checkbutton(body,
            text="Auto-commit after sync  (git add -A + git commit)",
            variable=self._var_autocommit,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
            text="  Only fires when the project is a git repo and the working tree has changes.\n"
                 "  Commit message: \"chore: tokensave sync\"  (or AI-generated if enabled below)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        from controllers.pin_watcher import ENABLED_KEY as _PIN_WATCH_KEY
        self._var_pin_watch = tk.BooleanVar(
            value=bool(raw.get(_PIN_WATCH_KEY, False)))
        themed_checkbutton(body,
            text="Apply \u201cSet as Active\u201d to Claude Desktop immediately",
            variable=self._var_pin_watch,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
            text="  Claude Desktop reads the active project once, when it starts its\n"
                 "  tokensave server \u2014 so changing it normally does nothing until you\n"
                 "  restart Desktop. With this on, the manager ends the server left on\n"
                 "  the old project and Desktop starts a fresh one.\n"
                 "  Only works while TokenSave Manager is running, and only touches\n"
                 "  servers the manager itself started. Claude Code sessions bind to\n"
                 "  their own project and are never affected.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        from helpers.smoke_runner import is_hook_installed
        _active_project = (raw.get("projects") or [{}])[0].get("path") or ""
        _hook_active = is_hook_installed(_active_project) if _active_project else False
        self._var_precommit_hook = tk.BooleanVar(value=_hook_active)
        themed_checkbutton(body,
            text="Run smoke tests before commits  (pre-commit hook)",
            variable=self._var_precommit_hook,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
            text="  Installs a .git/hooks/pre-commit script that runs the tests/ suite\n"
                 "  before every commit.  Only affects the active project's git repo.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _build_mcp_section(self, body):
        """MCP integration status row and wrapper health summary."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="MCP integration",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        mcp_row = tk.Frame(body, bg=C["base"])
        mcp_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(mcp_row, text="🔌  Manage MCP wiring…",
                   command=self._open_mcp_configurator).pack(side=tk.LEFT)
        try:
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"]
                      for _, p in _mcp_configs()]
        except Exception:
            states = []
        if states and all(s == "ok" for s in states):
            summary = "✓  Both Claude Desktop and Claude Code route through the wrapper."
            summary_fg = C["green"]
        elif "no_file" in states or "missing" in states:
            summary = "✗  One or more Claude configs need a tokensave entry."
            summary_fg = C["red"]
        elif any(s in ("direct_serve", "wrong_wrapper", "unparseable") for s in states):
            summary = "⚠  One or more Claude configs bypass the wrapper (★ pin won't work for them)."
            summary_fg = C["peach"]
        else:
            summary = ""
            summary_fg = C["overlay0"]
        if summary:
            tk.Label(body, text="  " + summary,
                     font=("Segoe UI", 9), bg=C["base"], fg=summary_fg,
                     justify=tk.LEFT, anchor=tk.W,
                     wraplength=620).pack(anchor=tk.W, padx=36, pady=(0, 2))
        tk.Label(body,
            text="  Routes tokensave through the manager's pin-aware wrapper so\n"
                 "  ★ Set as Active swaps projects live, without restarting Claude.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    # ── Cross-dialog launchers (lazy-imported per Rule 6) ───────────────────

    def _open_mcp_configurator(self):
        """Launch the MCP integration configurator.

        Lazy import (Rule 6) — avoids any module-load cycle between
        dialogs/settings.py and dialogs/mcp_config.py. MCPConfigDialog
        is only needed when the user clicks this button, so the
        in-handler import has no performance cost.
        """
        from dialogs.mcp_config import MCPConfigDialog
        MCPConfigDialog(self, cfg=self._cfg)

    def _open_tool_manager(self):
        """Launch the v4.8 Tool Manager dialog.

        Single discovery surface for install/update/uninstall of the two
        code-graph tools (tokensave + codegraph).  Lazy-imported to
        avoid pulling the dialog's helpers into the settings dialog
        import graph until first use.
        """
        from dialogs.tool_manager import ToolManagerDialog
        ToolManagerDialog(self, self._cfg)

    # ── Search-roots row handlers ────────────────────────────────────────

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            "Label for this category:\n(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self,
        )
        if lbl is None:
            return   # user cancelled
        self._roots_tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))

    def _edit_root_label(self):
        sel = self._roots_tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl  = self._roots_tv.set(iid, "label")
        new_lbl  = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self)
        if new_lbl is not None:
            self._roots_tv.set(iid, "label", new_lbl.strip() or cur_lbl)

    def _remove_root(self):
        sel = self._roots_tv.selection()
        if sel:
            self._roots_tv.delete(sel[0])

    def _scroll_to_codegraph(self):
        """Pull the CodeGraph section into view (delegates to the section)."""
        self._codegraph.focus_path_entry()

    def _save(self):
        """Persist the dialog's pending changes via the cfg-mutation contract.

        The flow:
          1. Each section writes its fields via save_into(raw); a section
             returning False (e.g. tokensave exe path doesn't exist)
             aborts the save with the dialog left open
          2. Dialog-level fields (search roots, behavior toggles, the
             pre-commit hook install/uninstall side effect) are written here
          3. Call self._save_fn(self._cfg.raw) — writes to disk via
             helpers.config._save_config
          4. Call self._callback() — App._on_settings_saved, which
             refreshes _state.refresh_derived() AND re-binds the
             legacy module globals (TOKENSAVE, GIT_EXE, etc.) so any
             remaining global reader sees the new values

        Plan Rule 5 holds throughout: we mutate raw (the writable
        surface), NEVER assign a derived @property — those would
        raise AttributeError on assignment.
        """
        raw = self._cfg.raw
        for section in self._sections:
            if not section.save_into(raw):
                return

        raw["search_roots"] = [
            {"path": self._roots_tv.set(iid, "path"),
             "label": self._roots_tv.set(iid, "label")}
            for iid in self._roots_tv.get_children()
        ]
        raw["auto_commit_after_sync"] = self._var_autocommit.get()
        from controllers.pin_watcher import ENABLED_KEY as _PIN_WATCH_KEY
        raw[_PIN_WATCH_KEY] = self._var_pin_watch.get()

        # Pre-commit smoke-test hook — install or uninstall based on toggle.
        _precommit_wanted = self._var_precommit_hook.get()
        _active_proj = (raw.get("projects") or [{}])[0].get("path") or ""
        if _active_proj:
            from helpers.smoke_runner import (
                is_hook_installed, install_pre_commit_hook,
                uninstall_pre_commit_hook,
            )
            from tkinter import messagebox as _mb
            _currently = is_hook_installed(_active_proj)
            if _precommit_wanted and not _currently:
                _ok, _msg = install_pre_commit_hook(_active_proj)
                if not _ok:
                    _mb.showwarning("Smoke-test hook", _msg, parent=self)
                    self._var_precommit_hook.set(False)
            elif not _precommit_wanted and _currently:
                _ok, _msg = uninstall_pre_commit_hook(_active_proj)
                if not _ok:
                    _mb.showwarning("Smoke-test hook", _msg, parent=self)
                    self._var_precommit_hook.set(True)

        self._save_fn()
        self.destroy()
        self._callback()
