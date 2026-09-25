"""BatchCommitDialog — one prompt for what a bulk action wrote to many projects.

A bulk Repair used to raise one "Commit this change?" popup per project, each
opening its own commit dialog. Every one of those commits is the same kind of
change, and the Manager knows what it wrote, so this shows them together with a
message derived from rules (`helpers/manager_changes`).

The dialog is a VIEW over three separate facts and never blurs them:

    what the operation reported   candidates, kept in the `BatchItem`
    what is provably the Manager's `Inspection`, computed by `batch_commit`
    what was committed            `CommitResult`, read back from git

Built from an immutable tuple of `BatchItem`, so one bulk invocation is exactly one
dialog. The inspection shown is for DISPLAY: `commit_project` inspects again
immediately before staging, because a dialog can sit open for minutes and a file
may have changed. Nothing here shells out on the Tk thread.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from constants import APP_VERSION, C
from helpers import batch_commit as bcm
from helpers import manager_changes as mch
from theme import UiPumpMixin, _Tooltip

SOURCE_RULES = "RULES"
SOURCE_USER = "USER"

_STATE_TEXT = {
    bcm.READY: ("ready", "green"),
    bcm.NO_ELIGIBLE: ("nothing provably ours", "peach"),
    bcm.BLOCKED: ("blocked", "red"),
}
_RESULT_TEXT = {
    bcm.COMMITTED: ("committed", "green"),
    bcm.NOTHING: ("nothing to commit", "overlay0"),
    bcm.REFUSED: ("refused", "peach"),
    bcm.FAILED: ("failed", "red"),
    bcm.MISMATCH: ("committed, but not what was intended", "red"),
}


def _files_text(ins: "bcm.Inspection") -> str:
    """Exactly what would be committed, and what is left out and why."""
    lines = []
    if ins.state == bcm.BLOCKED:
        return "Blocked: %s" % ins.reason
    if ins.detached:
        lines.append("! Detached HEAD: this commit will not be attached to a branch.")
    if ins.main_branch:
        lines.append("! On %s: committed in place, nothing is pushed." % ins.branch)
    if ins.other_changes:
        lines.append("%d other uncommitted change(s) in this project will be left "
                     "alone." % ins.other_changes)
    if lines:
        lines.append("")
    eligible = ins.eligible
    lines.append("Will be committed (%d):" % len(eligible))
    lines += ["    " + f.path for f in eligible] or ["    (none)"]
    excluded = ins.excluded
    if excluded:
        lines += ["", "Left out (%d):" % len(excluded)]
        lines += ["    %s  -  %s" % (f.path, f.reason) for f in excluded]
    return "\n".join(lines)


class BatchCommitDialog(UiPumpMixin, tk.Toplevel):
    """Review, then commit, every project a bulk action changed."""

    def __init__(self, parent, items, cfg, on_log=None, on_committed=None):
        super().__init__(parent)
        self.title("Commit Manager changes")
        self.configure(bg=C["base"])
        self.geometry("1120x660")
        self._items = {i.root: i for i in items}
        self._order = [i.root for i in items]
        self._cfg = cfg
        self._on_log = on_log or (lambda *a, **k: None)
        #: `(root, message)` after a project's VERIFIED commit, on the Tk thread.
        #: The caller runs its private-repo sync from here, per project.
        self._on_committed = on_committed or (lambda *a: None)
        self._inspections: dict = {}
        self._ticks: dict = {}
        self._messages: dict = {}
        self._source: dict = {}
        self._results: dict = {}
        self._busy = False
        self._selected = ""
        self._build()
        self._start_ui_pump()
        self._scan()

    # ── layout ───────────────────────────────────────────────────────────

    def _build(self) -> None:
        tk.Label(self, text="Commit what the Manager wrote",
                 font=("Segoe UI", 12, "bold"), bg=C["base"],
                 fg=C["blue"]).pack(anchor=tk.W, padx=18, pady=(14, 0))
        tk.Label(self,
                 text="Only files that are provably the Manager's own change are "
                      "committed. Anything you had already changed is left "
                      "alone. Nothing is pushed.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                 wraplength=1060, justify=tk.LEFT).pack(anchor=tk.W, padx=18,
                                                        pady=(0, 8))
        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        left = tk.Frame(body, bg=C["base"])
        self._tree = ttk.Treeview(
            left, columns=("tick", "branch", "files", "status"),
            show="tree headings", selectmode="browse", height=16)
        self._tree.heading("#0", text="Project")
        for col, text, width in (("tick", "Commit", 56), ("branch", "Branch", 84),
                                 ("files", "Files", 56), ("status", "Status", 270)):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, anchor=tk.W, stretch=False)
        self._tree.column("#0", width=170)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self._tree.bind("<Button-1>", self._on_click)
        body.add(left, weight=3)

        right = tk.Frame(body, bg=C["base"])
        tk.Label(right, text="Files", font=("Segoe UI", 9, "bold"), bg=C["base"],
                 fg=C["subtext"]).pack(anchor=tk.W)
        files_box = tk.Frame(right, bg=C["base"])
        files_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self._files = tk.Text(files_box, height=9, wrap=tk.WORD, bg=C["surface0"],
                              fg=C["text"], font=("Consolas", 9), relief=tk.FLAT)
        scroll = ttk.Scrollbar(files_box, orient="vertical",
                               command=self._files.yview)
        self._files.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        head = tk.Frame(right, bg=C["base"])
        head.pack(fill=tk.X)
        tk.Label(head, text="Message", font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
        self._source_lbl = tk.Label(head, text="", font=("Segoe UI", 8),
                                    bg=C["base"], fg=C["overlay0"])
        self._source_lbl.pack(side=tk.LEFT, padx=8)
        self._msg = tk.Text(right, height=10, wrap=tk.WORD, bg=C["surface0"],
                            fg=C["text"], insertbackground=C["text"],
                            font=("Consolas", 9), relief=tk.FLAT)
        self._msg.pack(fill=tk.BOTH, expand=True)
        self._msg.bind("<KeyRelease>", lambda e: self._on_edit())
        self._all_btn = ttk.Button(right, text="Use this message for all",
                                   command=self._use_for_all)
        self._all_btn.pack(anchor=tk.W, pady=(6, 0))
        _Tooltip(self._all_btn,
                 "Applies this exact text to every ticked project. It is then "
                 "your message, not the rule-built one, and is kept as written.")
        body.add(right, weight=4)

        self._summary = tk.Label(self, text="Scanning…", font=("Segoe UI", 9),
                                 bg=C["base"], fg=C["overlay0"], anchor=tk.W,
                                 justify=tk.LEFT, wraplength=920)
        self._summary.pack(fill=tk.X, padx=18, pady=(4, 0))
        bar = tk.Frame(self, bg=C["base"])
        bar.pack(fill=tk.X, padx=18, pady=(6, 14))
        self._go = ttk.Button(bar, text="Commit", style="Primary.TButton",
                              command=self._commit)
        self._go.pack(side=tk.LEFT)
        self._go.state(["disabled"])
        ttk.Button(bar, text="Leave dirty", command=self.destroy).pack(
            side=tk.LEFT, padx=(8, 0))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    # ── scanning (off the Tk thread) ─────────────────────────────────────

    def _scan(self) -> None:
        git_exe = self._cfg.git_exe
        items = [self._items[r] for r in self._order]

        def worker():
            found = {}
            for item in items:
                try:
                    found[item.root] = bcm.inspect_project(item, git_exe)
                except Exception as exc:                    # noqa: BLE001
                    found[item.root] = bcm.Inspection(
                        item.root, item.name, bcm.BLOCKED, "inspection failed: %s" % exc)
            self._post(lambda: self._render(found))

        threading.Thread(target=worker, daemon=True).start()

    def _render(self, found: dict) -> None:
        if not self.winfo_exists():
            return
        self._inspections = found
        for root in self._order:
            ins = found[root]
            if ins.state == bcm.NOTHING_CHANGED:
                continue                        # nothing to offer: never listed
            self._ticks[root] = ins.state == bcm.READY
            self._messages[root] = mch.compose(ins.files, APP_VERSION).text()
            self._source[root] = SOURCE_RULES
        self._refresh_rows()
        offered = [r for r in self._order if r in self._ticks]
        skipped = len(self._order) - len(offered)
        note = ("%d project(s) had nothing left to commit (local-only or already "
                "committed) and are not listed." % skipped) if skipped else ""
        self._summary.configure(
            text=note or ("%d project(s)." % len(offered)) if offered else
            "Nothing to commit: none of the changes differ from what is committed.")
        if offered:
            first = offered[0]
            self._tree.selection_set(first)
            self._on_select()
        self._sync_button()

    def _refresh_rows(self) -> None:
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for root in self._order:
            if root not in self._ticks:
                continue
            ins = self._inspections[root]
            result = self._results.get(root)
            if result is not None:
                status, colour = _RESULT_TEXT.get(result.outcome, (result.outcome, "text"))
                if result.detail:
                    status += ": " + result.detail
            else:
                status, colour = _STATE_TEXT.get(ins.state, (ins.state, "text"))
                if ins.detached:
                    status += " (detached HEAD)"
                elif ins.main_branch:
                    status += " (on %s)" % ins.branch
            tick = "☑" if self._ticks[root] else "☐"
            if ins.state != bcm.READY or result is not None:
                tick = "–"
            self._tree.insert("", tk.END, iid=root, text=ins.name, values=(
                tick, ins.branch or ("(detached)" if ins.detached else ""),
                "%d/%d" % (len(ins.eligible), len(ins.files)), status))

    # ── interaction ──────────────────────────────────────────────────────

    def _on_click(self, event) -> None:
        if self._busy or self._tree.identify_column(event.x) != "#1":
            return
        iid = self._tree.identify_row(event.y)
        ins = self._inspections.get(iid)
        if iid and ins is not None and ins.state == bcm.READY \
                and iid not in self._results:
            self._ticks[iid] = not self._ticks[iid]
            self._refresh_rows()
            self._tree.selection_set(iid)
            self._sync_button()

    def _on_select(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        self._store_message()
        self._selected = sel[0]
        ins = self._inspections[self._selected]
        self._set_text(self._files, _files_text(ins), readonly=True)
        self._set_text(self._msg, self._messages.get(self._selected, ""))
        self._source_lbl.configure(
            text="rule-built" if self._source.get(self._selected) == SOURCE_RULES
            else "your text")

    def _set_text(self, widget, text: str, readonly: bool = False) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        if readonly:
            widget.configure(state=tk.DISABLED)

    def _store_message(self) -> None:
        """Keep an edit as the user's; never let a refresh overwrite it."""
        root = self._selected
        if root in self._messages:
            text = self._msg.get("1.0", tk.END).rstrip("\n")
            if text != self._messages[root]:
                self._messages[root] = text
                self._source[root] = SOURCE_USER

    def _on_edit(self) -> None:
        self._store_message()
        self._source_lbl.configure(
            text="rule-built" if self._source.get(self._selected) == SOURCE_RULES
            else "your text")

    def _use_for_all(self) -> None:
        self._store_message()
        text = self._msg.get("1.0", tk.END).rstrip("\n")
        for root in self._ticks:
            self._messages[root] = text
            self._source[root] = SOURCE_USER
        self._source_lbl.configure(text="your text")

    def _sync_button(self) -> None:
        n = sum(1 for r, on in self._ticks.items()
                if on and self._inspections[r].state == bcm.READY
                and r not in self._results)
        self._go.configure(text="Commit %d project%s" % (n, "" if n == 1 else "s"))
        self._go.state(["!disabled"] if n and not self._busy else ["disabled"])

    # ── committing ───────────────────────────────────────────────────────

    def _commit(self) -> None:
        if self._busy:
            return
        self._store_message()
        chosen = [r for r in self._order
                  if self._ticks.get(r) and self._inspections[r].state == bcm.READY
                  and r not in self._results]
        if not chosen:
            return
        self._busy = True
        self._go.state(["disabled"])
        self._summary.configure(text="Committing…")
        git_exe = self._cfg.git_exe
        plan = [(self._items[r], self._source[r], self._messages[r]) for r in chosen]

        def worker():
            try:
                for item, source, text in plan:
                    def words(fresh, source=source, text=text):
                        # Rule-built text is rebuilt from the FRESH inspection, so
                        # its counts describe what is really committed.
                        return text if source == SOURCE_USER else \
                            mch.compose(fresh.files, APP_VERSION).text()
                    try:
                        result = bcm.commit_project(item, words, git_exe)
                    except Exception as exc:                # noqa: BLE001
                        result = bcm.CommitResult(item.root, item.name, bcm.FAILED,
                                                  "unexpected error: %s" % exc)
                    self._post(lambda r=result, t=text: self._row_done(r, t))
            finally:
                # However it ended, the button must not stay disabled.
                self._post(self._finish)

        threading.Thread(target=worker, daemon=True).start()

    def _row_done(self, result: "bcm.CommitResult", text: str) -> None:
        if not self.winfo_exists():
            return
        self._results[result.root] = result
        label, colour = _RESULT_TEXT.get(result.outcome, (result.outcome, "text"))
        line = "%s: %s%s" % (result.name, label,
                             (" (%s)" % result.sha[:8]) if result.sha else "")
        if result.detail:
            line += " - " + result.detail
        self._on_log("Commit: " + line, C[colour])
        self._refresh_rows()
        if result.outcome in (bcm.COMMITTED, bcm.MISMATCH):
            # After THIS project's commit and only then: a failure elsewhere
            # must never trigger a sync as though it had succeeded.
            self._on_committed(result.root, text)

    def _finish(self) -> None:
        if not self.winfo_exists():
            return
        self._busy = False
        done = [r for r in self._results.values() if r.outcome == bcm.COMMITTED]
        bad = [r for r in self._results.values()
               if r.outcome in (bcm.FAILED, bcm.MISMATCH, bcm.REFUSED)]
        self._summary.configure(
            text="%d committed, %d not committed. Nothing was pushed." % (
                len(done), len(bad)))
        self._sync_button()
