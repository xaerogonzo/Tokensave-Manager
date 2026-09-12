"""McpStatusSection -- what the Claude configs on this machine actually say.

Moved out of `dialogs/settings.py` when Settings became a tab. It is a
read-only summary plus a button into the configurator, so `save_into` writes
nothing and always succeeds -- it exists only so the section list is uniform.

The two pure helpers below are the reason this is a module rather than four
lines inline. Both encode a judgement that has been wrong before:

`_summary` must not claim the wrapper is in use once either migration has
completed. Saying "both route through the wrapper" would describe exactly the
arrangement the user deliberately dismantled.

`_blurb` must not promise that the star pin decides which graph serves a
Claude Code session. Once Claude Desktop's entry is retired the pin picks this
manager's default project and nothing more -- and the previous sentence
promised otherwise, which is the Roadmap-11 mistake of rendering a file's
CONTENTS as an effective scope.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.mcp import (DESKTOP_SCOPE_RETIRED_KEY, USER_SCOPE_RETIRED_KEY,
                         _mcp_configs, _classify_mcp_entry)

if TYPE_CHECKING:
    from state import ManagerConfig


class McpStatusSection:
    """MCP integration status row and the configurator launcher."""

    def __init__(self, host, body: tk.Frame, cfg: "ManagerConfig") -> None:
        self._host = host
        self._cfg = cfg
        self._build(body)

    # ── Contract ─────────────────────────────────────────────────────────

    def save_into(self, raw: dict) -> bool:
        """Nothing to persist -- this section only reports."""
        return True

    def bind_dirty(self, callback) -> None:
        """Nothing here can be edited, so nothing can make the page dirty."""

    # ── Build ────────────────────────────────────────────────────────────

    def _build(self, body):
        tk.Label(body, text="MCP integration",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20,
                                                  pady=(0, 2))
        mcp_row = tk.Frame(body, bg=C["base"])
        mcp_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(mcp_row, text="\U0001f50c  Manage MCP wiring…",
                   command=self._open_mcp_configurator).pack(side=tk.LEFT)
        try:
            states = [_classify_mcp_entry(p, self._cfg.raw)["state"]
                      for _, p in _mcp_configs()]
        except Exception:
            states = []
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        summary, summary_fg = self._summary(states, raw)
        if summary:
            tk.Label(body, text="  " + summary,
                     font=("Segoe UI", 9), bg=C["base"], fg=summary_fg,
                     justify=tk.LEFT, anchor=tk.W,
                     wraplength=620).pack(anchor=tk.W, padx=36, pady=(0, 2))
        tk.Label(body, text=self._blurb(raw),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _open_mcp_configurator(self):
        """Lazy import (Rule 6) -- avoids any dialog module-load cycle."""
        from dialogs.mcp_config import MCPConfigDialog
        MCPConfigDialog(self._host, cfg=self._cfg)

    # ── Pure helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _summary(states: list, raw: dict) -> tuple:
        """``(text, colour)`` for the MCP status line."""
        retired = [name for name, key in
                   (("Claude Desktop", DESKTOP_SCOPE_RETIRED_KEY),
                    ("Claude Code", USER_SCOPE_RETIRED_KEY))
                   if raw.get(key)]
        if states and all(s == "ok" for s in states):
            if retired:
                return ("✓  %s retired — each project serves its own "
                        "graph." % " and ".join(retired), C["green"])
            return ("✓  Both Claude Desktop and Claude Code route through "
                    "the wrapper.", C["green"])
        if "no_file" in states or "missing" in states:
            return ("✗  One or more Claude configs need a tokensave entry.",
                    C["red"])
        if any(s in ("direct_serve", "wrong_wrapper", "unparseable")
               for s in states):
            return ("⚠  One or more Claude configs bypass the wrapper "
                    "(★ pin won't work for them).", C["peach"])
        return "", C["overlay0"]

    @staticmethod
    def _blurb(raw: dict) -> str:
        """What the ★ pin actually does, which the migration changes."""
        if raw.get(DESKTOP_SCOPE_RETIRED_KEY):
            return ("  Each Claude Code session is served by its own "
                    "project's .mcp.json.\n"
                    "  ★ Set as Active now picks this manager's default "
                    "project only — not the MCP graph.")
        return ("  Routes tokensave through the manager's pin-aware wrapper "
                "so\n"
                "  ★ Set as Active swaps projects live, without "
                "restarting Claude.")
