"""HousekeepingDialog — finish the cleanup tokensave can only report.

Opened from the Projects tab command bar. Two independent panels:

  * **Stale tokensave entries** — what `tokensave doctor` reports but will only
    purge from a real terminal.
  * **Redundant backups** — ``*.bak``-style files left behind by tokensave's own
    config rewrites that are byte-identical to the file they shadow.

Presentation only. Every operation goes through `HousekeepingController`, which
owns the threading; this module never runs a subprocess or touches SQLite.

Two things this dialog is deliberately careful about
----------------------------------------------------
**It never claims an outcome it hasn't checked.** tokensave only offers its
purge prompt on a real terminal, so purging usually means opening one and
letting the user confirm there. A launched terminal — even one that exits
cleanly — proves nothing. So the purge button hands off, says plainly that
nothing has changed yet, and the *verification scan* is what reports the
result: verified, partially purged, no change, or unverified.

**Only exact duplicates can be deleted.** A backup whose contents differ from
the live file, or whose live file is missing, is never selectable and is
summarised as a count instead of listed. Each deletion re-hashes the file
immediately beforehand, so one that changed since the scan is skipped rather
than destroyed.

Layout follows the v4.5 sticky-footer pattern (action bar packed ``side=BOTTOM``
before the expanding middle) and mirrors `dialogs/codegraph_daemon_manager.py`.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from theme import bind_mousewheel

# Scan lifecycle
STATE_IDLE = "idle"
STATE_SCANNING = "scanning"
STATE_READY = "ready"
STATE_ERROR = "error"

# What, if anything, is currently mutating something
ACT_NONE = "none"
ACT_PURGING = "purging"
ACT_DELETING = "deleting_backups"


class HousekeepingDialog(tk.Toplevel):
    """Two-panel cleanup surface for what tokensave can't finish on its own."""

    def __init__(self, parent, path: str, ctrl) -> None:
        super().__init__(parent)
        self._path = path
        self._ctrl = ctrl
        self.title("Housekeeping")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(680, 600)
        self.grab_set()

        self._scan_state = STATE_IDLE
        self._action_state = ACT_NONE
        self._findings = None
        self._baseline: list = []          # entries as of the last scan
        self._backup_vars: list = []       # (BooleanVar, BackupCandidate)

        self._build_header()
        self._build_action_bar()
        self._build_body()

        self._centre_on_parent(parent)
        self.refresh()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C["surface0"], padx=14, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🧹  Housekeeping", bg=C["surface0"], fg=C["blue"],
                 font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
        tk.Label(
            hdr,
            text=("Cleanup that tokensave detects but can't finish by itself. "
                  "Nothing here is reported as done until it has been "
                  "re-checked — a terminal that opened, or even exited "
                  "cleanly, is not evidence that anything changed."),
            bg=C["surface0"], fg=C["text"], font=("Segoe UI", 9),
            wraplength=620, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

    def _build_action_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        bar = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._rescan_btn = ttk.Button(bar, text="↻  Re-scan",
                                      command=self.refresh)
        self._rescan_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self._status = tk.Label(bar, text="", bg=C["base"], fg=C["overlay0"],
                                font=("Segoe UI", 9))
        self._status.pack(side=tk.LEFT)

    def _build_body(self) -> None:
        outer = tk.Frame(self, bg=C["base"])
        outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 4))

        canvas = tk.Canvas(outer, bg=C["base"], highlightthickness=0)
        bind_mousewheel(canvas)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = tk.Frame(canvas, bg=C["base"])
        body_id = canvas.create_window((0, 0), window=self._body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(body_id, width=e.width))
        self._body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    # ── State ─────────────────────────────────────────────────────────────────

    def _set_state(self, scan_state: str = "", action_state: str = "") -> None:
        """Single place that decides what is clickable.

        Actions are disabled whenever a scan is running (the findings on screen
        may be about to change) or an action is running (no double-clicks), and
        re-scan is disabled mid-action so results can't be pulled out from
        under an operation in flight.
        """
        if scan_state:
            self._scan_state = scan_state
        if action_state:
            self._action_state = action_state

        busy = (self._scan_state == STATE_SCANNING
                or self._action_state != ACT_NONE)
        acting = self._action_state != ACT_NONE
        self._rescan_btn.configure(
            state=tk.DISABLED if (acting or self._scan_state == STATE_SCANNING)
            else tk.NORMAL)
        for btn in getattr(self, "_action_buttons", []):
            try:
                btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
            except tk.TclError:
                pass

    def _say(self, text: str, colour: str = "") -> None:
        self._status.configure(text=text, fg=colour or C["overlay0"])

    # ── Scan ──────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._action_state != ACT_NONE:
            return
        self._set_state(scan_state=STATE_SCANNING)
        self._say("Scanning…")
        self._render_placeholder("Scanning…")
        self._ctrl.scan_async(self._path, self._on_scanned)

    def _on_scanned(self, findings) -> None:
        self._findings = findings
        self._baseline = list(findings.entries)
        self._set_state(scan_state=STATE_READY if findings.ok else STATE_ERROR)
        self._say("" if findings.ok else "Scan failed",
                  "" if findings.ok else C["red"])
        self._render()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_placeholder(self, text: str) -> None:
        for w in self._body.winfo_children():
            w.destroy()
        tk.Label(self._body, text=text, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=20)

    def _render(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()
        self._action_buttons: list = []
        self._backup_vars = []
        # Panels render independently — a failure in one must not blank the
        # other, so the stale panel's error state doesn't stop backups showing.
        self._render_stale_panel()
        self._render_backup_panel()
        self._set_state()

    def _panel(self, title: str) -> tk.LabelFrame:
        f = tk.LabelFrame(self._body, text=title, fg=C["subtext"], bg=C["base"],
                          font=("Segoe UI", 9, "bold"))
        f.pack(fill=tk.X, expand=False, pady=(0, 10))
        return f

    def _render_stale_panel(self) -> None:
        f = self._findings
        entries = f.entries if f else []
        # Narrow the heading only when every entry agrees; otherwise stay
        # generic, since "cost history" would be a lie for a registry row.
        sources = {e.source for e in entries}
        title = "Stale tokensave entries"
        if entries and sources == {"cost_history"}:
            title = "Stale cost history"
        panel = self._panel(title)

        if f and not f.ok:
            tk.Label(panel,
                     text=f"?  Could not determine — {f.error}",
                     bg=C["base"], fg=C["red"], font=("Segoe UI", 9),
                     wraplength=600, justify=tk.LEFT
                     ).pack(anchor=tk.W, padx=10, pady=8)
            return

        if not entries:
            tk.Label(panel, text="✓  No stale entries found",
                     bg=C["base"], fg=C["green"], font=("Segoe UI", 9)
                     ).pack(anchor=tk.W, padx=10, pady=8)
            return

        tk.Label(panel,
                 text=("Records whose project state no longer matches the "
                       "filesystem. tokensave itself performs the removal."),
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8),
                 wraplength=600, justify=tk.LEFT
                 ).pack(anchor=tk.W, padx=10, pady=(6, 6))

        for e in entries:
            row = tk.Frame(panel, bg=C["base"])
            row.pack(fill=tk.X, padx=10, pady=(0, 6))
            tk.Label(row, text=os.path.basename(e.path) or e.path,
                     bg=C["base"], fg=C["text"], font=("Consolas", 9)
                     ).pack(anchor=tk.W)
            tk.Label(row, text=f"{e.source_label} · {e.reason_label}",
                     bg=C["base"], fg=C["subtext"], font=("Segoe UI", 8)
                     ).pack(anchor=tk.W)
            if e.may_regenerate:
                tk.Label(row,
                         text="⚠ May return after `tokensave cost` — session "
                              "logs for this project are still on disk",
                         bg=C["base"], fg=C["peach"], font=("Segoe UI", 8),
                         wraplength=580, justify=tk.LEFT).pack(anchor=tk.W)
            elif e.reason == "not_indexed":
                tk.Label(row,
                         text="Re-indexing the directory should clear this "
                              "naturally",
                         bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8)
                         ).pack(anchor=tk.W)
            tk.Label(row, text=e.path, bg=C["base"], fg=C["overlay0"],
                     font=("Consolas", 8), wraplength=600, justify=tk.LEFT
                     ).pack(anchor=tk.W)

        bar = tk.Frame(panel, bg=C["base"])
        bar.pack(fill=tk.X, padx=10, pady=(4, 8))
        b = ttk.Button(bar, text="Purge stale entries via tokensave",
                       command=self._on_purge)
        b.pack(side=tk.LEFT)
        self._action_buttons.append(b)
        v = ttk.Button(bar, text="Verify cleanup", command=self._on_verify)
        v.pack(side=tk.LEFT, padx=(6, 0))
        self._action_buttons.append(v)

    def _render_backup_panel(self) -> None:
        scan = self._findings.backups if self._findings else None
        panel = self._panel("Redundant backups")
        dups = scan.duplicates if scan else []

        if not dups:
            tk.Label(panel, text="✓  No verified-identical backups found",
                     bg=C["base"], fg=C["green"], font=("Segoe UI", 9)
                     ).pack(anchor=tk.W, padx=10, pady=8)
        else:
            for cand in dups:
                var = tk.BooleanVar(value=True)
                self._backup_vars.append((var, cand))
                row = tk.Frame(panel, bg=C["base"])
                row.pack(fill=tk.X, padx=10, pady=(4, 0))
                ttk.Checkbutton(row, variable=var,
                                text=os.path.basename(cand.path)
                                ).pack(side=tk.LEFT)
                tk.Label(row,
                         text=f"SHA-256 identical to live file · "
                              f"{cand.size:,} B",
                         bg=C["base"], fg=C["subtext"], font=("Segoe UI", 8)
                         ).pack(side=tk.LEFT, padx=(8, 0))
                tk.Label(panel, text=cand.path, bg=C["base"], fg=C["overlay0"],
                         font=("Consolas", 8), wraplength=600, justify=tk.LEFT
                         ).pack(anchor=tk.W, padx=32)

            bar = tk.Frame(panel, bg=C["base"])
            bar.pack(fill=tk.X, padx=10, pady=(8, 4))
            b = ttk.Button(bar, text="Delete selected duplicates",
                           command=self._on_delete_backups)
            b.pack(side=tk.LEFT)
            self._action_buttons.append(b)

        note = ("Only exact byte-for-byte duplicates are offered. Different or "
                "orphaned backups are never selected automatically.")
        if scan and scan.kept:
            note += f"   ({scan.kept_label})"
        tk.Label(panel, text=note, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8), wraplength=600, justify=tk.LEFT
                 ).pack(anchor=tk.W, padx=10, pady=(2, 8))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_purge(self) -> None:
        if not self._baseline:
            return
        n = len(self._baseline)
        if not messagebox.askyesno(
                "Purge stale entries?",
                f"tokensave will be asked to remove {n} stale "
                f"entr{'y' if n == 1 else 'ies'}.\n\n"
                "tokensave only offers its purge prompt on a real terminal, so "
                "this may open one for you to confirm in. Nothing is reported "
                "as removed until the manager re-checks afterwards.",
                parent=self):
            return
        self._set_state(action_state=ACT_PURGING)
        self._say("Purging…")
        self._ctrl.purge_async(self._path, self._baseline, self._on_purged)

    def _on_purged(self, result) -> None:
        from helpers.doctor_service import PurgeResult
        self._set_state(action_state=ACT_NONE)
        if result.status == PurgeResult.HANDED_OFF:
            self._say("Handed off to a terminal — nothing changed yet",
                      C["sky"])
            messagebox.showinfo(
                "Finish in the terminal",
                "tokensave only offers its purge prompt on a real terminal, so "
                "one has been opened for you.\n\n"
                "Type 'y' at the prompt there, then come back and press "
                "“Verify cleanup” — that re-checks and reports what actually "
                "changed.\n\n"
                "Nothing has been removed yet.",
                parent=self)
            return
        self._report_verification(result)

    def _on_verify(self) -> None:
        self._set_state(action_state=ACT_PURGING)
        self._say("Verifying…")
        self._ctrl.verify_async(self._path, self._baseline, self._on_verified)

    def _on_verified(self, result) -> None:
        self._set_state(action_state=ACT_NONE)
        self._report_verification(result)

    def _report_verification(self, result) -> None:
        from controllers.doctor_ctrl import DoctorController
        from helpers.doctor_service import VERIFY_VERIFIED
        label = DoctorController.verification_label(result) or result.status
        self._say(label,
                  C["green"] if result.verification_status == VERIFY_VERIFIED
                  else C["peach"])
        self.refresh()

    def _on_delete_backups(self) -> None:
        chosen = [c for var, c in self._backup_vars if var.get()]
        if not chosen:
            return
        listing = "\n".join(f"  • {os.path.basename(c.path)}" for c in chosen)
        if not messagebox.askyesno(
                "Delete duplicate backups?",
                f"Permanently delete {len(chosen)} file"
                f"{'' if len(chosen) == 1 else 's'}?\n\n{listing}\n\n"
                "Each is byte-for-byte identical to the live file it shadows, "
                "and will be re-checked immediately before deletion.",
                parent=self):
            return
        self._set_state(action_state=ACT_DELETING)
        self._say("Deleting…")
        self._ctrl.delete_backups_async(chosen, self._on_deleted)

    def _on_deleted(self, outcomes: list) -> None:
        self._set_state(action_state=ACT_NONE)
        ok = [o for o in outcomes if o.ok]
        bad = [o for o in outcomes if not o.ok]
        lines = [f"  ✓ {o.path}" for o in ok]
        lines += [f"  ✗ {o.path} — {o.reason}" for o in bad]
        messagebox.showinfo(
            "Deletion results",
            f"{len(ok)} deleted, {len(bad)} skipped.\n\n" + "\n".join(lines),
            parent=self)
        self._say(f"{len(ok)} deleted, {len(bad)} skipped",
                  C["green"] if not bad else C["peach"])
        self.refresh()

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _centre_on_parent(self, parent) -> None:
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except tk.TclError:                                 # pragma: no cover
            return
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
