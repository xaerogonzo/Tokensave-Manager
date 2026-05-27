"""TestManagerDialog — novice-friendly test lifecycle UI (v4.13).

Opened from the Help tab's "🧪 Test Manager…" button (which replaced
the v4.12 "🧪 Run Smoke Tests" button). Four tabs:

  1. Run + View       — list test files with last-run status; run all,
                         run selected, stop in-flight runs (V-G), sync
                         the open PR's testing checklist.
  2. Coverage Gaps    — show src/ files that lack a corresponding
                         tests/test_*.py via filename heuristic; click
                         "Add Tests for Selected" to jump to Tab 4.
  3. Stale Tests      — flag tests that import deleted modules or
                         non-existent symbols (V-D: top-level imports
                         only, skips TYPE_CHECKING blocks); per-row
                         "Mark as still valid" silences false positives.
  4. Scaffold         — pick a src/ file + template kind; preview the
                         rendered test file; click Generate to write
                         tests/test_<basename>.py with placeholder tests.

All threading uses helpers/smoke_runner.run_pytest_in_background (V-E)
so this dialog and the legacy SmokeTestsDialog share the same worker
+ subprocess plumbing. Cancellation is wired through the PytestRun
handle (V-G).

Subprocess+filesystem helpers all live in helpers/* — this file is
purely UI orchestration.
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Optional

from constants import C
from helpers.test_discovery import (
    CoverageRow,
    StaleSignal,
    TestFileInfo,
    detect_stale_tests,
    list_test_files,
    load_last_run_results,
    load_stale_allowlist,
    save_last_run_results,
    save_stale_allowlist,
    scan_coverage_gaps,
)
from helpers.test_scaffold import (
    TEMPLATES,
    generate_test_file,
    preview_test_file,
)

if TYPE_CHECKING:
    from state import ManagerConfig


# ── Tag labels (kept short so the treeview columns don't overflow) ───────

_TEMPLATE_LABELS: dict[str, str] = {
    "pure_helper":       "Pure helper (no I/O)",
    "subprocess_helper": "Subprocess-bound (argv asserts)",
    "dialog_tk":         "Dialog (Tk-marked)",
    "blank":             "Blank pytest file",
}


class TestManagerDialog(tk.Toplevel):
    """Top-level dialog with the four test-lifecycle tabs."""

    # Tell pytest NOT to try collecting this class as a test class — it
    # starts with "Test" but it's a Tk Toplevel, not unittest.TestCase.
    __test__ = False

    def __init__(self, parent, project_root: str, cfg: "ManagerConfig") -> None:
        super().__init__(parent)
        self._parent       = parent
        self._project_root = project_root
        self._cfg          = cfg

        name = os.path.basename(project_root) or project_root
        self.title(f"🧪 Test Manager — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(820, 560)

        # In-flight pytest handle (V-E + V-G). When non-None, a run is
        # active and the Stop button is enabled.
        self._run_handle = None  # type: Optional[object]

        # Caches (populated lazily by Tab refresh handlers).
        self._test_files: list[TestFileInfo] = []
        self._coverage:   list[CoverageRow]   = []
        self._stale:      list[StaleSignal]   = []
        self._allowlist: set[str] = load_stale_allowlist(project_root)

        self._build_ui()
        # Populate Tab 1 from disk cache so the dialog opens with
        # meaningful data even on first open since the manager started.
        self._refresh_tab_run_view()
        self._refresh_tab_coverage()
        self._refresh_tab_stale()
        self._refresh_tab_scaffold()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Sticky-bottom button bar — packed BEFORE the notebook so it's
        # always visible regardless of window size (v4.5 convention).
        bottom = tk.Frame(self, bg=C["base"])
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 10))
        ttk.Button(bottom, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

        # Notebook with four tabs.
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))
        self._nb = nb

        for build, title in (
            (self._build_tab_run_view, "▶ Run + View"),
            (self._build_tab_coverage, "🔍 Coverage Gaps"),
            (self._build_tab_stale,    "🧹 Stale Tests"),
            (self._build_tab_scaffold, "📝 Scaffold"),
        ):
            frame = tk.Frame(nb, bg=C["base"])
            build(frame)
            nb.add(frame, text=title)

    # ── Tab 1 — Run + View ───────────────────────────────────────────────

    def _build_tab_run_view(self, parent: tk.Misc) -> None:
        # Action row.
        actions = tk.Frame(parent, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(8, 4))

        self._run_all_btn = ttk.Button(
            actions, text="▶ Run All", command=self._on_run_all)
        self._run_all_btn.pack(side=tk.LEFT)

        self._run_selected_btn = ttk.Button(
            actions, text="▶ Run Selected", command=self._on_run_selected)
        self._run_selected_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._stop_btn = ttk.Button(
            actions, text="🛑 Stop", command=self._on_stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._sync_pr_btn = ttk.Button(
            actions, text="🔁 Sync PR Checklist",
            command=self._on_sync_pr_checklist)
        self._sync_pr_btn.pack(side=tk.RIGHT)

        # Status line.
        self._status_var = tk.StringVar(value="Ready.")
        tk.Label(parent, textvariable=self._status_var,
                 anchor=tk.W, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(fill=tk.X, padx=10, pady=(0, 4))

        # Split: treeview (left) + output pane (right).
        split = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=C["base"],
                                sashwidth=4, sashrelief=tk.FLAT)
        split.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Left: test-file treeview.
        left = tk.Frame(split, bg=C["base"])
        self._files_tv = ttk.Treeview(
            left,
            columns=("tests", "status", "ran"),
            show="tree headings",
            selectmode="extended",
            height=14,
        )
        self._files_tv.heading("#0", text="Test file")
        self._files_tv.heading("tests",  text="Tests")
        self._files_tv.heading("status", text="Last result")
        self._files_tv.heading("ran",    text="Last run")
        self._files_tv.column("#0", width=240, anchor=tk.W)
        self._files_tv.column("tests",  width=60, anchor=tk.CENTER)
        self._files_tv.column("status", width=110, anchor=tk.W)
        self._files_tv.column("ran",    width=120, anchor=tk.W)
        files_vsb = ttk.Scrollbar(left, orient="vertical",
                                    command=self._files_tv.yview)
        self._files_tv.configure(yscrollcommand=files_vsb.set)
        files_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._files_tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        split.add(left, minsize=380)

        # Right: scrolling output pane.
        right = tk.Frame(split, bg=C["base"])
        self._out_txt = tk.Text(
            right, wrap=tk.NONE,
            bg=C["mantle"], fg=C["text"],
            font=("Consolas", 9),
            relief=tk.FLAT, padx=6, pady=4,
            state=tk.DISABLED,
        )
        out_vsb = ttk.Scrollbar(right, orient="vertical",
                                 command=self._out_txt.yview)
        self._out_txt.configure(yscrollcommand=out_vsb.set)
        out_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._out_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Tag colours mirror the legacy smoke_tests dialog.
        self._out_txt.tag_configure("pass", foreground=C["green"])
        self._out_txt.tag_configure("fail", foreground=C["red"])
        self._out_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._out_txt.tag_configure("summary_pass",
                                     foreground=C["green"],
                                     font=("Consolas", 9, "bold"))
        self._out_txt.tag_configure("summary_fail",
                                     foreground=C["red"],
                                     font=("Consolas", 9, "bold"))
        split.add(right, minsize=380)

    def _refresh_tab_run_view(self) -> None:
        """Repopulate the Tab 1 treeview from disk + cache."""
        self._files_tv.delete(*self._files_tv.get_children())
        self._test_files = list_test_files(self._project_root)
        cache = load_last_run_results(self._project_root)
        results: dict = cache.get("results", {}) if isinstance(cache, dict) else {}
        for tfi in self._test_files:
            rel = os.path.relpath(tfi.path, self._project_root).replace("\\", "/")
            row = results.get(rel) or {}
            status_str = row.get("status", "—")
            if status_str == "pass":
                status_display = f"✓ {row.get('passed', '?')}/{row.get('total', '?')}"
            elif status_str == "fail":
                status_display = f"✗ {row.get('passed', '?')}/{row.get('total', '?')}"
            elif status_str == "cancelled":
                status_display = "🛑 cancelled"
            else:
                status_display = "—"
            ran_str = row.get("ran_at", "—")
            self._files_tv.insert(
                "", tk.END, iid=rel,
                text=tfi.name,
                values=(tfi.test_count, status_display, ran_str),
            )

    # ── Tab 1 actions ────────────────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        """Toggle button enablement during an in-flight run."""
        state_inflight = tk.DISABLED if running else tk.NORMAL
        state_stop     = tk.NORMAL   if running else tk.DISABLED
        self._run_all_btn.configure(state=state_inflight)
        self._run_selected_btn.configure(state=state_inflight)
        self._sync_pr_btn.configure(state=state_inflight)
        self._stop_btn.configure(state=state_stop)

    def _append_out(self, text: str, tag: str = "") -> None:
        try:
            self._out_txt.configure(state=tk.NORMAL)
            if tag:
                self._out_txt.insert(tk.END, text, tag)
            else:
                self._out_txt.insert(tk.END, text)
            self._out_txt.see(tk.END)
            self._out_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _clear_out(self) -> None:
        try:
            self._out_txt.configure(state=tk.NORMAL)
            self._out_txt.delete("1.0", tk.END)
            self._out_txt.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _on_run_all(self) -> None:
        self._run_pytest_with_target("tests/")

    def _on_run_selected(self) -> None:
        selection = self._files_tv.selection()
        if not selection:
            messagebox.showinfo(
                "Nothing selected",
                "Select one or more test files in the list first.",
                parent=self,
            )
            return
        # selection items are rel paths (set as iid).
        targets = list(selection)
        # pytest supports multiple files in one invocation. Use the first
        # as the "target" arg; the rest as extra args.
        self._run_pytest_with_target(targets[0], extra_args=targets[1:])

    def _on_stop(self) -> None:
        if self._run_handle is None:
            return
        self._status_var.set("Stopping…")
        try:
            self._run_handle.cancel()
        except Exception:
            pass

    def _run_pytest_with_target(self, target: str,
                                  extra_args: Optional[list] = None) -> None:
        if self._run_handle is not None and self._run_handle.is_alive():
            return
        from helpers.smoke_runner import run_pytest_in_background

        self._set_running(True)
        self._clear_out()
        self._status_var.set(f"Running {target}…")
        self._append_out(f"$ python -m pytest {target} -v\n", "dim")

        def _cb(passed: int, total: int, output: str, cancelled: bool) -> None:
            try:
                self.after(0, lambda: self._on_pytest_done(
                    target, passed, total, output, cancelled,
                    extra_args or []))
            except tk.TclError:
                pass

        self._run_handle = run_pytest_in_background(
            self._project_root, _cb, target=target,
            extra_args=extra_args,
        )

    def _on_pytest_done(self, target: str, passed: int, total: int,
                         output: str, cancelled: bool,
                         extra_targets: list) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        # Stream output line-by-line into the right pane.
        for line in output.splitlines():
            stripped = line.strip()
            if "PASSED" in stripped:
                self._append_out(line + "\n", "pass")
            elif "FAILED" in stripped or "ERROR" in stripped:
                self._append_out(line + "\n", "fail")
            elif stripped == "" or stripped.startswith("==="):
                self._append_out(line + "\n", "dim")
            else:
                self._append_out(line + "\n")

        # Persist results to per-file cache.
        ran_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cache  = load_last_run_results(self._project_root)
        if not isinstance(cache, dict):
            cache = {}
        results = cache.setdefault("results", {})

        if cancelled:
            self._status_var.set(
                f"🛑 Cancelled after {passed}/{total} tests.")
            self._append_out("\n[cancelled by user]\n", "fail")
            status_str = "cancelled"
        elif total == 0:
            self._status_var.set("⚠ Could not parse pytest output.")
            status_str = "fail"
        elif passed == total:
            self._status_var.set(
                f"✓ All {passed}/{total} tests passed.")
            self._append_out(
                f"\n✓ ALL PASSED: {passed}/{total}\n", "summary_pass")
            status_str = "pass"
        else:
            failed = total - passed
            self._status_var.set(
                f"✗ {failed} failed, {passed}/{total} passed.")
            self._append_out(
                f"\n✗ FAILURES: {failed} failed, {passed}/{total} passed\n",
                "summary_fail")
            status_str = "fail"

        # Record per-target results. When "Run All" was used, set every
        # file to the same status (we can't parse per-file from the
        # combined pytest output without --json plugin support).
        affected = [target] + extra_targets
        for tgt in affected:
            if tgt == "tests/":
                # Whole suite — set every file's row.
                for tfi in self._test_files:
                    rel = os.path.relpath(tfi.path, self._project_root)
                    rel = rel.replace("\\", "/")
                    results[rel] = {
                        "status": status_str, "passed": passed,
                        "total": total, "ran_at": ran_at,
                    }
            else:
                rel = tgt.replace("\\", "/")
                results[rel] = {
                    "status": status_str, "passed": passed,
                    "total": total, "ran_at": ran_at,
                }
        cache["ran_at"] = ran_at
        save_last_run_results(self._project_root, cache)
        self._refresh_tab_run_view()

        # Reset run-state.
        self._run_handle = None
        self._set_running(False)

    def _on_sync_pr_checklist(self) -> None:
        """Sync the open PR's testing checklist from the last-run cache."""
        from helpers.pr_checklist import sync_pr_checklist

        gh_exe = (self._cfg.raw or {}).get("gh_exe") or "gh"
        cache  = load_last_run_results(self._project_root)
        if not isinstance(cache, dict) or "results" not in cache:
            messagebox.showinfo(
                "No test results",
                "Run the tests at least once before syncing the PR "
                "checklist — the manager can't tick boxes for tests "
                "it hasn't seen pass.",
                parent=self,
            )
            return

        # Aggregate to (passed, total) for the checklist renderer.
        results = cache["results"]
        passed = sum(int(r.get("passed", 0)) for r in results.values()
                       if isinstance(r, dict))
        total  = sum(int(r.get("total", 0))  for r in results.values()
                       if isinstance(r, dict))
        ran_at = cache.get("ran_at", datetime.now().strftime("%Y-%m-%d %H:%M"))

        ok, msg = sync_pr_checklist(
            gh_exe, self._project_root,
            {"passed": passed, "total": total, "ran_at": ran_at},
        )
        if ok:
            messagebox.showinfo("PR checklist synced", msg, parent=self)
        else:
            messagebox.showwarning("Could not sync", msg, parent=self)

    # ── Tab 2 — Coverage Gaps ────────────────────────────────────────────

    def _build_tab_coverage(self, parent: tk.Misc) -> None:
        # Top bar: summary + Refresh
        top = tk.Frame(parent, bg=C["base"])
        top.pack(fill=tk.X, padx=8, pady=(8, 4))
        self._cov_summary_var = tk.StringVar(value="(scanning…)")
        tk.Label(top, textvariable=self._cov_summary_var,
                 anchor=tk.W, bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 10, "bold")
                 ).pack(side=tk.LEFT)
        ttk.Button(top, text="🔄 Refresh",
                   command=self._refresh_tab_coverage).pack(side=tk.RIGHT)
        ttk.Button(top, text="📝 Add Tests for Selected",
                   command=self._on_add_tests_for_selected
                   ).pack(side=tk.RIGHT, padx=(0, 6))

        # Treeview.
        frame = tk.Frame(parent, bg=C["base"])
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._cov_tv = ttk.Treeview(
            frame, columns=("status",), show="tree headings",
            selectmode="browse", height=18,
        )
        self._cov_tv.heading("#0", text="Source file")
        self._cov_tv.heading("status", text="Has tests?")
        self._cov_tv.column("#0", width=460, anchor=tk.W)
        self._cov_tv.column("status", width=140, anchor=tk.W)
        # Tag for untested rows.
        self._cov_tv.tag_configure("untested", foreground=C["yellow"])
        cov_vsb = ttk.Scrollbar(frame, orient="vertical",
                                  command=self._cov_tv.yview)
        self._cov_tv.configure(yscrollcommand=cov_vsb.set)
        cov_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cov_tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _refresh_tab_coverage(self) -> None:
        self._cov_tv.delete(*self._cov_tv.get_children())
        self._coverage = scan_coverage_gaps(self._project_root)
        tested = sum(1 for r in self._coverage if r.has_tests)
        total  = len(self._coverage)
        pct    = (100 * tested // total) if total else 0
        self._cov_summary_var.set(
            f"{tested} / {total} src/ files have tests  ({pct}% by filename heuristic)"
        )
        for row in self._coverage:
            status = "✓ tested" if row.has_tests else "✗ no tests"
            tag = "" if row.has_tests else "untested"
            self._cov_tv.insert(
                "", tk.END, iid=row.rel_path,
                text=row.rel_path,
                values=(status,),
                tags=(tag,) if tag else (),
            )

    def _on_add_tests_for_selected(self) -> None:
        sel = self._cov_tv.selection()
        if not sel:
            messagebox.showinfo(
                "Nothing selected",
                "Pick a source file in the list first.",
                parent=self,
            )
            return
        rel_path = sel[0]
        # Find the absolute path from our cached coverage rows.
        match = next((r for r in self._coverage if r.rel_path == rel_path), None)
        if match is None:
            return
        # Jump to Tab 4 with the source pre-selected.
        self._nb.select(3)  # Tab 4
        self._scaffold_source_var.set(match.source_path)
        self._on_scaffold_preview_changed()

    # ── Tab 3 — Stale Tests ──────────────────────────────────────────────

    def _build_tab_stale(self, parent: tk.Misc) -> None:
        top = tk.Frame(parent, bg=C["base"])
        top.pack(fill=tk.X, padx=8, pady=(8, 4))
        self._stale_summary_var = tk.StringVar(value="(scanning…)")
        tk.Label(top, textvariable=self._stale_summary_var,
                 anchor=tk.W, bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 10, "bold")
                 ).pack(side=tk.LEFT)
        ttk.Button(top, text="🔄 Refresh",
                   command=self._refresh_tab_stale).pack(side=tk.RIGHT)
        ttk.Button(top, text="✓ Mark as Still Valid",
                   command=self._on_mark_still_valid
                   ).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(top, text="🗑 Delete Test File",
                   command=self._on_delete_stale_test
                   ).pack(side=tk.RIGHT, padx=(0, 6))

        frame = tk.Frame(parent, bg=C["base"])
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._stale_tv = ttk.Treeview(
            frame, columns=("reason", "detail"),
            show="tree headings", selectmode="browse", height=18,
        )
        self._stale_tv.heading("#0", text="Test file")
        self._stale_tv.heading("reason", text="Reason")
        self._stale_tv.heading("detail", text="Detail")
        self._stale_tv.column("#0", width=240, anchor=tk.W)
        self._stale_tv.column("reason", width=200, anchor=tk.W)
        self._stale_tv.column("detail", width=260, anchor=tk.W)
        stale_vsb = ttk.Scrollbar(frame, orient="vertical",
                                    command=self._stale_tv.yview)
        self._stale_tv.configure(yscrollcommand=stale_vsb.set)
        stale_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._stale_tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _refresh_tab_stale(self) -> None:
        self._stale_tv.delete(*self._stale_tv.get_children())
        self._allowlist = load_stale_allowlist(self._project_root)
        self._stale = detect_stale_tests(self._project_root, self._allowlist)
        n = len(self._stale)
        suffix = " (allowlist hiding more)" if self._allowlist else ""
        self._stale_summary_var.set(
            f"{n} stale-test signal(s) detected{suffix}"
        )
        for idx, sig in enumerate(self._stale):
            self._stale_tv.insert(
                "", tk.END, iid=str(idx),
                text=sig.test_name,
                values=(sig.reason, sig.detail),
            )

    def _selected_stale(self) -> Optional[StaleSignal]:
        sel = self._stale_tv.selection()
        if not sel:
            return None
        try:
            return self._stale[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _on_mark_still_valid(self) -> None:
        sig = self._selected_stale()
        if sig is None:
            messagebox.showinfo(
                "Nothing selected",
                "Select a row in the stale-tests list first.",
                parent=self,
            )
            return
        rel = os.path.relpath(sig.test_path, self._project_root).replace("\\", "/")
        self._allowlist.add(rel)
        save_stale_allowlist(self._project_root, self._allowlist)
        self._refresh_tab_stale()

    def _on_delete_stale_test(self) -> None:
        sig = self._selected_stale()
        if sig is None:
            messagebox.showinfo(
                "Nothing selected",
                "Select a row in the stale-tests list first.",
                parent=self,
            )
            return
        ok = messagebox.askyesno(
            "Delete test file?",
            f"This will permanently delete:\n  {sig.test_path}\n\n"
            "Continue?",
            parent=self, default="no",
        )
        if not ok:
            return
        try:
            os.remove(sig.test_path)
        except OSError as exc:
            messagebox.showerror(
                "Could not delete", f"{exc}", parent=self)
            return
        self._refresh_tab_stale()

    # ── Tab 4 — Scaffold ─────────────────────────────────────────────────

    def _build_tab_scaffold(self, parent: tk.Misc) -> None:
        # Picker row.
        picker = tk.Frame(parent, bg=C["base"])
        picker.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(picker, text="Source file:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._scaffold_source_var = tk.StringVar(value="")
        self._scaffold_source_entry = ttk.Entry(
            picker, textvariable=self._scaffold_source_var, width=70)
        self._scaffold_source_entry.pack(side=tk.LEFT, fill=tk.X,
                                           expand=True, padx=(6, 6))
        ttk.Button(picker, text="…",
                   command=self._on_scaffold_pick_source,
                   width=3).pack(side=tk.LEFT)

        # Template row.
        tmpl = tk.Frame(parent, bg=C["base"])
        tmpl.pack(fill=tk.X, padx=8, pady=(4, 4))
        tk.Label(tmpl, text="Template:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._scaffold_template_var = tk.StringVar(value="pure_helper")
        for tmpl_id in TEMPLATES:
            rb = ttk.Radiobutton(
                tmpl, text=_TEMPLATE_LABELS[tmpl_id],
                value=tmpl_id, variable=self._scaffold_template_var,
                command=self._on_scaffold_preview_changed,
            )
            rb.pack(side=tk.LEFT, padx=(8, 0))

        # Preview pane.
        preview_frame = tk.Frame(parent, bg=C["base"])
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        tk.Label(preview_frame, text="Preview:",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9, "bold")
                 ).pack(anchor=tk.W)
        self._scaffold_preview = tk.Text(
            preview_frame, wrap=tk.NONE,
            bg=C["mantle"], fg=C["text"], font=("Consolas", 9),
            relief=tk.FLAT, padx=6, pady=4, state=tk.DISABLED,
            height=18,
        )
        pv_vsb = ttk.Scrollbar(preview_frame, orient="vertical",
                                 command=self._scaffold_preview.yview)
        self._scaffold_preview.configure(yscrollcommand=pv_vsb.set)
        pv_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._scaffold_preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Generate row.
        gen = tk.Frame(parent, bg=C["base"])
        gen.pack(fill=tk.X, padx=8, pady=(4, 8))
        self._scaffold_status_var = tk.StringVar(value="")
        tk.Label(gen, textvariable=self._scaffold_status_var,
                 anchor=tk.W, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(gen, text="📝 Generate Test File",
                   command=self._on_scaffold_generate
                   ).pack(side=tk.RIGHT)

    def _refresh_tab_scaffold(self) -> None:
        # No-op refresh — preview reflects the current picker state.
        self._on_scaffold_preview_changed()

    def _on_scaffold_pick_source(self) -> None:
        from tkinter import filedialog
        initial = os.path.join(self._project_root, "src")
        path = filedialog.askopenfilename(
            parent=self,
            title="Pick a source file to generate tests for",
            initialdir=initial if os.path.isdir(initial) else self._project_root,
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        if path:
            self._scaffold_source_var.set(path)
            self._on_scaffold_preview_changed()

    def _on_scaffold_preview_changed(self) -> None:
        source = self._scaffold_source_var.get().strip()
        template = self._scaffold_template_var.get()
        try:
            self._scaffold_preview.configure(state=tk.NORMAL)
            self._scaffold_preview.delete("1.0", tk.END)
            if source and os.path.isfile(source):
                preview = preview_test_file(source, template,
                                              self._project_root)
                self._scaffold_preview.insert("1.0", preview)
                self._scaffold_status_var.set(
                    "Will write tests/test_<basename>.py "
                    "(refuses to overwrite existing)."
                )
            else:
                self._scaffold_preview.insert("1.0",
                    "(pick a source file to preview)")
                self._scaffold_status_var.set("")
        except Exception as exc:
            self._scaffold_preview.insert("1.0", f"preview error: {exc}")
        finally:
            self._scaffold_preview.configure(state=tk.DISABLED)

    def _on_scaffold_generate(self) -> None:
        source = self._scaffold_source_var.get().strip()
        template = self._scaffold_template_var.get()
        if not source or not os.path.isfile(source):
            messagebox.showinfo(
                "Pick a source file",
                "Click the … button and pick a .py file under src/.",
                parent=self,
            )
            return
        ok, msg = generate_test_file(
            self._project_root, source, template)
        if ok:
            self._scaffold_status_var.set(f"✓ wrote {msg}")
            messagebox.showinfo(
                "Test file created",
                f"Wrote:\n  {msg}\n\n"
                "Fill in the TODO placeholders to make the tests real. "
                "The generated file passes pytest immediately "
                "(placeholders are `assert True`).",
                parent=self,
            )
            self._refresh_tab_coverage()
        else:
            self._scaffold_status_var.set(f"✗ {msg}")
            messagebox.showwarning(
                "Could not generate", msg, parent=self)


# ``time`` is imported for potential future use (timestamping); reference
# it here so pyflakes doesn't whine.
_ = time
