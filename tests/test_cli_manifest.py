"""`commands --json` as the capability manifest: tool identity, flags, safety.

The table in `helpers/commands.py` already carried each command's side-effect
class and whether it needs a project. What it could not say is which flags a
command takes, so a caller had to read `--help`. These tests pin the additive
`tool` and `invocation` keys, and that they agree with the table and with the
parser they are derived from. Existing keys are guarded by
`test_commands_table.py` and `test_cli_cost.py`.
"""
from __future__ import annotations

import json

import cli
from cli import EXIT_OK, main
from constants import APP_VERSION
from helpers import commands


def _manifest(capsys) -> dict:
    code = main(["commands", "--json"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    return json.loads(out)["data"]


def _flags(entry: dict) -> dict:
    return {a["flag"]: a for a in entry["args"]}


def test_the_manifest_names_the_tool_and_its_versions(capsys):
    tool = _manifest(capsys)["tool"]
    assert tool == {"name": "manager-cli", "cli_version": APP_VERSION,
                    "schema_version": cli.SCHEMA_VERSION, "scope": "target"}


def test_every_cli_subcommand_is_described_and_nothing_else(capsys):
    invocation = _manifest(capsys)["invocation"]
    assert set(invocation) == set(cli._COMMANDS)
    assert set(invocation) == {c.cli for c in commands.COMMANDS if c.cli}


def test_unattended_safe_is_the_derived_set_not_a_second_list(capsys):
    invocation = _manifest(capsys)["invocation"]
    for name, entry in invocation.items():
        assert entry["unattended_safe"] == (name in cli.UNATTENDED_SAFE_COMMANDS)
    assert invocation["sync"]["unattended_safe"] is False
    assert invocation["commit-request"]["unattended_safe"] is False
    assert invocation["status"]["unattended_safe"] is True
    assert invocation["doctor"]["unattended_safe"] is True


def test_the_table_and_the_parser_agree_about_needing_a_project(capsys):
    """`requires_project` (table) and `--project` (parser) are stated in two
    places that nothing else compares; a disagreement would tell an editor to
    skip a flag the command then rejects."""
    invocation = _manifest(capsys)["invocation"]
    for c in commands.COMMANDS:
        if not c.cli:
            continue
        project = _flags(invocation[c.cli]).get("--project")
        assert (project is not None) == c.requires_project, c.cli
        if project:
            assert project["required"] is True


def test_the_table_and_the_parser_agree_about_paths_and_tests(capsys):
    invocation = _manifest(capsys)["invocation"]
    for c in commands.COMMANDS:
        if not c.cli:
            continue
        flags = _flags(invocation[c.cli])
        assert ("--paths" in flags) == c.accepts_paths, c.cli
        assert ("--tests" in flags) == c.accepts_tests, c.cli


def test_flag_rows_carry_kind_default_choices_and_multiplicity(capsys):
    invocation = _manifest(capsys)["invocation"]

    timeout = _flags(invocation["doctor"])["--timeout"]
    assert (timeout["kind"], timeout["default"]) == ("number", 120.0)

    assert _flags(invocation["sync"])["--force"]["kind"] == "flag"
    assert _flags(invocation["sync"])["--force"]["default"] is False

    rng = _flags(invocation["cost"])["--range"]
    assert rng["kind"] == "string" and "30d" in rng["choices"]

    paths = _flags(invocation["checks"])["--paths"]
    assert paths["multiple"] is True and paths["default"] == []

    markers = _flags(invocation["test-run"])["--markers"]
    assert markers["multiple"] is False and markers["help"]


def test_the_help_action_is_not_listed_as_a_flag(capsys):
    for entry in _manifest(capsys)["invocation"].values():
        assert "--help" not in _flags(entry)


def test_the_existing_manifest_keys_are_still_there(capsys):
    data = _manifest(capsys)
    for key in ("side_effect_classes", "commands", "manager_actions",
                "unexposed_manager_actions"):
        assert key in data
