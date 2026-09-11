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
from controllers import startup_checks_ctrl
from controllers.startup_checks_ctrl import (
    StartupChecksController as StartupChecks,
)


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


# ── _log (marshals to the Tk thread via UiPumpMixin._post) ──────────────────────────

def test_log_inserts_message_via_post():
    log_widget = mock.MagicMock()
    # _post(fn, *args) → run it immediately (simulates the pump draining).
    stub = SimpleNamespace(log=log_widget,
                           _post=lambda fn, *a: fn(*a))
    App._log(stub, "hello", "red")
    # The message (with newline) was inserted with a colour tag.
    args = log_widget.insert.call_args[0]
    assert args[1] == "hello\n"
    assert args[2] == "col_red"


def test_log_defaults_colour_tag():
    log_widget = mock.MagicMock()
    stub = SimpleNamespace(log=log_widget, _post=lambda fn, *a: fn(*a))
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
        _get_project_list=lambda: [{"path": "/proj", "name": "proj",
                                   "has_git": True}],
        _cfg=cfg,
        _log=lambda m, c=None: logged.append(m))
    orphans = [{"worktree_path": "/proj/wt1", "branch": "feature1",
               "head": "abc12345", "project_path": "/proj",
               "project_name": "proj"}]
    with mock.patch("controllers.startup_checks_ctrl.find_orphaned_worktrees", return_value=orphans):
        StartupChecks._check_worktree_health(stub)
    assert any("1 git worktree" in m for m in logged)
    assert any("feature1" in m and "/proj/wt1" in m for m in logged)


def test_check_worktree_health_forwards_the_project_list():
    """The argument, not just the logging.

    When this moved off `App` it kept reading the list as
    `getattr(self, "projects", [])`, which on any other object returns the
    default forever -- the check would have scanned ZERO projects, silently.
    Every existing test here mocks `find_orphaned_worktrees`, so a wrong
    argument sailed straight through all of them. Assert the argument.
    """
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    projects = [{"path": "/a", "name": "a", "has_git": True}]
    stub = SimpleNamespace(_get_project_list=lambda: projects, _cfg=cfg,
                           _log=lambda m, c=None: None)
    with mock.patch("controllers.startup_checks_ctrl.find_orphaned_worktrees",
                    return_value=[]) as f:
        StartupChecks._check_worktree_health(stub)
    f.assert_called_once_with(projects, "git")


def test_check_worktree_health_silent_when_clean():
    logged = []
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(_get_project_list=lambda: [], _cfg=cfg,
                           _log=lambda m, c=None: logged.append(m))
    with mock.patch("controllers.startup_checks_ctrl.find_orphaned_worktrees", return_value=[]):
        StartupChecks._check_worktree_health(stub)
    assert logged == []


def test_check_worktree_health_never_opens_a_dialog():
    """Detect-only at startup — Doctor is the action surface, not this."""
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(
        _get_project_list=lambda: [{"path": "/proj", "name": "proj",
                                   "has_git": True}],
        _cfg=cfg, _log=lambda m, c=None: None)
    orphans = [{"worktree_path": "/proj/wt1", "branch": "b",
               "head": "1234", "project_path": "/proj", "project_name": "proj"}]
    with mock.patch("controllers.startup_checks_ctrl.find_orphaned_worktrees", return_value=orphans):
        StartupChecks._check_worktree_health(stub)
    # Stronger than patching a dialog module and asserting no call: this
    # module never imports one, so a prompt here would be a NameError.
    assert not hasattr(startup_checks_ctrl, "messagebox")


def test_check_worktree_health_handles_missing_projects_attr():
    """App.projects doesn't exist until the first refresh() — tolerate it."""
    cfg = mock.MagicMock()
    cfg.git_exe = "git"
    stub = SimpleNamespace(_cfg=cfg, _log=lambda m, c=None: None,
                           _get_project_list=lambda: [])
    with mock.patch("controllers.startup_checks_ctrl.find_orphaned_worktrees", return_value=[]) as f:
        StartupChecks._check_worktree_health(stub)
    f.assert_called_once_with([], "git")


# ── Manager-source change banner (Roadmap-9 Phase 2.4) ──────────────────────

def test_banner_appears_when_source_changed():
    stub = SimpleNamespace(
        _src_root="/proj/src", _src_baseline={}, _src_banner_dismissed=False,
        nb=object(), after=lambda *a: None, shown=None,
        _SRC_CHECK_MS=60_000, _check_source_changed=None)
    stub._show_source_banner = lambda ch: setattr(stub, "shown", ch)
    with mock.patch("app.snapshot_sources", return_value={"/proj/src/a.py": (1, 2)}), \
         mock.patch("app.changed_files", return_value=["/proj/src/a.py"]):
        App._check_source_changed(stub)
    assert stub.shown == ["/proj/src/a.py"]


def test_banner_stays_hidden_when_nothing_changed():
    stub = SimpleNamespace(
        _src_root="/proj/src", _src_baseline={}, _src_banner_dismissed=False,
        nb=object(), after=lambda *a: None, shown=None,
        _SRC_CHECK_MS=60_000, _check_source_changed=None)
    stub._show_source_banner = lambda ch: setattr(stub, "shown", ch)
    with mock.patch("app.snapshot_sources", return_value={}), \
         mock.patch("app.changed_files", return_value=[]):
        App._check_source_changed(stub)
    assert stub.shown is None


def test_dismissed_banner_does_not_come_back():
    """Re-raising on every later edit would nag through the exact session
    where the user has already decided to restart when convenient."""
    stub = SimpleNamespace(
        _src_root="/proj/src", _src_baseline={}, _src_banner_dismissed=True,
        nb=object(), after=lambda *a: None, shown=None,
        _SRC_CHECK_MS=60_000, _check_source_changed=None)
    stub._show_source_banner = lambda ch: setattr(stub, "shown", ch)
    with mock.patch("app.snapshot_sources", return_value={"a": (1, 2)}), \
         mock.patch("app.changed_files", return_value=["/proj/src/a.py"]):
        App._check_source_changed(stub)
    assert stub.shown is None


def test_check_reschedules_itself_even_when_the_scan_raises():
    """The timer must survive a transient filesystem error, or the feature
    silently stops working for the rest of the session."""
    scheduled = []
    stub = SimpleNamespace(
        _src_root="/proj/src", _src_baseline={}, _src_banner_dismissed=False,
        nb=object(), after=lambda ms, fn: scheduled.append(ms),
        _SRC_CHECK_MS=60_000, _check_source_changed=None,
        _show_source_banner=lambda ch: None)
    with mock.patch("app.snapshot_sources", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            App._check_source_changed(stub)
    assert scheduled, "the next check was never scheduled"


def test_banner_text_names_the_changed_files():
    captured = {}
    stub = SimpleNamespace(
        _src_root="/proj/src",
        _src_banner=SimpleNamespace(winfo_ismapped=lambda: False,
                                    pack=lambda **kw: None),
        _src_banner_lbl=SimpleNamespace(
            configure=lambda **kw: captured.update(kw)),
        nb=object())
    with mock.patch("app.describe_changes", return_value="helpers/git.py"):
        App._show_source_banner(stub, ["/proj/src/helpers/git.py"])
    assert "helpers/git.py" in captured["text"]
    assert "restart" in captured["text"].lower()


def test_dismiss_hides_and_latches():
    hidden = []
    stub = SimpleNamespace(
        _src_banner_dismissed=False,
        _src_banner=SimpleNamespace(pack_forget=lambda: hidden.append(1)))
    App._dismiss_source_banner(stub)
    assert stub._src_banner_dismissed is True
    assert hidden == [1]


# ── Startup ordering: the pump exists before anything can post to it ─────────

class TestStartupOrdering:
    """`App.__init__` must start its UI pump before it starts any worker.

    Asserted against the source rather than a constructed App: building the
    real window pulls in the whole controller graph, and the property at stake
    is purely an ordering one that a reader should be able to check too.

    The defect this guards was live until 2026-09-08 and had a comment next to
    it claiming the opposite — "Started before _build so nothing can post into
    a queue that is not being drained" sat directly above a call that ran after
    both `_build()` and `_update_poller.start()`. A comment is not a guard.
    """

    def _init_source(self):
        import inspect
        import app as app_mod
        return inspect.getsource(app_mod.App.__init__)

    def _line_of(self, src, needle):
        for i, line in enumerate(src.splitlines()):
            if needle in line and not line.strip().startswith("#"):
                return i
        raise AssertionError(f"{needle!r} not found in App.__init__")

    def test_the_pump_starts_before_the_update_poller(self):
        src = self._init_source()
        pump = self._line_of(src, "_start_ui_pump()")
        poller = self._line_of(src, "_update_poller.start()")
        assert pump < poller, (
            "App starts a worker before its UI pump exists. A poster on that "
            "thread hits a queue that is not there, dies where nobody can see "
            "it, and the message is lost."
        )

    def test_the_pump_starts_before_the_widgets_are_built(self):
        """_build wires callbacks that workers reach; the channel comes first."""
        src = self._init_source()
        assert self._line_of(src, "_start_ui_pump()") < self._line_of(src, "self._build()")

    def test_the_guard_can_still_say_no(self):
        """The scan finds real lines, so a passing run is not an empty one."""
        src = self._init_source()
        assert self._line_of(src, "_start_ui_pump()") >= 0
        with pytest.raises(AssertionError):
            self._line_of(src, "_a_call_that_is_not_there()")
