"""CrossProjectSearchDialog — one query across several indexed projects.

Opened from the Projects tab when more than one project is selected. The
merge rule lives in ``helpers/cross_project_search``; this is presentation.

Threading note: the search shells out once per project, so it runs on a worker
and reports back through a ``queue.Queue`` drained by a main-thread ``after``
pump. Calling ``self.after(0, ...)`` FROM the worker raises "main thread is
not in main loop" whenever the main thread is not inside Tcl at that instant —
timing-dependent, so it fails intermittently rather than obviously.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.cross_project_search import search_projects

if TYPE_CHECKING:
    from state import ManagerConfig


class CrossProjectSearchDialog(tk.Toplevel):
    """Search N projects, show results grouped by rank rather than score."""

    _DRAIN_MS = 120

    def __init__(self, parent, project_paths: list, cfg: "ManagerConfig"):
        super().__init__(parent)
        self._paths = list(project_paths)
        self._cfg = cfg
        self._queue: queue.Queue = queue.Queue()
        self._drain_id = None
        self._busy = False

        n = len(self._paths)
        self.title(f"🔍 Search across {n} projects")
        self.configure(bg=C["base"])
        self.minsize(760, 460)
        self._build_ui()
        self._drain()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._entry.focus_set()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        top = tk.Frame(self, bg=C["base"])
        top.pack(fill=tk.X, padx=10, pady=(10, 4))

        tk.Label(top, text="Find:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._entry = ttk.Entry(top, width=42)
        self._entry.pack(side=tk.LEFT, padx=(6, 6))
        self._entry.bind("<Return>", lambda _e: self._on_search())
        self._search_btn = ttk.Button(top, text="Search",
                                      style="Primary.TButton",
                                      command=self._on_search)
        self._search_btn.pack(side=tk.LEFT)

        names = ", ".join(os.path.basename(p.rstrip("/\\")) or p
                          for p in self._paths)
        tk.Label(self, text=f"in: {names}", bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8), anchor=tk.W, wraplength=740,
                 justify=tk.LEFT).pack(fill=tk.X, padx=12, pady=(0, 4))

        self._status = tk.StringVar(
            value="Results interleave each project's best matches — "
                  "search scores are not comparable between projects.")
        tk.Label(self, textvariable=self._status, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9), anchor=tk.W,
                 wraplength=740, justify=tk.LEFT).pack(
                     fill=tk.X, padx=12, pady=(0, 6))

        frame = tk.Frame(self, bg=C["base"])
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self._tv = ttk.Treeview(
            frame, columns=("project", "kind", "location"),
            show="tree headings", selectmode="browse", height=16)
        self._tv.heading("#0", text="Symbol")
        self._tv.heading("project", text="Project")
        self._tv.heading("kind", text="Kind")
        self._tv.heading("location", text="Location")
        self._tv.column("#0", width=250, anchor=tk.W)
        self._tv.column("project", width=170, anchor=tk.W)
        self._tv.column("kind", width=80, anchor=tk.W)
        self._tv.column("location", width=280, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tv.yview)
        self._tv.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        bottom = tk.Frame(self, bg=C["base"])
        bottom.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Copy location",
                   command=self._copy_location).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    # ── search ────────────────────────────────────────────────────────────

    def _on_search(self) -> None:
        query = self._entry.get().strip()
        if not query or self._busy:
            return
        self._busy = True
        self._search_btn.configure(state=tk.DISABLED)
        self._tv.delete(*self._tv.get_children())
        self._status.set(f"Searching {len(self._paths)} projects…")

        exe = getattr(self._cfg, "tokensave_exe", "") or "tokensave"
        paths, q = list(self._paths), self._queue

        def worker() -> None:
            try:
                hits, failures = search_projects(exe, paths, query)
                q.put(("done", hits, failures))
            except Exception as exc:                        # noqa: BLE001
                q.put(("error", [], [("search", str(exc))]))

        threading.Thread(target=worker, daemon=True,
                         name="cross-project-search").start()

    def _drain(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, hits, failures = self._queue.get_nowait()
                self._render(kind, hits, failures)
        except queue.Empty:
            pass
        try:
            self._drain_id = self.after(self._DRAIN_MS, self._drain)
        except tk.TclError:
            self._drain_id = None

    def _render(self, kind: str, hits: list, failures: list) -> None:
        self._busy = False
        try:
            self._search_btn.configure(state=tk.NORMAL)
        except tk.TclError:
            return
        for i, h in enumerate(hits):
            self._tv.insert("", tk.END, iid=str(i), text=h.name,
                            values=(h.project, h.kind, h.location))
        # Failures are reported, never folded into "no results" — a project
        # that could not be searched is a different answer from one with no
        # matches, and hiding that lets a broken index look like a clean miss.
        bits = []
        if hits:
            projects = len({h.project for h in hits})
            bits.append(f"{len(hits)} result"
                        f"{'s' if len(hits) != 1 else ''} "
                        f"across {projects} project"
                        f"{'s' if projects != 1 else ''}")
        else:
            bits.append("No matches")
        if failures:
            detail = "; ".join(f"{name}: {why}" for name, why in failures[:3])
            bits.append(f"⚠ {len(failures)} project(s) could not be "
                        f"searched — {detail}")
        self._status.set("  ·  ".join(bits))

    # ── misc ──────────────────────────────────────────────────────────────

    def _copy_location(self) -> None:
        sel = self._tv.selection()
        if not sel:
            return
        vals = self._tv.item(sel[0], "values")
        text = f"{vals[0]}  {self._tv.item(sel[0], 'text')}  {vals[2]}"
        self.clipboard_clear()
        self.clipboard_append(text)

    def _on_destroy(self, evt=None) -> None:
        if evt is not None and evt.widget is not self:
            return
        if self._drain_id:
            try:
                self.after_cancel(self._drain_id)
            except tk.TclError:
                pass
            self._drain_id = None
