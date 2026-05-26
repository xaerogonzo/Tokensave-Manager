"""Roadmap Manager dialog — Theme E of Roadmap-7.

Three-tab Toplevel:
  Audit — parse ROADMAP.md and cross-reference against git history.
          Surfaces stale / promotable entries; double-click opens DocDrafter.
  Plan  — insert a new Roadmap N skeleton through ProposalBridge.
  Ship  — map [Unreleased] changelog bullets to active 🟡 entries (Jaccard
          similarity) and batch-promote them to ✅ through ProposalBridge.

All file writes go through ProposalBridge — no direct disk mutations here.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C


# ── Jaccard helper ────────────────────────────────────────────────────────────

def _jaccard(a: str, b: str) -> float:
    """Token-level Jaccard similarity between two strings (case-insensitive)."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa and not sb:
        return 1.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union)

_JACCARD_FLOOR = 0.5


# ── Main dialog ───────────────────────────────────────────────────────────────

class RoadmapManagerDialog(tk.Toplevel):
    """Roadmap Manager — Audit / Plan / Ship tabs."""

    def __init__(self, parent, project_path: str, cfg):
        super().__init__(parent)
        self._parent       = parent
        self._project_path = project_path
        self._cfg          = cfg

        name = os.path.basename(project_path)
        self.title(f"📋 Roadmap Manager — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(680, 480)

        self._roadmap_path   = os.path.join(project_path, "docs", "ROADMAP.md")
        self._changelog_path = os.path.join(project_path, "CHANGELOG.md")

        self._entries   = []   # list[RoadmapEntry]  — loaded in bg thread
        self._findings  = []   # list[AuditFinding]  — loaded in bg thread

        self._build_ui()
        self._load_async()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📋  Roadmap Manager",
                 bg=C["base"], fg=C["blue"], font=("Segoe UI", 12, "bold")
                 ).pack(anchor=tk.W)
        tk.Label(hdr, text=self._roadmap_path,
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8)
                 ).pack(anchor=tk.W)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._audit_frame = tk.Frame(nb, bg=C["base"])
        self._plan_frame  = tk.Frame(nb, bg=C["base"])
        self._ship_frame  = tk.Frame(nb, bg=C["base"])
        nb.add(self._audit_frame, text="  Audit  ")
        nb.add(self._plan_frame,  text="  Plan   ")
        nb.add(self._ship_frame,  text="  Ship   ")

        self._build_audit_tab()
        self._build_plan_tab()
        self._build_ship_tab()

    # ─── Audit tab ────────────────────────────────────────────────────────────

    def _build_audit_tab(self):
        f = self._audit_frame

        toolbar = tk.Frame(f, bg=C["base"], padx=8, pady=6)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="🔄 Refresh",
                   command=self._load_async).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="🆙 Promote selected",
                   command=self._on_promote_selected).pack(side=tk.LEFT, padx=(6, 0))
        self._audit_status_var = tk.StringVar(value="Loading…")
        tk.Label(toolbar, textvariable=self._audit_status_var,
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8)
                 ).pack(side=tk.LEFT, padx=(12, 0))

        cols = ("status", "roadmap", "title", "days", "action")
        tv = ttk.Treeview(f, columns=cols, show="headings",
                          selectmode="browse")
        tv.heading("status",  text="Status")
        tv.heading("roadmap", text="Roadmap")
        tv.heading("title",   text="Title")
        tv.heading("days",    text="Days")
        tv.heading("action",  text="Action")
        tv.column("status",   width=55,  anchor=tk.CENTER, stretch=False)
        tv.column("roadmap",  width=80,  anchor=tk.CENTER, stretch=False)
        tv.column("title",    width=300, anchor=tk.W)
        tv.column("days",     width=55,  anchor=tk.CENTER, stretch=False)
        tv.column("action",   width=100, anchor=tk.CENTER, stretch=False)

        sb = ttk.Scrollbar(f, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side=tk.LEFT, fill=tk.Y, pady=(0, 8))

        tv.tag_configure("promotable", foreground=C.get("green",  "#a6e3a1"))
        tv.tag_configure("stale",      foreground=C.get("red",    "#f38ba8"))
        tv.tag_configure("ok",         foreground=C.get("text",   "#cdd6f4"))
        tv.tag_configure("done",       foreground=C.get("overlay0","#6c7086"))

        tv.bind("<Double-1>", self._on_audit_double_click)
        self._audit_tree = tv

    # ─── Plan tab ─────────────────────────────────────────────────────────────

    def _build_plan_tab(self):
        f = self._plan_frame
        pad = {"padx": 18, "pady": 6}

        tk.Label(f, text="Insert a new Roadmap skeleton into ROADMAP.md",
                 bg=C["base"], fg=C["subtext0"], font=("Segoe UI", 9)
                 ).pack(anchor=tk.W, **pad)

        fields_frame = tk.Frame(f, bg=C["base"])
        fields_frame.pack(fill=tk.X, padx=18)

        def _row(label, var, hint=""):
            row = tk.Frame(fields_frame, bg=C["base"])
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, bg=C["base"], fg=C["text"],
                     font=("Segoe UI", 9), width=16, anchor=tk.W
                     ).pack(side=tk.LEFT)
            e = ttk.Entry(row, textvariable=var, width=50)
            e.pack(side=tk.LEFT)
            if hint:
                tk.Label(row, text=hint, bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(6, 0))

        self._plan_n_var    = tk.StringVar(value="?")
        self._plan_theme_var = tk.StringVar()
        self._plan_date_var  = tk.StringVar()
        self._plan_note_var  = tk.StringVar()

        _row("Roadmap N",    self._plan_n_var,    "(auto-detected)")
        _row("Theme title",  self._plan_theme_var, "e.g. 'Performance + UX polish'")
        _row("Target date",  self._plan_date_var,  "e.g. '2026-Q3'")
        _row("CHANGELOG note", self._plan_note_var, "one-line summary (optional)")

        ttk.Button(f, text="🛡️  Insert skeleton via Proposal",
                   command=self._on_plan_insert).pack(anchor=tk.W, padx=18, pady=12)

        self._plan_status_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self._plan_status_var,
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8)
                 ).pack(anchor=tk.W, padx=18)

    # ─── Ship tab ─────────────────────────────────────────────────────────────

    def _build_ship_tab(self):
        f = self._ship_frame

        info = tk.Frame(f, bg=C["base"], padx=8, pady=6)
        info.pack(fill=tk.X)
        tk.Label(info,
                 text="Match [Unreleased] changelog bullets to active 🟡 entries"
                      " (Jaccard ≥ 0.50).  Select promotions and apply.",
                 bg=C["base"], fg=C["subtext0"], font=("Segoe UI", 9),
                 wraplength=580, justify=tk.LEFT).pack(anchor=tk.W)

        toolbar = tk.Frame(f, bg=C["base"], padx=8, pady=4)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="🔄 Refresh matches",
                   command=self._populate_ship_tab).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="✓ Apply selected promotions",
                   command=self._on_ship_apply).pack(side=tk.LEFT, padx=(6, 0))

        self._ship_status_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self._ship_status_var,
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8)
                 ).pack(side=tk.LEFT, padx=(12, 0))

        canvas_frame = tk.Frame(f, bg=C["base"])
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        canvas = tk.Canvas(canvas_frame, bg=C["base"], highlightthickness=0)
        sb = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)

        self._ship_canvas = canvas
        self._ship_inner  = tk.Frame(canvas, bg=C["base"])
        self._ship_win_id = canvas.create_window((0, 0), window=self._ship_inner,
                                                  anchor=tk.NW)
        self._ship_inner.bind("<Configure>",
                              lambda e: canvas.configure(
                                  scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(self._ship_win_id,
                                                width=e.width))
        # Checkboxes stored here: list of (BooleanVar, roadmap_n, title)
        self._ship_checks: list[tuple[tk.BooleanVar, int, str]] = []

    # ── Async data loading ────────────────────────────────────────────────────

    def _load_async(self):
        self._audit_status_var.set("Loading…")
        for item in self._audit_tree.get_children():
            self._audit_tree.delete(item)
        t = threading.Thread(target=self._bg_load, daemon=True)
        t.start()

    def _bg_load(self):
        from helpers.roadmap_parser import parse_roadmap_md, next_roadmap_n
        from helpers.roadmap_audit  import audit_entries

        text = ""
        if os.path.exists(self._roadmap_path):
            try:
                with open(self._roadmap_path, encoding="utf-8-sig") as fh:
                    text = fh.read()
            except OSError:
                pass

        entries  = parse_roadmap_md(text)
        findings = audit_entries(
            entries,
            repo_path=self._project_path,
            git_exe=self._cfg.git_exe,
            tokensave_exe=getattr(self._cfg, "tokensave_exe", ""),
        )

        self._entries  = entries
        self._findings = findings

        self.after(0, lambda n=next_roadmap_n(entries): self._on_load_done(n))

    def _on_load_done(self, next_n: int):
        # Update audit tree
        for item in self._audit_tree.get_children():
            self._audit_tree.delete(item)

        promotable = stale = 0
        for f in self._findings:
            e = f.entry
            days_str = str(f.days_since_last_evidence) if f.days_since_last_evidence is not None else "—"
            if f.flag == "promotable":
                action = "🆙 Promote"
                tag    = "promotable"
                promotable += 1
            elif f.flag == "stale":
                action = "💤 Stale"
                tag    = "stale"
                stale += 1
            elif e.status == "✅":
                action = "✅ Done"
                tag    = "done"
            else:
                action = ""
                tag    = "ok"

            self._audit_tree.insert("", tk.END, values=(
                e.status, f"R-{e.roadmap_n}", e.title, days_str, action
            ), tags=(tag,))

        total = len(self._findings)
        self._audit_status_var.set(
            f"{total} entries — {promotable} promotable, {stale} stale"
        )

        # Update plan tab N
        self._plan_n_var.set(str(next_n))

        # Populate ship tab
        self._populate_ship_tab()

    # ── Audit tab actions ─────────────────────────────────────────────────────

    def _on_audit_double_click(self, event):
        item = self._audit_tree.focus()
        if not item:
            return
        row = self._audit_tree.item(item, "values")
        if not row:
            return
        # row = (status, "R-N", title, days, action)
        try:
            roadmap_n = int(row[1].replace("R-", ""))
        except ValueError:
            return
        title = row[2]
        # Find matching entry
        entry = next((e for e in self._entries
                      if e.roadmap_n == roadmap_n and e.title == title), None)
        if entry is None:
            return
        self._open_doc_drafter_for_roadmap(entry)

    def _open_doc_drafter_for_roadmap(self, entry):
        if not os.path.exists(self._roadmap_path):
            messagebox.showwarning("No ROADMAP.md",
                                   "docs/ROADMAP.md not found.",
                                   parent=self)
            return
        from dialogs.doc_drafter import DocDrafterDialog
        DocDrafterDialog(self._parent, self._project_path, self._cfg)

    def _on_promote_selected(self):
        item = self._audit_tree.focus()
        if not item:
            return
        row = self._audit_tree.item(item, "values")
        if not row:
            return
        try:
            roadmap_n = int(row[1].replace("R-", ""))
        except ValueError:
            return
        title = row[2]
        entry = next((e for e in self._entries
                      if e.roadmap_n == roadmap_n and e.title == title), None)
        if entry is None:
            return
        if entry.status == "✅":
            messagebox.showinfo("Already done",
                                f"'{title}' is already ✅.",
                                parent=self)
            return
        self._promote_one(roadmap_n, title, "✅")

    def _promote_one(self, roadmap_n: int, title: str, new_status: str):
        """Run promote_status through ProposalBridge in a worker thread."""
        from helpers.roadmap_patch import _compute_promote_status
        from dialogs.proposal import WriteProposal, ProposalBridge

        def _worker():
            try:
                with open(self._roadmap_path, encoding="utf-8-sig") as fh:
                    original = fh.read()
            except OSError as exc:
                self.after(0, lambda m=str(exc): messagebox.showerror(
                    "Read error", m, parent=self))
                return

            proposed, ok, msg = _compute_promote_status(
                original, roadmap_n, title, new_status)
            if not ok:
                self.after(0, lambda m=msg: messagebox.showerror(
                    "Promote failed", m, parent=self))
                return

            proposal = WriteProposal(
                filepath=self._roadmap_path,
                original_content=original,
                proposed_content=proposed,
                rationale=f"Promote '{title}' to {new_status} in Roadmap {roadmap_n}",
            )
            bridge = ProposalBridge(self._parent, proposal)
            accepted, final = bridge.invoke()
            if accepted and final is not None:
                from helpers.roadmap_patch import _atomic_write
                _atomic_write(self._roadmap_path, final,
                              f"promoted '{title}' to {new_status}")
                self.after(0, self._load_async)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Plan tab actions ──────────────────────────────────────────────────────

    def _on_plan_insert(self):
        try:
            n = int(self._plan_n_var.get())
        except ValueError:
            messagebox.showwarning("Invalid N",
                                   "Roadmap N must be a number.",
                                   parent=self)
            return

        theme = self._plan_theme_var.get().strip()
        if not theme:
            messagebox.showwarning("Missing theme",
                                   "Enter a theme title.",
                                   parent=self)
            return

        date_str = self._plan_date_var.get().strip()
        note     = self._plan_note_var.get().strip()

        content = self._build_skeleton(n, theme, date_str, note)
        self._plan_status_var.set("Opening proposal…")
        threading.Thread(
            target=self._bg_plan_insert,
            args=(n, theme, content),
            daemon=True,
        ).start()

    def _build_skeleton(self, n: int, theme: str,
                        date_str: str, note: str) -> str:
        lines = []
        if date_str:
            lines.append(f"_Target: {date_str}_\n")
        if note:
            lines.append(f"{note}\n")
        lines.append("### 🔮 Placeholder item")
        lines.append("_Replace this with actual planned items._")
        return "\n".join(lines)

    def _bg_plan_insert(self, roadmap_n: int, theme_title: str, content: str):
        from helpers.roadmap_patch import _compute_insert_roadmap_section
        from dialogs.proposal import WriteProposal, ProposalBridge

        if os.path.exists(self._roadmap_path):
            try:
                with open(self._roadmap_path, encoding="utf-8-sig") as fh:
                    original = fh.read()
            except OSError as exc:
                self.after(0, lambda m=str(exc): messagebox.showerror(
                    "Read error", m, parent=self))
                self.after(0, lambda: self._plan_status_var.set(""))
                return
        else:
            original = ""

        proposed, ok, msg = _compute_insert_roadmap_section(
            original, roadmap_n, theme_title, content)
        if not ok:
            self.after(0, lambda m=msg: messagebox.showerror(
                "Insert failed", m, parent=self))
            self.after(0, lambda: self._plan_status_var.set(""))
            return

        proposal = WriteProposal(
            filepath=self._roadmap_path,
            original_content=original,
            proposed_content=proposed,
            rationale=f"Insert Roadmap {roadmap_n} — {theme_title} skeleton",
        )
        bridge = ProposalBridge(self._parent, proposal)
        accepted, final = bridge.invoke()
        if accepted and final is not None:
            from helpers.roadmap_patch import _atomic_write
            _atomic_write(self._roadmap_path, final,
                          f"inserted Roadmap {roadmap_n} skeleton")
            self.after(0, self._load_async)
            self.after(0, lambda: self._plan_status_var.set(
                f"Roadmap {roadmap_n} inserted."))
        else:
            self.after(0, lambda: self._plan_status_var.set("Proposal rejected."))

    # ── Ship tab ──────────────────────────────────────────────────────────────

    def _populate_ship_tab(self):
        from helpers.changelog_patch import read_unreleased

        # Clear existing checkboxes
        for widget in self._ship_inner.winfo_children():
            widget.destroy()
        self._ship_checks.clear()

        unreleased_text = read_unreleased(self._changelog_path)
        bullets = [
            line.lstrip("- ").strip()
            for line in (unreleased_text or "").splitlines()
            if line.strip().startswith("-")
        ]

        active_entries = [e for e in self._entries if e.status == "🟡"]

        if not bullets:
            tk.Label(self._ship_inner,
                     text="No [Unreleased] bullets found in CHANGELOG.md.",
                     bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9)
                     ).pack(anchor=tk.W, padx=8, pady=4)
            self._ship_status_var.set("")
            return

        if not active_entries:
            tk.Label(self._ship_inner,
                     text="No active 🟡 entries in ROADMAP.md.",
                     bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9)
                     ).pack(anchor=tk.W, padx=8, pady=4)
            self._ship_status_var.set("")
            return

        matched = 0
        for entry in active_entries:
            best_score = 0.0
            best_bullet = ""
            for bullet in bullets:
                score = _jaccard(entry.title, bullet)
                if score > best_score:
                    best_score = score
                    best_bullet = bullet

            if best_score >= _JACCARD_FLOOR:
                var = tk.BooleanVar(value=True)
                self._ship_checks.append((var, entry.roadmap_n, entry.title))
                row = tk.Frame(self._ship_inner, bg=C["base"])
                row.pack(fill=tk.X, padx=8, pady=2)
                ttk.Checkbutton(row, variable=var).pack(side=tk.LEFT)
                tk.Label(row,
                         text=f"R-{entry.roadmap_n}  {entry.status} {entry.title}",
                         bg=C["base"], fg=C["text"], font=("Segoe UI", 9),
                         anchor=tk.W).pack(side=tk.LEFT)
                tk.Label(row,
                         text=f"  ← \"{best_bullet[:60]}\"  ({best_score:.2f})",
                         bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8),
                         anchor=tk.W).pack(side=tk.LEFT)
                matched += 1

        if matched == 0:
            tk.Label(self._ship_inner,
                     text="No matches above Jaccard 0.50 threshold.",
                     bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9)
                     ).pack(anchor=tk.W, padx=8, pady=4)

        self._ship_status_var.set(
            f"{matched} match(es) found from {len(bullets)} bullet(s)"
        )

    def _on_ship_apply(self):
        selected = [(rn, title) for var, rn, title in self._ship_checks
                    if var.get()]
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "Tick at least one entry to promote.",
                                parent=self)
            return
        self._ship_status_var.set(f"Promoting {len(selected)} entries…")
        threading.Thread(
            target=self._bg_ship_apply,
            args=(selected,),
            daemon=True,
        ).start()

    def _bg_ship_apply(self, selected: list[tuple[int, str]]):
        from helpers.roadmap_patch import _compute_promote_status
        from dialogs.proposal import WriteProposal, ProposalBridge

        done = 0
        for roadmap_n, title in selected:
            try:
                with open(self._roadmap_path, encoding="utf-8-sig") as fh:
                    original = fh.read()
            except OSError as exc:
                self.after(0, lambda m=str(exc): messagebox.showerror(
                    "Read error", m, parent=self))
                break

            proposed, ok, msg = _compute_promote_status(
                original, roadmap_n, title, "✅")
            if not ok:
                self.after(0, lambda m=msg: messagebox.showwarning(
                    "Skipped", m, parent=self))
                continue

            proposal = WriteProposal(
                filepath=self._roadmap_path,
                original_content=original,
                proposed_content=proposed,
                rationale=f"Ship: promote '{title}' (R-{roadmap_n}) to ✅",
            )
            bridge = ProposalBridge(self._parent, proposal)
            accepted, final = bridge.invoke()
            if accepted and final is not None:
                from helpers.roadmap_patch import _atomic_write
                _atomic_write(self._roadmap_path, final,
                              f"shipped '{title}'")
                done += 1

        self.after(0, lambda d=done: self._ship_status_var.set(
            f"{d} of {len(selected)} promoted to ✅"))
        self.after(0, self._load_async)
