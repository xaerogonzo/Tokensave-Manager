"""Tests for helpers/handoff_setup.py and the Projects tab's handoff entry.

A project takes part in handoff only when it has the rule; the machine-wide switch
is separate. The menu entry writes into a project, so what is checked is that it
asks first, offers the direction that is actually available, and says plainly when
the global switch is still off.
"""
import os
import subprocess
import types

import pytest

from controllers import handoff_project_ctrl as ctrl
from helpers import handoff_rules, handoff_setup


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "t")
    with open(os.path.join(root, "a.txt"), "w") as fh:
        fh.write("1")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


# -- the shared definition of the command ---------------------------------------------

def test_the_runner_uses_a_bare_python_not_the_guis_pythonw():
    runner, _config = handoff_setup.runner_and_config()
    assert runner[0] == "python"
    assert runner[1].replace("\\", "/").endswith("src/cli.py")


def test_the_config_defaults_to_the_managers_own_and_an_override_is_made_absolute():
    from constants import _CONFIG_PATH
    assert handoff_setup.runner_and_config()[1] == _CONFIG_PATH
    assert handoff_setup.runner_and_config("some.json")[1] == os.path.abspath("some.json")


def test_the_cli_and_the_windows_write_the_same_rule(tmp_path, repo):
    """One definition of the command: a rule written by either is current to both."""
    runner, config = handoff_setup.runner_and_config()
    assert handoff_setup.enable(repo).ok
    assert handoff_rules.status(repo, runner, config) == handoff_rules.STATE_CURRENT


# -- taking part ------------------------------------------------------------------------

def test_a_project_does_not_take_part_until_it_has_the_rule(repo):
    assert handoff_setup.takes_part(repo) is False
    assert handoff_setup.enable(repo).ok
    assert handoff_setup.takes_part(repo) is True


def test_a_stale_rule_still_counts_as_taking_part(repo, monkeypatch):
    handoff_setup.enable(repo)
    monkeypatch.setattr(handoff_setup, "runner_and_config",
                        lambda *a: (["python", "E:/moved/cli.py"], "E:/cfg.json"))
    assert handoff_rules.status(repo, *handoff_setup.runner_and_config()) == \
        handoff_rules.STATE_STALE
    assert handoff_setup.takes_part(repo) is True        # a refresh, not a withdrawal


def test_disabling_removes_only_the_rule_and_the_project_stops_taking_part(repo):
    handoff_setup.enable(repo)
    assert handoff_setup.disable(repo).ok
    assert handoff_setup.takes_part(repo) is False


def test_a_committed_rule_file_is_never_treated_as_opting_in(repo):
    with open(handoff_rules.rule_path(repo), "w") as fh:
        fh.write("team rules\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    assert handoff_setup.takes_part(repo) is False
    assert not handoff_setup.enable(repo).ok


# -- the menu entry ------------------------------------------------------------------------

class _Log:
    def __init__(self):
        self.lines, self.asked, self.errors = [], [], []


@pytest.fixture
def ui(monkeypatch):
    log = _Log()
    log.answer = True
    monkeypatch.setattr(ctrl.messagebox, "askyesno",
                        lambda title, msg, **k: log.asked.append((title, msg)) or log.answer)
    monkeypatch.setattr(ctrl.messagebox, "showerror",
                        lambda title, msg, **k: log.errors.append((title, msg)))
    log.on_log = lambda msg, colour=None: log.lines.append(msg)
    return log


def _cfg(enabled=True):
    return types.SimpleNamespace(git_exe="", enable_agent_handoff=enabled)


def test_the_label_offers_enable_for_a_project_without_the_rule_and_stop_for_one_with(repo):
    assert ctrl.label_for(_cfg(), repo) == ctrl.ON_LABEL
    handoff_setup.enable(repo)
    assert ctrl.label_for(_cfg(), repo) == ctrl.OFF_LABEL


def test_an_unreadable_project_offers_enable_and_never_raises():
    assert ctrl.label_for(_cfg(), "") == ctrl.ON_LABEL
    assert ctrl.label_for(_cfg(), "Z:/not/a/folder") == ctrl.ON_LABEL


def test_enabling_asks_first_and_writes_nothing_when_declined(repo, ui):
    ui.answer = False
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    assert ui.asked and not os.path.exists(handoff_rules.rule_path(repo))


def test_enabling_writes_the_rule_and_logs_it(repo, ui):
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    assert os.path.isfile(handoff_rules.rule_path(repo))
    assert any("added the handoff rule" in line for line in ui.lines)


def test_the_enable_prompt_says_what_is_written_and_that_git_never_sees_it(repo, ui):
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    title, text = ui.asked[0]
    assert title == "Enable agent handoff here"
    assert "CLAUDE.local.md" in text and "git ignores" in text
    assert "never touched" in text


def test_the_prompt_warns_when_the_machine_wide_switch_is_still_off(repo, ui):
    ctrl.toggle(None, _cfg(enabled=False), ui.on_log, repo)
    assert "switched OFF in Settings" in ui.asked[0][1]
    ui.asked.clear()
    ctrl.toggle(None, _cfg(enabled=True), ui.on_log, repo)
    # (the project is now enabled, so this asks to STOP, not to enable)
    assert "switched OFF" not in ui.asked[0][1]


def test_stopping_asks_first_and_removes_only_the_rule(repo, ui):
    handoff_setup.enable(repo)
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    assert ui.asked[0][0] == "Stop agent handoff here"
    assert not os.path.exists(handoff_rules.rule_path(repo))
    assert any("removed the handoff rule" in line for line in ui.lines)


def test_declining_to_stop_leaves_the_rule(repo, ui):
    handoff_setup.enable(repo)
    ui.answer = False
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    assert os.path.isfile(handoff_rules.rule_path(repo))


def test_a_refusal_is_shown_and_logged_not_swallowed(repo, ui):
    with open(handoff_rules.rule_path(repo), "w") as fh:
        fh.write("team\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    ctrl.toggle(None, _cfg(), ui.on_log, repo)
    assert ui.errors and "committed" in ui.errors[0][1]
    assert any("committed" in line for line in ui.lines)


def test_no_selected_project_is_a_no_op(ui):
    ctrl.toggle(None, _cfg(), ui.on_log, "")
    assert not ui.asked and not ui.lines
