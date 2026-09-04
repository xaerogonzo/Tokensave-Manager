"""Renders the duplicate `~/.claude.json` project-key panel.

Split out of ``dialogs/mcp_config.py`` (Roadmap-16 god-class
split), following the `DesktopMigrationMixin` precedent that
file already set rather than introducing a second pattern.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from constants import C
from helpers.mcp import (
    duplicate_project_keys,
)


class DuplicateKeysMixin:
    """Renders the duplicate `~/.claude.json` project-key panel.

    A mixin, so it reads ``self`` attributes the host
    dialog owns (``_body``, ``_cfg``, ``_render()``,
    ``_post()``, ``_log_to_app()``). It is never
    instantiated on its own.
    """

    def _render_duplicate_keys(self):
        """Note — quietly — that one directory is recorded under several spellings.

        This was a ⚠ block listing every duplicate, and it dominated the top of
        the dialog while describing something that needs no action. Two reasons
        it is now collapsed and informational:

        Its severity was inherited from a claim that has since stopped being
        true. It warned that duplicates mean "two independent sets of MCP
        approvals" — but approval lives in each project's own
        `.claude/settings.local.json`, not in these keys, so the part that
        actually mattered does not depend on the spelling any more. What is
        left is the trust flag, allowed-tools and session history: a re-prompt
        at worst.

        And nothing is being asked of the user. The manager no longer mints
        duplicates, and merging existing ones means choosing whose settings
        survive — a decision for the show-diff protocol, not a status render.
        A red block with no action attached trains people to ignore red blocks.
        """
        dups = duplicate_project_keys(projects=self._claude_projects)
        if not dups:
            return

        if not self._show_dups:
            strip = tk.Frame(self._body, bg=C["base"])
            strip.pack(fill=tk.X, padx=4, pady=(8, 2))
            tk.Label(strip,
                     text="•  %d project%s recorded under more than one path "
                          "spelling in ~/.claude.json — no action needed"
                          % (len(dups), "" if len(dups) == 1 else "s"),
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"], anchor=tk.W).pack(side=tk.LEFT)
            ttk.Button(strip, text="details",
                       command=self._toggle_dups).pack(
                side=tk.LEFT, padx=(10, 0))
            return

        box = tk.Frame(self._body, bg=C["surface0"])
        box.pack(fill=tk.X, padx=4, pady=(10, 2), ipady=6)
        head = tk.Frame(box, bg=C["surface0"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(head,
                 text="•  %d project%s recorded under more than one path "
                      "spelling" % (len(dups), "" if len(dups) == 1 else "s"),
                 font=("Segoe UI", 10, "bold"),
                 bg=C["surface0"], fg=C["blue"], anchor=tk.W).pack(side=tk.LEFT)
        ttk.Button(head, text="hide", command=self._toggle_dups).pack(
            side=tk.RIGHT)
        tk.Label(box,
                 text=("  Claude Code keys per-project state by the directory "
                       "a session started in, spelled however the launcher "
                       "spelled it. This does NOT affect MCP approval — that "
                       "lives in each project's own .claude/settings.local.json. "
                       "What is split across spellings is the trust flag, "
                       "allowed-tools and session history, so an unfamiliar "
                       "spelling may re-ask the trust question once."),
                 font=("Segoe UI", 9), bg=C["surface0"], fg=C["text"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8)

        # Say which of these are nobody's real project. Most were created by
        # tools running `claude` in a directory Claude Code had not seen — this
        # manager's own status checks included — so "are these supposed to
        # exist?" has an answer, and it is mostly "no".
        from helpers.mcp import stale_duplicate_keys
        stale = stale_duplicate_keys(projects=self._claude_projects)
        n_stale = sum(len(v) for v in stale.values())
        if n_stale:
            tk.Label(
                box,
                text=("  %d of these hold no session and no settings a "
                      "sibling spelling does not already have. Those are "
                      "leftovers from something running `claude` in the "
                      "directory once — this manager's own status checks "
                      "included. Deleting one costs at most the trust "
                      "question being asked again, once, if you ever launch "
                      "a session with that exact spelling."
                      % n_stale),
                font=("Segoe UI", 9), bg=C["surface0"], fg=C["subtext"],
                justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
                fill=tk.X, padx=8, pady=(4, 0))

        # A fixed-height scroller rather than one Label per path. Expanded, the
        # per-Label version grew with the duplicate count until it pushed the
        # bindings off the page and scrolled its own `hide` button out of
        # reach — so the only way to close a panel needing no action was to
        # scroll back up hunting for it. Bounded here, the whole panel stays
        # about twelve lines whatever the count, and because it scrolls, every
        # group can be listed instead of truncating at six.
        listing = tk.Frame(box, bg=C["surface0"])
        listing.pack(fill=tk.X, padx=8, pady=(4, 2))
        text = tk.Text(listing, height=8, font=("Consolas", 8),
                       bg=C["mantle"], fg=C["subtext"], relief=tk.FLAT,
                       padx=8, pady=6, wrap=tk.NONE)
        bar = ttk.Scrollbar(listing, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        for group in dups.values():
            for key in group:
                text.insert(tk.END, key + "\n")
            text.insert(tk.END, "\n")
        # Swallow the wheel so it scrolls this box and not the dialog behind
        # it — the SettingsDialog rule for any nested scroller.
        text.bind("<MouseWheel>",
                  lambda e, w=text: (w.yview_scroll(
                      int(-1 * (e.delta / 120)), "units"), "break")[1])
        text.configure(state=tk.DISABLED)

        tk.Label(box,
                 text=("  The manager now launches `claude` using a spelling "
                       "Claude Code already has on file, so it no longer adds "
                       "new ones. Collapsing the existing duplicates means "
                       "deciding which side's settings survive, so it is left "
                       "to you — edit the file directly if you want them "
                       "merged. Leaving them alone is a fine answer."),
                 font=("Segoe UI", 8, "italic"),
                 bg=C["surface0"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(2, 2))
        ttk.Button(box, text="Open ~/.claude.json",
                   command=lambda: self._open_file(
                       os.path.join(os.path.expanduser("~"),
                                    ".claude.json"))).pack(
            anchor=tk.W, padx=8, pady=(2, 4))

    def _toggle_dups(self):
        self._show_dups = not self._show_dups
        self._render()
