"""tests/test_dialog_scrub_history.py — ScrubHistoryDialog (Tk-marked).

Verifies the v4.5 destructive-action safety nets:

* Filter-repo availability gate shows when filter-repo is absent
* No-file-selected → ``_on_scrub_now`` is a silent no-op
* Confirmation-phrase enforcement (basename match required)
* Backup branch naming + creation argv
* Filter-repo install action runs ``pip install --user git-filter-repo``

All ``subprocess`` calls into git / filter-repo / pip are mocked at the
import site (G-E).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.scrub_history import ScrubHistoryDialog

pytestmark = pytest.mark.tk


def _preflight_ok(**overrides):
    """Default preflight dict — everything ready for a scrub."""
    base = {
        "git_exe":            "git.exe",
        "git_exe_present":    True,
        "filter_repo":        True,
        "is_git_repo":        True,
        "head_branch":        "Roadmap-7",
        "working_tree_clean": True,
        "remote_url":         "https://github.com/foo/bar.git",
    }
    base.update(overrides)
    return base


# ── Construction with various preflight states ───────────────────────────

def test_dialog_constructs_when_filter_repo_present(
    tk_root, mock_config, mocker, tmp_path
):
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok())
    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    assert dialog._preflight["filter_repo"] is True


def test_dialog_constructs_when_filter_repo_missing(
    tk_root, mock_config, mocker, tmp_path
):
    """If filter-repo is missing, the dialog should still construct so the
    user can see the 'Install filter-repo' button without crashing."""
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok(filter_repo=False))
    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    assert dialog._preflight["filter_repo"] is False


# ── _on_scrub_now refuses on missing inputs ──────────────────────────────

def test_scrub_now_silent_when_no_file_selected(
    tk_root, mock_config, mocker, tmp_path
):
    """An empty file selection should return immediately — no messagebox,
    no subprocess, no state change."""
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok())
    mock_ask = mocker.patch("dialogs.scrub_history.messagebox.askyesno")
    mock_create = mocker.patch("dialogs.scrub_history.create_backup_branch")

    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    dialog._selected_file.set("")     # nothing selected
    dialog._on_scrub_now()

    mock_ask.assert_not_called()
    mock_create.assert_not_called()


def test_scrub_now_refuses_when_user_declines_confirmation(
    tk_root, mock_config, mocker, tmp_path
):
    """User clicks No on the askyesno → no work."""
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok())
    mocker.patch("dialogs.scrub_history.messagebox.askyesno",
                 return_value=False)
    mock_create = mocker.patch("dialogs.scrub_history.create_backup_branch")

    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    dialog._selected_file.set("secrets.json")
    dialog._on_scrub_now()
    mock_create.assert_not_called()


def test_scrub_now_runs_backup_then_filter_repo(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """Happy path: backup branch created BEFORE filter-repo runs."""
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok())
    mocker.patch("dialogs.scrub_history.messagebox.askyesno",
                 return_value=True)

    call_order: list = []
    mocker.patch("dialogs.scrub_history.create_backup_branch",
                 side_effect=lambda *a, **kw: (
                     call_order.append("backup"),
                     (True, "branch created"),
                 )[1])
    mocker.patch("dialogs.scrub_history.get_remote_url",
                 return_value="https://github.com/foo/bar.git")
    mock_scrub = mocker.patch(
        "dialogs.scrub_history.run_scrub",
        side_effect=lambda *a, **kw: (
            call_order.append("filter_repo"),
            (True, ""),
        )[1],
    )

    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    patch_after(dialog)
    dialog._selected_file.set("secrets.json")
    dialog._backup_branch_name = "backup/before-scrub-1700000000"
    dialog._on_scrub_now()

    wait_for(lambda: mock_scrub.called, timeout_s=3.0)

    assert call_order == ["backup", "filter_repo"]


def test_scrub_now_aborts_if_backup_fails(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """If backup branch creation fails, run_scrub must NOT be called."""
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok())
    mocker.patch("dialogs.scrub_history.messagebox.askyesno",
                 return_value=True)
    mock_backup = mocker.patch("dialogs.scrub_history.create_backup_branch",
                               return_value=(False, "branch creation failed"))
    mock_scrub = mocker.patch("dialogs.scrub_history.run_scrub")

    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    harness = patch_after(dialog)
    dialog._selected_file.set("secrets.json")
    dialog._backup_branch_name = "backup/before-scrub-1700000000"

    # TEMPORARY DIAGNOSTIC (Roadmap-9). This test fails only on the Linux CI
    # runner, and three hypotheses about why were all wrong: waiting on
    # _scrub_in_flight (patch_after captures the callback so it never
    # clears), waiting on threading.active_count() (a global other tests
    # perturb), and waiting on the harness queue. Rather than guess a fourth
    # time, collect the real state and put it in the failure message.
    # Revert once it has pointed at the cause.
    import threading as _th
    import time as _time
    import traceback as _tb

    worker_errors = []
    prev_hook = _th.excepthook

    def _capture(args):
        # A daemon worker's exception is swallowed by default, so an early
        # crash looks identical to "never scheduled anything".
        worker_errors.append("".join(_tb.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))
        prev_hook(args)

    _th.excepthook = _capture
    try:
        before = len(harness._queue)
        threads_before = sorted(t.name for t in _th.enumerate())
        dialog._on_scrub_now()

        deadline = _time.monotonic() + 10.0
        while _time.monotonic() < deadline:
            tk_root.update()
            if len(harness._queue) > before:
                break
            _time.sleep(0.02)
    finally:
        _th.excepthook = prev_hook

    if len(harness._queue) <= before:
        alive = sorted(
            "{0}(daemon={1},alive={2})".format(t.name, t.daemon, t.is_alive())
            for t in _th.enumerate())
        queued = [getattr(cb, "__name__", repr(cb))[:40]
                  for _dl, _sq, cb, _ar in harness._queue]
        raise AssertionError(chr(10).join([
            "DIAGNOSTIC - the worker scheduled nothing.",
            "  scrub_in_flight   : {0}".format(
                getattr(dialog, "_scrub_in_flight", "<missing>")),
            "  queue before/after: {0}/{1}".format(before, len(harness._queue)),
            "  queue contents    : {0}".format(queued),
            "  backup mock calls : {0}".format(mock_backup.call_count),
            "  scrub mock calls  : {0}".format(mock_scrub.call_count),
            "  threads before    : {0}".format(threads_before),
            "  threads now       : {0}".format(alive),
            "  dialog exists     : {0}".format(dialog.winfo_exists()),
            "  worker exceptions : {0}".format(worker_errors or ["none"]),
        ]))

    mock_scrub.assert_not_called()

# ── _on_install_filter_repo ──────────────────────────────────────────────

def test_install_filter_repo_invokes_helper(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    mocker.patch("dialogs.scrub_history.preflight",
                 return_value=_preflight_ok(filter_repo=False))
    mock_install = mocker.patch(
        "dialogs.scrub_history.install_filter_repo",
        return_value=(True, "installed"),
    )

    dialog = ScrubHistoryDialog(tk_root, str(tmp_path), mock_config)
    patch_after(dialog)
    dialog._on_install_filter_repo()
    wait_for(lambda: mock_install.called, timeout_s=3.0)
    mock_install.assert_called_once()
