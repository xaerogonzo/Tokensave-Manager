"""ChecksDialog — run pre-merge quality checks with live per-check results.

Four checks (toggleable, persisted to cfg.raw["checks_enabled"]):
  syntax   — python -m compileall src/ -q
  pyflakes — python -m pyflakes src/
  doctor   — _audit_project_tree (pure function, called directly)
  claude   — git diff <base>...HEAD reviewed by Claude CLI (opt-in, uses tokens)

All enabled checks run concurrently via ThreadPoolExecutor so the fast
deterministic checks don't queue behind the potentially slow Claude review.
Tkinter messagebox (for the large-diff warning) runs on the main thread
before the executor starts, as required by Tkinter's single-thread rule.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

from constants import C

if TYPE_CHECKING:
    from state import ManagerConfig

_DEFAULT_CHECKS: dict[str, bool] = {
    "syntax":   True,
    "pyflakes": True,
    "doctor":   True,
    "claude":   False,
}

_ICONS = {
    "pending":  "⏳",
    "pass":     "✓",
    "fail":     "✗",
    "skip":     "—",
}

_LABELS = {
    "syntax":   "Python syntax",
    "pyflakes": "pyflakes",
    "doctor":   "Doctor audit",
    "claude":   "Claude Code review",
}


# ── Check implementations ────────────────────────────────────────────────────

def _check_syntax(path: str) -> tuple[bool, str]:
    """Run python -m compileall src/ -q. Returns (passed, summary)."""
    src = os.path.join(path, "src")
    result = subprocess.run(
        [sys.executable, "-m", "compileall", src, "-q"],
        capture_output=True,
        text=True,
        cwd=path,
    )
    if result.returncode == 0:
        return True, "passed (0 errors)"
    errors = (result.stdout + result.stderr).strip()
    first_line = errors.splitlines()[0] if errors else "syntax error"
    return False, first_line[:200]


def _check_pyflakes(path: str) -> tuple[bool, str]:
    """Run python -m pyflakes src/. Returns (passed, summary)."""
    src = os.path.join(path, "src")
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", src],
        capture_output=True,
        text=True,
        cwd=path,
    )
    if result.returncode == 0:
        return True, "passed (0 warnings)"
    output = (result.stdout + result.stderr).strip()
    lines = [l for l in output.splitlines() if l.strip()]
    count = len(lines)
    summary = lines[0][:200] if lines else "warnings found"
    if count > 1:
        summary += f" (+{count - 1} more)"
    return False, summary


def _check_doctor(path: str, cfg: "ManagerConfig") -> tuple[bool, str]:
    """Call _audit_project_tree directly (pure function). Returns (passed, summary)."""
    try:
        from controllers.doctor_ctrl import _audit_project_tree
        raw = cfg.raw if isinstance(cfg.raw, dict) else {}
        skip_rel = set(raw.get("doctor_skip_paths") or [])
        violations, _exempts, files_scanned = _audit_project_tree(path, skip_rel)
        count = len(violations)
        if count == 0:
            return True, f"passed — {files_scanned} files scanned, 0 violations"
        first = violations[0][:150] if violations else ""
        suffix = f" (+{count - 1} more)" if count > 1 else ""
        return False, f"{count} violation(s): {first}{suffix}"
    except Exception as e:
        return False, f"error: {e}"


def _get_pr_diff(path: str, base: str, git_exe: str) -> str:
    """Return the PR-scope diff (triple-dot merge base)."""
    bare_base = base.split("/")[-1]
    result = subprocess.run(
        [git_exe or "git", "diff", f"{bare_base}...HEAD"],
        capture_output=True,
        text=True,
        cwd=path,
    )
    return result.stdout


def _check_claude_review(diff: str, cfg: "ManagerConfig", cancelled: threading.Event) -> tuple[bool, str]:
    """Send diff to Claude CLI for review. Returns (passed, summary)."""
    if cancelled.is_set():
        return True, "cancelled"
    try:
        from helpers.claude_cli import call_claude_cli_print
        prompt = (
            "You are a code reviewer. Review the following git diff for the PR. "
            "Flag bugs, regressions, security issues, and significant style violations. "
            "Be concise. Summarise findings in 3–5 bullet points.\n\n"
            f"```diff\n{diff[:30_000]}\n```"
        )
        model = (cfg.raw or {}).get("claude_cli_model") or "claude-haiku-4-5-20251001"
        output = call_claude_cli_print(
            claude_exe=cfg.claude_cli_exe or "claude",
            prompt=prompt,
            model=model,
            timeout=60,
        )
        if not output or not output.strip():
            return True, "no issues flagged"
        lines = [l for l in output.strip().splitlines() if l.strip()]
        preview = "\n".join(lines[:3])
        if len(lines) > 3:
            preview += f"\n… ({len(lines) - 3} more lines)"
        return True, preview
    except Exception as e:
        return False, f"error: {e}"


# ── Dialog ───────────────────────────────────────────────────────────────────

class ChecksDialog(tk.Toplevel):
    """Modal pre-merge checks panel. Construct on the Tk main thread."""

    def __init__(
        self,
        parent: tk.Misc,
        path: str,
        cfg: "ManagerConfig",
        base: str,
        on_log: Callable[[str, str], None],
    ) -> None:
        super().__init__(parent)
        self.title("Run checks")
        self.resizable(True, True)
        self.minsize(540, 300)
        self.configure(bg=C["base"])
        self.grab_set()

        self._path   = path
        self._cfg    = cfg
        self._base   = base
        self._on_log = on_log
        self._cancelled = threading.Event()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

        raw = cfg.raw if isinstance(cfg.raw, dict) else {}
        self._enabled: dict[str, bool] = {
            **_DEFAULT_CHECKS,
            **raw.get("checks_enabled", {}),
        }

        self._vars: dict[str, tk.BooleanVar] = {}
        self._result_rows: dict[str, dict] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = dict(padx=12, pady=6)

        # ── Checkboxes ────────────────────────────────────────────────────────
        chk_frame = tk.Frame(self, bg=C["base"])
        chk_frame.pack(fill=tk.X, **pad)

        for key, label in _LABELS.items():
            var = tk.BooleanVar(value=self._enabled[key])
            self._vars[key] = var
            suffix = "  (uses API tokens)" if key == "claude" else ""
            cb = tk.Checkbutton(
                chk_frame,
                text=label + suffix,
                variable=var,
                bg=C["base"],
                fg=C["text"],
                selectcolor=C["surface0"],
                activebackground=C["base"],
                activeforeground=C["text"],
                font=("Segoe UI", 10),
                command=lambda k=key, v=var: self._on_toggle(k, v),
            )
            cb.pack(anchor=tk.W)

        # ── Run button ────────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=12, pady=(0, 6))
        self._run_btn = tk.Button(
            btn_frame,
            text="Run checks",
            command=self._on_run,
            bg=C["blue"],
            fg=C["base"],
            activebackground=C["sapphire"],
            activeforeground=C["base"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=12,
            pady=4,
        )
        self._run_btn.pack(side=tk.LEFT)

        # ── Separator ─────────────────────────────────────────────────────────
        sep = tk.Frame(self, height=1, bg=C["surface1"])
        sep.pack(fill=tk.X, padx=12, pady=4)

        # ── Result rows ───────────────────────────────────────────────────────
        results_frame = tk.Frame(self, bg=C["base"])
        results_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        for key, label in _LABELS.items():
            row_frame = tk.Frame(results_frame, bg=C["base"])
            row_frame.pack(fill=tk.X, pady=2)

            icon_lbl = tk.Label(row_frame, text="—", width=2, bg=C["base"],
                                fg=C["overlay0"], font=("Segoe UI", 11))
            icon_lbl.pack(side=tk.LEFT)

            name_lbl = tk.Label(row_frame, text=label, width=18, anchor=tk.W,
                                bg=C["base"], fg=C["subtext1"],
                                font=("Segoe UI", 10, "bold"))
            name_lbl.pack(side=tk.LEFT)

            summary_lbl = tk.Label(row_frame, text="—", anchor=tk.W,
                                   bg=C["base"], fg=C["overlay0"],
                                   font=("Segoe UI", 10),
                                   wraplength=340, justify=tk.LEFT)
            summary_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            self._result_rows[key] = {
                "icon": icon_lbl,
                "summary": summary_lbl,
            }

    def _on_toggle(self, key: str, var: tk.BooleanVar) -> None:
        self._enabled[key] = var.get()
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        raw["checks_enabled"] = {**_DEFAULT_CHECKS, **self._enabled}
        self._cfg.raw = raw
        try:
            self._cfg.save()
        except Exception:
            pass

    def _set_row(self, key: str, state: str, summary: str = "") -> None:
        """Update a result row. Safe to call from any thread via after(0, ...)."""
        row = self._result_rows[key]
        icon = _ICONS.get(state, "—")
        colour_map = {
            "pass":    C["green"],
            "fail":    C["red"],
            "pending": C["yellow"],
            "skip":    C["overlay0"],
        }
        colour = colour_map.get(state, C["overlay0"])
        row["icon"].configure(text=icon, fg=colour)
        row["summary"].configure(text=summary or "", fg=colour)

    def _on_run(self) -> None:
        """Main-thread entry point for running all enabled checks."""
        self._run_btn.configure(state=tk.DISABLED)
        self._cancelled.clear()

        enabled = {k: v for k, v in self._enabled.items() if v}

        # Reset all rows
        for key in _LABELS:
            if enabled.get(key):
                self.after(0, self._set_row, key, "pending", "running…")
            else:
                self.after(0, self._set_row, key, "skip", "skipped")

        # ── Large-diff check for Claude review (main thread only) ─────────────
        diff: str | None = None
        if enabled.get("claude"):
            git_exe = (self._cfg.raw or {}).get("git_exe") or "git"
            diff = _get_pr_diff(self._path, self._base, git_exe)
            if len(diff) > 10_000:
                ok = messagebox.askyesno(
                    "Large diff",
                    f"The diff is ~{len(diff) // 1000}k chars — this will use API tokens.\n"
                    "Continue with Claude review?",
                    parent=self,
                )
                if not ok:
                    enabled.pop("claude")
                    self.after(0, self._set_row, "claude", "skip", "skipped (large diff)")

        # ── Launch executor ───────────────────────────────────────────────────
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="checks"
        )

        futures: dict[str, concurrent.futures.Future] = {}

        if enabled.get("syntax"):
            futures["syntax"] = self._executor.submit(_check_syntax, self._path)
        if enabled.get("pyflakes"):
            futures["pyflakes"] = self._executor.submit(_check_pyflakes, self._path)
        if enabled.get("doctor"):
            futures["doctor"] = self._executor.submit(
                _check_doctor, self._path, self._cfg
            )
        if enabled.get("claude") and diff is not None:
            futures["claude"] = self._executor.submit(
                _check_claude_review, diff, self._cfg, self._cancelled
            )

        self._executor.shutdown(wait=False)

        # Attach done-callbacks for each future
        for key, future in futures.items():
            future.add_done_callback(
                lambda f, k=key: self._on_check_done(k, f)
            )

    def _on_check_done(
        self, key: str, future: concurrent.futures.Future
    ) -> None:
        """Called by the future's done-callback (may be on any thread)."""
        try:
            passed, summary = future.result()
            state = "pass" if passed else "fail"
        except concurrent.futures.CancelledError:
            state, summary = "skip", "cancelled"
        except Exception as e:
            state, summary = "fail", f"error: {e}"

        self.after(0, self._set_row, key, state, summary)
        self.after(0, self._maybe_reenable_run)

    def _maybe_reenable_run(self) -> None:
        """Re-enable the Run button once all pending rows have resolved."""
        for row in self._result_rows.values():
            if row["icon"].cget("text") == _ICONS["pending"]:
                return
        self._run_btn.configure(state=tk.NORMAL)

    def _on_close(self) -> None:
        self._cancelled.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()
