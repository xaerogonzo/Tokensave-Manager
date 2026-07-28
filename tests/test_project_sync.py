"""Tests for controllers/project_sync_ctrl.py — ProjectSyncCtrl.

Covers the background git-status column refresh logic WITHOUT a real Tk root or
real git: the worker thread is run synchronously by patching
``project_sync_ctrl.threading.Thread`` at the import site, ``on_shell`` is a mock
(never shells out), and the Treeview is a MagicMock. ``_format_git_status_cell``
and ``_parse_git_status_v2`` are the real helpers (pure functions).
"""

import os
from types import SimpleNamespace
from unittest import mock

from controllers.project_sync_ctrl import ProjectSyncCtrl, _GIT_STATUS_TAGS


# ── Helpers ───────────────────────────────────────────────────────────────────

class _SyncThread:
    """Drop-in for threading.Thread that runs the target synchronously on start.

    Lets us exercise the worker body deterministically without real threads.
    """
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_ctrl(*, tab=None, cfg=None, on_shell=None, get_tree=None):
    return ProjectSyncCtrl(
        tab=tab if tab is not None else mock.MagicMock(),
        cfg=cfg if cfg is not None else SimpleNamespace(git_exe="git"),
        on_shell=on_shell if on_shell is not None else mock.MagicMock(
            return_value=("", 0)),
        get_tree=get_tree if get_tree is not None else (lambda: None),
    )


# ── _update_cell ──────────────────────────────────────────────────────────────

def test_update_cell_noop_when_tree_none():
    ctrl = _make_ctrl(get_tree=lambda: None)
    # Must not raise even though there is no tree.
    ctrl._update_cell("proj:/p", {"clean": True})


def test_update_cell_noop_when_row_missing():
    tree = mock.MagicMock()
    tree.exists.return_value = False
    ctrl = _make_ctrl(get_tree=lambda: tree)
    ctrl._update_cell("proj:/p", {})
    tree.set.assert_not_called()


def test_update_cell_sets_text_and_replaces_status_tag():
    tree = mock.MagicMock()
    tree.exists.return_value = True
    # Existing tags include a stale git status tag plus a non-git tag.
    tree.item.return_value = ("pinned", "git_dirty")
    ctrl = _make_ctrl(get_tree=lambda: tree)

    # A clean status → new tag "git_clean", DIFFERENT from the stale "git_dirty",
    # so the strip-and-replace is observable.
    status = {"has_remote": False, "dirty": False, "ahead": 0, "behind": 0}
    ctrl._update_cell("proj:/p", status)

    # The "git" column text was set.
    assert tree.set.call_args[0][0] == "proj:/p"
    assert tree.set.call_args[0][1] == "git"
    # The new tags: stale git_dirty stripped, non-git "pinned" kept, git_clean added.
    new_tags = tree.item.call_args_list[-1].kwargs["tags"]
    assert "pinned" in new_tags
    assert "git_dirty" not in new_tags     # stale status tag removed
    assert "git_clean" in new_tags         # fresh status tag added
    # Exactly one git-status tag present after the update.
    assert sum(1 for t in new_tags if t in _GIT_STATUS_TAGS) == 1


def test_update_cell_survives_tclerror_on_set():
    import tkinter as tk
    tree = mock.MagicMock()
    tree.exists.return_value = True
    tree.set.side_effect = tk.TclError("gone")
    ctrl = _make_ctrl(get_tree=lambda: tree)
    status = {"has_remote": False, "dirty": False, "ahead": 0, "behind": 0}
    # Should swallow the TclError and not attempt the tag update.
    ctrl._update_cell("proj:/p", status)
    tree.item.assert_not_called()


# ── _kick_off / refresh (worker run synchronously) ───────────────────────────

def test_refresh_skips_non_git_projects(monkeypatch):
    monkeypatch.setattr("controllers.project_sync_ctrl.threading.Thread", _SyncThread)
    on_shell = mock.MagicMock(return_value=("", 0))
    ctrl = _make_ctrl(on_shell=on_shell)
    ctrl.refresh([{"path": "/p", "has_git": False}])
    on_shell.assert_not_called()
    assert ctrl._running is False   # finally-block cleared it


def test_refresh_skips_unchanged_index(monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.project_sync_ctrl.threading.Thread", _SyncThread)
    # Create a real .git/index so getmtime succeeds deterministically.
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    idx = gitdir / "index"
    idx.write_text("x")
    mtime = os.path.getmtime(str(idx))

    on_shell = mock.MagicMock(return_value=("", 0))
    ctrl = _make_ctrl(on_shell=on_shell)
    # cached status present AND matching mtime → skip the shell call.
    proj = {"path": str(tmp_path), "has_git": True,
            "git_status": {"cached": True}, "_git_idx_mtime": mtime}
    ctrl.refresh([proj])
    on_shell.assert_not_called()


def test_refresh_runs_shell_and_schedules_update(monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.project_sync_ctrl.threading.Thread", _SyncThread)
    monkeypatch.setattr("controllers.project_sync_ctrl.time.sleep", lambda *_a: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("x")

    on_shell = mock.MagicMock(return_value=("# branch.head main\n", 0))
    tab = mock.MagicMock()
    tab.winfo_exists.return_value = True
    ctrl = _make_ctrl(tab=tab, on_shell=on_shell,
                      cfg=SimpleNamespace(git_exe="git"))

    proj = {"path": str(tmp_path), "has_git": True}
    ctrl.refresh([proj])

    # Shell was invoked with a porcelain-v2 status command.
    args = on_shell.call_args[0][0]
    assert "status" in args and "--porcelain=v2" in args
    # The project dict was updated with parsed status + mtime.
    assert "git_status" in proj and "_git_idx_mtime" in proj
    # A UI update was scheduled on the tab via after(0, ...).
    assert tab.after.called
    assert tab.after.call_args[0][1] == ctrl._update_cell


def test_refresh_shell_exception_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.project_sync_ctrl.threading.Thread", _SyncThread)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("x")

    def boom(*_a, **_k):
        raise RuntimeError("git exploded")

    ctrl = _make_ctrl(on_shell=boom)
    # The worker catches the exception per-project and keeps going.
    ctrl.refresh([{"path": str(tmp_path), "has_git": True}])
    assert ctrl._running is False


def test_refresh_cancel_flag_set_when_already_running(monkeypatch):
    # Thread that does NOT run the target → _running stays True after kick.
    class _NoRunThread:
        def __init__(self, target=None, daemon=None, **kw): pass
        def start(self): pass

    monkeypatch.setattr("controllers.project_sync_ctrl.threading.Thread", _NoRunThread)
    ctrl = _make_ctrl()
    ctrl._running = True            # simulate an in-flight refresh
    ctrl.refresh([])
    assert ctrl._cancel is False    # reset to False after requesting cancel
    assert ctrl._running is True    # new run marked running


# ── Module-level constant sanity ─────────────────────────────────────────────

def test_git_status_tags_constant():
    assert "git_clean" in _GIT_STATUS_TAGS
    assert "git_none" in _GIT_STATUS_TAGS
    assert len(_GIT_STATUS_TAGS) == 7
