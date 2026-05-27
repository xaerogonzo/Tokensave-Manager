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

    yaml = _build_yaml(checks_enabled)
    out_path = os.path.join(workflows_dir, "quality-checks.yml")
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(yaml)
    except OSError as e:
        return False, f"could not write {out_path}: {e}"

    rel = os.path.relpath(out_path, project_path).replace("\\", "/")
    return True, f"written: {rel}"


def _build_yaml(checks_enabled: dict[str, bool]) -> str:
    """Render the workflow YAML as a string."""
    want_syntax   = checks_enabled.get("syntax",   True)
    want_pyflakes = checks_enabled.get("pyflakes", True)

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
    )
