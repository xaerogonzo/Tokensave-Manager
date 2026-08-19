"""tests/test_gh_ci_status.py — CI badge state resolution.

The badge is only useful if its states are trustworthy, so most of these pin
distinctions that are easy to collapse and misleading when collapsed:

  * a RUNNING build must not read as FAILED (an in-flight run has a null
    conclusion, and "not success" is the wrong default for null);
  * NO_RESULT must not read as FAILED — that covers both a brand-new branch
    and a run whose jobs all skipped;
  * UNAVAILABLE must not read as FAILED ("we could not ask gh" and "the build
    is broken" are different facts).

subprocess is patched at the import site (`helpers.gh_ci_status.subprocess`)
per the G-E rule — never globally, and no real `gh` process is spawned.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers import gh_ci_status
from helpers.gh_ci_status import (
    FAILED,
    NO_RESULT,
    RUNNING,
    SUCCESS,
    UNAVAILABLE,
    CIStatus,
    classify_run,
    get_latest_run_status,
)


def _run_ok(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


# ── classify_run: status wins over conclusion ────────────────────────────────

@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting",
                                    "requested", "pending"])
def test_in_flight_statuses_are_running_despite_null_conclusion(status):
    """The trap: an unfinished run reports conclusion=None."""
    assert classify_run({"status": status, "conclusion": None}) == RUNNING


def test_completed_success():
    assert classify_run(
        {"status": "completed", "conclusion": "success"}) == SUCCESS


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out",
                                        "startup_failure", "stale",
                                        "action_required"])
def test_bad_conclusions_are_failed(conclusion):
    assert classify_run(
        {"status": "completed", "conclusion": conclusion}) == FAILED


def test_completed_without_a_conclusion_is_not_reported_as_failed():
    """Unknown is not the same as broken; err toward RUNNING, not FAILED."""
    assert classify_run(
        {"status": "completed", "conclusion": None}) != FAILED


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_inconclusive_runs_are_neither_green_nor_red(conclusion):
    """A skipped run proved nothing — do not paint it green OR red.

    This repo's CI has four jobs gated by `if:` predicates, so whole runs
    legitimately skip; rendering that as a failure is a false alarm the
    project has already been bitten by once.
    """
    state = classify_run({"status": "completed", "conclusion": conclusion})
    assert state == NO_RESULT
    assert state not in (SUCCESS, FAILED)


# ── get_latest_run_status ─────────────────────────────────────────────────────

def test_success_carries_branch_and_url():
    payload = [{"status": "completed", "conclusion": "success",
                "url": "https://example/runs/1", "displayTitle": "fix: thing"}]
    with patch.object(gh_ci_status.subprocess, "run", return_value=_run_ok(payload)):
        st = get_latest_run_status("gh", ".", "Roadmap-9")
    assert st.state == SUCCESS
    assert st.branch == "Roadmap-9"
    assert st.url == "https://example/runs/1"
    assert st.is_clickable


def test_empty_list_is_no_runs_not_failed():
    with patch.object(gh_ci_status.subprocess, "run", return_value=_run_ok([])):
        st = get_latest_run_status("gh", ".", "brand-new-branch")
    assert st.state == NO_RESULT
    assert st.state != FAILED
    assert not st.is_clickable, "nothing to open when there is no run"


def test_the_branch_asked_for_is_the_branch_queried():
    """Guards the whole point of the helper: never silently poll master."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _run_ok([])

    with patch.object(gh_ci_status.subprocess, "run", side_effect=fake_run):
        get_latest_run_status("gh", ".", "Roadmap-9")
    cmd = seen["cmd"]
    assert "--branch" in cmd
    assert cmd[cmd.index("--branch") + 1] == "Roadmap-9"
    assert "master" not in cmd


@pytest.mark.parametrize("exc", [FileNotFoundError("no gh"), OSError("boom")])
def test_missing_gh_is_unavailable_not_failed(exc):
    with patch.object(gh_ci_status.subprocess, "run", side_effect=exc):
        st = get_latest_run_status("gh", ".", "main")
    assert st.state == UNAVAILABLE
    assert st.state != FAILED


def test_timeout_is_unavailable():
    with patch.object(gh_ci_status.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("gh", 15)):
        st = get_latest_run_status("gh", ".", "main")
    assert st.state == UNAVAILABLE
    assert "timed out" in st.detail


def test_nonzero_exit_is_unavailable_with_stderr_detail():
    proc = SimpleNamespace(returncode=1, stdout="",
                           stderr="not authenticated with GitHub")
    with patch.object(gh_ci_status.subprocess, "run", return_value=proc):
        st = get_latest_run_status("gh", ".", "main")
    assert st.state == UNAVAILABLE
    assert "authenticated" in st.detail


def test_unparseable_output_is_unavailable():
    proc = SimpleNamespace(returncode=0, stdout="not json at all", stderr="")
    with patch.object(gh_ci_status.subprocess, "run", return_value=proc):
        assert get_latest_run_status("gh", ".", "main").state == UNAVAILABLE


def test_unexpected_record_shape_is_unavailable():
    with patch.object(gh_ci_status.subprocess, "run",
                      return_value=_run_ok(["a string, not an object"])):
        assert get_latest_run_status("gh", ".", "main").state == UNAVAILABLE


def test_empty_gh_exe_short_circuits_without_spawning():
    with patch.object(gh_ci_status.subprocess, "run") as run:
        st = get_latest_run_status("", ".", "main")
    assert st.state == UNAVAILABLE
    run.assert_not_called()


def test_empty_branch_short_circuits_without_spawning():
    """A detached HEAD yields no branch — do not query with an empty --branch."""
    with patch.object(gh_ci_status.subprocess, "run") as run:
        st = get_latest_run_status("gh", ".", "")
    assert st.state == UNAVAILABLE
    run.assert_not_called()


# ── labels ────────────────────────────────────────────────────────────────────

def test_every_state_names_the_branch_it_describes():
    """A badge that does not say which branch it means invites the exact
    confusion this helper exists to remove."""
    for state in (SUCCESS, RUNNING, FAILED, NO_RESULT):
        assert "Roadmap-9" in CIStatus(state, branch="Roadmap-9").label()


def test_each_state_has_a_distinct_glyph():
    glyphs = {CIStatus(s).glyph
              for s in (SUCCESS, RUNNING, FAILED, NO_RESULT, UNAVAILABLE)}
    assert len(glyphs) == 5, "states must be visually distinguishable"
