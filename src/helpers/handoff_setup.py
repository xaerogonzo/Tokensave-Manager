"""Opt one project in or out of agent handoff, for the Manager's own windows.

Two switches, kept apart on purpose. `enable_agent_handoff` in the config is the
machine-wide one (Settings -> AI). A project TAKES PART only when it also has the
handoff rule (`CLAUDE.local.md`), which is what tells an agent the command exists
and what the Cursor hook checks before it records anything. So the machine-wide
switch alone changes nothing about any project, and a rule alone does nothing
while the switch is off: the CLI refuses in both cases.

This module is the one place that answers "what command does the rule name",
shared with `manager-cli handoff --action rule`, so the GUI and the CLI cannot
write two different rules.
"""

from __future__ import annotations

import os

from helpers import handoff_rules, vscode_tasks


def runner_and_config(config_override: str = "") -> "tuple[list, str]":
    """`(runner argv, config path)` as this Manager would write them.

    The interpreter is a bare `python`, not `sys.executable`: the GUI runs under
    `pythonw`, which has no console, so a command copied from it would print
    nothing for an agent to read.
    """
    from constants import _BASE_DIR, _CONFIG_PATH
    frozen = bool(os.environ.get("NUITKA_ONEFILE_PARENT"))
    runner = vscode_tasks.default_runner(_BASE_DIR, frozen, "python").argv
    config = (os.path.abspath(os.path.expanduser(config_override))
              if config_override else _CONFIG_PATH)
    return runner, config


def takes_part(project: str, git_exe: str = "") -> bool:
    """Has this project been opted in? True when its rule is present (current or
    stale: a moved Manager is a thing to refresh, not a withdrawal of consent)."""
    runner, config = runner_and_config()
    return handoff_rules.status(project, runner, config, git_exe) in (
        handoff_rules.STATE_CURRENT, handoff_rules.STATE_STALE)


def enable(project: str, git_exe: str = "") -> handoff_rules.RuleResult:
    runner, config = runner_and_config()
    return handoff_rules.install(project, runner, config, git_exe)


def disable(project: str, git_exe: str = "") -> handoff_rules.RuleResult:
    return handoff_rules.remove(project, git_exe)
