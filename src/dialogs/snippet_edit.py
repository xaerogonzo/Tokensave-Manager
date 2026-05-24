"""SnippetEditDialog — add or edit a prompt snippet.

Used by SnippetsController (Reference tab). On save, fires a callback
with `(title, text, edit_meta)` where `edit_meta` is None for "new"
and the original `_active_snippets_map` entry for "edit in place".

Round-4 Reference-tab overhaul: now also used to edit BUILT-IN snippet
overrides. When `read_only_title=True`:
  - The title field becomes a read-only Label (built-in titles can't
    be renamed — the title is the override key).
  - Empty body is ACCEPTED rather than rejected — saving a blank body
    is the user's intent to discard the override (the controller's
    `_on_snippet_saved` pops the override key in that case).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

from constants import C


class SnippetEditDialog(tk.Toplevel):
    """Add or edit a user-defined OR built-in-override prompt snippet."""

    def __init__(self, parent, edit_meta, callback, read_only_title: bool = False):
        """
        edit_meta: None for new snippet, or the _active_snippets_map entry for editing.
        callback(title, text, edit_meta): called on save; edit_meta is passed back.
        read_only_title: when True, title renders as a Label (not Entry) and an
            empty body is accepted as the user's intent to revert the override.
            Used by SnippetsController to route built-in edits through the same
            dialog without allowing title rename or blank user-snippet creation.
        """
        super().__init__(parent)
        # Heading reflects mode: built-in override vs. new user snippet vs. edit.
        if read_only_title:
            heading = "Edit Built-in Prompt (clear body to reset)"
        else:
            heading = "Add Snippet" if edit_meta is None else "Edit Snippet"
        self.title(heading)
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._callback = callback
        self._edit_meta = edit_meta
        self._read_only_title = read_only_title

        pad = dict(padx=20, pady=4)

        tk.Label(self,
                 text=heading,
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 8))

        # Title field — Label for read-only mode (built-in overrides), Entry otherwise.
        tk.Label(self, text="Title", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        initial_title = edit_meta["data"]["title"] if edit_meta else ""
        self._title_var = tk.StringVar(value=initial_title)
        if read_only_title:
            # Static label — built-in titles ARE their override-dict keys,
            # so renaming would silently orphan the override.
            tk.Label(self, text=initial_title, bg=C["mantle"], fg=C["text"],
                     font=("Segoe UI", 10), padx=8, pady=4, anchor=tk.W,
                     width=52).pack(anchor=tk.W, padx=20, pady=(2, 6))
        else:
            ttk.Entry(self, textvariable=self._title_var, width=52).pack(
                anchor=tk.W, padx=20, pady=(2, 6))

        # Body field
        tk.Label(self, text="Prompt text", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)

        body_wrap = tk.Frame(self, bg=C["mantle"])
        body_wrap.pack(fill=tk.X, padx=20, pady=(2, 12))
        vsb = ttk.Scrollbar(body_wrap, orient="vertical")
        self._body_txt = tk.Text(
            body_wrap, height=8, width=52,
            font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD,
            yscrollcommand=vsb.set,
        )
        vsb.configure(command=self._body_txt.yview)
        self._body_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        if edit_meta:
            self._body_txt.insert(tk.END, edit_meta["data"]["text"])

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        title = self._title_var.get().strip().replace("\n", " ")
        text  = self._body_txt.get("1.0", tk.END).strip()

        if not title:
            messagebox.showwarning("Empty title",
                "Please enter a title for this snippet.", parent=self)
            return
        # Empty body: blocked for new/user snippets; ALLOWED for built-in
        # overrides (the controller's _on_snippet_saved interprets blank
        # body as "discard override / revert to default").
        if not text and not self._read_only_title:
            messagebox.showwarning("Empty text",
                "Please enter the prompt text.", parent=self)
            return

        self.destroy()
        self._callback(title, text, self._edit_meta)
