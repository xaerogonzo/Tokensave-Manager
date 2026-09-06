"""tests/test_dialog_action_gates.py — what each dialog does before it acts.

`RetrofitDialog`, `ScaffoldDialog`, `WorkspaceBuilderDialog`,
`AICodeReviewDialog` and `HousekeepingDialog`. Five dialogs with no test,
each with a rule about *when the action is allowed to happen at all*:

* retrofit **silently does nothing** if every option is unticked, while
  scaffold always calls back. That asymmetry is deliberate — retrofit writes
  into someone else's project — and it is exactly the kind of difference a
  later "consistency" cleanup erases;
* the workspace planner has no plan without a target, and must not read a
  descriptor that is not there;
* an AI review result carries a **token**, and a result whose token no longer
  matches is a reply to a question the user already replaced. Rendering it
  puts a stale review on screen under the current diff;
* housekeeping disables actions while a scan is in flight and refuses to
  re-scan mid-action, so findings cannot be pulled out from under an
  operation.

``object.__new__`` plus the attributes each method reads.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.ai_code_review import AICodeReviewDialog
from dialogs.housekeeping import HousekeepingDialog
from dialogs.retrofit import RetrofitDialog
from dialogs.scaffold import ScaffoldDialog
from dialogs.workspace_builder import WorkspaceBuilderDialog


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _Recorder:
    def __init__(self):
        self.calls: list = []
        self.kwargs: list = []
        self.destroyed = False

    def callback(self, *args, **kw):
        self.calls.append(args)
        self.kwargs.append(kw)

    def destroy(self):
        self.destroyed = True


# ── RetrofitDialog ──────────────────────────────────────────────────────

def _retrofit(**flags):
    rec = _Recorder()
    dlg = object.__new__(RetrofitDialog)
    dlg.var_ts = _Var(flags.get("ts", False))
    dlg.var_bi = _Var(flags.get("bi", False))
    dlg.var_nuitka = _Var(flags.get("nuitka", False))
    dlg.var_shadow = _Var(flags.get("shadow", False))
    dlg.var_hook = _Var(flags.get("hook", False))
    dlg.path = "C:/other-project"
    dlg.callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_retrofit_does_nothing_when_every_option_is_unticked():
    """It writes into a project that is not this one — doing nothing is the
    correct response to being asked for nothing."""
    dlg, rec = _retrofit()
    dlg._apply()
    assert rec.calls == []
    assert rec.destroyed, "the dialog still closes; it just does not act"


def test_retrofit_acts_when_any_single_option_is_ticked():
    dlg, rec = _retrofit(shadow=True)
    dlg._apply()
    assert rec.calls == [("C:/other-project", False, False, False, True)]
    assert rec.kwargs == [{"add_git_hook": False}]


def test_retrofit_passes_the_hook_flag_by_keyword():
    """It is keyword-only at the callsite; a positional would land in
    `shadow` and silently enable the wrong thing."""
    dlg, rec = _retrofit(ts=True, hook=True)
    dlg._apply()
    assert rec.kwargs == [{"add_git_hook": True}]


# ── ScaffoldDialog ──────────────────────────────────────────────────────

def _scaffold(**flags):
    rec = _Recorder()
    dlg = object.__new__(ScaffoldDialog)
    dlg._bi_var = _Var(flags.get("bi", False))
    dlg._init_var = _Var(flags.get("init", False))
    dlg._nuitka_var = _Var(flags.get("nuitka", False))
    dlg._hook_var = _Var(flags.get("hook", False))
    dlg.path = "C:/new-project"
    dlg.callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_scaffold_calls_back_even_with_nothing_ticked():
    """Deliberately unlike retrofit: the controller decides what an
    all-false scaffold means. Pinned so the two are not "harmonised"."""
    dlg, rec = _scaffold()
    dlg._apply()
    assert rec.calls == [("C:/new-project",)]
    assert rec.kwargs == [{"create_bi": False, "run_init": False,
                           "scaffold_nuitka": False, "add_git_hook": False}]


def test_scaffold_forwards_every_option_by_keyword():
    dlg, rec = _scaffold(bi=True, init=True, nuitka=True, hook=True)
    dlg._apply()
    assert rec.kwargs == [{"create_bi": True, "run_init": True,
                           "scaffold_nuitka": True, "add_git_hook": True}]


# ── WorkspaceBuilderDialog ──────────────────────────────────────────────

def _builder(rows, target):
    dlg = object.__new__(WorkspaceBuilderDialog)
    dlg._rows = [(_Var(ticked), path) for ticked, path in rows]
    dlg._target = _Var(target)
    return dlg


def test_selected_returns_only_ticked_projects():
    dlg = _builder([(True, "a"), (False, "b"), (True, "c")], "w.code-workspace")
    assert dlg._selected() == ["a", "c"]


def test_there_is_no_plan_without_a_target():
    """The preview reads this; returning a plan for "" would render a
    workspace nobody chose a location for."""
    dlg = _builder([(True, "a")], "   ")
    assert dlg._plan() is None


def test_a_target_that_does_not_exist_yet_plans_from_scratch(monkeypatch, tmp_path):
    """`read_workspace` is only consulted for a file that is actually there —
    a brand-new descriptor must not be treated as an unreadable one."""
    seen: list = []
    monkeypatch.setattr("dialogs.workspace_builder.read_workspace",
                        lambda p: seen.append(p))
    monkeypatch.setattr("dialogs.workspace_builder.plan_workspace_merge",
                        lambda existing, selected, target:
                        {"existing": existing, "selected": selected})
    target = str(tmp_path / "new.code-workspace")
    plan = _builder([(True, "a")], target)._plan()
    assert plan["existing"] is None
    assert seen == [], "read_workspace must not be called for a missing file"


def test_an_existing_target_is_read_before_planning(monkeypatch, tmp_path):
    target = tmp_path / "existing.code-workspace"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("dialogs.workspace_builder.read_workspace",
                        lambda p: {"folders": []})
    monkeypatch.setattr("dialogs.workspace_builder.plan_workspace_merge",
                        lambda existing, selected, target:
                        {"existing": existing, "selected": selected})
    plan = _builder([(True, "a")], str(target))._plan()
    assert plan["existing"] == {"folders": []}


# ── AICodeReviewDialog._on_review_ready ─────────────────────────────────

class _Widget:
    def __init__(self):
        self.states: list = []

    def configure(self, **kw):
        self.states.append(kw)

    def delete(self, *_a):
        pass

    def insert(self, *_a):
        pass


def _review(token, current_token, cancelled=False):
    dlg = object.__new__(AICodeReviewDialog)
    dlg._review_token = current_token
    dlg._cancelled = cancelled
    dlg._stop_btn = _Widget()
    dlg._regen_btn = _Widget()
    dlg._copy_btn = _Widget()
    dlg._rev_txt = _Widget()
    dlg._llm_cfg = {"provider": "ollama", "model": "qwen"}
    dlg._last_review = None
    dlg.rendered: list = []
    dlg._render_review = lambda text: dlg.rendered.append(text)
    dlg._show_status = lambda *a, **kw: None
    return dlg, token


def test_a_stale_review_result_is_discarded():
    """The user regenerated; this is a reply to the previous question.

    Rendering it would put a review of an older diff on screen with no sign
    that it is out of date.
    """
    dlg, token = _review(token=1, current_token=2)
    dlg._on_review_ready(token, "some review")
    assert dlg.rendered == []
    assert dlg._last_review is None


def test_a_cancelled_review_is_discarded_even_with_a_matching_token():
    dlg, token = _review(token=3, current_token=3, cancelled=True)
    dlg._on_review_ready(token, "some review")
    assert dlg.rendered == []


def test_an_empty_result_reports_failure_rather_than_rendering_nothing():
    """An empty string is a failed call, not a review with no findings."""
    dlg, token = _review(token=3, current_token=3)
    dlg._on_review_ready(token, "")
    assert dlg.rendered == []
    assert dlg._last_review is None


def test_a_current_result_is_rendered_and_remembered():
    dlg, token = _review(token=3, current_token=3)
    dlg._on_review_ready(token, "the review")
    assert dlg.rendered == ["the review"]
    assert dlg._last_review == "the review"


# ── HousekeepingDialog._set_state / refresh ─────────────────────────────

def _housekeeping(scan_state="", action_state=""):
    from dialogs.housekeeping import ACT_NONE, STATE_READY
    dlg = object.__new__(HousekeepingDialog)
    dlg._scan_state = scan_state or STATE_READY
    dlg._action_state = action_state or ACT_NONE
    dlg._rescan_btn = _Widget()
    dlg._action_buttons = [_Widget(), _Widget()]
    return dlg


def _last_state(widget):
    return widget.states[-1]["state"]


def test_actions_are_enabled_when_idle():
    dlg = _housekeeping()
    dlg._set_state()
    assert all(_last_state(b) == tk.NORMAL for b in dlg._action_buttons)
    assert _last_state(dlg._rescan_btn) == tk.NORMAL


def test_actions_are_disabled_while_a_scan_is_running():
    """The findings on screen are about to be replaced."""
    from dialogs.housekeeping import STATE_SCANNING
    dlg = _housekeeping()
    dlg._set_state(scan_state=STATE_SCANNING)
    assert all(_last_state(b) == tk.DISABLED for b in dlg._action_buttons)


def test_rescan_is_disabled_while_an_action_is_running():
    """Otherwise a re-scan could pull results out from under an operation."""
    dlg = _housekeeping()
    dlg._set_state(action_state="purging")
    assert _last_state(dlg._rescan_btn) == tk.DISABLED
    assert all(_last_state(b) == tk.DISABLED for b in dlg._action_buttons)


def test_refresh_is_a_no_op_while_an_action_is_in_flight():
    """The guard that stops a scan starting underneath a running purge."""
    dlg = _housekeeping(action_state="purging")
    called: list = []
    dlg._ctrl = type("C", (), {"scan_async": lambda *a: called.append(a)})()
    dlg._path = "C:/repo"
    dlg._say = lambda *a, **kw: None
    dlg._render_placeholder = lambda *a: None
    dlg.refresh()
    assert called == []


def test_a_dead_action_button_does_not_break_the_state_update():
    """Widgets are destroyed on re-render; a stale one must not strand the
    rest of the bar in whatever state it was last in."""
    class _Dead:
        def configure(self, **kw):
            raise tk.TclError("destroyed")

    dlg = _housekeeping()
    live = _Widget()
    dlg._action_buttons = [_Dead(), live]
    dlg._set_state()
    assert _last_state(live) == tk.NORMAL


# ── Git tab: opening a CLI session is not a git operation ─────────────────
# `btn_claude_cli` was added to `_git_all_btns` -- the convenient list -- and
# inherited its `is_repo` gate, so it was disabled on exactly the projects
# with no repository yet. Its own tooltip sells it as "no need to open a
# terminal and cd there yourself", which is the case it was refusing. Found
# when a user had to grant MCP trust in several folders, three of which were
# not git repos, and the button that exists to do that was greyed out.


def _git_ctl(in_flight=False):
    from controllers.git_tab import GitTabController

    ctl = object.__new__(GitTabController)
    ctl._git_op_in_flight = in_flight
    ctl._git_all_btns = [_Widget()]
    ctl._git_project_btns = [_Widget()]
    ctl._git_push_pull_btns = []
    ctl._git_release_btns = []
    return ctl


def test_the_cli_button_is_available_on_a_project_with_no_repo():
    from controllers.git_tab import _update_button_states

    ctl = _git_ctl()
    _update_button_states(ctl, is_repo=False, remote="", has_project=True)

    assert _last_state(ctl._git_project_btns[0]) == tk.NORMAL, (
        "the Claude CLI button is disabled on a non-repo project, which is "
        "the one case it is most needed -- granting MCP trust in a folder "
        "that is not a git repository"
    )
    assert _last_state(ctl._git_all_btns[0]) == tk.DISABLED, (
        "the genuinely git-gated buttons must still be disabled; ungating one "
        "button must not ungate the rest"
    )


def test_the_cli_button_needs_a_selected_project():
    from controllers.git_tab import _update_button_states

    ctl = _git_ctl()
    _update_button_states(ctl, is_repo=False, remote="", has_project=False)
    assert _last_state(ctl._git_project_btns[0]) == tk.DISABLED


def test_an_in_flight_git_operation_still_disables_everything():
    """The one gate that must keep covering it: a run in progress."""
    from controllers.git_tab import _update_button_states

    ctl = _git_ctl(in_flight=True)
    _update_button_states(ctl, is_repo=True, remote="origin", has_project=True)
    assert _last_state(ctl._git_project_btns[0]) == tk.DISABLED
    assert _last_state(ctl._git_all_btns[0]) == tk.DISABLED
