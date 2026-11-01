"""Settings -> AI: the integration preset picker and the integration check.

Two controls, deliberately thin; every decision lives in `helpers/agent_presets`
and `helpers/agent_integration`, which have no Tk.

* **Preset.** Choosing one and pressing Apply shows exactly which settings it
  would change, and on confirmation sets them in the *form* - not in the config.
  The page goes dirty and nothing is written until Settings is saved, so a preset
  is as undoable as any other edit (close without saving). The label above is
  derived from the form each time it is shown and is never stored.
* **Check.** Runs the free checks against the *saved* settings on a worker
  thread and prints one line per finding. It says so, because a preset applied
  in the form but not yet saved is not what it is checking.

The panel is handed what it needs (`read_raw`, `write_change`) rather than
discovering the section's variables, per `dialogs/settings_section.py`.
"""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constants import C
from helpers import agent_integration as integ
from helpers import agent_presets as presets
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
