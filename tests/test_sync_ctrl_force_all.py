"""tests/test_sync_ctrl_force_all.py — Force Sync All gathers indexed
projects and hands them to run_batch("force"); run_batch already owns the
confirmation dialog and the sequential runner, so there is nothing else for
this command to get wrong.
"""
from __future__ import annotations

from unittest import mock

from controllers.sync_ctrl import SyncStatusController


def _controller(get_projects):
    return SyncStatusController(
        tab=mock.MagicMock(), cfg=None,
        on_log=lambda *a, **k: None,
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
        on_refresh=lambda: None,
        on_run=lambda *a, **k: None,
        on_run_capture=lambda *a, **k: ("", 0, 0.0),
        get_projects=get_projects,
    )


def test_force_sync_all_only_rebuilds_indexed_projects(mocker):
    projects = [
        {"name": "a", "path": "/a", "has_tokensave": True},
        {"name": "b", "path": "/b", "has_tokensave": False},
        {"name": "c", "path": "/c", "has_tokensave": True},
    ]
    ctrl = _controller(lambda: projects)
    batch = mocker.patch.object(ctrl, "run_batch")

    ctrl.cmd_force_sync_all()

    batch.assert_called_once_with(["/a", "/c"], "force")


def test_force_sync_all_noop_on_no_projects(mocker):
    ctrl = _controller(lambda: [])
    batch = mocker.patch.object(ctrl, "run_batch")
    info = mocker.patch("controllers.sync_ctrl.messagebox.showinfo")

    ctrl.cmd_force_sync_all()

    batch.assert_not_called()
    info.assert_called_once()


def test_force_sync_all_noop_when_nothing_indexed(mocker):
    projects = [{"name": "a", "path": "/a", "has_tokensave": False}]
    ctrl = _controller(lambda: projects)
    batch = mocker.patch.object(ctrl, "run_batch")
    info = mocker.patch("controllers.sync_ctrl.messagebox.showinfo")

    ctrl.cmd_force_sync_all()

    batch.assert_not_called()
    info.assert_called_once()
