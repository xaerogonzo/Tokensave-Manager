"""MergePRDialog — pick an open Pull Request and a merge strategy, then confirm.

Treeview lists open PRs (number, title, source → base, +X/-Y). Three
strategy buttons (Merge commit / Squash / Rebase) each fire a final
confirmation modal showing PR details + the delete-source-branch flag
before calling back to the caller's gh-merge runner.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class MergePRDialog(tk.Toplevel):
    """Pick an open Pull Request and a merge strategy, then confirm.

    Callback: callback(path, pr_number, strategy, delete_branch, title)
      • path           — project root (used to scope the gh invocation)
      • pr_number      — int, the PR number to merge
      • strategy       — 'merge' | 'squash' | 'rebase' (matches gh flags)
      • delete_branch  — bool, whether to pass --delete-branch
      • title          — the PR title (just for logging)

    The list view shows: #N, title (truncated), source → base, +X/-Y.
    Selecting a row enables the action buttons. Three confirm buttons
    correspond to the three gh merge strategies; clicking one pops a
    final "Merge PR #N — <title> — into <base> using <strategy>?"
    confirmation before the callback fires.
    """

    def __init__(self, parent, path: str, prs: list[dict], callback):
        super().__init__(parent)
        self.title("Merge Pull Request")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(720, 380)
        self.geometry("840x460")
        self.grab_set()
        self.transient(parent)

        self._path = path
        self._prs = prs
        self._callback = callback
        # Tracks whether to also delete the source branch on GitHub
        # after a successful merge (the same toggle gh's web UI offers).
        self._var_delete_branch = tk.BooleanVar(value=True)

        self._build_header_section(path)
        self._build_pr_list_section(prs)
        self._build_options_section()
        self._build_buttons_section()

    def _build_header_section(self, path: str):
        """Title row + helper-text label."""
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🐙  Merge Pull Request",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text=os.path.basename(path),
                 font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(
            side=tk.LEFT, padx=(10, 0))

        tk.Label(self,
            text=("Choose an open PR and a merge strategy.  After a "
                  "successful merge, your local clone is auto-switched "
                  "to the base branch and pulled."),
            font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W,
            wraplength=780).pack(fill=tk.X, padx=18, pady=(0, 8))

    def _build_pr_list_section(self, prs: list[dict]):
        """LabelFrame + Treeview + vertical scrollbar; populate rows."""
        list_frame = tk.LabelFrame(
            self, text=f" Open PRs ({len(prs)}) ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))

        tv_wrap = tk.Frame(list_frame, bg=C["mantle"])
        tv_wrap.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._tv = ttk.Treeview(
            tv_wrap,
            columns=("title", "branch", "diff"),
            show="tree headings", height=8)
        self._tv.heading("#0",      text="#")
        self._tv.heading("title",   text="Title")
        self._tv.heading("branch",  text="Source → Base")
        self._tv.heading("diff",    text="+/-")
        self._tv.column("#0",       width=60,  anchor=tk.E)
        self._tv.column("title",    width=380, anchor=tk.W)
        self._tv.column("branch",   width=210, anchor=tk.W)
        self._tv.column("diff",     width=110, anchor=tk.E)
        tv_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                command=self._tv.yview)
        self._tv.configure(yscrollcommand=tv_vsb.set)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv.bind("<<TreeviewSelect>>", self._on_select)

        for pr in prs:
            n      = pr.get("number", "?")
            title  = pr.get("title", "(no title)")
            if len(title) > 60:
                title = title[:57] + "…"
            head   = pr.get("headRefName", "?")
            base   = pr.get("baseRefName", "?")
            add    = pr.get("additions", 0)
            rem    = pr.get("deletions", 0)
            self._tv.insert("", tk.END, iid=str(n),
                             text=f"#{n}",
                             values=(title,
                                     f"{head} → {base}",
                                     f"+{add} -{rem}"))

    def _build_options_section(self):
        """Delete-source-branch checkbox row."""
        opts_row = tk.Frame(self, bg=C["base"])
        opts_row.pack(fill=tk.X, padx=18, pady=(4, 2))
        tk.Checkbutton(opts_row,
            text="Also delete the source branch on GitHub after merge",
            variable=self._var_delete_branch,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(side=tk.LEFT)

    def _build_buttons_section(self):
        """Three strategy buttons (Merge / Squash / Rebase) + Close."""
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(2, 14))
        self._btn_merge = ttk.Button(
            btn_row, text="Merge commit",
            style="Primary.TButton",
            command=lambda: self._confirm("merge"),
            state=tk.DISABLED)
        self._btn_merge.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_squash = ttk.Button(
            btn_row, text="Squash and merge",
            command=lambda: self._confirm("squash"),
            state=tk.DISABLED)
        self._btn_squash.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_rebase = ttk.Button(
            btn_row, text="Rebase and merge",
            command=lambda: self._confirm("rebase"),
            state=tk.DISABLED)
        self._btn_rebase.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _on_select(self, _evt=None):
        sel = self._tv.selection()
        state = tk.NORMAL if sel else tk.DISABLED
        for b in (self._btn_merge, self._btn_squash, self._btn_rebase):
            b.configure(state=state)

    def _confirm(self, strategy: str):
        sel = self._tv.selection()
        if not sel:
            return
        try:
            pr_number = int(sel[0])
        except ValueError:
            return
        pr = next((p for p in self._prs if p.get("number") == pr_number),
                  None)
        if pr is None:
            return
        title = pr.get("title", "(no title)")
        head  = pr.get("headRefName", "?")
        base  = pr.get("baseRefName", "?")
        add   = pr.get("additions", 0)
        rem   = pr.get("deletions", 0)
        delete_branch = bool(self._var_delete_branch.get())

        strategy_name = {
            "merge":  "Merge commit",
            "squash": "Squash and merge",
            "rebase": "Rebase and merge",
        }.get(strategy, strategy)

        msg = (f"Merge PR #{pr_number} into {base}?\n\n"
               f"  Title:    {title}\n"
               f"  Source:   {head}\n"
               f"  Target:   {base}\n"
               f"  Changes:  +{add} / -{rem}\n"
               f"  Strategy: {strategy_name}\n"
               f"  Delete source branch: "
               f"{'yes' if delete_branch else 'no'}\n\n"
               "This runs `gh pr merge` and pushes the result to "
               "GitHub.  Your local master will then be switched-to "
               "and pulled so it reflects the merged state.")
        if not messagebox.askyesno(
                "Merge Pull Request?",
                msg, parent=self):
            return
        self.destroy()
        self._callback(self._path, pr_number, strategy, delete_branch,
                       title)
