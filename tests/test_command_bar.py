"""Tests for controllers/command_bar_ctrl.py — CommandBarCtrl.

Pure-delegate sub-controller (no Tkinter import) — every ``cmd_*`` resolves a
path via ``get_path``, optionally gates on ``require_tokensave``, then forwards
to a sub-controller with the path. These tests verify the delegation wiring
with MagicMock sub-controllers; no real git / subprocess / Tk is touched.
"""

import pytest
from unittest import mock

from controllers.command_bar_ctrl import CommandBarCtrl


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def subs():
    """The 8 sub-controller mocks, keyed by the constructor kwarg name."""
    return {name: mock.MagicMock(name=name) for name in (
        "sync", "doctor", "codegraph", "gitops",
        "fileops", "shadowlinks", "scaffold", "ai_tasks")}


def _make(subs, *, path="/proj", tokensave_ok=True):
    """Build a CommandBarCtrl whose get_path returns *path* and
    require_tokensave returns *tokensave_ok*."""
    get_path = mock.MagicMock(return_value=path)
    require_tokensave = mock.MagicMock(return_value=tokensave_ok)
    ctrl = CommandBarCtrl(
        get_path=get_path,
        require_tokensave=require_tokensave,
        **subs,
    )
    return ctrl, get_path, require_tokensave


# ── Path-guarded, non-tokensave-gated delegates ──────────────────────────────

@pytest.mark.parametrize("method,sub,target", [
    ("cmd_codegraph_init",    "codegraph",   "cmd_init"),
    ("cmd_codegraph_sync",    "codegraph",   "cmd_sync"),
    ("cmd_codegraph_reindex", "codegraph",   "cmd_reindex"),
    ("cmd_codegraph_status",  "codegraph",   "cmd_status"),
    ("cmd_codegraph_remove",  "codegraph",   "cmd_remove"),
    ("cmd_git_log",           "gitops",      "cmd_git_log"),
    ("cmd_git_commit",        "gitops",      "cmd_git_commit"),
    ("cmd_ai_code_review",    "gitops",      "cmd_ai_code_review"),
    ("cmd_git_init",          "gitops",      "cmd_git_init"),
    ("cmd_manage_gitignore",  "gitops",      "cmd_manage_gitignore"),
    ("cmd_precommit_hook",    "gitops",      "cmd_precommit_hook"),
    ("cmd_untrack_ignored",   "gitops",      "cmd_untrack_ignored"),
    ("cmd_private_repo",      "gitops",      "cmd_private_repo"),
    ("cmd_draft_changelog",   "ai_tasks",    "cmd_draft_changelog"),
    ("cmd_refactor_scout",    "ai_tasks",    "cmd_refactor_scout"),
    ("cmd_run_checks",        "ai_tasks",    "cmd_run_checks"),
    ("cmd_open_folder",       "fileops",     "cmd_open_folder"),
    ("cmd_open_editor",       "fileops",     "cmd_open_editor"),
    ("cmd_copy_path",         "fileops",     "cmd_copy_path"),
    ("cmd_remove",            "fileops",     "cmd_remove"),
    ("cmd_shadow_links",      "shadowlinks", "cmd_shadow_links"),
    ("cmd_retrofit_selected", "scaffold",    "cmd_retrofit_selected"),
])
def test_path_guarded_delegate_forwards_path(subs, method, sub, target):
    ctrl, _gp, _rt = _make(subs, path="/proj")
    getattr(ctrl, method)()
    getattr(subs[sub], target).assert_called_once_with("/proj")


@pytest.mark.parametrize("method,sub,target", [
    ("cmd_codegraph_init",  "codegraph", "cmd_init"),
    ("cmd_git_log",         "gitops",    "cmd_git_log"),
    ("cmd_open_folder",     "fileops",   "cmd_open_folder"),
    ("cmd_refactor_scout",  "ai_tasks",  "cmd_refactor_scout"),
])
def test_path_guarded_delegate_noop_when_no_path(subs, method, sub, target):
    ctrl, _gp, _rt = _make(subs, path=None)
    getattr(ctrl, method)()
    getattr(subs[sub], target).assert_not_called()


# ── Tokensave-gated delegates ────────────────────────────────────────────────

@pytest.mark.parametrize("method,sub,target", [
    ("cmd_set_active", "sync",   "cmd_set_active"),
    ("cmd_sync",       "sync",   "cmd_sync"),
    ("cmd_status",     "sync",   "cmd_status"),
    ("cmd_force_sync", "sync",   "cmd_force_sync"),
    ("cmd_doctor",     "doctor", "cmd_doctor"),
])
def test_tokensave_gated_forwards_when_indexed(subs, method, sub, target):
    ctrl, _gp, require = _make(subs, path="/proj", tokensave_ok=True)
    getattr(ctrl, method)()
    require.assert_called_once_with("/proj")
    getattr(subs[sub], target).assert_called_once_with("/proj")


@pytest.mark.parametrize("method,sub,target", [
    ("cmd_set_active", "sync",   "cmd_set_active"),
    ("cmd_sync",       "sync",   "cmd_sync"),
    ("cmd_status",     "sync",   "cmd_status"),
    ("cmd_force_sync", "sync",   "cmd_force_sync"),
    ("cmd_doctor",     "doctor", "cmd_doctor"),
])
def test_tokensave_gated_blocks_when_not_indexed(subs, method, sub, target):
    ctrl, _gp, require = _make(subs, path="/proj", tokensave_ok=False)
    getattr(ctrl, method)()
    require.assert_called_once_with("/proj")
    getattr(subs[sub], target).assert_not_called()


@pytest.mark.parametrize("method,sub,target", [
    ("cmd_set_active", "sync",   "cmd_set_active"),
    ("cmd_sync",       "sync",   "cmd_sync"),
    ("cmd_doctor",     "doctor", "cmd_doctor"),
])
def test_tokensave_gated_noop_when_no_path(subs, method, sub, target):
    ctrl, _gp, require = _make(subs, path=None)
    getattr(ctrl, method)()
    # No path → require_tokensave is never even consulted for most;
    # cmd_doctor returns early on no path before the tokensave check.
    getattr(subs[sub], target).assert_not_called()


# ── Unguarded delegates (no path, no tokensave check) ────────────────────────

def test_cmd_auto_always_delegates(subs):
    ctrl, _gp, _rt = _make(subs, path=None)   # path irrelevant
    ctrl.cmd_auto()
    subs["sync"].cmd_auto.assert_called_once_with()


def test_cmd_sync_all_always_delegates(subs):
    ctrl, _gp, _rt = _make(subs, path=None)
    ctrl.cmd_sync_all()
    subs["sync"].cmd_sync_all.assert_called_once_with()


def test_cmd_scaffold_always_delegates(subs):
    ctrl, _gp, _rt = _make(subs, path=None)
    ctrl.cmd_scaffold()
    subs["scaffold"].cmd_scaffold.assert_called_once_with()


def test_cmd_retrofit_always_delegates(subs):
    ctrl, _gp, _rt = _make(subs, path=None)
    ctrl.cmd_retrofit()
    subs["scaffold"].cmd_retrofit.assert_called_once_with()


# ── Constructor stores all references ────────────────────────────────────────

def test_constructor_stores_all_sub_controllers(subs):
    ctrl, get_path, require = _make(subs)
    assert ctrl.get_path is get_path
    assert ctrl.require_tokensave is require
    assert ctrl._sync is subs["sync"]
    assert ctrl._doctor is subs["doctor"]
    assert ctrl._codegraph is subs["codegraph"]
    assert ctrl._gitops is subs["gitops"]
    assert ctrl._fileops is subs["fileops"]
    assert ctrl._shadowlinks is subs["shadowlinks"]
    assert ctrl._scaffold is subs["scaffold"]
    assert ctrl._ai_tasks is subs["ai_tasks"]
