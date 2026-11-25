"""The "Not found" group on the Projects tab: rows and their right-click menu.

Kept out of `projects_tab.py` (near its line cap). Rows use `missing:<path>`
iids, so every `proj:`-guarded command ignores them.
"""

from __future__ import annotations

import tkinter as tk

GROUP = "cat:__missing__"
PREFIX = "missing:"


def insert_missing(tree, missing: list) -> None:
    """Add a "Not found" group for projects listed before but not found now."""
    if not missing:
        return
    tree.insert("", tk.END, iid=GROUP, open=True, text="⚠ Not found",
                tags=("category",))
    for m in missing:
        tree.insert(GROUP, tk.END, iid=f"{PREFIX}{m['path']}", text=m["name"],
                    values=("", f"{m['path']}   —   {m['reason']}",
                            "—", "—", "—", "—"),
                    tags=("missing",))


def show_menu(tree, event, menu: tk.Menu, on_settings, on_forget) -> None:
    """Right-click on a not-found row: add a search root, or forget the entry."""
    paths = [i[len(PREFIX):] for i in tree.selection() if i.startswith(PREFIX)]
    n = len(paths)
    menu.add_command(label="Add a search root… (Settings)", command=on_settings)
    menu.add_command(label=f"Forget {n} entr{'y' if n == 1 else 'ies'}",
                     command=lambda: on_forget(paths))
    menu.tk_popup(event.x_root, event.y_root)
