"""SnippetsController — owns the Reference (Snippets + CLI cheatsheet) tab.

Two stacked panels:
  - CLI cheatsheet at top (static, just rendered Text).
  - Prompt-snippets selector at bottom — built-in PROMPT_SNIPPETS
    plus user-added ones from manager-config.json.

Mutates `cfg.raw["user_snippets"]` and calls `cfg.save()` when the user
adds / edits / deletes a custom snippet. Per Decision 5, no
`refresh_derived()` is needed — `user_snippets` is a raw list that no
ManagerConfig @property depends on.

Per Round 4 plan: `self._cfg.raw.get(...)` at execution time (Rule 3),
NOT a snapshot in __init__.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C, _BASE_DIR
from dialogs.snippet_edit import SnippetEditDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class SnippetsController:
    """Owns the Reference/Snippets tab UI."""

    def __init__(self, notebook: ttk.Notebook, cfg: "ManagerConfig",
                 prompt_snippets: list):
        """
        prompt_snippets: the static `PROMPT_SNIPPETS` list from the monolith.
            Passed in (rather than imported) so this controller doesn't have
            to know which module owns the built-in snippet catalogue.
        """
        self._cfg = cfg
        self._prompt_snippets = prompt_snippets
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Reference  ")
        self._build()

    def _build(self):
        tab = self._tab

        # ── Top: CLI cheatsheet ───────────────────────────────────────────────
        tk.Label(tab, text="CLI COMMANDS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=14, pady=(10, 4))

        cli_wrap = tk.Frame(tab, bg=C["mantle"])
        cli_wrap.pack(fill=tk.X, padx=14)

        cli_sb = ttk.Scrollbar(cli_wrap, orient="vertical")
        cli_txt = tk.Text(cli_wrap, height=9, font=("Consolas", 9),
                          bg=C["mantle"], fg=C["text"], relief=tk.FLAT,
                          padx=12, pady=8, wrap=tk.NONE,
                          cursor="arrow", state=tk.NORMAL,
                          yscrollcommand=cli_sb.set)
        cli_sb.configure(command=cli_txt.yview)
        cli_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cli_sb.pack(side=tk.RIGHT, fill=tk.Y)

        cli_txt.tag_configure("hd",  font=("Segoe UI", 9, "bold"), foreground=C["blue"],   spacing1=8, spacing3=2)
        cli_txt.tag_configure("cmd", font=("Consolas", 9),          foreground=C["peach"],  spacing3=1)
        cli_txt.tag_configure("dim", font=("Consolas", 9),          foreground=C["overlay0"])

        def cli_row(cmd, desc):
            cli_txt.insert(tk.END, f"  {cmd:<38}", "cmd")
            cli_txt.insert(tk.END, f"{desc}\n", "dim")

        def cli_h(t):
            cli_txt.insert(tk.END, f"\n  {t}\n", "hd")

        cli_h("Daily use")
        cli_row("tokensave sync",               "Incremental re-index (fast)")
        cli_row("tokensave sync --force",        "Full re-index from scratch")
        cli_row("tokensave sync --doctor",       "Sync and list what changed")
        cli_row("tokensave status",              "Stats + estimated token savings")
        cli_row("tokensave status --details",    "Stats with node-kind breakdown")
        cli_row("tokensave files",               "List all indexed files")
        cli_row("tokensave monitor",             "Live TUI of MCP tool calls")
        cli_h("Setup")
        cli_row("tokensave init",                "First-time index of a project")
        cli_row("tokensave install --agent claude", "Wire up Claude Code integration")
        cli_row("tokensave daemon",              "Auto-sync daemon (foreground)")
        cli_row("tokensave daemon --enable-autostart", "Install daemon as a service")
        cli_h("Troubleshooting")
        cli_row("tokensave doctor",              "Health check — diagnose issues")
        cli_row("tokensave upgrade",             "Self-update to latest version")
        cli_h("Cost & token tracking")
        cli_row("tokensave cost",                "7-day cost summary")
        cli_row("tokensave cost today",          "Today's spend only")
        cli_row("tokensave cost --by-model",     "Breakdown by Claude model")
        cli_h("Branches")
        cli_row("tokensave branch add",          "Track current git branch")
        cli_row("tokensave branch list",         "View tracked branches + DB sizes")
        cli_row("tokensave branch gc",           "Clean up deleted branches")

        cli_txt.configure(state=tk.DISABLED)

        # ── Bottom: Claude prompt snippets ────────────────────────────────────
        snippets_header = tk.Frame(tab, bg=C["base"])
        snippets_header.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(snippets_header, text="CLAUDE PROMPT SNIPPETS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(snippets_header, text="📖  Open Full Guide",
                   command=self._open_guide).pack(side=tk.RIGHT)

        snippets_frame = tk.Frame(tab, bg=C["base"])
        snippets_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        list_wrap = tk.Frame(snippets_frame, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self.snippet_lb = tk.Listbox(
            list_wrap, width=26, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        list_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                                command=self.snippet_lb.yview)
        self.snippet_lb.configure(yscrollcommand=list_sb.set)
        self.snippet_lb.pack(side=tk.LEFT, fill=tk.Y)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = tk.Frame(snippets_frame, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    def _refresh_snippet_list(self, reselect_index=None):
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        for title, text in self._prompt_snippets:
            self.snippet_lb.insert(tk.END, f"  {title}")
            self._active_snippets_map.append({"type": "builtin", "data": {"title": title, "text": text}})

        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        for idx, u in enumerate(self._cfg.raw.get("user_snippets", [])):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            self.snippet_lb.selection_clear(0, tk.END)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            return

        text = meta["data"]["text"]
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        is_user = (meta["type"] == "user")
        self._edit_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return
        self._tab.clipboard_clear()
        self._tab.clipboard_append(meta["data"]["text"])
        self._copy_status.configure(text="✔ Copied!")
        self._tab.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self._tab, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        SnippetEditDialog(self._tab, meta, self._on_snippet_saved)

    def _delete_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Delete snippet",
            f"Delete '{title}'?\n\nThis cannot be undone.",
            parent=self._tab,
        ):
            return
        raw = self._cfg.raw
        user_snippets = raw.get("user_snippets", [])
        idx = meta["index"]
        del user_snippets[idx]
        raw["user_snippets"] = user_snippets
        self._cfg.save()
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog — save and refresh."""
        raw = self._cfg.raw
        user_snippets = raw.get("user_snippets", [])
        if edit_meta is None:
            user_snippets.append({"title": title, "text": text})
            new_idx = len(self._prompt_snippets) + 1 + len(user_snippets) - 1
        else:
            idx = edit_meta["index"]
            user_snippets[idx] = {"title": title, "text": text}
            new_idx = len(self._prompt_snippets) + 1 + idx
        raw["user_snippets"] = user_snippets
        self._cfg.save()
        self._refresh_snippet_list(reselect_index=new_idx)

    def _open_guide(self):
        guide = os.path.join(_BASE_DIR, "TOKENSAVE_GUIDE.md")
        if os.path.isfile(guide):
            os.startfile(guide)
        else:
            messagebox.showerror("Not found",
                f"Guide not found at:\n{guide}", parent=self._tab)
