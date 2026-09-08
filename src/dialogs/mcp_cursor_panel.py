"""Renders the Cursor MCP binding panel.

A mixin on ``dialogs/mcp_config.py``, following the `DesktopMigrationMixin` /
`DuplicateKeysMixin` precedent that file already set rather than introducing a
second pattern. It reads ``self`` attributes the host dialog owns (``_body``,
``_cfg``, ``_render()``, ``_open_file()``) and is never instantiated alone.

Why this is a separate panel rather than more rows in the Claude section
-----------------------------------------------------------------------
The file format is shape-identical, but almost nothing else is, and mixing the
two would force one vocabulary onto two products:

* **No trust gate.** Claude Code will not load a project's ``.mcp.json`` in a
  folder it has not been trusted in, and the Claude rows carry a good deal of
  machinery for saying so. Cursor has no equivalent, so a Cursor row must never
  render a trust verdict — it would be inventing a state the product cannot
  produce.
* **No running-app guard.** Claude Desktop rewrites its config from an
  in-memory cache every minute or two, which is why the Claude rows warn about
  it. Cursor reads ``.cursor/mcp.json`` rather than rewriting it, so a write
  here is not raced by a running editor. Stated rather than silently omitted,
  because "this panel has no warning" should be a decision a reader can check.
* **Five states, not the Claude classifier's.** In particular a `malformed`
  file gets NO Apply button: overwriting it would discard whatever the user was
  in the middle of editing, and the whole reason the state exists separately
  from `absent` is to make that refusal possible.

What it shares with the Claude rows is the prerequisite: a project entry says
``"command": "tokensave"`` so the file stays portable, which makes PATH
resolution a precondition. Offering to bind while that is unmet would write a
config that parses fine and cannot start.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers.mcp_classify import _canonical_project_entry
from helpers.mcp_cursor import (
    STATE_ABSENT,
    STATE_LABELS,
    STATE_MALFORMED,
    STATE_PRESENT_CORRECT,
    STATE_PRESENT_WRONG_TARGET,
    STATE_UNREADABLE,
    bind_cursor_project,
    cursor_global_mcp_path,
    cursor_project_mcp_path,
    ensure_cursor_mcp_ignored,
    unbind_cursor_project,
)

#: States that describe a file we must not rewrite. They still render a row —
#: staying quiet about a broken config is how it goes unnoticed — but the row
#: offers "Open file", never "Bind".
_UNWRITABLE = (STATE_MALFORMED, STATE_UNREADABLE)


class CursorBindingMixin:
    """Renders the per-project Cursor MCP binding panel."""

    # ── Entry point ──────────────────────────────────────────────────────

    def _render_cursor_section(self):
        """Draw the Cursor panel, or one quiet line when Cursor is absent.

        Rendering the full section on a machine without Cursor would be a
        permanently empty block for most users; rendering nothing at all would
        make the feature undiscoverable for the ones about to install it. One
        line is the honest middle.

        An existing binding also counts as reason to show the section: someone
        who bound a project then uninstalled Cursor still has a config file on
        disk, and hiding the row would leave nothing in the UI that mentions it.
        """
        rows = self._cursor_rows()
        installed = os.path.isdir(os.path.dirname(cursor_global_mcp_path()))
        if not installed and not any(r[2] != STATE_ABSENT for r in rows):
            self._render_cursor_absent_strip()
            return

        tk.Label(self._body,
                 text="  Per-project bindings  (Cursor)",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(
            anchor=tk.W, padx=4, pady=(16, 0))
        tk.Label(self._body,
                 text=("  Cursor reads <project>/.cursor/mcp.json, and "
                       "~/.cursor/mcp.json for servers available everywhere. "
                       "Unlike Claude Code there is no trust prompt gating "
                       "the project file, and Cursor does not rewrite it, so "
                       "a binding takes effect on its next reload."),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=740, anchor=tk.W).pack(
            fill=tk.X, padx=4, pady=(0, 4))

        self._render_cursor_global_row()
        if rows:
            self._render_cursor_project_rows(rows)

    def _render_cursor_project_rows(self, rows: list):
        """Bucket the projects and draw them, problems first.

        Order is the point: a malformed file and a binding aimed at another
        project both need a human, and both are invisible if they sort below
        fifteen healthy rows. Bound projects collapse behind a count for the
        same reason the Claude section does.
        """
        blocked = self._cursor_blocked_reason()
        needs = [r for r in rows if r[2] == STATE_ABSENT]
        broken = [r for r in rows
                  if r[2] in _UNWRITABLE or r[2] == STATE_PRESENT_WRONG_TARGET]
        bound = [r for r in rows if r[2] == STATE_PRESENT_CORRECT]

        for name, root, state, detail in broken + needs:
            self._render_cursor_row(name, root, state, detail, blocked)

        if not bound:
            return
        if getattr(self, "_show_cursor_bound", False):
            for name, root, state, detail in bound:
                self._render_cursor_row(name, root, state, detail, blocked)
            return

        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(6, 2))
        tk.Label(strip,
                 text="✓  %d project%s bound in Cursor" % (
                     len(bound), "" if len(bound) == 1 else "s"),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["green"]).pack(
            side=tk.LEFT)
        ttk.Button(strip, text="show",
                   command=self._toggle_cursor_bound).pack(
            side=tk.LEFT, padx=(10, 0))

    # ── State gathering ──────────────────────────────────────────────────

    def _cursor_rows(self) -> list:
        """``(name, root, state, detail)`` for every indexed project.

        Same filter as the Claude section: a project with no tokensave index
        has nothing to bind a server TO. Never raises — a discovery failure
        must not take the whole dialog down with it.
        """
        from helpers.mcp_cursor import cursor_binding_state
        try:
            from helpers.project_discovery import find_projects
            projects = find_projects(self._cfg.search_roots)
        except Exception:                                    # noqa: BLE001
            return []
        rows = []
        for proj in projects:
            root = proj.get("path") if isinstance(proj, dict) else str(proj)
            if not root or not os.path.isdir(os.path.join(root, ".tokensave")):
                continue
            name = (proj.get("name") if isinstance(proj, dict) else "") \
                or os.path.basename(root) or root
            state, detail = cursor_binding_state(root)
            rows.append((name, root, state, detail))
        return rows

    def _cursor_blocked_reason(self) -> str:
        """Why binding is unavailable right now, or "" when it is fine."""
        try:
            from helpers import path_setup
            if path_setup.read_state(self._cfg.raw).is_ready:
                return ""
        except Exception:                                    # noqa: BLE001
            return ""
        return ("Blocked: `tokensave` does not resolve as a command yet. "
                "Binding now would write a config Cursor cannot start.")

    # ── Rows ─────────────────────────────────────────────────────────────

    def _render_cursor_absent_strip(self):
        """One line, for a machine with no Cursor and nothing bound."""
        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(12, 2))
        tk.Label(strip,
                 text=("•  Cursor not detected — no ~/.cursor "
                       "directory. Install Cursor to bind projects to it."),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                 anchor=tk.W).pack(side=tk.LEFT)

    def _render_cursor_global_row(self):
        """The machine-wide ~/.cursor/mcp.json, read-only.

        Shown but not editable from here: the global file is what
        `tokensave install --agent cursor` writes, and giving this dialog a
        second way to write it would be two tools owning one file. The row
        exists so a user wondering why a project is unbound can see whether a
        global entry is already covering it.
        """
        path = cursor_global_mcp_path()
        from helpers.mcp_cursor import read_cursor_mcp
        data, state = read_cursor_mcp(path)
        has_ts = bool(data and isinstance(data.get("mcpServers"), dict)
                      and "tokensave" in data["mcpServers"])
        if state in _UNWRITABLE:
            text, colour = ("⚠  Global ~/.cursor/mcp.json: %s"
                            % STATE_LABELS[state]), C["peach"]
        elif has_ts:
            text, colour = ("✓  Global ~/.cursor/mcp.json has a "
                            "tokensave entry"), C["green"]
        else:
            text, colour = ("•  Global ~/.cursor/mcp.json has no "
                            "tokensave entry — use Tool Manager → "
                            "Wire into agents to add one"), C["overlay0"]
        strip = tk.Frame(self._body, bg=C["base"])
        strip.pack(fill=tk.X, padx=4, pady=(2, 6))
        tk.Label(strip, text=text, font=("Segoe UI", 9),
                 bg=C["base"], fg=colour, anchor=tk.W).pack(side=tk.LEFT)
        ttk.Button(strip, text="open",
                   command=lambda p=path: self._open_file(p)).pack(
            side=tk.LEFT, padx=(10, 0))

    def _render_cursor_row(self, name: str, root: str, state: str,
                           detail: str, blocked: str):
        """One project's Cursor binding, with the action its state allows."""
        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(4, 2), ipady=4)

        head = tk.Frame(box, bg=C["surface0"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        badge, colour = self._cursor_badge(state)
        tk.Label(head, text="%s  %s" % (badge, name),
                 font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=colour, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(head, text=STATE_LABELS.get(state, state),
                 font=("Segoe UI", 8), bg=C["surface0"],
                 fg=C["subtext"]).pack(side=tk.LEFT, padx=(10, 0))

        tk.Label(box, text="  " + cursor_project_mcp_path(root),
                 font=("Consolas", 8), bg=C["surface0"], fg=C["overlay0"],
                 anchor=tk.W).pack(fill=tk.X, padx=8)

        if state == STATE_PRESENT_WRONG_TARGET:
            tk.Label(box,
                     text=("  The entry names a different project: %s" % detail),
                     font=("Segoe UI", 8), bg=C["surface0"], fg=C["peach"],
                     justify=tk.LEFT, wraplength=700, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(2, 0))

        btns = tk.Frame(box, bg=C["surface0"])
        btns.pack(fill=tk.X, padx=8, pady=(4, 2))

        if state in _UNWRITABLE:
            # Deliberately no Bind button. Rewriting would discard whatever is
            # in the file, and "broken" is not "empty".
            tk.Label(btns,
                     text=("Left alone — fix or remove the file, then "
                           "re-detect. It is not overwritten."),
                     font=("Segoe UI", 8, "italic"), bg=C["surface0"],
                     fg=C["overlay0"]).pack(side=tk.LEFT)
            ttk.Button(btns, text="Open file",
                       command=lambda r=root: self._open_file(
                           cursor_project_mcp_path(r))).pack(
                side=tk.RIGHT)
            return

        if state == STATE_PRESENT_CORRECT:
            ttk.Button(btns, text="Unbind",
                       command=lambda r=root: self._cursor_unbind(r)).pack(
                side=tk.LEFT)
            ttk.Button(btns, text="Open file",
                       command=lambda r=root: self._open_file(
                           cursor_project_mcp_path(r))).pack(side=tk.RIGHT)
            return

        if blocked:
            tk.Label(btns, text=blocked, font=("Segoe UI", 8),
                     bg=C["surface0"], fg=C["peach"],
                     justify=tk.LEFT, wraplength=700).pack(side=tk.LEFT)
            return

        label = "Rebind" if state == STATE_PRESENT_WRONG_TARGET else "Bind"
        ttk.Button(btns, text=label, style="Primary.TButton",
                   command=lambda r=root: self._cursor_bind(r)).pack(
            side=tk.LEFT)

    @staticmethod
    def _cursor_badge(state: str) -> tuple:
        if state == STATE_PRESENT_CORRECT:
            return "✓", C["green"]
        if state in _UNWRITABLE:
            return "✗", C["red"]
        if state == STATE_PRESENT_WRONG_TARGET:
            return "⚠", C["peach"]
        return "•", C["subtext"]

    # ── Actions ──────────────────────────────────────────────────────────

    def _cursor_bind(self, root: str):
        """Write the portable entry into <project>/.cursor/mcp.json."""
        entry = _canonical_project_entry(self._cfg.raw)
        ok, err = bind_cursor_project(root, entry)
        if not ok:
            messagebox.showerror("Could not bind Cursor", err, parent=self)
            return
        note = ""
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        added, detail = ensure_cursor_mcp_ignored(root, raw)
        if added:
            # Worth saying out loud: the rules files under .cursor/ stay
            # committed, and only this one file is ignored.
            note = ("\n\n" + detail
                    + "\n(.cursor/rules/ is not ignored — those are "
                      "shared project config.)")
        messagebox.showinfo(
            "Cursor binding written",
            "%s now has a tokensave MCP entry for Cursor.%s"
            % (os.path.basename(root), note), parent=self)
        self._render()

    def _cursor_unbind(self, root: str):
        """Remove only our server key, leaving any others in place."""
        ok, err = unbind_cursor_project(root)
        if not ok:
            messagebox.showerror("Could not unbind", err, parent=self)
            return
        self._render()

    def _toggle_cursor_bound(self):
        self._show_cursor_bound = not getattr(self, "_show_cursor_bound", False)
        self._render()
