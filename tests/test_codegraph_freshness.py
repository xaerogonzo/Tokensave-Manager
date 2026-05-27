"""tests/test_codegraph_freshness.py — ensure_fresh + autosync debounce.

Tests focus on the deterministic logic:

* ``ensure_fresh`` — does the right thing per health-tier (missing / broken
  / stale / healthy)
* ``kick_autosync`` — two-layer debounce (per-project + global lock)
* ``_run_codegraph_sync`` / ``_run_codegraph_reindex`` — argv correctness

We do NOT exercise the real ``ThreadPoolExecutor`` here — autosync's
threading is best covered at the dialog-integration layer.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from helpers import codegraph_freshness


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset codegraph_freshness module-level state between tests.

    The module caches per-project debounce timestamps + in-flight set
    + per-session reindex-prompted set. Tests share a process so we
    must reset these or test order leaks.
    """
    codegraph_freshness._last_autosync_ts.clear()
    codegraph_freshness._autosync_in_flight.clear()
    codegraph_freshness._reindex_prompted_for.clear()
    yield
    codegraph_freshness._last_autosync_ts.clear()
    codegraph_freshness._autosync_in_flight.clear()
    codegraph_freshness._reindex_prompted_for.clear()


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── ensure_fresh ─────────────────────────────────────────────────────────

def test_ensure_fresh_missing_when_no_codegraph_exe(tmp_path):
    status, detail = codegraph_freshness.ensure_fresh(str(tmp_path), "")
    assert status == "missing"
    assert "codegraph_exe" in detail


def test_ensure_fresh_passes_through_healthy(tmp_path, mocker):
    mocker.patch("helpers.doc_grounding._codegraph_index_health",
                 return_value=("healthy", "85 files"))
    sync = mocker.patch("helpers.codegraph_freshness._run_codegraph_sync")
    status, detail = codegraph_freshness.ensure_fresh(str(tmp_path), "cg.exe")
    assert status == "healthy"
    assert "85 files" in detail
    sync.assert_not_called()  # no sync needed when already healthy


def test_ensure_fresh_runs_sync_on_stale_then_rechecks(tmp_path, mocker):
    """When health is 'stale', sync should run, THEN status is rechecked."""
    health_results = iter([
        ("stale", "index older than newest source"),
        ("healthy", "synced — 85 files"),
    ])
    mocker.patch(
        "helpers.doc_grounding._codegraph_index_health",
        side_effect=lambda *a, **kw: next(health_results),
    )
    sync = mocker.patch("helpers.codegraph_freshness._run_codegraph_sync",
                        return_value=True)
    status, detail = codegraph_freshness.ensure_fresh(str(tmp_path), "cg.exe")
    sync.assert_called_once_with(str(tmp_path), "cg.exe")
    assert status == "healthy"
    assert "85 files" in detail


def test_ensure_fresh_passes_through_missing(tmp_path, mocker):
    """'missing' is a terminal state — no sync attempted."""
    mocker.patch("helpers.doc_grounding._codegraph_index_health",
                 return_value=("missing", "no .codegraph/ directory"))
    sync = mocker.patch("helpers.codegraph_freshness._run_codegraph_sync")
    status, _detail = codegraph_freshness.ensure_fresh(str(tmp_path), "cg.exe")
    assert status == "missing"
    sync.assert_not_called()


def test_ensure_fresh_passes_through_broken(tmp_path, mocker):
    """'broken' indicates a deeper issue — sync alone can't fix; skip it."""
    mocker.patch("helpers.doc_grounding._codegraph_index_health",
                 return_value=("broken", "only 2 files indexed"))
    sync = mocker.patch("helpers.codegraph_freshness._run_codegraph_sync")
    status, _detail = codegraph_freshness.ensure_fresh(str(tmp_path), "cg.exe")
    assert status == "broken"
    sync.assert_not_called()


def test_ensure_fresh_returns_broken_on_exception(tmp_path, mocker):
    """Any exception inside ensure_fresh → broken (fail-open)."""
    mocker.patch("helpers.doc_grounding._codegraph_index_health",
                 side_effect=RuntimeError("health check exploded"))
    status, detail = codegraph_freshness.ensure_fresh(str(tmp_path), "cg.exe")
    assert status == "broken"
    assert "exploded" in detail


# ── _run_codegraph_sync (argv check) ─────────────────────────────────────

def test_run_codegraph_sync_argv(tmp_path, mocker):
    mock_run = mocker.patch("helpers.codegraph_freshness.subprocess.run",
                            return_value=_proc(rc=0))
    ok = codegraph_freshness._run_codegraph_sync(str(tmp_path), "cg.exe")
    assert ok is True
    cmd = mock_run.call_args[0][0]
    assert cmd == ["cg.exe", "sync", str(tmp_path)]


def test_run_codegraph_sync_returns_false_on_exception(tmp_path, mocker):
    mocker.patch("helpers.codegraph_freshness.subprocess.run",
                 side_effect=FileNotFoundError("cg not found"))
    assert codegraph_freshness._run_codegraph_sync(str(tmp_path), "cg.exe") is False


def test_run_codegraph_sync_returns_false_on_nonzero_rc(tmp_path, mocker):
    mocker.patch("helpers.codegraph_freshness.subprocess.run",
                 return_value=_proc(rc=1, stderr="sync failed"))
    assert codegraph_freshness._run_codegraph_sync(str(tmp_path), "cg.exe") is False


# ── _run_codegraph_reindex (argv check) ──────────────────────────────────

def test_run_codegraph_reindex_argv(tmp_path, mocker):
    mock_run = mocker.patch("helpers.codegraph_freshness.subprocess.run",
                            return_value=_proc(rc=0))
    ok = codegraph_freshness._run_codegraph_reindex(str(tmp_path), "cg.exe")
    assert ok is True
    cmd = mock_run.call_args[0][0]
    assert cmd == ["cg.exe", "index", "--force", str(tmp_path)]


# ── kick_autosync debounce ───────────────────────────────────────────────

def test_kick_autosync_no_exe_returns_silently(tmp_path, mocker):
    """No codegraph_exe → no work submitted."""
    mock_submit = mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "")
    mock_submit.assert_not_called()


def test_kick_autosync_submits_to_executor_first_time(tmp_path, mocker):
    """First call with valid exe submits a job."""
    mock_submit = mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    mock_submit.assert_called_once()


def test_kick_autosync_debounces_within_30s(tmp_path, mocker):
    """Second call within 30s should NOT submit again."""
    mock_submit = mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    assert mock_submit.call_count == 1


def test_kick_autosync_admits_after_debounce_window(tmp_path, mocker):
    """After 30s+ since last attempt, a new call should submit.

    In production the worker thread clears the in-flight entry when it
    finishes. Since we've mocked submit() (so the worker never runs),
    we simulate that cleanup explicitly between the two calls.
    """
    mock_submit = mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    # Simulate worker completion: clear the in-flight entry.
    codegraph_freshness._autosync_in_flight.discard(str(tmp_path))
    # Backdate the recorded timestamp to simulate >30s elapsed.
    codegraph_freshness._last_autosync_ts[str(tmp_path)] = (
        time.monotonic() - codegraph_freshness._AUTOSYNC_DEBOUNCE_S - 1
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    assert mock_submit.call_count == 2


def test_kick_autosync_global_in_flight_blocks(tmp_path, mocker):
    """If the project is already in the in-flight set, drop the new request."""
    mock_submit = mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
    )
    codegraph_freshness._autosync_in_flight.add(str(tmp_path))
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    mock_submit.assert_not_called()


def test_kick_autosync_handles_executor_shutdown(tmp_path, mocker):
    """If the executor has been shut down (app exit race), state must
    still be cleaned up so we don't leave orphan in_flight entries."""
    mocker.patch.object(
        codegraph_freshness._autosync_executor, "submit",
        side_effect=RuntimeError("executor shut down"),
    )
    codegraph_freshness.kick_autosync(str(tmp_path), "cg.exe")
    # In-flight set should be cleaned up after the RuntimeError.
    assert str(tmp_path) not in codegraph_freshness._autosync_in_flight


# ── maybe_prompt_reindex (per-session dedup) ─────────────────────────────

def test_maybe_prompt_reindex_silent_when_no_exe(tmp_path, mocker):
    """No codegraph_exe → no prompt."""
    askyesno = mocker.patch("tkinter.messagebox.askyesno", return_value=False)
    codegraph_freshness.maybe_prompt_reindex(None, str(tmp_path), "")
    askyesno.assert_not_called()


def test_maybe_prompt_reindex_skips_already_prompted(tmp_path, mocker):
    """Per-session dedup: second call for the same project is a silent no-op."""
    codegraph_freshness._reindex_prompted_for.add(str(tmp_path))
    askyesno = mocker.patch("tkinter.messagebox.askyesno", return_value=True)
    codegraph_freshness.maybe_prompt_reindex(None, str(tmp_path), "cg.exe")
    askyesno.assert_not_called()


def test_maybe_prompt_reindex_records_project_after_call(tmp_path, mocker):
    """Even if the user declines, the project is recorded so we don't re-prompt."""
    mocker.patch("tkinter.messagebox.askyesno", return_value=False)
    codegraph_freshness.maybe_prompt_reindex(None, str(tmp_path), "cg.exe")
    assert str(tmp_path) in codegraph_freshness._reindex_prompted_for
