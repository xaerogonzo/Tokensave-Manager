"""CommitRequestBanner — the Git tab's commit-request handoff banner.

Extracted from GitTabController (Roadmap-8 god-class fix). Owns the
non-modal banner shown when an external tool (typically a Claude Code
chat session) has written ``.tokensave-manager/commit_request.json``
for the selected project — see helpers/commit_request.py for the
schema and consumption contract.

House pattern: callback injection, no controller back-reference.
``get_path`` returns the currently selected project (or None);
``on_commit`` opens the normal commit dialog (which pre-seeds itself
from the request and consumes it on commit); ``get_anchor`` returns
the frame the banner packs ``before=`` so it lands between the Git
header and the status panes.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from constants import C
from helpers.commit_request import clear_commit_request, load_commit_request


class CommitRequestBanner:
    """Hidden-by-default banner; ``update()`` shows/hides it per refresh."""

    def __init__(self, tab: tk.Frame, *, get_path, on_commit,
                 get_anchor) -> None:
        self._get_path = get_path
        self._on_commit = on_commit
        self._get_anchor = get_anchor

        bar = tk.Frame(tab, bg=C["surface0"], padx=14, pady=6)
        self._banner = bar
        self._lbl = tk.Label(
            bar, text="", bg=C["surface0"], fg=C["blue"],
            font=("Segoe UI", 9), wraplength=560,
            justify=tk.LEFT, anchor=tk.W)
        self._lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(bar, text="📝 Review && Commit…",
                   command=self._on_review).pack(side=tk.LEFT, padx=(8, 6))
        ttk.Button(bar, text="✕ Dismiss",
                   command=self._on_dismiss).pack(side=tk.LEFT)
        # NOT packed — update() shows/hides it.

    def update(self, path: str, is_repo: bool) -> None:
        """Show the banner when a pending commit request exists for *path*."""
        req = load_commit_request(path) if (path and is_repo) else None
        if not req:
            self._banner.pack_forget()
            return
        detail = req["note"] or req["suggested_scope"] or ""
        files_part = f"{len(req['files'])} file(s)"
        text = f"🤝  Commit request from Claude Code — {files_part}"
        if detail:
            text += f":  {detail}"
        self._lbl.configure(text=text)
        self._banner.pack(fill=tk.X, before=self._get_anchor())

    def _on_review(self) -> None:
        """Open the normal commit dialog — it pre-seeds itself from the
        pending request and consumes it on commit."""
        path = self._get_path()
        if path:
            self._on_commit(path)

    def _on_dismiss(self) -> None:
        """Discard the pending request without committing."""
        path = self._get_path()
        if path:
            clear_commit_request(path)
        self._banner.pack_forget()
