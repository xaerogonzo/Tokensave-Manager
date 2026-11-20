"""`manager-cli release-guide`: the Wizard's pre-flight and plan as one envelope.

git and gh are the scripted `World` from test_release_flow. The point is the contract: exit code
follows the verdict (0 ready, 1 blocked, 4 not confirmed), the plan is REPORTED and never run, and
the envelope carries the same facts the panel shows.
"""
from __future__ import annotations

import json

import pytest

import cli
import cli_release_command as crc
from helpers import release_flow as rfl
from test_release_flow import TAG, TIP, World, project  # noqa: F401


def _run(capsys, project, world, *extra):
    crc_env = world.env()
    orig = crc._env_for
    crc._env_for = lambda _config: crc_env
    try:
        code = cli.main(["release-guide", "--project", str(project), "--json", *extra])
    finally:
        crc._env_for = orig
    return code, json.loads(capsys.readouterr().out)


def test_ready_tip_exits_zero_and_lists_the_checks(capsys, project):
    code, env = _run(capsys, project, World())
    assert code == 0 and env["command"] == "release-guide"
    assert env["data"]["verdict"] == "ready" and env["data"]["state"]["tag"] == TAG
    assert {c["id"] for c in env["data"]["checks"]} >= {"version", "sync", "tag_state", "release_state", "ci"}
    assert "plan" not in env["data"]


def test_a_failing_check_exits_one(capsys, project):
    red = rfl.RunResult(json.dumps([{"workflowName": "ci", "status": "completed", "conclusion": "failure", "url": "u"}]), "", 0)
    code, env = _run(capsys, project, World({("gh", "run", "list"): red}))
    assert code == 1 and env["data"]["verdict"] == "blocked"


def test_an_unreadable_source_is_not_a_zero(capsys, project):
    broken = rfl.RunResult("", "not logged in", 4)
    code, env = _run(capsys, project, World({("gh", "run", "list"): broken}))
    assert code == 4 and env["data"]["verdict"] == "unconfirmed"


def test_plan_stage_reports_commands_and_runs_none(capsys, project):
    world = World()
    code, env = _run(capsys, project, world, "--stage", "plan")
    plan = env["data"]["plan"]
    assert [s["command"] for s in plan["steps"]] == [
        f'git tag -a {TAG} {TIP} -m "Release 0.11.0"', f"git push origin refs/tags/{TAG}"]
    assert [s["needs_typed_confirmation"] for s in plan["steps"]] == [False, True]
    assert not world.ran("git", "tag") and not world.ran("git", "push")


def test_post_stage_judges_the_draft(capsys, project):
    world = World({("gh", "release", "view"): rfl.RunResult("", "release not found", 1)})
    code, env = _run(capsys, project, world, "--stage", "post")
    ids = {c["id"]: c["status"] for c in env["data"]["post"]["checks"]}
    assert ids["draft"] == "warn" and code == 4


def test_an_explicit_tag_is_the_one_judged(capsys, project):
    _, env = _run(capsys, project, World(), "--tag", "v0.11.1")
    assert env["data"]["state"]["tag"] == "v0.11.1"


def test_the_command_is_in_the_vocabulary_as_observe_refresh():
    from helpers import commands
    entry = next(c for c in commands.COMMANDS if c.cli == "release-guide")
    assert entry.side_effect == commands.OBSERVE_REFRESH and entry.requires_project and entry.vscode == ""
