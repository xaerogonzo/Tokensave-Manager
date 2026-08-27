"""GitHub Actions workflow generator for the manager's quality checks.

Generates `.github/workflows/quality-checks.yml` — a manager-curated
workflow that mirrors the checks available in the Run Checks dialog.

Design decisions:
  * Creates a SEPARATE file (`quality-checks.yml`) alongside any existing
    `ci.yml`. Coexistence is fine — running syntax/pyflakes twice is cheap.
  * Doctor step IS included, but ADVISORY (continue-on-error). The rules moved
    to `helpers/doctor_rules.py`, which is stdlib-only, so they finally run on
    ubuntu-latest — the Tk import that used to block this is gone. It does not
    gate the build: the caps are an aspiration, and any project with an
    existing backlog would otherwise get a permanently red badge on day one.
    It reports into the job summary so the number is visible and can be driven
    down deliberately.
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

    Only enabled CI-compatible checks produce workflow steps. Doctor is
    included as an ADVISORY step (it no longer needs Tk); Claude is still
    omitted, since it needs interactive auth CI cannot provide.
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

    if checks_enabled.get("doctor", True):
        # Advisory, never a gate — see the module docstring. The
        # trailing "|| true" means even a crash in the audit cannot
        # block a build.
        steps.append(
            "      - name: Doctor audit (advisory)\n"
            "        continue-on-error: true\n"
            "        run: |\n"
            "          python - <<'EOF' >> \"$GITHUB_STEP_SUMMARY\" || true\n"
            "          import sys; sys.path.insert(0, 'src')\n"
            "          from helpers.doctor_rules import _audit_project_tree\n"
            "          v, _ex, n = _audit_project_tree('.', set())\n"
            "          print(f'### Doctor audit: {len(v)} violation(s) across {n} files')\n"
            "          for line in v[:25]:\n"
            "              print('- ' + str(line).strip())\n"
            "          if len(v) > 25:\n"
            "              print(f'- ...and {len(v) - 25} more')\n"
            "          EOF"
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
        "# Doctor audit runs below as an advisory step; it reports into the\n"
        "# job summary and never gates the build.\n"
        "#\n"
        "# Not included here:\n"
        "#   Claude review — local-only (requires interactive auth).\n"
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
