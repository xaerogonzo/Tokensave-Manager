"""src/prepush_runner.py — git pre-push hook entry point.

Invoked by `.git/hooks/pre-push` (installed via the manager's Run Checks
dialog). Runs the enabled quality checks (syntax, pyflakes) and exits 1
to block the push on any failure, or 0 to allow it.

## Fail-open invariant (infrastructure errors only)

Runner infrastructure failures (missing config, import errors, unhandled
exception) exit 0 with a warning — never 1.  This mirrors the pre-commit
hook philosophy: don't punish the developer because the manager is sick.

The ONLY case we exit 1 is when a check actually runs and fails.

## Skipped checks (doctor + claude)

Doctor is skipped by default: it audits the entire project tree for
pre-existing style/complexity violations and will block pushes for issues
the current commit didn't introduce.  Run it manually from the Run Checks
dialog to track down violations at your own pace.

Claude is always skipped — too slow (60s timeout) and token-consuming for
automatic push gating. Run it manually from the Run Checks dialog.

## Usage

Invoked by the hook script as:
    python prepush_runner.py <project_path>

Project path is hardcoded at install time by prepush_hook._render_hook_script.
"""

from __future__ import annotations

import os
import sys


# ── sys.path bootstrap so helpers.* resolves ─────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))   # → <manager>/src
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


_DEFAULT_CHECKS: dict[str, bool] = {
    "syntax":   True,
    "pyflakes": True,
    "doctor":   False,  # skipped by default — audits pre-existing debt, not new breakage
    "claude":   False,  # never runs in pre-push; too slow + token cost
}


def _stderr(msg: str) -> None:
    print(f"[tokensave prepush] {msg}", file=sys.stderr)


def _load_config():
    """Return (cfg, error_string). error_string is '' on success."""
    try:
        from state import ManagerConfig
    except ImportError as e:
        return None, f"could not import ManagerConfig: {e}"
    try:
        cfg = ManagerConfig.load()
        return cfg, ""
    except Exception as e:
        return None, f"could not load manager config: {type(e).__name__}: {e}"


def _run_checks(project_path: str, checks_enabled: dict) -> list[tuple[str, bool, str]]:
    """Run each enabled check. Returns list of (name, passed, summary)."""
    from helpers.quality_checks import run_syntax_check, run_pyflakes_check

    results = []

    if checks_enabled.get("syntax"):
        try:
            passed, summary = run_syntax_check(project_path)
        except Exception as e:
            passed, summary = True, f"skipped (error: {e})"  # fail-open
        results.append(("syntax", passed, summary))

    if checks_enabled.get("pyflakes"):
        try:
            passed, summary = run_pyflakes_check(project_path)
        except Exception as e:
            passed, summary = True, f"skipped (error: {e})"  # fail-open
        results.append(("pyflakes", passed, summary))

    if checks_enabled.get("doctor"):
        results.append(_run_doctor(project_path))

    # claude: always skip in pre-push
    return results


def _doctor_overrides(project_path: str) -> dict:
    """Per-directory cap tiers from manager-config.json, or {}.

    Read straight from disk rather than through ManagerConfig: this runs
    inside a git hook, where importing the manager's config stack would drag
    in far more than a pre-push check should need. Any failure yields {},
    which is exactly the previous behaviour.
    """
    import json
    cfg_path = os.path.join(project_path, "manager-config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    overrides = raw.get("doctor_path_overrides") if isinstance(raw, dict) else None
    return overrides if isinstance(overrides, dict) else {}


def _run_doctor(project_path: str) -> tuple[str, bool, str]:
    """Run the doctor audit. Fail-open if doctor module is unavailable."""
    try:
        from helpers.doctor_rules import _audit_project_tree
    except ImportError as e:
        # doctor_rules is stdlib-only and Tk-free, so this should not
        # fail — but a hook must never block a push on its own import
        # error, so keep failing open.
        return "doctor", True, f"skipped (unavailable: {e})"
    try:
        # cfg-driven skip paths aren't critical here — use empty set if
        # cfg unavailable; the hook already loaded cfg for checks_enabled.
        violations, _exempts, files_scanned = _audit_project_tree(
            project_path, set(), _doctor_overrides(project_path))
        count = len(violations)
        if count == 0:
            return "doctor", True, f"passed — {files_scanned} files, 0 violations"
        first = violations[0][:150] if violations else ""
        suffix = f" (+{count - 1} more)" if count > 1 else ""
        return "doctor", False, f"{count} violation(s): {first}{suffix}"
    except Exception as e:
        return "doctor", True, f"skipped (error: {type(e).__name__}: {e})"


def main() -> int:
    if len(sys.argv) < 2:
        _stderr("no project path provided — skipping checks")
        return 0

    project_path = sys.argv[1]
    if not os.path.isdir(project_path):
        _stderr(f"project path not found: {project_path!r} — skipping checks")
        return 0

    cfg, err = _load_config()
    if err:
        _stderr(f"{err} — skipping checks")
        return 0

    raw = cfg.raw if isinstance(cfg.raw, dict) else {}
    checks_enabled: dict[str, bool] = {
        **_DEFAULT_CHECKS,
        **raw.get("checks_enabled", {}),
    }
    # doctor + claude are never run from the pre-push hook regardless of config
    checks_enabled["doctor"] = False
    checks_enabled["claude"] = False

    results = _run_checks(project_path, checks_enabled)

    failures = [(name, summary) for name, passed, summary in results if not passed]

    for name, passed, summary in results:
        status = "passed" if passed else "FAILED"
        _stderr(f"{name}: {status} — {summary}")

    if failures:
        _stderr(f"{len(failures)} check(s) failed — push blocked.")
        _stderr("Fix the issues above, or use  git push --no-verify  to skip.")

        # G-K: GUI git clients (GitHub Desktop, GitKraken, Sourcetree, the
        # VS Code git panel, etc.) swallow stderr — the user sees only a
        # generic "hook rejected push" message with no idea WHICH check
        # failed.  When stderr is NOT a TTY, also surface a tkinter
        # messagebox so GUI users at least see the failures summary.
        #
        # Wrapped in try/except Exception:pass so headless Linux/WSL/SSH
        # contexts where Tk init fails (no DISPLAY) still get a clean
        # exit 1 with the stderr output they already have.
        if not _stderr_is_tty():
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                _root = _tk.Tk()
                _root.withdraw()
                summary = "\n".join(
                    f"  • {name}: {msg}" for name, msg in failures
                )
                _mb.showerror(
                    "Push blocked by quality checks",
                    f"{len(failures)} check(s) failed:\n\n{summary}\n\n"
                    "Fix the issues, or from a terminal run:\n"
                    "    git push --no-verify"
                )
                _root.destroy()
            except Exception:
                # Fail-open on the GUI fallback itself — exit 1 still
                # happens, the user sees stderr (or the GUI client's
                # "hook rejected" message, however unhelpful).
                pass

        return 1

    _stderr("All checks passed.")
    return 0


def _stderr_is_tty() -> bool:
    """Best-effort isatty() probe.

    Thin alias over helpers.runner_io.stderr_is_tty, which the pre-commit
    hook shares. Kept as a module-level name because the existing tests
    monkeypatch it here.
    """
    from helpers.runner_io import stderr_is_tty
    return stderr_is_tty()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        _stderr(f"unhandled exception, allowing push: "
                f"{type(exc).__name__}: {exc}")
        sys.exit(0)
