"""tests/test_ci_workflow.py — generate_github_workflow YAML correctness.

Tests the manager's GitHub Actions workflow generator against the
expected file shape. Assertions are regex-on-YAML-string rather than
`PyYAML.load(...)` to avoid adding PyYAML as a test dependency — the
generator's output is small and predictable.
"""
from __future__ import annotations

import os
import pathlib
import re

from helpers.ci_workflow import generate_github_workflow


# ── Fixtures ──────────────────────────────────────────────────────────────

def _generate(tmp_path, checks_enabled):
    """Call generate_github_workflow + return (ok, message, content)."""
    ok, msg = generate_github_workflow(str(tmp_path), checks_enabled)
    out_path = tmp_path / ".github" / "workflows" / "quality-checks.yml"
    content = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    return ok, msg, content


# ── Output-path tests ─────────────────────────────────────────────────────

def test_creates_workflows_directory(tmp_path):
    """The .github/workflows/ directory should be created if absent."""
    assert not (tmp_path / ".github" / "workflows").exists()
    ok, _msg = generate_github_workflow(str(tmp_path), {"syntax": True})
    assert ok
    assert (tmp_path / ".github" / "workflows").is_dir()


def test_writes_quality_checks_yml(tmp_path):
    """The output filename must be exactly ``quality-checks.yml``.

    The manager generates a SEPARATE file from any existing ``ci.yml``
    — coexistence is intentional. If this filename ever changes, callers
    in the Run Checks dialog and downstream docs all break.
    """
    ok, msg, _content = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    assert ok
    assert "quality-checks.yml" in msg
    assert (tmp_path / ".github" / "workflows" / "quality-checks.yml").is_file()


def test_does_not_clobber_existing_ci_yml(tmp_path):
    """Generator must NOT touch ``ci.yml`` — both files should coexist."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    ci_yml = workflows / "ci.yml"
    ci_yml.write_text("name: Existing CI\n", encoding="utf-8")
    ci_mtime_before = ci_yml.stat().st_mtime_ns

    ok, _msg, _content = _generate(tmp_path, {"syntax": True})
    assert ok
    assert ci_yml.read_text(encoding="utf-8") == "name: Existing CI\n"
    assert ci_yml.stat().st_mtime_ns == ci_mtime_before


# ── Content shape tests ───────────────────────────────────────────────────

def test_includes_manager_managed_comment(tmp_path):
    """Generated YAML must start with the manager-managed header comment.

    Users seeing the file should immediately know it's auto-generated
    rather than try to hand-edit it.
    """
    _ok, _msg, content = _generate(tmp_path, {"syntax": True})
    assert content.startswith("# Managed by TokenSave Manager")


def test_workflow_name(tmp_path):
    """Workflow name must be the canonical 'Quality Checks (manager)'."""
    _ok, _msg, content = _generate(tmp_path, {"syntax": True})
    assert re.search(r"^name: Quality Checks \(manager\)$", content, re.M)


def test_triggers_on_pr_and_push_to_main_master(tmp_path):
    """Workflow triggers on PR/push to main/master only."""
    _ok, _msg, content = _generate(tmp_path, {"syntax": True})
    assert "pull_request:" in content
    assert "push:" in content
    assert re.search(r"branches:\s*\[main,\s*master\]", content)


def test_uses_setup_python_and_checkout(tmp_path):
    """Workflow uses pinned actions/checkout@v5 + actions/setup-python@v6."""
    _ok, _msg, content = _generate(tmp_path, {"syntax": True})
    assert "actions/checkout@v5" in content
    assert "actions/setup-python@v6" in content
    assert "python-version: '3.11'" in content


def test_generated_pins_match_the_ones_this_repo_runs_itself(tmp_path):
    """The Manager may not write other people a workflow older than its own.

    This drifted once and the drift was invisible: the repo moved to
    `checkout@v5` + `setup-python@v6`, the generator stayed on `@v4` + `@v5`,
    and nothing connected the two — so the Manager kept writing the exact Node
    20 deprecation this repo had just finished removing, into other people's
    repositories.

    Matching on the pins the repo's own CI uses means a future bump either
    updates both or fails here. Parsed from `ci.yml` rather than hardcoded a
    second time, since a second copy is the thing that drifts.
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    ours = repo.joinpath(".github", "workflows", "ci.yml").read_text(
        encoding="utf-8")
    _ok, _msg, generated = _generate(tmp_path, {"syntax": True})

    for action in ("actions/checkout", "actions/setup-python"):
        mine = set(re.findall(rf"{action}@(v\d+)", ours))
        theirs = set(re.findall(rf"{action}@(v\d+)", generated))
        assert len(mine) == 1, f"this repo pins {action} inconsistently: {mine}"
        assert theirs, f"the generated workflow never uses {action}"
        assert theirs == mine, (
            f"generated workflow pins {action}@{theirs} while this repo runs "
            f"@{mine} — bump helpers/ci_workflow.py to match")


# ── Conditional step inclusion ────────────────────────────────────────────

def test_syntax_step_included_when_enabled(tmp_path):
    _ok, _msg, content = _generate(tmp_path, {"syntax": True, "pyflakes": False})
    assert "Syntax check" in content
    assert "python -m compileall src/ -q" in content


def test_syntax_step_omitted_when_disabled(tmp_path):
    _ok, _msg, content = _generate(tmp_path, {"syntax": False, "pyflakes": True})
    assert "Syntax check" not in content
    assert "python -m compileall" not in content


def test_pyflakes_step_included_when_enabled(tmp_path):
    _ok, _msg, content = _generate(tmp_path, {"syntax": False, "pyflakes": True})
    assert "Pyflakes" in content
    assert "python -m pyflakes src/" in content


def test_pyflakes_step_omitted_when_disabled(tmp_path):
    _ok, _msg, content = _generate(tmp_path, {"syntax": True, "pyflakes": False})
    assert "Pyflakes" not in content
    assert "pyflakes --quiet" not in content   # the pip install line too


def test_pip_install_pyflakes_only_when_pyflakes_enabled(tmp_path):
    """Pip-install-pyflakes step must NOT appear if pyflakes is disabled."""
    _ok, _msg, syntax_only = _generate(tmp_path, {"syntax": True, "pyflakes": False})
    assert "pip install pyflakes" not in syntax_only

    _ok, _msg, both = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    assert "pip install pyflakes --quiet" in both


def test_both_disabled_produces_no_check_steps(tmp_path):
    """All-checks-off produces a degenerate but well-formed workflow.

    The job still has checkout+setup-python so it's a valid workflow;
    it just doesn't run anything. Users get a green tick with zero work
    — a graceful no-op rather than a YAML parse error.
    """
    _ok, _msg, content = _generate(tmp_path, {"syntax": False, "pyflakes": False})
    assert "actions/checkout" in content
    assert "Syntax check" not in content
    assert "Pyflakes" not in content
    assert "pip install pyflakes" not in content


# ── Excluded-checks tests (doctor + claude must NEVER be in CI) ───────────

def _job_steps_block(yaml: str) -> str:
    """Return only the lines after `jobs:` (strips the documentation header).

    The header comment legitimately mentions Doctor and Claude as
    explanations for WHY they're excluded; we want to assert on the
    actual workflow steps, not on the comment block.
    """
    idx = yaml.find("jobs:")
    return yaml[idx:] if idx != -1 else yaml


def test_doctor_step_is_included_but_advisory(tmp_path):
    """Doctor now runs in CI, and must never gate the build.

    It used to be omitted because controllers/doctor_ctrl imports Tk at
    module scope, which fails on ubuntu-latest. The rules moved to the
    stdlib-only helpers/doctor_rules, so that reason is gone.

    It stays advisory on purpose: the caps describe an aspiration, and this
    repo alone carries 100+ existing violations. A gating step would paint
    every project red on the day it is generated, and a red badge nobody can
    fix is a badge nobody reads.
    """
    _ok, _msg, content = _generate(tmp_path, {
        "syntax":   True,
        "pyflakes": True,
        "doctor":   True,
        "claude":   True,
    })
    steps = _job_steps_block(content)
    assert "- name: Doctor audit (advisory)" in steps
    assert "continue-on-error: true" in steps
    assert "_audit_project_tree" in steps
    # It reaches for the Tk-free module, never the controller.
    assert "helpers.doctor_rules" in steps
    assert "doctor_ctrl" not in steps


def test_doctor_step_can_be_turned_off(tmp_path):
    _ok, _msg, content = _generate(tmp_path, {
        "syntax": True, "pyflakes": True, "doctor": False, "claude": True})
    assert "- name: Doctor audit" not in _job_steps_block(content)


def test_claude_step_is_still_omitted(tmp_path):
    """Unchanged: Claude review needs interactive auth CI cannot provide."""
    _ok, _msg, content = _generate(tmp_path, {
        "syntax": True, "pyflakes": True, "doctor": True, "claude": True})
    steps = _job_steps_block(content)
    assert "- name: Claude" not in steps


def test_claude_step_never_included(tmp_path):
    """Claude review step requires interactive auth — never in CI steps."""
    _ok, _msg, content = _generate(tmp_path, {
        "syntax":   True,
        "pyflakes": True,
        "claude":   True,
    })
    steps = _job_steps_block(content)
    assert "- name: Claude" not in steps
    assert "claude_cli" not in steps


# ── Idempotency ───────────────────────────────────────────────────────────

def test_idempotent_overwrite(tmp_path):
    """Re-generating produces a clean overwrite (no append, no merge)."""
    _ok, _msg, first = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    # Mutate the file out-of-band to simulate manual edits.
    out_path = tmp_path / ".github" / "workflows" / "quality-checks.yml"
    out_path.write_text(first + "\n\n# user-added comment\n", encoding="utf-8")

    _ok2, _msg2, second = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    assert second == first
    assert "user-added comment" not in second


def test_fewer_checks_after_regen_removes_steps(tmp_path):
    """After disabling pyflakes, regen must REMOVE pyflakes steps."""
    _ok, _msg, with_pyflakes = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    assert "Pyflakes" in with_pyflakes

    _ok2, _msg2, without_pyflakes = _generate(tmp_path, {"syntax": True, "pyflakes": False})
    assert "Pyflakes" not in without_pyflakes
    assert "pip install pyflakes" not in without_pyflakes


# ── Filesystem error handling ─────────────────────────────────────────────

# ── Auto-added test job (v4.12 / G-I) ─────────────────────────────────────

def test_auto_test_job_included_when_project_has_tests_and_requirements(tmp_path):
    """If project has tests/ + requirements-dev.txt, a 'test' job appears."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "requirements-dev.txt").write_text("pytest>=7\n")
    _ok, _msg, content = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    steps = _job_steps_block(content)
    assert "test:" in steps
    assert "python -m pytest" in steps
    assert "xvfb-run" in steps
    assert "python3-tk" in steps


def test_auto_test_job_omitted_when_no_tests_dir(tmp_path):
    """No tests/ dir → no test job (lean workflow for downstream projects)."""
    (tmp_path / "requirements-dev.txt").write_text("pytest>=7\n")
    _ok, _msg, content = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    steps = _job_steps_block(content)
    assert "test:" not in steps
    assert "python -m pytest" not in steps


def test_auto_test_job_omitted_when_no_requirements_dev(tmp_path):
    """No requirements-dev.txt → no test job (likely no pytest configured)."""
    (tmp_path / "tests").mkdir()
    _ok, _msg, content = _generate(tmp_path, {"syntax": True, "pyflakes": True})
    steps = _job_steps_block(content)
    assert "test:" not in steps


def test_returns_false_when_makedirs_fails(tmp_path, mocker):
    """When ``os.makedirs`` raises OSError, generator returns ok=False.

    Patches at the generator's import-site (helpers.ci_workflow.os.makedirs)
    rather than globally — G-E import-site discipline.
    """
    mocker.patch("helpers.ci_workflow.os.makedirs",
                 side_effect=OSError("permission denied"))
    ok, msg = generate_github_workflow(str(tmp_path), {"syntax": True})
    assert not ok
    assert "could not create" in msg.lower()


def test_returns_false_when_write_fails(tmp_path, mocker):
    """When the file ``open(..., 'w')`` raises OSError, ok=False."""
    # Let makedirs succeed (so we reach the write), but make open() fail.
    real_open = open

    def _failing_open(path, *a, **kw):
        if "quality-checks.yml" in str(path) and "w" in (a[0] if a else kw.get("mode", "")):
            raise OSError("disk full")
        return real_open(path, *a, **kw)

    mocker.patch("builtins.open", side_effect=_failing_open)
    ok, msg = generate_github_workflow(str(tmp_path), {"syntax": True})
    assert not ok
    assert "could not write" in msg.lower()
