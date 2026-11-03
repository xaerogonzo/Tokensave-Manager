"""The integration check, the presets, and the environment agents start in.

What matters: unknown is never a pass, a different CLI version is a dated
warning rather than a failure, only the hook event that was measured to break
Cursor is warned about, a preset is a diff that reports no-ops honestly and is
derived (never stored), and nothing here touches a file outside the Manager.
"""
import json
import os
import types

import pytest

from helpers import agent_cli
from helpers import agent_integration as integ
from helpers import agent_presets as presets
from helpers import cursor_hooks as ch


# -- the environment agents start in --------------------------------------------------

def test_a_git_bash_environment_is_stripped_for_agents(monkeypatch):
    monkeypatch.setattr(agent_cli.sys, "platform", "win32")
    env = agent_cli.agent_env({"PATH": "p", "SHELL": "/bin/bash.exe", "MSYSTEM": "MINGW64",
                               "BASH_ENV": "x", "bash_func_f%%": "y", "HOME": "h"})
    assert env == {"PATH": "p", "HOME": "h"}


def test_a_posix_shell_is_left_alone(monkeypatch):
    monkeypatch.setattr(agent_cli.sys, "platform", "linux")
    assert agent_cli.agent_env({"SHELL": "/bin/zsh"}) is None


def test_the_runners_actually_pass_the_cleaned_environment(monkeypatch, tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["env"] = kw.get("env", "MISSING")
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(agent_cli.subprocess, "run", run)
    monkeypatch.setattr(agent_cli.sys, "platform", "win32")
    monkeypatch.setenv("SHELL", "/bin/bash.exe")
    agent_cli.call_print(agent_cli.CURSOR, "agent.cmd", "hi")
    assert seen["env"] != "MISSING" and "SHELL" not in seen["env"]

    popen = {}
    monkeypatch.setattr(agent_cli.subprocess, "Popen",
                        lambda payload, **kw: popen.update(kw))
    agent_cli.spawn(agent_cli.CURSOR, "agent.cmd", str(tmp_path), "do it")
    assert "SHELL" not in popen["env"]


# -- versions -------------------------------------------------------------------------

def _probe(table):
    return lambda exe: table[exe]


def test_the_verified_version_passes_and_says_when():
    got = integ.check_versions({"claude": "c", "cursor": "k"},
                               _probe({"c": ("2.1.261", ""),
                                       "k": ("2026.10.22-27dcf5f", "")}))
    assert [c.state for c in got] == [integ.PASS, integ.PASS]
    assert "2026-10-31" in got[0].detail


def test_a_different_version_is_a_dated_warning_not_a_failure():
    got = integ.check_versions({"claude": "c"}, _probe({"c": ("2.2.0", "")}))
    assert got[0].state == integ.WARN
    assert "2.2.0" in got[0].detail and "2.1.261" in got[0].detail


def test_a_version_that_cannot_be_read_is_unknown_never_pass():
    got = integ.check_versions({"cursor": "k"}, _probe({"k": ("", "timed out")}))
    assert got[0].state == integ.UNKNOWN and "timed out" in got[0].detail


def test_an_agent_with_no_executable_is_not_checked():
    assert integ.check_versions({"claude": "", "cursor": ""}, _probe({})) == []


def test_a_build_hash_suffix_is_the_same_version_but_a_prefix_match_is_not():
    assert integ._same_version("2026.10.22-abc", "2026.10.22")
    assert not integ._same_version("2026.10.220", "2026.10.22")


def test_probe_version_never_raises_and_reports_why(tmp_path):
    exe = tmp_path / "nothing.exe"
    exe.write_text("not a program")
    version, why = integ.probe_version(str(exe))
    assert version == "" and why
    assert integ.probe_version(str(tmp_path / "missing.exe"))[0] == ""


# -- routing --------------------------------------------------------------------------

def _resolver(state_by_feature):
    def resolve(feature=""):
        state = state_by_feature.get(feature, agent_cli.RESOLUTION_OK)
        return agent_cli.AgentResolution(
            agent_cli.CLAUDE, "x" if state == agent_cli.RESOLUTION_OK else "", state,
            requested_id="bogus", feature=feature)
    return resolve


def test_routing_all_runnable_is_one_pass():
    got = integ.check_routing(_resolver({}))
    assert [c.state for c in got] == [integ.PASS]


def test_an_unknown_agent_is_a_failure_and_a_missing_one_a_warning():
    got = integ.check_routing(_resolver({"": agent_cli.RESOLUTION_UNKNOWN_AGENT,
                                         "review": agent_cli.RESOLUTION_UNAVAILABLE}))
    assert {c.state for c in got} == {integ.FAIL, integ.WARN}


# -- the hooks in ~/.claude/settings.json ---------------------------------------------

def _settings(tmp_path, hooks):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return str(path)


def _entry(command, args=None):
    e = {"type": "command", "command": command}
    if args:
        e["args"] = args
    return {"hooks": [e]}


@pytest.fixture
def spaced_exe(tmp_path):
    folder = tmp_path / "Token Save"
    folder.mkdir()
    exe = folder / "tokensave.exe"
    exe.write_text("")
    return str(exe)


def test_a_quoted_one_string_hook_passes(tmp_path, spaced_exe):
    path = _settings(tmp_path, {"PreToolUse": [_entry(f'"{spaced_exe}" hook-pre-tool-use')]})
    assert integ.check_claude_hooks(path, cursor_present=True).state == integ.PASS


def test_a_bare_spaced_path_fails_for_both_agents(tmp_path, spaced_exe):
    path = _settings(tmp_path, {"Stop": [_entry(spaced_exe)]})
    got = integ.check_claude_hooks(path, cursor_present=False)
    assert got.state == integ.FAIL and "Stop" in got.detail


def test_the_args_form_on_pretooluse_warns_only_when_cursor_is_present(tmp_path, spaced_exe):
    path = _settings(tmp_path, {"PreToolUse": [_entry(spaced_exe, ["hook-pre-tool-use"])]})
    assert integ.check_claude_hooks(path, cursor_present=True).state == integ.WARN
    assert integ.check_claude_hooks(path, cursor_present=False).state == integ.PASS


def test_the_args_form_on_other_events_is_noted_not_warned(tmp_path, spaced_exe):
    """Only PreToolUse was measured to break anything; a nag about the rest
    would be about an effect nobody has seen."""
    path = _settings(tmp_path, {"Stop": [_entry(spaced_exe, ["hook-stop"])]})
    got = integ.check_claude_hooks(path, cursor_present=True)
    assert got.state == integ.PASS and "no effect observed" in got.detail


def test_non_tokensave_hooks_are_not_this_checks_business(tmp_path):
    path = _settings(tmp_path, {"PostToolUse": [_entry("C:\\a b\\py.exe", ["x.py"])]})
    assert "no tokensave hooks" in integ.check_claude_hooks(path, True).detail


def test_an_unreadable_settings_file_is_unknown(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{broken")
    assert integ.check_claude_hooks(str(bad), True).state == integ.UNKNOWN
    assert integ.check_claude_hooks(str(tmp_path / "absent.json"), True).state == integ.PASS


def test_restore_quotes_pretooluse_args_form_and_leaves_other_events(tmp_path, spaced_exe):
    from helpers import claude_pretooluse as cpt
    path = _settings(tmp_path, {
        "PreToolUse": [_entry(spaced_exe, ["hook-pre-tool-use"])],
        "Stop": [_entry(spaced_exe, ["hook-stop"])],
    })
    got = cpt.restore_quoted_pretooluse(path)
    assert got.ok and got.changed and os.path.exists(got.backup)
    data = json.loads(open(path, encoding="utf-8").read())
    pre = data["hooks"]["PreToolUse"][0]["hooks"][0]
    assert "args" not in pre
    assert pre["command"] == '"%s" hook-pre-tool-use' % spaced_exe
    stop = data["hooks"]["Stop"][0]["hooks"][0]
    assert stop.get("args") == ["hook-stop"]
    assert integ.check_claude_hooks(path, cursor_present=True).state == integ.PASS


def test_restore_is_a_no_op_when_pretooluse_is_already_quoted(tmp_path, spaced_exe):
    from helpers import claude_pretooluse as cpt
    path = _settings(tmp_path, {
        "PreToolUse": [_entry('"%s" hook-pre-tool-use' % spaced_exe)],
    })
    before = open(path, encoding="utf-8").read()
    got = cpt.restore_quoted_pretooluse(path)
    assert got.ok and not got.changed and not got.backup
    assert open(path, encoding="utf-8").read() == before


def test_restore_refuses_malformed_settings(tmp_path):
    from helpers import claude_pretooluse as cpt
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    got = cpt.restore_quoted_pretooluse(str(path))
    assert not got.ok and "unreadable" in got.error
    assert path.read_text(encoding="utf-8") == "{broken"


# -- the Cursor session-end hook -------------------------------------------------------

def test_cursor_hook_states_against_the_handoff_switch():
    f = integ.check_cursor_hook
    assert f(False, ch.STATE_ABSENT) is None
    assert f(True, ch.STATE_ABSENT).state == integ.WARN
    assert f(True, ch.STATE_CURRENT).state == integ.PASS
    assert f(False, ch.STATE_CURRENT).state == integ.PASS
    assert f(True, ch.STATE_STALE).state == integ.WARN
    assert f(True, ch.STATE_UNREADABLE).state == integ.UNKNOWN


# -- the whole set -----------------------------------------------------------------------

class _Cfg:
    def __init__(self, raw=None):
        self.raw = raw or {}
        self.claude_cli_exe = ""
        self.cursor_cli_exe = ""

    def resolve_agent_cli(self, feature=""):
        return agent_cli.AgentResolution(agent_cli.CLAUDE, "x", agent_cli.RESOLUTION_OK)


def test_run_checks_composes_and_never_raises(tmp_path):
    got = integ.run_checks(_Cfg(), home=str(tmp_path))
    assert [c.id for c in got] == ["routing", "claude_hooks"]
    assert all(c.state == integ.PASS for c in got)


def test_a_check_that_blows_up_becomes_unknown_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(integ, "check_routing",
                        lambda resolve: (_ for _ in ()).throw(RuntimeError("boom")))
    got = integ.run_checks(_Cfg(), home=str(tmp_path))
    assert any(c.id == "internal" and c.state == integ.UNKNOWN and "boom" in c.detail
               for c in got)


def test_worst_orders_fail_over_unknown_over_warn_over_pass():
    assert integ.worst([integ.PASS, integ.WARN, integ.UNKNOWN]) == integ.UNKNOWN
    assert integ.worst([integ.PASS, integ.FAIL]) == integ.FAIL
    assert integ.worst([]) == integ.PASS


# -- presets ----------------------------------------------------------------------------------

def test_detection_is_derived_from_the_values():
    assert presets.detect({}) == "claude_only"
    assert presets.detect({"agent_cli": "claude", "agent_overrides": {}}) == "claude_only"
    assert presets.detect({"enable_agent_handoff": True}) == "claude_and_cursor"
    assert presets.detect({"agent_cli": "cursor"}) == presets.CUSTOM
    assert presets.detect({"agent_overrides": {"review": "cursor"}}) == presets.CUSTOM


def test_an_explicitly_off_handoff_is_the_same_as_absent():
    assert presets.detect({"enable_agent_handoff": False}) == "claude_only"
    assert presets.detect({"enable_agent_handoff": "true"}) == "claude_only"   # not a literal True


def test_a_preset_that_already_matches_changes_nothing():
    assert presets.plan({}, "claude_only") == []
    assert presets.plan({"enable_agent_handoff": True}, "claude_and_cursor") == []


def test_claude_only_does_not_add_keys_to_an_install_that_never_had_them():
    raw = {}
    assert presets.apply(raw, "claude_only") == [] and raw == {}


def test_the_plan_lists_old_and_new_and_apply_makes_exactly_those_changes():
    raw = {"agent_cli": "cursor", "agent_overrides": {"review": "cursor"},
           "enable_agent_handoff": True, "unrelated": 1}
    shown = [c.describe() for c in presets.plan(raw, "claude_only")]
    assert shown == ["agent_cli: 'cursor' -> 'claude'",
                     "agent_overrides: {'review': 'cursor'} -> (not set)",
                     "enable_agent_handoff: True -> (not set)"]
    presets.apply(raw, "claude_only")
    assert raw == {"agent_cli": "claude", "unrelated": 1}


def test_claude_and_cursor_leaves_the_default_agent_and_routing_alone():
    raw = {"agent_cli": "cursor", "agent_overrides": {"review": "claude"}}
    presets.apply(raw, "claude_and_cursor")
    assert raw == {"agent_cli": "cursor", "agent_overrides": {"review": "claude"},
                   "enable_agent_handoff": True}


def test_a_preset_cannot_reach_outside_the_managers_own_config():
    allowed = {"agent_cli", "agent_overrides", "enable_agent_handoff"}
    for preset in presets.PRESETS:
        assert {k for k, _ in preset.settings} <= allowed


def test_at_most_one_preset_matches_any_config():
    for raw in ({}, {"enable_agent_handoff": True}, {"agent_cli": "cursor"},
                {"enable_agent_handoff": True, "agent_cli": "cursor"}):
        assert sum(presets.matches(raw, p) for p in presets.PRESETS) <= 1


# -- the Settings panel, against a real page --------------------------------------------------

@pytest.mark.tk
def test_the_panel_shows_the_derived_label_and_applies_into_the_form_only(
        tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    mock_config.raw.update({"agent_cli": "cursor", "enable_agent_handoff": True,
                            "agent_overrides": {"review": "cursor"}})
    ctl = _build(tk_root, mock_config, mocker)
    panel = ctl._ai._integration
    assert "Custom" in panel._current.cget("text") or \
        "Claude and Cursor" in panel._current.cget("text")

    mocker.patch("dialogs.settings_integration.messagebox.askyesno", return_value=True)
    panel._choice.set("Claude only")
    panel._apply()
    # Applied to the FORM...
    assert ctl._ai._var_agent_cli.get() == "claude"
    assert ctl._ai._var_enable_agent_handoff.get() is False
    assert ctl._ai._var_override_review.get() == "inherit"
    assert "Claude only" in panel._current.cget("text")
    # ...and not to the saved config, until Settings is saved.
    assert mock_config.raw["agent_cli"] == "cursor"
    assert mock_config.raw["enable_agent_handoff"] is True
    ctl._save()
    assert mock_config.raw["agent_cli"] == "claude"
    assert "agent_overrides" not in mock_config.raw


@pytest.mark.tk
def test_declining_the_confirmation_changes_nothing(tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    mock_config.raw["agent_cli"] = "cursor"
    ctl = _build(tk_root, mock_config, mocker)
    panel = ctl._ai._integration
    mocker.patch("dialogs.settings_integration.messagebox.askyesno", return_value=False)
    panel._choice.set("Claude only")
    panel._apply()
    assert ctl._ai._var_agent_cli.get() == "cursor"


@pytest.mark.tk
def test_choosing_a_preset_in_the_combobox_alone_is_not_an_edit(tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    ctl = _build(tk_root, mock_config, mocker)
    ctl._ai._integration._choice.set("Claude and Cursor")
    ctl._ai._integration._describe()
    assert ctl.is_dirty() is False
    # ...whereas applying it into the form is.
    mocker.patch("dialogs.settings_integration.messagebox.askyesno", return_value=True)
    ctl._ai._integration._apply()
    assert ctl.is_dirty() is True


@pytest.mark.tk
def test_the_check_renders_every_state_with_its_own_glyph(tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    ctl = _build(tk_root, mock_config, mocker)
    panel = ctl._ai._integration
    panel._show([(integ.PASS, "a"), (integ.WARN, "b"), (integ.FAIL, "c"), (integ.UNKNOWN, "d")])
    text = panel._out.get("1.0", "end")
    for glyph in ("\u2713 a", "\u26a0 b", "\u2717 c", "? d"):
        assert glyph in text


# The Settings page starts PyScope's own status probe on a thread, and `_build`
# stubs subprocess.run to raise, so that unrelated thread dies loudly while this
# test waits. It is not the panel's thread.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.tk
def test_pressing_check_runs_off_thread_and_prints_every_finding(
        tk_root, mock_config, mocker, wait_for):
    from tests.test_settings_tab import _build
    ctl = _build(tk_root, mock_config, mocker)
    panel = ctl._ai._integration
    mocker.patch("dialogs.settings_integration.integ.run_checks", return_value=[
        integ.Check("a", "alpha", integ.PASS, "fine"),
        integ.Check("b", "beta", integ.WARN, "drifted", "do this")])
    panel._check()
    assert panel._running is True            # a second press while running is ignored
    wait_for(lambda: panel._running is False)
    text = panel._out.get("1.0", "end")
    assert "✓ alpha: fine" in text and "⚠ beta: drifted" in text
    assert "do this" in text and "SAVED settings" in text


# -- Doctor ------------------------------------------------------------------------------------

def _doctor_log(monkeypatch, checks):
    from controllers.doctor_ctrl import DoctorController
    monkeypatch.setattr(integ, "run_checks", lambda cfg: checks)
    lines = []
    stub = types.SimpleNamespace(_cfg=None, _on_log=lambda text, colour=None: lines.append(text))
    DoctorController._log_agent_integration(stub)
    return lines


def test_doctor_is_silent_when_every_integration_check_passes(monkeypatch):
    assert _doctor_log(monkeypatch, [integ.Check("a", "alpha", integ.PASS, "ok")]) == []


def test_doctor_prints_only_what_is_not_passing_with_its_fix(monkeypatch):
    lines = _doctor_log(monkeypatch, [
        integ.Check("a", "alpha", integ.PASS, "ok"),
        integ.Check("b", "beta", integ.WARN, "drifted", "re-run it"),
        integ.Check("c", "gamma", integ.UNKNOWN, "could not tell")])
    joined = "\n".join(lines)
    assert "alpha" not in joined
    assert "[warn] beta: drifted  -> re-run it" in joined
    assert "[unknown] gamma" in joined


def _restore_calls(monkeypatch, cfg, checks=None):
    from controllers.doctor_ctrl import DoctorController
    from helpers import claude_pretooluse as cpt
    called = []

    def fake_restore(path=""):
        called.append(path)
        return cpt.RestoreResult(ok=True)

    monkeypatch.setattr(cpt, "restore_quoted_pretooluse", fake_restore)
    monkeypatch.setattr(integ, "run_checks", lambda _cfg: checks or [])
    lines = []
    stub = types.SimpleNamespace(
        _cfg=cfg, _on_log=lambda text, colour=None: lines.append(text))
    DoctorController._log_agent_integration(stub)
    return called, lines


def test_doctor_restores_quoted_pretooluse_only_when_cursor_is_configured(monkeypatch):
    called, _lines = _restore_calls(
        monkeypatch, types.SimpleNamespace(cursor_cli_exe="C:/cursor-agent.exe"))
    assert len(called) == 1


def test_doctor_skips_pretooluse_restore_without_a_cursor_cli(monkeypatch):
    called, _lines = _restore_calls(
        monkeypatch, types.SimpleNamespace(cursor_cli_exe=""))
    assert called == []
