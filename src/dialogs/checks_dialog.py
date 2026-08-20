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
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Callable

from constants import C
from theme import UiPumpMixin, _Tooltip, themed_checkbutton
from helpers.quality_checks import run_syntax_check, run_pyflakes_check
from helpers.prepush_hook import (
    is_pre_push_hook_installed,
    install_pre_push_hook,
    remove_pre_push_hook,
    prepush_runner_script_path,
)
from helpers.ci_workflow import generate_github_workflow

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
# Syntax and pyflakes checks live in helpers/quality_checks.py (no Tk dep)
# so the pre-push runner can import them without pulling in tkinter.

def _check_syntax(path: str) -> tuple[bool, str]:
    """Thin alias — delegates to the Tk-free helper."""
    return run_syntax_check(path)


def _check_pyflakes(path: str) -> tuple[bool, str]:
    """Thin alias — delegates to the Tk-free helper."""
    return run_pyflakes_check(path)


def _check_doctor(path: str, cfg: "ManagerConfig") -> tuple[bool, str]:
    """Call _audit_project_tree directly (pure function). Returns (passed, summary)."""
    try:
        from helpers.doctor_rules import _audit_project_tree
        raw = cfg.raw if isinstance(cfg.raw, dict) else {}
        skip_rel = set(raw.get("doctor_skip_paths") or [])
        # Per-directory cap tiers, e.g. looser thresholds for scripts/ so the
        # directory is audited rather than blanket-skipped.
        overrides = raw.get("doctor_path_overrides") or {}
        violations, _exempts, files_scanned = _audit_project_tree(
            path, skip_rel, overrides)
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

class ChecksDialog(UiPumpMixin, tk.Toplevel):
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
        # Start the worker -> UI channel before anything can post to it.
        self._start_ui_pump()
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
        self._hook_installed: bool = is_pre_push_hook_installed(path)

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
            cb = themed_checkbutton(
                chk_frame,
                text=label + suffix,
                variable=var,
                bg=C["base"],
                fg=C["text"],
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
            activebackground=C["lavender"],
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
                                bg=C["base"], fg=C["subtext"],
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

        # ── CI / Hook bar ─────────────────────────────────────────────────────
        sep2 = tk.Frame(self, height=1, bg=C["surface1"])
        sep2.pack(fill=tk.X, padx=12, pady=(4, 0))

        ci_frame = tk.Frame(self, bg=C["base"])
        ci_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        _btn_kw = dict(
            bg=C["surface0"],
            fg=C["text"],
            activebackground=C["surface1"],
            activeforeground=C["text"],
            relief=tk.FLAT,
            font=("Segoe UI", 9),
            padx=10,
            pady=3,
        )
        _gen_btn = tk.Button(
            ci_frame,
            text="📋 Generate GitHub Actions",
            command=self._on_generate_workflow,
            **_btn_kw,
        )
        _gen_btn.pack(side=tk.LEFT, padx=(0, 8))
        _Tooltip(_gen_btn,
                 "Writes .github/workflows/quality-checks.yml so the checks"
                 + "\n" +
                 "ticked above also run on GitHub for every push and PR."
                 + "\n\n" +
                 "Safe alongside an existing ci.yml — it is a separate file,"
                 + "\n" +
                 "and re-generating overwrites only its own."
                 + "\n\n" +
                 "The Doctor step it writes is ADVISORY: it reports findings"
                 + "\n" +
                 "to the job summary and never fails the build.")

        hook_label = (
            "Remove pre-push hook" if self._hook_installed
            else "🔗 Install pre-push hook"
        )
        self._hook_btn = tk.Button(
            ci_frame,
            text=hook_label,
            command=self._on_toggle_hook,
            **_btn_kw,
        )
        self._hook_btn.pack(side=tk.LEFT)
        _Tooltip(self._hook_btn,
                 "Installs .git/hooks/pre-push, which runs the deterministic"
                 + "\n" +
                 "checks (syntax + pyflakes) before every push."
                 + "\n\n" +
                 "Doctor and the AI review are NOT run by the hook — they are"
                 + "\n" +
                 "advisory, and a push should not be blocked on an opinion."
                 + "\n\n" +
                 "A blocked push can always be overridden with:"
                 + "\n" +
                 "    git push --no-verify")

        self._ci_status_lbl = tk.Label(
            ci_frame, text="", bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9),
        )
        self._ci_status_lbl.pack(side=tk.LEFT, padx=(10, 0))

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
                self._post(self._set_row, key, "pending", "running…")
            else:
                self._post(self._set_row, key, "skip", "skipped")

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
                    self._post(self._set_row, "claude", "skip", "skipped (large diff)")

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

        self._post(self._set_row, key, state, summary)
        self._post(self._maybe_reenable_run)

    def _maybe_reenable_run(self) -> None:
        """Re-enable the Run button once all pending rows have resolved."""
        for row in self._result_rows.values():
            if row["icon"].cget("text") == _ICONS["pending"]:
                return
        self._run_btn.configure(state=tk.NORMAL)

    def _on_generate_workflow(self) -> None:
        """Generate .github/workflows/quality-checks.yml from enabled checks."""
        ok, msg = generate_github_workflow(self._path, self._enabled)
        colour = C["green"] if ok else C["red"]
        prefix = "✓" if ok else "✗"
        self._ci_status_lbl.configure(text=f"{prefix} {msg}", fg=colour)

    def _on_toggle_hook(self) -> None:
        """Install or remove the pre-push git hook."""
        if self._hook_installed:
            ok, msg = remove_pre_push_hook(self._path)
            if ok:
                self._hook_installed = False
                self._hook_btn.configure(text="🔗 Install pre-push hook")
            colour = C["green"] if ok else C["red"]
            self._ci_status_lbl.configure(
                text=f"{'✓' if ok else '✗'} pre-push hook {msg}", fg=colour)
        else:
            ok, msg = install_pre_push_hook(
                self._path,
                sys.executable,
                prepush_runner_script_path(),
            )
            if ok:
                self._hook_installed = True
                self._hook_btn.configure(text="Remove pre-push hook")
            colour = C["green"] if ok else C["red"]
            self._ci_status_lbl.configure(
                text=f"{'✓' if ok else '✗'} pre-push hook {msg}", fg=colour)

    def _on_close(self) -> None:
        self._cancelled.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()
