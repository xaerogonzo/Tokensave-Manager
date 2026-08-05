"""Tests for app.py — App (the root tk.Tk window).

App is a heavy ``tk.Tk`` subclass whose ``__init__`` builds every tab and
controller, so constructing a real instance in a unit test is impractical.
Instead — matching the project's established pattern (see
``tests/test_pr_gap_body_refresh.py``) — these tests call App's methods
*unbound* against lightweight stub ``self`` objects, asserting the delegation
and control-flow logic. No real Tk window, subprocess, git, or network is used.
"""

import pytest
from types import SimpleNamespace
from unittest import mock

from app import App


# ── _get_git_path / _get_ask_project_path (identical resolution logic) ───────

@pytest.mark.parametrize("method", ["_get_git_path", "_get_ask_project_path"])
def test_path_resolution_prefers_selected_project(method):
    stub = SimpleNamespace(
        _projects=SimpleNamespace(get_selected_path=lambda: "/selected"),
        active_path="/fallback")
    assert getattr(App, method)(stub) == "/selected"


@pytest.mark.parametrize("method", ["_get_git_path", "_get_ask_project_path"])
def test_path_resolution_falls_back_to_active_path(method):
    stub = SimpleNamespace(
        _projects=SimpleNamespace(get_selected_path=lambda: None),
        active_path="/fallback")
    assert getattr(App, method)(stub) == "/fallback"


@pytest.mark.parametrize("method", ["_get_git_path", "_get_ask_project_path"])
def test_path_resolution_no_projects_attr(method):
    # No _projects attribute at all → straight to active_path.
    stub = SimpleNamespace(active_path="/only-active")
    assert getattr(App, method)(stub) == "/only-active"


@pytest.mark.parametrize("method", ["_get_git_path", "_get_ask_project_path"])
def test_path_resolution_returns_none_when_nothing(method):
    stub = object()   # no _projects, no active_path
    assert getattr(App, method)(stub) is None


# ── _on_project_selected ──────────────────────────────────────────────────────

def test_on_project_selected_refreshes_when_git_visible():
    git = mock.MagicMock()
    git.is_visible.return_value = True
    stub = SimpleNamespace(_git=git)
    App._on_project_selected(stub, "/proj")
    git.set_active_path.assert_called_once_with("/proj")
    git.refresh.assert_called_once_with()


def test_on_project_selected_skips_refresh_when_git_hidden():
    git = mock.MagicMock()
    git.is_visible.return_value = False
    stub = SimpleNamespace(_git=git)
    App._on_project_selected(stub, "/proj")
    git.set_active_path.assert_called_once_with("/proj")
    git.refresh.assert_not_called()


# ── _set_running ──────────────────────────────────────────────────────────────

def test_set_running_true_enables_stop_button():
    stop_btn = mock.MagicMock()
    label = mock.MagicMock()
    stub = SimpleNamespace(_stop_btn=stop_btn, _running_label=label)
    App._set_running(stub, True, "Sync All")
    stop_btn.configure.assert_called_once()
    assert stop_btn.configure.call_args.kwargs.get("state") == "normal" \
        or stop_btn.configure.call_args[1]["state"] == "normal"
    assert "Sync All" in label.configure.call_args[1]["text"]


def test_set_running_false_disables_stop_button():
    stop_btn = mock.MagicMock()
    label = mock.MagicMock()
    stub = SimpleNamespace(_stop_btn=stop_btn, _running_label=label)
    App._set_running(stub, False)
    assert stop_btn.configure.call_args[1]["state"] == "disabled"
    assert label.configure.call_args[1]["text"] == ""


# ── _stop_current ─────────────────────────────────────────────────────────────

def test_stop_current_kills_running_proc():
    proc = mock.MagicMock()
    proc.poll.return_value = None          # still running
    projects = mock.MagicMock()
    stub = SimpleNamespace(_current_proc=proc, _log=mock.MagicMock(),
                           _projects=projects)
    App._stop_current(stub)
    assert stub._stop_requested is True
    proc.kill.assert_called_once_with()
    stub._log.assert_called_once()
    projects.stop.assert_called_once_with()


def test_stop_current_does_not_kill_finished_proc():
    proc = mock.MagicMock()
    proc.poll.return_value = 0             # already exited
    projects = mock.MagicMock()
    stub = SimpleNamespace(_current_proc=proc, _log=mock.MagicMock(),
                           _projects=projects)
    App._stop_current(stub)
    proc.kill.assert_not_called()
    projects.stop.assert_called_once_with()   # controller stop still fires


def test_stop_current_handles_no_proc():
    projects = mock.MagicMock()
    stub = SimpleNamespace(_current_proc=None, _log=mock.MagicMock(),
                           _projects=projects)
    App._stop_current(stub)
    projects.stop.assert_called_once_with()


def test_stop_current_no_projects_attr_is_safe():
    # No _projects attribute → must not raise.
    stub = SimpleNamespace(_current_proc=None, _log=mock.MagicMock())
    App._stop_current(stub)
    assert stub._stop_requested is True


# ── _log (marshals to the Tk thread via self.after) ──────────────────────────

def test_log_inserts_message_via_after():
    log_widget = mock.MagicMock()
    # after(0, cb) → run the callback immediately (simulates the Tk loop).
    stub = SimpleNamespace(log=log_widget,
                           after=lambda _delay, cb: cb())
    App._log(stub, "hello", "red")
    # The message (with newline) was inserted with a colour tag.
    args = log_widget.insert.call_args[0]
    assert args[1] == "hello\n"
    assert args[2] == "col_red"


def test_log_defaults_colour_tag():
    log_widget = mock.MagicMock()
    stub = SimpleNamespace(log=log_widget, after=lambda _delay, cb: cb())
    App._log(stub, "plain")
    args = log_widget.insert.call_args[0]
    assert args[1] == "plain\n"
    assert args[2] == "col_None"   # colour=None → tag "col_None"


# ── tokensave version accessors (delegate to UpdatePollerController) ──────────

def test_tokensave_current_version_delegates():
    poller = SimpleNamespace(current_version="6.1.2")
    stub = SimpleNamespace(_update_poller=poller)
    # @property — call its getter unbound.
    assert App._tokensave_current_version.fget(stub) == "6.1.2"


def test_tokensave_available_version_property_delegates():
    poller = SimpleNamespace(available_version="6.2.0")
    stub = SimpleNamespace(_update_poller=poller)
    # It's a @property — call its getter unbound.
    assert App._tokensave_available_version.fget(stub) == "6.2.0"


def test_cmd_upgrade_tokensave_delegates():
    poller = mock.MagicMock()
    stub = SimpleNamespace(_update_poller=poller)
    App.cmd_upgrade_tokensave(stub)
    poller.cmd_upgrade.assert_called_once_with()


# ── _check_worktree_health ────────────────────────────────────────────────

def test_check_worktree_health_logs_each_orphan():
    logged = []
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(
        projects=[{"path": "/proj", "name": "proj", "has_git": True}],
        _cfg=cfg,
        _log=lambda m, c=None: logged.append(m))
    orphans = [{"worktree_path": "/proj/wt1", "branch": "feature1",
               "head": "abc12345", "project_path": "/proj",
               "project_name": "proj"}]
    with mock.patch("app.find_orphaned_worktrees", return_value=orphans):
        App._check_worktree_health(stub)
    assert any("1 git worktree" in m for m in logged)
    assert any("feature1" in m and "/proj/wt1" in m for m in logged)


def test_check_worktree_health_silent_when_clean():
    logged = []
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(projects=[], _cfg=cfg,
                           _log=lambda m, c=None: logged.append(m))
    with mock.patch("app.find_orphaned_worktrees", return_value=[]):
        App._check_worktree_health(stub)
    assert logged == []


def test_check_worktree_health_never_opens_a_dialog():
    """Detect-only at startup — Doctor is the action surface, not this."""
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(
        projects=[{"path": "/proj", "name": "proj", "has_git": True}],
        _cfg=cfg, _log=lambda m, c=None: None)
    orphans = [{"worktree_path": "/proj/wt1", "branch": "b",
               "head": "1234", "project_path": "/proj", "project_name": "proj"}]
    with mock.patch("app.find_orphaned_worktrees", return_value=orphans), \
         mock.patch("app.messagebox") as mb:
        App._check_worktree_health(stub)
    mb.askyesno.assert_not_called()


def test_check_worktree_health_handles_missing_projects_attr():
    """App.projects doesn't exist until the first refresh() — tolerate it."""
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(_cfg=cfg, _log=lambda m, c=None: None)
    with mock.patch("app.find_orphaned_worktrees", return_value=[]) as f:
        App._check_worktree_health(stub)
    f.assert_called_once_with([], "git")
