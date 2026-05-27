"""smoke_runner.py — Run the logic-layer smoke test suite from the manager UI.

Provides:
  run_smoke_tests(project_root)          → (passed, total, output_text)
  install_pre_commit_hook(repo_root)     → (ok, message)
  uninstall_pre_commit_hook(repo_root)   → (ok, message)
  is_hook_installed(repo_root)           → bool
"""
from __future__ import annotations

import os
import subprocess
import sys

# Marker embedded in every hook the manager writes so we can identify it.
_HOOK_MARKER = "# TokenSaveManager-smoke-hook"

# Content written to .git/hooks/pre-commit.
_HOOK_SCRIPT = """\
#!/bin/sh
{marker} — managed by Token Save Manager
# To disable: Settings → Behaviour → uncheck "Run smoke tests before commits"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
python tests/smoke_test.py
exit $?
""".format(marker=_HOOK_MARKER)

# --- subprocess creation flag (Windows: hide the console window) ----------
try:
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _CREATE_NO_WINDOW = 0


# ── Public helpers ─────────────────────────────────────────────────────────

def run_smoke_tests(project_root: str) -> tuple[int, int, str]:
    """Run ``tests/smoke_test.py`` in *project_root* and return
    ``(passed, total, combined_output)``.

    Returns ``(0, 0, err_text)`` when the test script can't be found or the
    Python interpreter fails to start.
    """
    test_script = os.path.join(project_root, "tests", "smoke_test.py")
    if not os.path.isfile(test_script):
        return 0, 0, f"smoke test file not found: {test_script}"

    try:
        proc = subprocess.run(
            [sys.executable, test_script],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=120,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 0, 0, "Smoke tests timed out after 120 s."
    except OSError as exc:
        return 0, 0, f"Failed to launch Python: {exc}"

    combined = (proc.stdout or "") + (proc.stderr or "")

    # Parse "N - PASS / FAIL" from the final summary line.
    passed = total = 0
    for line in combined.splitlines():
        stripped = line.strip()
        # Our runner prints e.g. "ALL PASSED: 123/123 passed"
        if "passed" in stripped and "/" in stripped:
            try:
                # Grab the "X/Y" fragment.
                frac = [tok for tok in stripped.split() if "/" in tok][0]
                p, t = frac.split("/")
                passed, total = int(p), int(t)
            except (ValueError, IndexError):
                pass

    return passed, total, combined


def is_hook_installed(repo_root: str) -> bool:
    """Return True iff the manager's pre-commit hook is installed."""
    hook_path = _hook_path(repo_root)
    if not os.path.isfile(hook_path):
        return False
    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            return _HOOK_MARKER in fh.read()
    except OSError:
        return False


def install_pre_commit_hook(repo_root: str) -> tuple[bool, str]:
    """Write (or overwrite) the manager's pre-commit hook.

    If a pre-existing hook WITHOUT the manager's marker is found, it is NOT
    overwritten — we return ``(False, reason)`` so the caller can warn the
    user that a custom hook already exists.
    """
    hook_path = _hook_path(repo_root)

    # Guard against overwriting a user's own hook.
    if os.path.isfile(hook_path):
        try:
            with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            if _HOOK_MARKER not in content:
                return (
                    False,
                    f"A pre-commit hook already exists at:\n  {hook_path}\n\n"
                    "It was NOT written by the manager — overwriting it could "
                    "break your workflow.\n\nDelete or rename it manually, then "
                    "re-enable the smoke-test hook here.",
                )
        except OSError as exc:
            return False, f"Could not read existing hook: {exc}"

    hooks_dir = os.path.dirname(hook_path)
    try:
        os.makedirs(hooks_dir, exist_ok=True)
        with open(hook_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_HOOK_SCRIPT)
        # Make the hook executable on POSIX.
        if os.name != "nt":
            os.chmod(hook_path, 0o755)
    except OSError as exc:
        return False, f"Could not write hook: {exc}"

    return True, f"Hook installed at:\n  {hook_path}"


def uninstall_pre_commit_hook(repo_root: str) -> tuple[bool, str]:
    """Remove the manager's pre-commit hook.

    Only removes the file if it bears the manager's marker — never deletes a
    user's custom hook.
    """
    hook_path = _hook_path(repo_root)
    if not os.path.isfile(hook_path):
        return True, "No hook file found — nothing to remove."

    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"Could not read hook file: {exc}"

    if _HOOK_MARKER not in content:
        return (
            False,
            "The pre-commit hook at:\n"
            f"  {hook_path}\n"
            "was NOT written by the manager.  It has NOT been removed.",
        )

    try:
        os.remove(hook_path)
    except OSError as exc:
        return False, f"Could not remove hook: {exc}"

    return True, "Hook removed."


# ── Internal ───────────────────────────────────────────────────────────────

def _hook_path(repo_root: str) -> str:
    return os.path.join(repo_root, ".git", "hooks", "pre-commit")
