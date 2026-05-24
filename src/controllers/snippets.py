"""SnippetsController — owns the Reference (Snippets + CLI cheatsheet) tab.

Two stacked panels:
  - CLI cheatsheet at top (static, just rendered Text).
  - Prompt-snippets selector at bottom — built-in PROMPT_SNIPPETS
    plus user-added ones from manager-config.json.

Round-4 overhaul (Reference-tab snippet system):

1. **Placeholder substitution.** Snippets may contain `[[double-bracket]]`
   tokens (single `[brackets]` are reserved for markdown). When a snippet
   with placeholders is selected, input fields render below the preview in
   a 2-column grid; on Copy, filled values are substituted in. Empty fields
   leave the `[[token]]` as literal text in the clipboard.

2. **Built-in overrides.** The original `PROMPT_SNIPPETS` in `src/prompts.py`
   is immutable (ROM). User edits land in `cfg.raw["builtin_snippet_overrides"]`
   (RAM overlay). Reset just pops the override key — the default text is
   never copied into the override dict, so Reset is always lossless.

3. **Per-session partial-fill cache.** `self._placeholder_values_cache`
   remembers what the user typed into each snippet's placeholder fields
   so accidental snippet-switches don't lose work. In-memory only; cleared
   on manager restart. Also cleared per-key on override-edit / reset / delete
   since the placeholder names may no longer match.

Mutates `cfg.raw["user_snippets"]` and `cfg.raw["builtin_snippet_overrides"]`
and calls `cfg.save()` after each change. Per Decision 5, no
`refresh_derived()` is needed — neither field backs a ManagerConfig @property.

Per Round 4 plan: `self._cfg.raw.get(...)` at execution time (Rule 3),
NOT a snapshot in __init__.
"""

from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C, _BASE_DIR
from dialogs.snippet_edit import SnippetEditDialog

if TYPE_CHECKING:
    from state import ManagerConfig


# Double-bracket on purpose — single [brackets] are reserved for markdown
# links / footnote refs inside user-authored snippets. The pattern is
# non-greedy on the inner group and explicitly forbids newlines + nested
# brackets so the worst case on a malformed `[[a [[b]] c]]` is parsing
# the inner `[[b]]` as the token, not a runaway match.
_PLACEHOLDER_RE = re.compile(r'\[\[([^\[\]\n]+?)\]\]')


def _extract_placeholders(text: str) -> list:
    """Return ordered, de-duplicated list of [[bracket]] tokens in text."""
    seen: list = []
    for m in _PLACEHOLDER_RE.finditer(text):
        tok = m.group(1).strip()
        if tok and tok not in seen:
            seen.append(tok)
    return seen


def _substitute_placeholders(text: str, values: dict) -> str:
    """Replace [[token]] with values[token] if non-empty; otherwise leave as-is."""
    def _sub(m):
        tok = m.group(1).strip()
        v = (values.get(tok, "") or "").strip()
        return v if v else m.group(0)
    return _PLACEHOLDER_RE.sub(_sub, text)


class SnippetsController:
    """Owns the Reference/Snippets tab UI."""

    def __init__(self, notebook: ttk.Notebook, cfg: "ManagerConfig",
                 prompt_snippets: list):
        """
        prompt_snippets: the static `PROMPT_SNIPPETS` list from src/prompts.py.
            Passed in (rather than imported) so this controller doesn't have
            to know which module owns the built-in snippet catalogue. The
            controller treats this list as IMMUTABLE — overrides layer on top
            via `cfg.raw["builtin_snippet_overrides"]`, never mutate this.
        """
        self._cfg = cfg
        self._prompt_snippets = prompt_snippets

        # Per-session partial-fill cache: snippet title → token → typed value.
        # In-memory only; not persisted to manager-config.json (transient
        # typing state would clutter the config file for marginal benefit).
        self._placeholder_values_cache: dict = {}
        # Tracks which snippet's placeholder fields are currently shown, so
        # _on_snippet_select can snapshot them into the cache BEFORE wiping
        # widgets when the user clicks a different snippet.
        self._currently_displayed_title: "str | None" = None
        # Bound after _build() runs.
        self._placeholder_entries: dict = {}

        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Reference  ")
        self._build()

    # ── UI build ──────────────────────────────────────────────────────────────

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

        # ── LAYOUT-JITTER FIX (Gemini #2) ────────────────────────────────────
        # Pack bottom-pinned rows FIRST so they claim their bottom strip;
        # the preview Text widget packs last with expand=True so it absorbs
        # all remaining vertical space. Result: adding placeholder fields
        # shrinks the preview, never pushes the buttons.

        # Bottom row 1 (visually lowest): user actions
        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._reset_btn = ttk.Button(user_btn_row, text="Reset",
                                     command=self._reset_snippet, state=tk.DISABLED)
        self._reset_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        # Bottom row 2: Copy Prompt + status
        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        # Bottom row 3: placeholder fields (dynamic — populated per snippet)
        self._placeholder_frame = tk.Frame(right, bg=C["base"])
        self._placeholder_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        # 2-column grid: cols 1 and 3 stretch (Entry cells); cols 0/2 are
        # fixed-width labels. Set up here once so per-render code stays small.
        self._placeholder_frame.columnconfigure(1, weight=1)
        self._placeholder_frame.columnconfigure(3, weight=1)

        # Top (above placeholders + buttons): preview absorbs remaining space
        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    # ── Snippet listbox & metadata ───────────────────────────────────────────

    def _refresh_snippet_list(self, reselect_index=None):
        """Rebuild the listbox from built-ins (with overrides applied) + user snippets."""
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        overrides = self._cfg.raw.get("builtin_snippet_overrides", {}) or {}

        # Built-ins — apply override-overlay; mark with ✎ if overridden.
        for title, default_text in self._prompt_snippets:
            override_text = overrides.get(title)
            is_overridden = override_text is not None
            effective_text = override_text if is_overridden else default_text
            display = f"  ✎ {title}" if is_overridden else f"  {title}"
            self.snippet_lb.insert(tk.END, display)
            self._active_snippets_map.append({
                "type": "builtin",
                "is_overridden": is_overridden,
                "data": {"title": title, "text": effective_text},
            })

        # Separator
        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        # User snippets
        for idx, u in enumerate(self._cfg.raw.get("user_snippets", []) or []):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            # Clean state: clear preview + placeholders + button enables.
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._clear_placeholder_frame()
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._reset_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            self._currently_displayed_title = None

    def _clear_placeholder_frame(self):
        """Tear down placeholder widgets without touching the frame itself."""
        for child in list(self._placeholder_frame.winfo_children()):
            child.destroy()
        self._placeholder_entries = {}

    def _snapshot_current_fills(self):
        """Save the currently-displayed snippet's placeholder values into the
        per-session cache. Called BEFORE wiping widgets on snippet-switch
        and also on copy — so accidental clicks never lose typed work."""
        if not self._currently_displayed_title:
            return
        if not self._placeholder_entries:
            return
        self._placeholder_values_cache[self._currently_displayed_title] = {
            tok: var.get() for tok, var in self._placeholder_entries.items()
        }

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return

        # Snapshot OUTGOING snippet's fields FIRST — before wiping widgets.
        self._snapshot_current_fills()

        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            self.snippet_lb.selection_clear(0, tk.END)
            self._clear_placeholder_frame()
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._reset_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            self._currently_displayed_title = None
            return

        title = meta["data"]["title"]
        text = meta["data"]["text"]

        # Preview
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        # Placeholder fields — 2-column grid (Gemini #1: vertical real estate)
        self._clear_placeholder_frame()
        placeholders = _extract_placeholders(text)
        cached = self._placeholder_values_cache.get(title, {})
        for i, tok in enumerate(placeholders):
            row, col_pair = divmod(i, 2)
            label_col = col_pair * 2          # 0 or 2
            entry_col = label_col + 1         # 1 or 3
            tk.Label(self._placeholder_frame,
                     text=f"[[{tok}]]",
                     bg=C["base"], fg=C["subtext"], font=("Segoe UI", 8),
                     ).grid(row=row, column=label_col, sticky="w",
                            padx=(0, 4), pady=2)
            var = tk.StringVar(value=cached.get(tok, ""))
            ttk.Entry(self._placeholder_frame, textvariable=var, width=18,
                      ).grid(row=row, column=entry_col, sticky="ew",
                             padx=(0, 8), pady=2)
            self._placeholder_entries[tok] = var

        self._currently_displayed_title = title

        # Button states.
        is_user = (meta["type"] == "user")
        is_builtin = (meta["type"] == "builtin")
        is_overridden = bool(meta.get("is_overridden"))
        self._edit_btn.configure(state=tk.NORMAL if (is_user or is_builtin) else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._reset_btn.configure(state=tk.NORMAL if (is_builtin and is_overridden) else tk.DISABLED)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return

        # Snapshot current fills BEFORE substituting (re-selecting later
        # restores what was typed even after a Copy).
        self._snapshot_current_fills()

        values = {tok: var.get() for tok, var in self._placeholder_entries.items()}
        substituted = _substitute_placeholders(meta["data"]["text"], values)

        self._tab.clipboard_clear()
        self._tab.clipboard_append(substituted)
        self._copy_status.configure(text="✔ Copied!")
        self._tab.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self._tab, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]

        if meta["type"] == "user":
            SnippetEditDialog(self._tab, meta, self._on_snippet_saved)
            return

        if meta["type"] == "builtin":
            # Built-in override edit: read-only title, empty body allowed
            # (interpreted as "reset" by _on_snippet_saved).
            title = meta["data"]["title"]
            edit_meta = {
                "type": "builtin",
                "override_target": title,
                "data": {"title": title, "text": meta["data"]["text"]},
            }
            SnippetEditDialog(self._tab, edit_meta, self._on_snippet_saved,
                              read_only_title=True)
            return

    def _reset_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "builtin" or not meta.get("is_overridden"):
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Reset to default",
            f"Discard your edits to '{title.strip()}' and restore the "
            "built-in default?\n\nThe original prompt text lives in "
            "src/prompts.py and is never lost — Reset just removes your "
            "override.",
            parent=self._tab,
        ):
            return
        overrides = self._cfg.raw.get("builtin_snippet_overrides", {}) or {}
        overrides.pop(title, None)
        self._cfg.raw["builtin_snippet_overrides"] = overrides
        # Drop any cached partial-fill for this title — text changed.
        self._placeholder_values_cache.pop(title, None)
        self._cfg.save()
        self._refresh_snippet_list(reselect_index=sel[0])

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
        user_snippets = raw.get("user_snippets", []) or []
        idx = meta["index"]
        del user_snippets[idx]
        raw["user_snippets"] = user_snippets
        # Cached fills for a deleted snippet are now orphan — clear them.
        self._placeholder_values_cache.pop(title, None)
        self._cfg.save()
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog.

        Branches on `edit_meta["override_target"]` (set only by built-in edits):
          - Set + empty text  → pop override key (implicit reset, Gemini #3)
          - Set + non-empty   → write override
          - Not set, new      → append user snippet
          - Not set, editing  → replace user snippet in place
        """
        raw = self._cfg.raw

        # Branch 1: built-in override edit
        if edit_meta and edit_meta.get("override_target"):
            overrides = raw.get("builtin_snippet_overrides", {}) or {}
            target_title = edit_meta["override_target"]
            if not text.strip():
                # IMPLICIT RESET — saving a blank body removes the override
                # entirely instead of persisting an empty string (which would
                # leave a ✎-marked built-in showing a blank preview, with no
                # way to tell "overridden empty" from "broken").
                overrides.pop(target_title, None)
            else:
                overrides[target_title] = text
            raw["builtin_snippet_overrides"] = overrides
            # Cache invalidation: override text may have different placeholders.
            self._placeholder_values_cache.pop(target_title, None)
            self._cfg.save()
            # Find this built-in's listbox index — it's the one whose data
            # title matches target_title in the current map.
            target_idx = 0
            for i, m in enumerate(self._active_snippets_map):
                if m.get("type") == "builtin" and m["data"]["title"] == target_title:
                    target_idx = i
                    break
            self._refresh_snippet_list(reselect_index=target_idx)
            return

        # Branch 2: user-snippet add or edit
        user_snippets = raw.get("user_snippets", []) or []
        if edit_meta is None:
            user_snippets.append({"title": title, "text": text})
            new_idx = len(self._prompt_snippets) + 1 + len(user_snippets) - 1
        else:
            idx = edit_meta["index"]
            # If the user renamed, clear the old cache entry.
            old_title = edit_meta["data"]["title"]
            if old_title != title:
                self._placeholder_values_cache.pop(old_title, None)
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
