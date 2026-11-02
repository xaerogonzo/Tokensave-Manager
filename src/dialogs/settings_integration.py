"""Settings -> AI: the integration preset picker and the integration check.

Controls, deliberately thin; every decision lives in `helpers/agent_presets`,
`helpers/agent_integration`, `helpers/cursor_hooks_repair` and
`helpers/memory_snapshot`, which have no Tk.

* **Preset.** Choosing one and pressing Apply shows exactly which settings it
  would change, and on confirmation sets them in the *form* - not in the config.
  The page goes dirty and nothing is written until Settings is saved, so a preset
  is as undoable as any other edit (close without saving). The label above is
  derived from the form each time it is shown and is never stored.
* **Repair Cursor hooks.** Shows the exact diff to `~/.cursor/hooks.json` and asks;
  on yes it backs the file up, quotes the broken tokensave commands, RUNS each one,
  and puts the file back if any fails. `tokensave install` writes them unquoted
  again on every upgrade, so this is a button and not a one-off.
* **Refresh snapshots.** Re-takes the Claude-memory snapshots that no longer match
  their source, after listing which. A project with memory and no snapshot is only
  offered, never taken silently.
* **Check.** Runs the free checks against the *saved* settings on a worker
  thread and prints one line per finding. It says so, because a preset applied
  in the form but not yet saved is not what it is checking.

The panel is handed what it needs (`read_raw`, `write_change`) rather than
discovering the section's variables, per `dialogs/settings_section.py`.
"""
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers import agent_integration as integ
from helpers import agent_presets as presets
from helpers import cursor_hooks as cursor_hooks
from helpers import cursor_hooks_repair as hooks_repair
from helpers import memory_snapshot as snapshots
from helpers.project_discovery import find_projects
from theme import UiPumpMixin

_GLYPH = {integ.PASS: "✓", integ.WARN: "⚠", integ.FAIL: "✗",
          integ.UNKNOWN: "?"}
_COLOUR = {integ.PASS: "green", integ.WARN: "peach", integ.FAIL: "red",
           integ.UNKNOWN: "overlay0"}


class IntegrationPanel(UiPumpMixin):
    """The panel mixes in the pump because the check shells out (CLI versions) and
    a worker never touches Tk: it posts, and the pump runs the result on the Tk thread."""

    def __init__(self, host, parent: tk.Frame, cfg, read_raw, write_change) -> None:
        self._host = host
        self._cfg = cfg
        self._read_raw = read_raw
        self._write_change = write_change
        self._running = False

        lf = tk.LabelFrame(parent, text="Claude and Cursor setup",
                           bg=C["base"], fg=C["subtext"],
                           font=("Segoe UI", 9, "bold"), relief=tk.GROOVE, bd=1)
        lf.pack(fill=tk.X, padx=20, pady=(0, 8))
        self._frame = lf

        self._current = tk.Label(lf, text="", bg=C["base"], fg=C["text"],
                                 font=("Segoe UI", 9), anchor=tk.W)
        self._current.pack(anchor=tk.W, padx=12, pady=(8, 2))

        row = tk.Frame(lf, bg=C["base"])
        row.pack(anchor=tk.W, padx=12, pady=(0, 2))
        self._choice = tk.StringVar(value=presets.PRESETS[0].label)
        self._combo = ttk.Combobox(row, textvariable=self._choice, width=22,
                                   state="readonly",
                                   values=[p.label for p in presets.PRESETS])
        self._combo.pack(side=tk.LEFT)
        ttk.Button(row, text="Apply preset…",
                   command=self._apply).pack(side=tk.LEFT, padx=(8, 0))
        self._check_btn = ttk.Button(row, text="Check Claude/Cursor setup",
                                     command=self._check)
        self._check_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._combo.bind("<<ComboboxSelected>>", lambda _e: self._describe())

        fixes = tk.Frame(lf, bg=C["base"])
        fixes.pack(anchor=tk.W, padx=12, pady=(0, 2))
        self._repair_btn = ttk.Button(fixes, text="Repair Cursor hooks…",
                                      command=self._repair_hooks)
        self._repair_btn.pack(side=tk.LEFT)
        self._snap_btn = ttk.Button(fixes, text="Refresh snapshots…",
                                    command=self._refresh_snapshots)
        self._snap_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._blurb = tk.Label(lf, text="", bg=C["base"], fg=C["overlay0"],
                               font=("Segoe UI", 8), justify=tk.LEFT, anchor=tk.W,
                               wraplength=560)
        self._blurb.pack(anchor=tk.W, padx=24, pady=(0, 4))
        self._out = tk.Text(lf, height=1, bg=C["base"], fg=C["text"],
                            font=("Consolas", 9), relief=tk.FLAT, wrap=tk.WORD,
                            state=tk.DISABLED, highlightthickness=0)
        for state, colour in _COLOUR.items():
            self._out.tag_configure(state, foreground=C[colour])
        self._out.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._describe()
        self.refresh()
        self._start_ui_pump()

    def _ui_host(self):
        """A panel is not a widget; the pump rides on its frame."""
        return self._frame

    # ── the label, derived each time ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-derive "what the form currently matches"."""
        found = presets.detect(self._read_raw())
        label = (presets.BY_ID[found].label if found in presets.BY_ID
                 else "Custom (your own combination)")
        self._current.config(text=f"Current settings: {label}")

    def _selected(self) -> "presets.Preset":
        for p in presets.PRESETS:
            if p.label == self._choice.get():
                return p
        return presets.PRESETS[0]

    def _describe(self) -> None:
        self._blurb.config(text=self._selected().description)

    # ── applying a preset into the form ───────────────────────────────────────────

    def _apply(self) -> None:
        preset = self._selected()
        changes = presets.plan(self._read_raw(), preset.id)
        if not changes:
            messagebox.showinfo("Apply preset",
                                f"Nothing to change: the settings already match "
                                f"\"{preset.label}\".", parent=self._host)
            return
        lines = "\n".join("  " + c.describe() for c in changes)
        if not messagebox.askyesno(
                "Apply preset",
                f"\"{preset.label}\" will change these settings in this form:\n\n"
                f"{lines}\n\nNothing is saved until you save Settings. "
                f"It does not touch any agent's own config files.",
                parent=self._host):
            return
        for change in changes:
            self._write_change(change)
        self.refresh()
        self._show([(integ.PASS, f"Applied \"{preset.label}\" in the form. "
                                 f"Save Settings to keep it.")])

    # ── the check, off the Tk thread ──────────────────────────────────────────────

    def _check(self) -> None:
        if self._running:
            return
        self._running = True
        self._check_btn.state(["disabled"])
        self._show([(integ.UNKNOWN, "Checking the saved settings…")])

        def work():
            try:
                checks = integ.run_checks(self._cfg)
                snap = integ.check_snapshots(self._survey())
                if snap is not None:
                    checks.append(snap)
            except Exception as exc:                              # noqa: BLE001
                checks = [integ.Check("internal", "integration check",
                                      integ.UNKNOWN, f"could not run: {exc}")]
            summary = integ.summarize(checks)
            self._post(self._done, checks, summary)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, checks: list, summary: str) -> None:
        self._running = False
        if not self._frame.winfo_exists():
            return
        self._check_btn.state(["!disabled"])
        rows = [(c.state, f"{c.label}: {c.detail}" + (f"  → {c.fix}" if c.fix else ""))
                for c in checks]
        rows.append((integ.PASS, f"({summary}; checks the SAVED settings)"))
        self._show(rows)

    def _show(self, rows: list) -> None:
        self._out.config(state=tk.NORMAL, height=max(1, min(14, len(rows) + 1)))
        self._out.delete("1.0", tk.END)
        for state, text in rows:
            self._out.insert(tk.END, f"{_GLYPH[state]} {text}\n", state)
        self._out.config(state=tk.DISABLED)

    # ── repairing the tokensave hooks Cursor cannot run ───────────────────────────

    def _repair_hooks(self) -> None:
        path = cursor_hooks.hooks_json_path()
        existing = cursor_hooks._read(path) if os.path.exists(path) else None
        plan = hooks_repair.plan_repair(existing)
        if not plan.ok:
            messagebox.showerror("Repair Cursor hooks", plan.error, parent=self._host)
            return
        if not plan.changed:
            messagebox.showinfo("Repair Cursor hooks",
                                "Nothing to repair: no tokensave hook there needs quoting.",
                                parent=self._host)
            return
        diff = cursor_hooks.unified_diff(existing, plan.new_text, "~/.cursor/hooks.json")
        if not messagebox.askyesno(
                "Repair Cursor hooks",
                f"{len(plan.fixes)} command(s) will be quoted so Windows does not cut "
                f"them at the first space. This is the change:\n\n{diff}\n"
                f"A timestamped backup is written first, each quoted command is then "
                f"run once, and the file is put back if any of them fails. Your other "
                f"hooks are not touched.\n\nContinue?", parent=self._host):
            return
        self._repair_btn.state(["disabled"])
        self._show([(integ.UNKNOWN, "Repairing and testing the hooks…")])

        def work():
            self._post(self._repaired, hooks_repair.repair())
        threading.Thread(target=work, daemon=True).start()

    def _repaired(self, result) -> None:
        if not self._frame.winfo_exists():
            return
        self._repair_btn.state(["!disabled"])
        rows = [(integ.PASS if code == 0 else integ.FAIL,
                 f"{event}: {'runs' if code == 0 else 'did not run'}"
                 + (f" ({line})" if line else ""))
                for event, code, line in result.tests]
        if result.ok:
            rows.insert(0, (integ.PASS, f"Repaired {len(result.fixes)} hook(s). "
                                        f"Backup: {result.backup}. Restart Cursor to pick them up."))
        else:
            rows.insert(0, (integ.FAIL, result.error or "the repair did not complete"))
        self._show(rows)

    # ── refreshing the Claude-memory snapshots ────────────────────────────────────

    def _survey(self) -> list:
        projects = [p["path"] for p in find_projects(self._cfg.search_roots)]
        return snapshots.survey(projects, git_exe=self._cfg.git_exe)

    def _refresh_snapshots(self) -> None:
        self._snap_btn.state(["disabled"])
        self._show([(integ.UNKNOWN, "Looking at which snapshots are out of date…")])

        def work():
            try:
                rows = self._survey()
            except Exception as exc:                              # noqa: BLE001
                self._post(self._surveyed, [], f"could not look: {exc}")
                return
            self._post(self._surveyed, rows, "")
        threading.Thread(target=work, daemon=True).start()

    def _surveyed(self, rows: list, error: str) -> None:
        if not self._frame.winfo_exists():
            return
        self._snap_btn.state(["!disabled"])
        if error:
            self._show([(integ.UNKNOWN, error)])
            return
        stale, missing = snapshots.stale(rows), snapshots.missing(rows)
        pick, why = (stale, "out of date") if stale else (missing, "have Claude memory but no snapshot yet")
        if not pick:
            current = sum(1 for r in rows if r.state == snapshots.STATE_CURRENT)
            self._show([(integ.PASS, f"All {current} snapshot(s) are current.")])
            return
        names = "\n".join("  " + r.name for r in pick)
        if not messagebox.askyesno(
                "Refresh snapshots",
                f"{len(pick)} project(s) {why}:\n\n{names}\n\nA snapshot is a copy of "
                f"Claude's memory INDEX in a git-ignored Cursor rule. Take it now?",
                parent=self._host):
            self._show([(integ.PASS, "Left as they were.")])
            return
        self._snap_btn.state(["disabled"])
        self._show([(integ.UNKNOWN, "Taking snapshots…")])

        def work():
            self._post(self._snapped, snapshots.refresh(pick, git_exe=self._cfg.git_exe))
        threading.Thread(target=work, daemon=True).start()

    def _snapped(self, results: list) -> None:
        if not self._frame.winfo_exists():
            return
        self._snap_btn.state(["!disabled"])
        self._show([(integ.PASS if r.ok else integ.FAIL,
                     f"{s.name}: " + (f"{r.entries} note(s)" if r.ok else r.error))
                    for s, r in results] or [(integ.PASS, "Nothing to do.")])
