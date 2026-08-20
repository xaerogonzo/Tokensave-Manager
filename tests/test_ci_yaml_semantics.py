"""tests/test_ci_yaml_semantics.py — the CI workflow means what it claims.

The three test jobs were near-identical and had to be edited in lockstep, so
their steps moved into a reusable workflow. That refactor is only safe if the
callers still differ in exactly the ways they differed before — and a workflow
mistake is expensive to find, because you learn about it from a merge that
should have been blocked and was not.

So these assert the gating story rather than the file's shape:

  * `test-warn` warns, the other two gate;
  * only `test-gate` collects coverage and enforces the floor;
  * the trigger predicates stay mutually exclusive, so one push never runs
    two test jobs against the same SHA;
  * the `check` job stays independent, since it is the one job that still
    runs when a bad import aborts test collection.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_SUITE = _ROOT / ".github" / "workflows" / "_test_suite.yml"

_CALLERS = ("test-warn", "test-gate", "test-postmerge")


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci():
    return _load(_CI)


@pytest.fixture(scope="module")
def suite():
    return _load(_SUITE)


# ── the callers ───────────────────────────────────────────────────────────────

def test_all_three_test_jobs_call_the_shared_workflow(ci):
    for job in _CALLERS:
        assert ci["jobs"][job]["uses"] == "./.github/workflows/_test_suite.yml"


def test_only_the_gate_collects_coverage(ci):
    """Coverage on every job would slow the warn-only path for no benefit,
    and enforcing a floor there would make it a gate by accident."""
    with_ = {j: (ci["jobs"][j].get("with") or {}) for j in _CALLERS}
    assert with_["test-gate"].get("coverage") is True
    assert with_["test-gate"].get("coverage_fail_under") == 14
    assert "coverage" not in with_["test-warn"]
    assert "coverage" not in with_["test-postmerge"]


def test_only_the_roadmap_branch_job_is_warn_only(ci):
    """The two jobs that guard main/master must be able to fail."""
    with_ = {j: (ci["jobs"][j].get("with") or {}) for j in _CALLERS}
    assert with_["test-warn"].get("continue_on_error") is True
    assert with_["test-gate"].get("continue_on_error") is not True
    assert with_["test-postmerge"].get("continue_on_error") is not True


def test_callers_do_not_set_continue_on_error_as_a_job_key(ci):
    """A job calling a reusable workflow may not use continue-on-error.

    Setting it would be silently ignored at best and a workflow syntax error
    at worst — either way the warn-only job would start gating.
    """
    for job in _CALLERS:
        assert "continue-on-error" not in ci["jobs"][job]


# ── gating predicates ─────────────────────────────────────────────────────────

def test_the_trigger_predicates_are_unchanged(ci):
    j = ci["jobs"]
    assert j["test-warn"]["if"] == (
        "github.event_name == 'push' && "
        "startsWith(github.ref, 'refs/heads/Roadmap-')")
    assert j["test-gate"]["if"] == "github.event_name == 'pull_request'"
    assert j["test-postmerge"]["if"] == (
        "github.event_name == 'push' && (github.ref == 'refs/heads/main' "
        "|| github.ref == 'refs/heads/master')")


def test_exactly_one_test_job_matches_any_given_event(ci):
    """Two matching predicates would run the suite twice on one SHA.

    Evaluated here rather than trusted: push-to-Roadmap, PR, and
    push-to-master must each select a single job.
    """
    preds = {j: ci["jobs"][j]["if"] for j in _CALLERS}

    def matches(pred, event, ref):
        expr = (pred
                .replace("github.event_name", repr(event))
                .replace("github.ref", repr(ref))
                .replace("&&", "and").replace("||", "or")
                .replace("startsWith(", "str.startswith("))
        # startsWith(a, b) -> str.startswith(a, b) is valid Python
        return bool(eval(expr))          # noqa: S307 - fixed strings above

    for event, ref in [("push", "refs/heads/Roadmap-9"),
                       ("pull_request", "refs/heads/whatever"),
                       ("push", "refs/heads/master")]:
        hits = [j for j, p in preds.items() if matches(p, event, ref)]
        assert len(hits) == 1, f"{event} {ref} matched {hits}"


def test_a_feature_branch_push_runs_no_test_job(ci):
    """Branch policy: short-lived branches are tested locally."""
    preds = {j: ci["jobs"][j]["if"] for j in _CALLERS}
    for pred in preds.values():
        expr = (pred.replace("github.event_name", "'push'")
                    .replace("github.ref", "'refs/heads/some-feature'")
                    .replace("&&", "and").replace("||", "or")
                    .replace("startsWith(", "str.startswith("))
        assert not eval(expr)            # noqa: S307


def test_check_job_is_independent_of_the_reusable_workflow(ci):
    """It must still run when a bad import aborts test collection."""
    check = ci["jobs"]["check"]
    assert "uses" not in check
    names = [s.get("name", "") for s in check["steps"]]
    assert "No third-party module-level imports in src/" in names


# ── the reusable workflow ─────────────────────────────────────────────────────

def test_suite_declares_the_three_inputs(suite):
    inputs = suite[True]["workflow_call"]["inputs"]
    assert set(inputs) == {"coverage", "coverage_fail_under",
                           "continue_on_error"}
    assert inputs["coverage"]["default"] is False
    assert inputs["continue_on_error"]["default"] is False


def test_every_command_from_the_old_jobs_survives(suite):
    """The refactor is behaviour-preserving or it is not a refactor."""
    runs = " || ".join(s.get("run", "") for s in suite["jobs"]["tests"]["steps"])
    for cmd in [
        "pip install -r requirements-dev.txt",
        "sudo apt-get update && sudo apt-get install -y python3-tk xvfb",
        'python -m pytest -m "not tk" -v',
        "xvfb-run -a python -m pytest -m tk -v",
        'python -m pytest -m "not tk" -v --cov=src',
        "python -m coverage xml --ignore-errors || true",
    ]:
        assert cmd in runs, f"lost command: {cmd}"


def test_the_coverage_floor_is_templated_from_the_input(suite):
    runs = " ".join(s.get("run", "") for s in suite["jobs"]["tests"]["steps"])
    assert "--cov-fail-under=${{ inputs.coverage_fail_under }}" in runs
    assert "--cov-fail-under=14" not in runs, \
        "the floor must come from the input, not be hard-coded twice"


def test_coverage_steps_are_gated_on_the_coverage_input(suite):
    """Otherwise the warn-only and post-merge jobs would upload artifacts and
    enforce a floor they never asked for."""
    for step in suite["jobs"]["tests"]["steps"]:
        name = step.get("name", "")
        if "coverage" in name.lower() or "XML report" in name:
            assert "inputs.coverage" in str(step.get("if", "")), \
                f"{name!r} is not gated on the coverage input"


def test_coverage_reporting_still_runs_after_a_failure(suite):
    """`always()` is what makes a failed run still explain its coverage."""
    for step in suite["jobs"]["tests"]["steps"]:
        if step.get("name") in ("Generate unified XML report",
                                "Upload coverage artifact"):
            assert "always()" in str(step.get("if", ""))


def test_the_artifact_upload_is_preserved(suite):
    steps = suite["jobs"]["tests"]["steps"]
    upload = [s for s in steps if str(s.get("uses", "")).startswith(
        "actions/upload-artifact")]
    assert len(upload) == 1
    assert upload[0]["with"]["if-no-files-found"] == "ignore"
