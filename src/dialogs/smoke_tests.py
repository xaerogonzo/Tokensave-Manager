"""smoke_tests.py — Smoke-test runner dialog for Token Save Manager.

Opened from the Help tab's "🧪 Run Smoke Tests" button.  Runs
``tests/smoke_test.py`` in a background thread and streams the output
into a read-only text widget so the user can see pass/fail details
without leaving the manager.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from theme import C


class SmokeTestsDialog(tk.Toplevel):
    """Non-modal dialog that runs the logic-layer smoke-test suite.

    Layout (top → bottom):
      - Header label with instruction / status summary
      - Read-only scrolled text widget for subprocess output
      - "Run Tests" + "Close" sticky-bottom button bar

    The dialog stays open after the tests complete so the user can
    scroll through the output.  Clicking "Run Tests" again re-runs.
    """

    def __init__(self, parent: tk.Misc, cfg) -> None:
        super().__init__(parent)
        self.title("🧪  Smoke Tests")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.geometry("700x520")

        # Resolve project root from cfg if available; fall back to cwd.
        import os
        try:
            project_root = cfg.raw.get("projects", [{}])[0].get("path", "") or ""
        except Exception:
            project_root = ""
        if not project_root or not os.path.isdir(project_root):
            # Derive from this file's location: src/dialogs → ../../
            project_root = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
        self._project_root = project_root

        self._running = False
        self._build_ui()
        self._set_status("Click  Run Tests  to start.", C["subtext1"])

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Status bar (top)
        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(
            self, textvariable=self._status_var,
            font=("Segoe UI", 10, "bold"),
            bg=C["base"], fg=C["subtext1"],
            anchor=tk.W, padx=12, pady=6,
        )
        self._status_lbl.pack(fill=tk.X)

        # Sticky-bottom button bar (pack BEFORE the text area so it never
        # gets squeezed off the window when the dialog is resized small).
        btn_bar = tk.Frame(self, bg=C["base"])
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(6, 10))

        self._run_btn = ttk.Button(
            btn_bar, text="▶  Run Tests", command=self._on_run,
        )
        self._run_btn.pack(side=tk.LEFT)

        ttk.Button(
            btn_bar, text="Close", command=self.destroy,
        ).pack(side=tk.RIGHT)

        # Scrolled output area.
        txt_frame = tk.Frame(self, bg=C["base"])
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 0))

        vsb = ttk.Scrollbar(txt_frame, orient="vertical")
        self._txt = tk.Text(
            txt_frame,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            insertbackground=C["text"],
            relief=tk.FLAT, borderwidth=0,
            wrap=tk.NONE,
            yscrollcommand=vsb.set,
            state=tk.DISABLED,
        )
        hsb = ttk.Scrollbar(txt_frame, orient="horizontal", command=self._txt.xview)
        self._txt.configure(xscrollcommand=hsb.set)
        vsb.configure(command=self._txt.yview)

        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Colour tags for pass/fail lines.
        self._txt.tag_configure("pass", foreground=C["green"])
        self._txt.tag_configure("fail", foreground=C["red"])
        self._txt.tag_configure("summary_pass", foreground=C["green"],
                                font=("Consolas", 9, "bold"))
        self._txt.tag_configure("summary_fail", foreground=C["red"],
                                font=("Consolas", 9, "bold"))
        self._txt.tag_configure("dim", foreground=C["overlay0"])

    # ── Helpers ───────────────────────────────────────────────────────────

    def _set_status(self, text: str, colour: str) -> None:
        self._status_var.set(text)
        self._status_lbl.configure(fg=colour)

    def _append(self, text: str, tag: str = "") -> None:
        """Append text to the output widget (must be called from main thread)."""
        self._txt.configure(state=tk.NORMAL)
        if tag:
            self._txt.insert(tk.END, text, tag)
        else:
            self._txt.insert(tk.END, text)
        self._txt.see(tk.END)
        self._txt.configure(state=tk.DISABLED)

    def _clear(self) -> None:
        self._txt.configure(state=tk.NORMAL)
        self._txt.delete("1.0", tk.END)
        self._txt.configure(state=tk.DISABLED)

    # ── Actions ───────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        if self._running:
            return
        self._running = True
        self._run_btn.configure(state=tk.DISABLED)
        self._clear()
        self._set_status("Running…", C["peach"])
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        from helpers.smoke_runner import run_smoke_tests
        passed, total, output = run_smoke_tests(self._project_root)
        self.after(0, lambda: self._on_done(passed, total, output))

    def _on_done(self, passed: int, total: int, output: str) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        # Stream colourised output line by line.
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("ok ") or " ... ok" in stripped:
                self._append(line + "\n", "pass")
            elif ("FAIL" in stripped or "ERROR" in stripped
                    or stripped.startswith("FAILED")):
                self._append(line + "\n", "fail")
            elif stripped.startswith("...") or stripped == "":
                self._append(line + "\n", "dim")
            else:
                self._append(line + "\n")

        # Final summary.
        self._append("\n")
        if total == 0:
            # Could not parse — show raw output hint.
            self._set_status(
                "Could not parse test results — check output above.",
                C["peach"],
            )
            self._append(
                "Note: 'tests/smoke_test.py' may not exist for this "
                "project, or the script exited unexpectedly.\n",
                "dim",
            )
        elif passed == total:
            summary = f"ALL PASSED: {passed}/{total} passed"
            self._append(summary + "\n", "summary_pass")
            self._set_status(f"✓  {summary}", C["green"])
        else:
            failed = total - passed
            summary = f"FAILURES: {failed} failed, {passed}/{total} passed"
            self._append(summary + "\n", "summary_fail")
            self._set_status(f"✗  {summary}", C["red"])

        self._running = False
        self._run_btn.configure(state=tk.NORMAL)
