"""ProjectsSection -- the search roots that decide which projects exist.

Moved out of `dialogs/settings.py` when Settings became a tab. The behaviour
is unchanged: a two-column Treeview of label/path, and Add / Edit Label /
Remove beside it. Each root's label becomes a category header in the project
list, which is why the label is editable at all.

`save_into` always writes the DICT form (`{"path": ..., "label": ...}`) even
for a root that was read as a bare string. Both forms are supported on read
-- `helpers.detection._root_path` / `_root_label` are the only places allowed
to know that -- but there is no reason to write the lossy one back.

DIRTY TRACKING IS EXPLICIT HERE. A Treeview is not a `tk.Variable`, so
`settings_section.bind_vars` cannot see a root being added or removed. The
three handlers report it themselves; forgetting that is how a Save button
stays greyed out over a real edit.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog
from typing import TYPE_CHECKING

from constants import C
from helpers.detection import _root_path, _root_label

if TYPE_CHECKING:
    from state import ManagerConfig


class ProjectsSection:
    """Search-root list: where the manager looks for tokensave projects."""

    def __init__(self, host, body: tk.Frame, cfg: "ManagerConfig") -> None:
        self._host = host
        self._cfg = cfg
        self._on_dirty = lambda: None
        self._build(body, cfg.raw)

    # ── Contract ─────────────────────────────────────────────────────────

    def save_into(self, raw: dict) -> bool:
        """Write the root list. Always succeeds."""
        raw["search_roots"] = [
            {"path": self._tv.set(iid, "path"),
             "label": self._tv.set(iid, "label")}
            for iid in self._tv.get_children()
        ]
        return True

    def bind_dirty(self, callback) -> None:
        self._on_dirty = callback

    def snapshot(self):
        """The root rows, for the controller's clean-state comparison.

        Without this the Save bar stays hidden after adding or removing a
        root: the controller compares Tk variables, and a Treeview is not
        one. See `dialogs/settings_section`.
        """
        return tuple((self._tv.set(iid, "label"), self._tv.set(iid, "path"))
                     for iid in self._tv.get_children())

    # ── Build ────────────────────────────────────────────────────────────

    def _build(self, body, raw):
        tk.Label(body,
                 text="Search roots  —  each root's label becomes a category "
                      "in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))
        roots_frame = tk.Frame(body, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))
        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._tv = ttk.Treeview(tv_wrap, columns=("label", "path"),
                                show="headings", height=8, selectmode="browse")
        self._tv.heading("label", text="Label")
        self._tv.heading("path",  text="Path")
        self._tv.column("label", width=130, stretch=False)
        self._tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                  command=self._tv.yview)
        self._tv.configure(yscrollcommand=roots_vsb.set)
        self._tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        for r in raw.get("search_roots", []):
            self._tv.insert("", tk.END, values=(_root_label(r), _root_path(r)))
        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",
                   command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label",
                   command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",
                   command=self._remove_root).pack(fill=tk.X)

    # ── Row handlers ─────────────────────────────────────────────────────

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self._host)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            "Label for this category:\n"
            "(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self._host,
        )
        if lbl is None:
            return   # user cancelled
        self._tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))
        self._on_dirty()

    def _edit_root_label(self):
        sel = self._tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl = self._tv.set(iid, "label")
        new_lbl = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self._host)
        if new_lbl is not None:
            self._tv.set(iid, "label", new_lbl.strip() or cur_lbl)
            self._on_dirty()

    def _remove_root(self):
        sel = self._tv.selection()
        if sel:
            self._tv.delete(sel[0])
            self._on_dirty()
