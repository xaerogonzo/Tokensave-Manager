"""Pure quality-check runner functions — no Tk dependency.

These are the deterministic (non-LLM) check implementations shared between:
  * dialogs/checks_dialog.py  — the interactive Run Checks dialog (UI)
  * prepush_runner.py         — the headless pre-push git hook runner

Kept here so neither consumer has to import tkinter just to run a subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys


def run_syntax_check(project_path: str) -> tuple[bool, str]:
    """Run ``python -m compileall src/ -q``. Returns (passed, summary)."""
    src = os.path.join(project_path, "src")
    result = subprocess.run(
        [sys.executable, "-m", "compileall", src, "-q"],
        capture_output=True,
        text=True,
        cwd=project_path,
    )
    if result.returncode == 0:
        return True, "passed (0 errors)"
    errors = (result.stdout + result.stderr).strip()
    first_line = errors.splitlines()[0] if errors else "syntax error"
    return False, first_line[:200]


def run_pyflakes_check(project_path: str) -> tuple[bool, str]:
    """Run ``python -m pyflakes src/``. Returns (passed, summary)."""
    src = os.path.join(project_path, "src")
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", src],
        capture_output=True,
        text=True,
        cwd=project_path,
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
