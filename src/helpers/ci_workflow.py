"""GitHub Actions workflow generator for the manager's quality checks.

Generates `.github/workflows/quality-checks.yml` — a manager-curated
workflow that mirrors the checks available in the Run Checks dialog.

Design decisions:
  * Creates a SEPARATE file (`quality-checks.yml`) alongside any existing
    `ci.yml`. Coexistence is fine — running syntax/pyflakes twice is cheap.
  * Doctor step is NOT included: `doctor_ctrl.py` imports Tk at module level,
    which fails on ubuntu-latest without extra `apt-get` + display setup.
    Doctor is local-only (Run Checks dialog + pre-push hook on the dev machine).
  * Claude step is NOT included: requires interactive auth unavailable in CI.
  * Test job (v4.12 / G-I): a pytest job is auto-added IFF the target
    project has both a ``tests/`` directory and a ``requirements-dev.txt``.
    Downstream projects without those keep the lean syntax+pyflakes workflow.
  * Idempotent: re-generating overwrites the file safely.
"""

from __future__ import annotations

import os


def generate_github_workflow(
    project_path: str,
    checks_enabled: dict[str, bool],
) -> tuple[bool, str]:
    """Write ``.github/workflows/quality-checks.yml``.

    Returns (ok, message). ok=False if the directory cannot be created or
    the file cannot be written.

    Only enabled CI-compatible checks produce workflow steps. Doctor and
    Claude are silently omitted (CI-incompatible for different reasons).
    """
    workflows_dir = os.path.join(project_path, ".github", "workflows")
    try:
        os.makedirs(workflows_dir, exist_ok=True)
    except OSError as e:
        return False, f"could not create {workflows_dir}: {e}"

    yaml = _build_yaml(checks_enabled, project_path=project_path)
    out_path = os.path.join(workflows_dir, "quality-checks.yml")
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(yaml)
    except OSError as e:
        return False, f"could not write {out_path}: {e}"

    rel = os.path.relpath(out_path, project_path).replace("\\", "/")
    return True, f"written: {rel}"


def _build_yaml(checks_enabled: dict[str, bool],
                  project_path: str = "") -> str:
    """Render the workflow YAML as a string.

    When ``project_path`` points at a directory that has BOTH a
    ``tests/`` subdirectory AND a ``requirements-dev.txt`` (i.e. the
    pytest scaffold from v4.12), an extra ``test`` job is added that
    runs ``python -m pytest`` on PRs and pushes. Otherwise the workflow
    stays lean (just syntax+pyflakes) so downstream projects without a
    test suite don't get a perpetually-red CI badge.
    """
    want_syntax   = checks_enabled.get("syntax",   True)
    want_pyflakes = checks_enabled.get("pyflakes", True)

    has_tests = bool(
        project_path
        and os.path.isdir(os.path.join(project_path, "tests"))
        and os.path.isfile(os.path.join(project_path, "requirements-dev.txt"))
    )

    steps: list[str] = []

    steps.append(
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.11'"
    )

    if want_pyflakes:
        steps.append(
            "      - name: Install dependencies\n"
            "        run: pip install pyflakes --quiet"
        )

    if want_syntax:
        steps.append(
            "      - name: Syntax check\n"
            "        run: python -m compileall src/ -q"
        )

    if want_pyflakes:
        steps.append(
            "      - name: Pyflakes\n"
            "        run: python -m pyflakes src/"
        )

    steps_yaml = "\n".join(steps)

    test_job_yaml = ""
    if has_tests:
        test_job_yaml = (
            "\n"
            "  # Auto-added because the project has tests/ + requirements-dev.txt.\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.11'\n"
            "      - name: Install dev deps\n"
            "        run: pip install -r requirements-dev.txt\n"
            "      - name: Install Tk for headless dialog tests\n"
            "        run: sudo apt-get update && sudo apt-get install -y python3-tk xvfb\n"
            "      - name: Pure-logic tests\n"
            "        run: python -m pytest -m \"not tk\" -v\n"
            "      - name: Tk dialog tests (under xvfb)\n"
            "        run: xvfb-run -a python -m pytest -m tk -v\n"
        )

    return (
        "# Managed by TokenSave Manager — regenerate via Run Checks dialog.\n"
        "# Coexists with ci.yml (if present); duplicate steps are harmless.\n"
        "#\n"
        "# Not included here:\n"
        "#   Doctor audit — local-only (requires project-specific Python setup).\n"
        "#   Claude review — local-only (requires interactive auth).\n"
        "# Both are available from the Run Checks dialog and the pre-push hook.\n"
        "name: Quality Checks (manager)\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main, master]\n"
        "  push:\n"
        "    branches: [main, master]\n"
        "jobs:\n"
        "  quality:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"{steps_yaml}\n"
        f"{test_job_yaml}"
    )
