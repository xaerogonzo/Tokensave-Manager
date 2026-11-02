"""Repairing what `tokensave install --agent cursor` leaves behind, and keeping
the Claude-memory snapshots from going quietly stale.

The belief under the repair (a quoted program is called with `&` in the
PowerShell Cursor evaluates hooks through) is tested against a real PowerShell
where there is one, because a unit test that only checks a string would pass
whether or not Windows can run it.
"""
import json
import os
import sys

import pytest

from helpers import agent_integration as integ
from helpers import cursor_hooks as ch
from helpers import cursor_hooks_repair as rep
from helpers import memory_snapshot as ms

EXE = "D:/Claude Co worker/Token Save/tokensave.exe"
BARE_PRE = f"{EXE} hook-pre-tool-use"
OURS = "python ./hooks/tokensave_session_marker.py"
THEIRS = "D:/My Tools/lint tool.exe --fast"


def _hooks_json(*, quoted=False):
    q = (lambda c: f'& "{EXE}"' + c[len(EXE):]) if quoted else (lambda c: c)
    return json.dumps({"version": 1, "hooks": {
        "sessionEnd": [{"command": OURS, "timeout": 30}],
        "preToolUse": [{"command": q(BARE_PRE), "matcher": "Grep|Shell"},
                       {"command": THEIRS}],
        "stop": [{"command": q(f"{EXE} hook-stop")}],
    }}, indent=2) + "\n"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".cursor").mkdir(parents=True)
    return str(h)


def _write(home, text):
    with open(ch.hooks_json_path(home), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _read(home):
    with open(ch.hooks_json_path(home), encoding="utf-8", newline="") as fh:
        return fh.read()


# -- recognising the shapes ----------------------------------------------------------

def test_a_spaced_unquoted_exe_is_the_broken_shape():
    assert rep.shape_of(BARE_PRE) == rep.SHAPE_BARE


def test_quoted_or_ampersand_forms_are_fine():
    assert rep.shape_of(f'& "{EXE}" hook-stop') == rep.SHAPE_QUOTED
    assert rep.shape_of(f'"{EXE}" hook-stop') == rep.SHAPE_QUOTED


def test_a_path_without_spaces_or_a_bare_name_is_not_ours_to_fix():
    assert rep.shape_of("D:/Tools/tokensave.exe hook-stop") == rep.SHAPE_OTHER
    assert rep.shape_of("tokensave hook-stop") == rep.SHAPE_OTHER


def test_requote_is_exact():
    assert rep.requote(BARE_PRE) == f'& "{EXE}" hook-pre-tool-use'
    assert rep.requote(f"{EXE}") == f'& "{EXE}"'
    assert rep.requote("tokensave hook-stop") == "tokensave hook-stop"


# -- scanning ------------------------------------------------------------------------

def test_scan_reports_only_tokensave_hooks():
    hooks, error = rep.scan(_hooks_json())
    assert not error
    assert {h.event for h in hooks} == {"preToolUse", "stop"}
    assert THEIRS not in [h.command for h in hooks] and OURS not in [h.command for h in hooks]
    assert {h.event for h in hooks if h.shape == rep.SHAPE_BARE} == {"preToolUse", "stop"}


def test_a_file_that_does_not_parse_is_an_error_never_a_clean_scan():
    hooks, error = rep.scan("{ not json")
    assert hooks == [] and "not valid JSON" in error


# -- the pure plan -------------------------------------------------------------------

def test_plan_quotes_only_the_broken_tokensave_commands():
    plan = rep.plan_repair(_hooks_json())
    assert plan.ok and {f.event for f in plan.fixes} == {"preToolUse", "stop"}
    data = json.loads(plan.new_text)
    assert data["hooks"]["preToolUse"][0]["command"] == f'& "{EXE}" hook-pre-tool-use'
    assert data["hooks"]["preToolUse"][0]["matcher"] == "Grep|Shell"


def test_plan_leaves_every_other_hook_untouched():
    data = json.loads(rep.plan_repair(_hooks_json()).new_text)
    assert data["hooks"]["sessionEnd"] == [{"command": OURS, "timeout": 30}]
    assert data["hooks"]["preToolUse"][1] == {"command": THEIRS}      # spaced, not ours


def test_plan_is_idempotent():
    again = rep.plan_repair(rep.plan_repair(_hooks_json()).new_text)
    assert again.ok and not again.changed


def test_plan_refuses_a_file_that_does_not_parse():
    plan = rep.plan_repair("{ nope")
    assert not plan.ok and not plan.changed


# -- doing it ------------------------------------------------------------------------

def test_repair_backs_up_byte_for_byte_then_writes_and_tests_each_command(home):
    original = _hooks_json()
    _write(home, original)
    seen = []

    def runner(command):
        seen.append(command)
        return 0, "ok"
    done = rep.repair(home, now_ms=111, runner=runner)
    assert done.ok and done.changed and len(done.fixes) == 2
    assert open(done.backup, encoding="utf-8", newline="").read() == original
    assert sorted(seen) == sorted(f.new for f in done.fixes)
    assert json.loads(_read(home))["hooks"]["stop"][0]["command"].startswith('& "')


def test_a_command_that_does_not_run_rolls_the_whole_file_back(home):
    original = _hooks_json()
    _write(home, original)
    done = rep.repair(home, now_ms=1, runner=lambda c: (1, "boom"))
    assert not done.ok and done.rolled_back
    assert _read(home) == original
    assert os.path.exists(done.backup)
    assert "put back" in done.error


def test_one_failing_command_among_passing_ones_still_rolls_back(home):
    original = _hooks_json()
    _write(home, original)
    done = rep.repair(home, now_ms=1, runner=lambda c: (0 if "hook-stop" in c else 2, ""))
    assert not done.ok and _read(home) == original


def test_nothing_to_repair_writes_nothing_and_makes_no_backup(home):
    _write(home, _hooks_json(quoted=True))
    done = rep.repair(home, runner=lambda c: pytest.fail("must not run"))
    assert done.ok and not done.changed and done.backup == ""
    assert not [n for n in os.listdir(os.path.dirname(ch.hooks_json_path(home))) if ".backup." in n]


def test_a_file_that_does_not_parse_is_left_exactly_as_found(home):
    _write(home, "{ nope")
    done = rep.repair(home, runner=lambda c: (0, ""))
    assert not done.ok and _read(home) == "{ nope"


def test_no_hooks_file_is_not_an_error(home):
    assert rep.repair(home).ok


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell is how Cursor evaluates hooks on Windows")
def test_the_quoted_form_really_runs_in_powershell_and_the_bare_one_really_does_not(tmp_path):
    folder = tmp_path / "dir with space"
    folder.mkdir()
    script = folder / "probe.cmd"
    script.write_text("@echo off\r\nexit /b 0\r\n")
    exe_path = str(script).replace("\\", "/")
    assert rep.run_command(f'& "{exe_path}" hook-x')[0] == 0
    assert rep.run_command(f"{exe_path} hook-x")[0] != 0


# -- the check and the snapshot survey -----------------------------------------------

def test_the_check_fails_on_a_bare_hook_and_names_the_repair(home):
    _write(home, _hooks_json())
    check = integ.check_cursor_tokensave_hooks(home)
    assert check.state == integ.FAIL and "Repair Cursor hooks" in check.fix


def test_the_check_passes_when_quoted_and_is_silent_without_hooks(home):
    _write(home, _hooks_json(quoted=True))
    assert integ.check_cursor_tokensave_hooks(home).state == integ.PASS
    _write(home, json.dumps({"hooks": {"sessionEnd": [{"command": OURS}]}}))
    assert integ.check_cursor_tokensave_hooks(home) is None


def test_the_check_is_unknown_not_pass_when_the_file_cannot_be_read(home):
    _write(home, "{ nope")
    assert integ.check_cursor_tokensave_hooks(home).state == integ.UNKNOWN


def _project(tmp_path, home, name, index="# Memory Index\n- [a](a.md) — one\n"):
    p = tmp_path / name
    p.mkdir()
    if index is not None:
        folder = ms.memory_dir(str(p), home)
        os.makedirs(folder)
        open(os.path.join(folder, "MEMORY.md"), "w", encoding="utf-8").write(index)
    return str(p)


def test_survey_classifies_and_leaves_out_projects_with_nothing_to_say(tmp_path, home):
    fresh = _project(tmp_path, home, "fresh")
    stale = _project(tmp_path, home, "stale")
    absent = _project(tmp_path, home, "absent")
    nothing = _project(tmp_path, home, "nothing", index=None)
    ms.write(fresh, home)
    ms.write(stale, home)
    open(os.path.join(ms.memory_dir(stale, home), "MEMORY.md"), "a", encoding="utf-8").write("- [b](b.md) — two\n")
    rows = ms.survey([fresh, stale, absent, nothing], home)
    assert {r.name: r.state for r in rows} == {
        "fresh": ms.STATE_CURRENT, "stale": ms.STATE_STALE, "absent": ms.STATE_ABSENT}
    assert [r.name for r in ms.stale(rows)] == ["stale"]
    assert [r.name for r in ms.missing(rows)] == ["absent"]


def test_refresh_retakes_the_stale_snapshot_and_makes_it_current(tmp_path, home):
    p = _project(tmp_path, home, "p")
    ms.write(p, home)
    open(os.path.join(ms.memory_dir(p, home), "MEMORY.md"), "a", encoding="utf-8").write("- [b](b.md) — two\n")
    rows = ms.survey([p], home)
    results = ms.refresh(ms.stale(rows), home)
    assert results[0][1].ok
    assert ms.status(p, home) == ms.STATE_CURRENT


def test_a_refused_project_does_not_stop_the_others(tmp_path, home):
    a = _project(tmp_path, home, "a")
    b = _project(tmp_path, home, "b")
    ms.write(a, home)
    ms.write(b, home)
    for p in (a, b):
        open(os.path.join(ms.memory_dir(p, home), "MEMORY.md"), "a", encoding="utf-8").write("- [x](x.md) — new\n")
    os.remove(os.path.join(ms.memory_dir(a, home), "MEMORY.md"))     # a's source is gone
    results = dict((r.name, res) for r, res in ms.refresh(ms.survey([a, b], home), home))
    assert not results["a"].ok and results["b"].ok


def test_the_snapshot_check_warns_on_stale_and_says_missing_is_optional(tmp_path, home):
    stale = _project(tmp_path, home, "stale")
    absent = _project(tmp_path, home, "absent")
    ms.write(stale, home)
    open(os.path.join(ms.memory_dir(stale, home), "MEMORY.md"), "a", encoding="utf-8").write("- [b](b.md) — two\n")
    check = integ.check_snapshots(ms.survey([stale, absent], home))
    assert check.state == integ.WARN and "Refresh snapshots" in check.fix
    ms.refresh(ms.stale(ms.survey([stale], home)), home)
    ok = integ.check_snapshots(ms.survey([stale, absent], home))
    assert ok.state == integ.PASS and "optional" in ok.detail
    assert integ.check_snapshots([]) is None


# -- the panel -----------------------------------------------------------------------

# The Settings page starts PyScope's status probe on a thread and `_build` stubs
# subprocess.run to raise, so that thread dies loudly; it is not this panel's.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.tk
def test_the_repair_button_shows_the_diff_asks_and_repairs(tk_root, mock_config, mocker, wait_for, tmp_path, monkeypatch):
    from tests.test_settings_tab import _build
    home = tmp_path / "h"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    _write(str(home), _hooks_json())
    ctl = _build(tk_root, mock_config, mocker)
    panel = ctl._ai._integration
    asked = mocker.patch("dialogs.settings_integration.messagebox.askyesno", return_value=True)
    real = rep.repair
    mocker.patch("dialogs.settings_integration.hooks_repair.repair",
                 side_effect=lambda: real(runner=lambda c: (0, "ok")))
    panel._repair_hooks()
    shown = asked.call_args[0][1]
    assert "- " in shown and "hook-pre-tool-use" in shown and "backup" in shown
    wait_for(lambda: "Repaired" in panel._out.get("1.0", "end"))
    assert json.loads(_read(str(home)))["hooks"]["stop"][0]["command"].startswith('& "')


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.tk
def test_declining_the_diff_changes_nothing(tk_root, mock_config, mocker, tmp_path, monkeypatch):
    from tests.test_settings_tab import _build
    home = tmp_path / "h2"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    original = _hooks_json()
    _write(str(home), original)
    ctl = _build(tk_root, mock_config, mocker)
    mocker.patch("dialogs.settings_integration.messagebox.askyesno", return_value=False)
    ctl._ai._integration._repair_hooks()
    assert _read(str(home)) == original
