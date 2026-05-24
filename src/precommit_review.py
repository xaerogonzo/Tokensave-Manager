"""src/precommit_review.py — git pre-commit hook entry point.

Invoked by `.git/hooks/pre-commit` (installed by the manager via the
right-click "Install Pre-commit AI Review hook" action). Reads
`git diff --cached`, calls the configured AI backend, parses severity
findings, prints them to stderr, and exits 0 (warn-only) or 1 (block)
based on `precommit_severity_threshold`.

This script is NOT a module — it's a top-level entry point. The hook
shell script (rendered by `helpers/precommit_hook._render_hook_script`)
invokes it via the user's `python_exe` with `cwd` set to the project
root. Imports from `helpers.*` work because we bootstrap sys.path to
the manager's src/ directory before any import.

## Fail-open invariant

Every error path here exits 0 with a stderr notice — never 1. The hook
MUST NEVER block a commit because the AI is sick:

  * No staged diff   → exit 0 (nothing to review)
  * `git diff` fails → exit 0 (don't punish the user for git oddities)
  * Config missing   → exit 0 (manager probably uninstalled)
  * Backend fails    → exit 0 (network down, model gone, etc.)
  * Unhandled excn   → exit 0 (master try/except catches everything)

The ONLY case we exit 1 is when the review completed cleanly AND the
parsed severity crosses the user's configured threshold. Users can
override even THAT with `git commit --no-verify` (printed in the
block message).
"""

from __future__ import annotations

import os
import subprocess
import sys


# ── Recursion guard (must run BEFORE any other work) ─────────────────────

if os.environ.get("TOKENSAVE_PRECOMMIT_RUNNING") == "1":
    # Already inside a hook invocation — bail to prevent infinite recursion
    # in the (currently theoretical) case where the reviewer itself triggers
    # a git commit. Cheap insurance.
    sys.exit(0)
os.environ["TOKENSAVE_PRECOMMIT_RUNNING"] = "1"


# ── sys.path bootstrap so `helpers.*` resolves ───────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))   # → <manager>/src
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _stderr(msg: str) -> None:
    """Tagged stderr output so users can grep for our lines."""
    print(f"[tokensave precommit] {msg}", file=sys.stderr)


def _read_staged_diff() -> "str | None":
    """Return the staged diff text, or None on any failure (fail-open)."""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _stderr(f"could not read `git diff --cached`: {e} — skipping review")
        return None
    if r.returncode != 0:
        _stderr(f"git diff --cached rc={r.returncode}: "
                f"{(r.stderr or '').strip()[:200]} — skipping review")
        return None
    return r.stdout or ""


def _load_and_review(diff: str):
    """Bring in heavy deps, load cfg, run the review.

    Returns (cfg, ok, findings_text, severity) on success, or None on
    any failure (after surfacing the reason via _stderr). Collapses the
    three near-identical try/except blocks (import / load / review) so
    main() stays a flat orchestrator.
    """
    try:
        from state import ManagerConfig
        from helpers.precommit_hook import run_review
    except ImportError as e:
        _stderr(f"could not import manager modules: {e} — skipping review")
        return None
    try:
        cfg = ManagerConfig.load()
    except Exception as e:
        _stderr(f"could not load manager config: {type(e).__name__}: {e} "
                "— skipping review")
        return None
    try:
        ok, findings_text, severity = run_review(cfg, diff)
    except Exception as e:
        _stderr(f"review crashed: {type(e).__name__}: {e} — skipping")
        return None
    return cfg, ok, findings_text, severity


def _print_findings(findings_text: str) -> None:
    """Print review output to stderr with our tag prefix on every line.

    Always-fires path: warn-only mode still wants the user to see what
    the AI found, even when we won't block.
    """
    if not findings_text:
        return
    _stderr("AI review findings:")
    for line in findings_text.splitlines():
        print(f"  {line}", file=sys.stderr)


def main() -> int:
    diff = _read_staged_diff()
    if diff is None or not diff.strip():
        return 0   # nothing to review, or git refused — fail-open

    loaded = _load_and_review(diff)
    if loaded is None:
        return 0
    cfg, ok, findings_text, severity = loaded

    if not ok:
        if findings_text:
            _stderr(findings_text)
        return 0

    _print_findings(findings_text)

    from helpers.precommit_hook import severity_blocks_commit
    threshold = (cfg.raw.get("precommit_severity_threshold") or "none").lower()
    if severity_blocks_commit(severity, threshold):
        h = severity.get("high", 0)
        m = severity.get("medium", 0)
        _stderr(f"BLOCKED — {h} high / {m} medium finding(s) "
                f"(threshold: {threshold}).")
        _stderr("Override with:  git commit --no-verify")
        return 1
    return 0


if __name__ == "__main__":
    # Master try/except — if ANYTHING slips through, fail-open. The hook
    # blocking a commit because of a python exception in our own code would
    # be the worst outcome.
    try:
        sys.exit(main())
    except Exception as exc:
        _stderr(f"unhandled exception, skipping review: "
                f"{type(exc).__name__}: {exc}")
        sys.exit(0)
