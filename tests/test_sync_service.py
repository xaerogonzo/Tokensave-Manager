"""tests/test_sync_service.py — the one implementation of "sync this project".

`helpers.sync_service` exists so the Git tab and the headless CLI cannot drift
into disagreeing about what a sync is. These tests pin the parts that would
drift silently: the argv, the environment, and the ordering of the shadow
refresh against the indexer that reads its output.

`run_sync` is the CLI's path and is deliberately total — a missing executable
is a *result*, not an exception, because the caller has to render it either
way and the CLI's exit-code 3 ("unavailable prerequisite") is exactly this
case. A helper that raised here would push that judgement onto every caller.
"""
from __future__ import annotations

import subprocess

import pytest

from helpers.sync_service import (
    ShadowPrep,
    init_argv,
    prepare_shadows,
    run_init,
    run_sync,
    sync_argv,
    tokensave_env,
)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="synced 3 files"):
        self.returncode = returncode
        self.stdout = stdout


# ── argv: one source of truth ───────────────────────────────────────────────

def test_sync_argv_is_the_single_source_for_both_forms():
    assert sync_argv() == ["sync"]
    assert sync_argv(force=True) == ["sync", "--force"]


def test_sync_argv_returns_a_fresh_list_each_call():
    """BATCH_OPS holds these at class scope; a shared list would be mutable
    global state that one caller could append to for everyone."""
    first = sync_argv()
    first.append("--oops")
    assert sync_argv() == ["sync"]


# ── environment: tokensave colours regardless of whether stdout is a tty ────

def test_env_suppresses_colour_so_output_stays_parseable():
    env = tokensave_env({"EXISTING": "kept"})
    assert env["NO_COLOR"] == "1"
    assert env["TERM"] == "dumb"
    assert env["EXISTING"] == "kept", "caller's environment must survive"


def test_env_does_not_mutate_the_caller_mapping():
    base = {"EXISTING": "kept"}
    tokensave_env(base)
    assert base == {"EXISTING": "kept"}


# ── the reporting policy, as data rather than log formatting ────────────────

@pytest.mark.parametrize("prep,expected", [
    (ShadowPrep(),                                   False),
    (ShadowPrep(ran=False, created=9),               False),
    (ShadowPrep(ran=True, created=0, failed=0),      False),
    (ShadowPrep(ran=True, created=3),                True),
    (ShadowPrep(ran=True, created=0, failed=4),      True),
])
def test_worth_reporting_matches_the_logging_policy(prep, expected):
    """Silence on an opt-out or a no-op; speak on creations and on failures.

    The failure case is the one a "report only when created > 0" rule would
    have hidden — links failing on a volume that supports them is a real
    problem that otherwise looks like the feature doing nothing.
    """
    assert prep.worth_reporting is expected


def test_prepare_shadows_is_inert_for_a_project_that_never_opted_in(tmp_path):
    prep = prepare_shadows(str(tmp_path))
    assert prep == ShadowPrep(ran=False, created=0, failed=0)


# ── run_sync: the CLI path ──────────────────────────────────────────────────

def test_run_sync_reports_success_with_output(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 return_value=_FakeCompleted(0, "indexed 12 files"))
    result = run_sync(str(tmp_path), "tokensave")
    assert result.ok is True
    assert result.returncode == 0
    assert "indexed 12 files" in result.output
    assert result.argv == ["sync"]
    assert result.error == ""


def test_run_sync_reports_a_nonzero_exit_as_not_ok(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 return_value=_FakeCompleted(2, "index locked"))
    result = run_sync(str(tmp_path), "tokensave")
    assert result.ok is False
    assert result.returncode == 2
    assert "index locked" in result.output


def test_run_sync_passes_force_through(tmp_path, mocker):
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted())
    result = run_sync(str(tmp_path), "tokensave", force=True)
    assert result.argv == ["sync", "--force"]
    assert run.call_args.args[0] == ["tokensave", "sync", "--force"]


def test_run_sync_runs_in_the_project_not_the_cwd(tmp_path, mocker):
    """The whole subsystem exists because something inferred a project from
    an ambient cwd. The service is explicit about it."""
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted())
    run_sync(str(tmp_path), "tokensave")
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


def test_run_sync_suppresses_colour_in_the_subprocess(tmp_path, mocker):
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted())
    run_sync(str(tmp_path), "tokensave")
    env = run.call_args.kwargs["env"]
    assert env["NO_COLOR"] == "1" and env["TERM"] == "dumb"


def test_a_missing_executable_is_a_result_not_an_exception(tmp_path, mocker):
    """Exit code 3 in the CLI contract is 'unavailable prerequisite'."""
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=FileNotFoundError())
    result = run_sync(str(tmp_path), "no-such-tokensave")
    assert result.ok is False
    assert "not found" in result.error
    assert "no-such-tokensave" in result.error


def test_a_timeout_is_a_result_not_an_exception(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("tokensave", 5))
    result = run_sync(str(tmp_path), "tokensave", timeout=5)
    assert result.ok is False
    assert "timed out" in result.error


def test_an_os_error_is_a_result_not_an_exception(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=OSError("permission denied"))
    result = run_sync(str(tmp_path), "tokensave")
    assert result.ok is False
    assert "permission denied" in result.error


def test_shadows_are_refreshed_before_the_indexer_reads_them(tmp_path, mocker):
    """Ordering is the point: links created after the sync are invisible to
    the index that was just built."""
    calls = []
    mocker.patch("helpers.sync_service.prepare_shadows",
                 side_effect=lambda p: calls.append("shadows") or ShadowPrep())
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=lambda *a, **k: calls.append("sync") or _FakeCompleted())
    run_sync(str(tmp_path), "tokensave")
    assert calls == ["shadows", "sync"]


def test_shadow_outcome_survives_a_failed_sync(tmp_path, mocker):
    """The links were still created; a caller reporting the failure should
    not lose that."""
    mocker.patch("helpers.sync_service.prepare_shadows",
                 return_value=ShadowPrep(ran=True, created=2))
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=FileNotFoundError())
    result = run_sync(str(tmp_path), "tokensave")
    assert result.ok is False
    assert result.shadows.created == 2


# ── init: a different command, not sync with a flag ─────────────────────────
#
# These exist because the Manager had no init path at all: Retrofit edited
# CLAUDE.md and called that "integrating tokensave", so a project whose
# CLAUDE.md was already integrated could never obtain an index through the UI.
# The argv is pinned for the same reason sync_argv is -- one source of truth,
# so a GUI-invoked init and a CLI-invoked init cannot mean different things.

def test_init_argv_declines_the_git_hook_by_default():
    """Not politeness -- correctness.

    Bare `tokensave init` *offers* to install the repository's git hooks, and
    an offer needs someone to answer it. Spawned from the GUI there is nobody,
    so the default must decline or the subprocess waits for a terminal that
    does not exist.
    """
    assert init_argv() == ["init", "--no-git-hook"]


def test_init_argv_can_still_ask_for_the_hook():
    assert init_argv(git_hook=True) == ["init", "--git-hook"]


def test_init_argv_returns_a_fresh_list_each_call():
    first = init_argv()
    first.append("--oops")
    assert init_argv() == ["init", "--no-git-hook"]


def test_run_init_invokes_the_real_command_shape(tmp_path, mocker):
    """A mock asserting 'the helper was called' would pass even if the argv
    were wrong. This crosses a process boundary, so pin the boundary."""
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted(0, "Initialized TokenSave"))
    result = run_init(str(tmp_path), "tokensave")
    assert run.call_args.args[0] == ["tokensave", "init", "--no-git-hook"]
    assert run.call_args.kwargs["cwd"] == str(tmp_path)
    assert result.ok is True


def test_run_init_closes_stdin_so_a_prompt_cannot_stall_it(tmp_path, mocker):
    """Belt and braces with --no-git-hook: if any future prompt appears, it
    reads EOF and fails fast instead of hanging until the timeout."""
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted())
    run_init(str(tmp_path), "tokensave")
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_run_init_suppresses_colour_like_every_other_reader(tmp_path, mocker):
    run = mocker.patch("helpers.sync_service.subprocess.run",
                       return_value=_FakeCompleted())
    run_init(str(tmp_path), "tokensave")
    env = run.call_args.kwargs["env"]
    assert env["NO_COLOR"] == "1" and env["TERM"] == "dumb"


def test_run_init_reports_a_nonzero_exit_as_not_ok(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 return_value=_FakeCompleted(1, "already initialized"))
    result = run_init(str(tmp_path), "tokensave")
    assert result.ok is False
    assert result.returncode == 1
    assert "exited 1" in result.error


def test_run_init_missing_executable_is_a_result_not_an_exception(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=FileNotFoundError())
    result = run_init(str(tmp_path), "no-such-tokensave")
    assert result.ok is False
    assert "not found" in result.error and "no-such-tokensave" in result.error


def test_run_init_timeout_is_a_result_not_an_exception(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("tokensave", 600))
    result = run_init(str(tmp_path), "tokensave", timeout=600)
    assert result.ok is False
    assert "timed out" in result.error


def test_run_init_os_error_is_a_result_not_an_exception(tmp_path, mocker):
    mocker.patch("helpers.sync_service.subprocess.run",
                 side_effect=OSError("permission denied"))
    result = run_init(str(tmp_path), "tokensave")
    assert result.ok is False
    assert "permission denied" in result.error
