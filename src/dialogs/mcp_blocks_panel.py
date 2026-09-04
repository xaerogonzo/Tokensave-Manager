"""Renders one block per MCP config file: header, badge, diff, actions.

Split out of ``dialogs/mcp_config.py`` (Roadmap-16 god-class
split), following the `DesktopMigrationMixin` precedent that
file already set rather than introducing a second pattern.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import ttk
from constants import C
from helpers.mcp import (
    ADVISORY_STATES,
    _classify_mcp_entry,
    annotate_project_binding,
)


class EntryBlocksMixin:
    """Renders one block per MCP config file: header, badge, diff, actions.

    A mixin, so it reads ``self`` attributes the host
    dialog owns (``_body``, ``_cfg``, ``_render()``,
    ``_post()``, ``_log_to_app()``). It is never
    instantiated on its own.
    """

    def _toggle_bound(self):
        self._show_bound = not self._show_bound
        self._render()

    def _render_block(self, label: str, path: str, blocked_reason: str = "",
                      project_root: str = ""):
        info = _classify_mcp_entry(path, self._cfg.raw)
        if project_root:
            info = annotate_project_binding(
                info, project_root, projects=self._claude_projects)
        self._config_state[path] = info

        frame = tk.LabelFrame(
            self._body, text=f"  {label}  ",
            bg=C["base"], fg=C["text"],
            font=("Segoe UI", 10, "bold"),
            bd=1, relief=tk.GROOVE)
        frame.pack(fill=tk.X, padx=4, pady=(8, 4), ipady=4)

        self._render_block_header(frame, label, path, info)
        # No diff for an advisory row: the file already matches the proposal,
        # so a current-vs-proposed box would show two identical blocks and
        # imply there is an edit to make.
        if info["state"] != "ok" and info["state"] not in ADVISORY_STATES:
            self._render_block_diff(frame, info)
        self._render_block_actions(frame, label, path, info, blocked_reason,
                                   project_root)

    def _render_block_header(self, frame, label: str, path: str, info: dict):
        """Path label, optional UWP tag, and status badge."""
        head = tk.Frame(frame, bg=C["base"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(head, text=path, font=("Consolas", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)

        # UWP / traditional install indicator — only meaningful for the
        # Desktop row.  Users hitting the UWP path-redirection footgun
        # need to SEE that the manager is targeting the package-internal
        # config, not %APPDATA%\Claude\.  Knowing this prevents a whole
        # category of "I edited the file but Desktop ignores my fix"
        # confusion.
        if label == "Claude Desktop":
            is_uwp = "\\Packages\\Claude_" in path
            install_tag = ("(UWP / Store install)" if is_uwp
                           else "(Traditional install)")
            tag_colour = C["blue"] if is_uwp else C["overlay0"]
            tk.Label(head, text="  " + install_tag,
                     font=("Segoe UI", 8, "italic"),
                     bg=C["base"], fg=tag_colour).pack(side=tk.LEFT)

        state = info["state"]
        badge = tk.Label(head, text=info["label"],
                         font=("Segoe UI", 9, "bold"),
                         bg=C["base"], fg=self._badge_colour(state))
        badge.pack(side=tk.RIGHT)

        # The default only made sense for the two global configs; a project
        # binding deliberately does NOT route through the wrapper. "bound to
        # this project" is deliberately NOT claimed here any more: this row is
        # rendered from the file alone, and the file cannot know whether Claude
        # Code is serving it. The verification pass upgrades the wording once
        # something has actually been asked.
        _ok_default = ("The file binds tokensave to this project."
                       if os.path.basename(path).lower() == ".mcp.json"
                       else "No action needed — already routes through the wrapper.")
        issue_text = info["issue"] or _ok_default
        issue = tk.Label(frame, text=issue_text,
                         font=("Segoe UI", 9),
                         bg=C["base"], fg=C["overlay0"],
                         justify=tk.LEFT, wraplength=720, anchor=tk.W)
        issue.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._row_widgets[path] = (badge, issue)

    @staticmethod
    def _badge_colour(state: str) -> str:
        """Colour for a status badge.

        project_mismatch sits with the unreadable/missing cases rather than the
        drift ones: a binding pointed at ANOTHER project answers every query
        from the wrong codebase and looks entirely normal doing it.
        """
        if state == "ok":
            return C["green"]
        if state in ("direct_serve", "wrong_wrapper", "project_unbound",
                     "project_absolute") or state in ADVISORY_STATES:
            return C["peach"]
        return C["red"]

    def _render_block_diff(self, frame, info: dict):
        """Diff text box showing current vs proposed JSON — only for non-ok states."""
        diff_box = tk.Text(
            frame, height=8, font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.NONE,
            state=tk.NORMAL)
        diff_box.tag_configure("old", foreground="#f38ba8")
        diff_box.tag_configure("new", foreground="#a6e3a1")
        diff_box.tag_configure("hdr", foreground=C["overlay0"],
                                font=("Consolas", 9, "italic"))

        if info["current"] is None:
            diff_box.insert(tk.END, "  (no current entry — will be added)\n", "hdr")
        else:
            diff_box.insert(tk.END, "  --- current ---\n", "hdr")
            for line in json.dumps(info["current"], indent=2).splitlines():
                diff_box.insert(tk.END, "  - " + line + "\n", "old")
        diff_box.insert(tk.END, "  +++ proposed +++\n", "hdr")
        for line in json.dumps(info["proposed"], indent=2).splitlines():
            diff_box.insert(tk.END, "  + " + line + "\n", "new")

        line_count = int(diff_box.index("end-1c").split(".")[0])
        diff_box.configure(height=min(max(line_count + 1, 6), 18),
                           state=tk.DISABLED)
        diff_box.pack(fill=tk.X, padx=8, pady=(2, 4))

    def _render_block_actions(self, frame, label: str, path: str, info: dict,
                              blocked_reason: str = "",
                              project_root: str = ""):
        """Apply / Skip / Open buttons and the backup-notice strip."""
        actions = tk.Frame(frame, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 4))

        # An advisory row has nothing to apply, so a "binding is blocked"
        # notice would name a prerequisite for work that is already done.
        if (blocked_reason and info["state"] != "ok"
                and info["state"] not in ADVISORY_STATES):
            # No Apply button rather than a disabled one: the reason is the
            # useful part, and a greyed control invites clicking to find
            # out why it is greyed.
            tk.Label(actions, text=blocked_reason,
                     font=("Segoe UI", 9), bg=C["base"], fg=C["peach"],
                     justify=tk.LEFT, wraplength=700, anchor=tk.W).pack(
                fill=tk.X)
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(
                side=tk.LEFT, pady=(4, 0))
            return

        if info["state"] == "ok" or info["state"] in ADVISORY_STATES:
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(side=tk.LEFT)
            # The retired row is the one "ok" state a user might want to leave,
            # and the flag that produces it has no other exit from the UI.
            if "retired" in info.get("label", ""):
                ttk.Button(actions, text="Re-add user-scoped entry…",
                           command=self._unretire_user_scoped).pack(
                    side=tk.LEFT, padx=(8, 0))
            # An unapproved row is the one advisory state the manager can
            # actually resolve itself, rather than only explaining.
            if info["state"] == "project_unapproved" and project_root:
                ttk.Button(
                    actions, text="Approve this binding",
                    style="Primary.TButton",
                    command=lambda r=project_root: self._approve_one(r)).pack(
                    side=tk.LEFT, padx=(8, 0))
        else:
            ttk.Button(
                actions, text="Apply this fix",
                style="Primary.TButton",
                command=lambda p=path, l=label: self._apply(p, l)).pack(side=tk.LEFT)
            ttk.Button(actions, text="Skip (don't warn again)",
                       command=lambda p=path: self._skip(p)).pack(
                side=tk.LEFT, padx=(8, 0))
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(
                side=tk.LEFT, padx=(8, 0))

        tk.Label(frame,
            text=("  A timestamped backup is written before any change. "
                  "Other mcpServers entries in this file are preserved "
                  "(as data \u2014 formatting and comments are not)."),
            font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))
