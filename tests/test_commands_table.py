"""tests/test_commands_table.py — the single command vocabulary.

Four surfaces name the same operations: `cli.py`'s subcommands, the generated
VS Code tasks, the extension's command ids, and (next) the Manager IPC request
actions. They used to be four hand-maintained lists, which drift silently — a
label corrected in one and not the others yields two names for one operation
and nothing fails.

So these tests are about the table being *the* source rather than *a* source:
every consumer derives from it, the identifiers are unique, and the
classification partitions the commands with no gaps or overlaps.

The side-effect classes get particular attention, because the set they replaced
made a promise two of its members did not keep. Each class here was measured —
see `test_doctor_is_observe_refresh_because_it_was_measured` for the one that
changed the answer.
"""
from __future__ import annotations

import dataclasses

import pytest

import cli
from helpers import commands
from helpers.commands import (
    COMMANDS,
    MUTATING,
    OBSERVE_REFRESH,
    PURE_READ,
    SIDE_EFFECT_CLASSES,
    Command,
)
from helpers.vscode_tasks import TASKS


# ── Identity ──────────────────────────────────────────────────────────────

def test_actions_are_unique():
    actions = [c.action for c in COMMANDS]
    assert len(actions) == len(set(actions))


def test_cli_subcommands_are_unique():
    clis = [c.cli for c in COMMANDS if c.cli]
    assert len(clis) == len(set(clis))


def test_vscode_command_ids_are_unique_and_namespaced():
    ids = [c.vscode for c in COMMANDS if c.vscode]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("tokensaveManager.") for i in ids)


def test_every_command_has_a_label_and_a_detail():
    """Both reach a user; an empty one renders as a blank row somewhere."""
    for c in COMMANDS:
        assert c.label.strip(), c.action
        assert c.detail.strip(), c.action


# ── Classification ────────────────────────────────────────────────────────

def test_every_side_effect_class_is_known():
    for c in COMMANDS:
        assert c.side_effect in SIDE_EFFECT_CLASSES


def test_an_unknown_class_is_rejected_at_construction():
    """A typo must fail loudly rather than produce an unclassified command."""
    with pytest.raises(ValueError, match="unknown side-effect class"):
        Command(action="x", cli="x", vscode="", label="X", detail="X",
                side_effect="mostly_harmless")


def test_the_three_classes_partition_the_table():
    seen = set()
    for name in SIDE_EFFECT_CLASSES:
        members = {c.action for c in commands.by_side_effect(name)}
        assert not (members & seen), f"{name} overlaps an earlier class"
        seen |= members
    assert seen == {c.action for c in COMMANDS}


def test_by_side_effect_rejects_an_unknown_class():
    with pytest.raises(ValueError):
        commands.by_side_effect("read_only")     # the name that was removed


def test_doctor_is_observe_refresh_because_it_was_measured():
    """The classification that changed once it was actually checked.

    `doctor` never touches the project, which is the promise a user cares
    about. But it is not write-free: it rewrites `~/.tokensave/state.toml`
    with byte-identical content, moving only the mtime, while touching neither
    `global.db` nor its WAL — so a WAL-only probe would have cleared it. The
    write was isolated against an idle control run, ruling out a background
    MCP server.
    """
    assert commands.BY_ACTION["doctor"].side_effect == OBSERVE_REFRESH


def test_cost_is_not_a_pure_read():
    """`cost` and `discover` ingest rows into ~/.tokensave/global.db."""
    assert commands.BY_ACTION["cost"].side_effect == OBSERVE_REFRESH


def test_the_mutating_set_is_pinned_by_name():
    """Adding one here is a claim that the command changes real state.

    `request` writes into the project's own inbox, which is a state change even
    though nothing it queues can commit, apply or approve on its own.
    """
    assert {c.action for c in commands.by_side_effect(MUTATING)} == {
        "sync", "commit-request", "request"}


def test_pure_read_members_touch_nothing():
    """Pinned by name: adding one here is a claim that needs measuring.

    `focus` belongs despite calling `SetForegroundWindow`, because the class is
    defined as no *data* mutation and says so — an OS-level effect on the
    desktop is outside what this classification governs.
    """
    assert {c.action for c in commands.by_side_effect(PURE_READ)} == {
        "status", "checks", "scout", "tests", "test-gaps", "mcp-status",
        "commands", "focus", "graph-trust"}


# ── Consumers derive rather than restate ──────────────────────────────────

def test_cli_registry_matches_the_table():
    """Every CLI subcommand is in the table, and vice versa."""
    assert set(cli._COMMANDS) == {c.cli for c in COMMANDS if c.cli}


def test_cli_classification_sets_come_from_the_table():
    for name, group in ((PURE_READ, cli.PURE_READ_COMMANDS),
                        (OBSERVE_REFRESH, cli.OBSERVE_REFRESH_COMMANDS),
                        (MUTATING, cli.MUTATING_COMMANDS)):
        assert group == {c.cli for c in commands.by_side_effect(name) if c.cli}


def test_projectless_commands_are_derived_not_restated():
    assert cli.PROJECTLESS_COMMANDS == {
        c.cli for c in COMMANDS if c.cli and not c.requires_project}


def test_vscode_tasks_are_derived_from_the_table():
    assert [t.command for t in TASKS] == [c.cli for c in COMMANDS if c.task]
    for task, command in zip(TASKS, [c for c in COMMANDS if c.task]):
        assert task.label == f"Manager: {command.label}"
        assert task.detail == command.detail


def test_only_project_scoped_commands_become_tasks():
    """A task passes `--project ${workspaceFolder}`, so a project-less command
    would be invoked with an argument its parser rejects."""
    for command in COMMANDS:
        if command.task:
            assert command.requires_project, command.action


def test_accepts_paths_is_only_claimed_by_commands_that_will_support_it():
    assert {c.action for c in COMMANDS if c.accepts_paths} == {
        "checks", "test-gaps"}


# ── as_json: the wire form four surfaces read ─────────────────────────────

def test_as_json_carries_every_command_and_every_field():
    payload = commands.as_json()
    assert len(payload["commands"]) == len(COMMANDS)
    expected = {f.name for f in dataclasses.fields(Command)}
    for entry in payload["commands"]:
        assert set(entry) == expected


def test_as_json_explains_each_side_effect_class():
    """A consumer must be able to render what a class means, not just its name."""
    classes = commands.as_json()["side_effect_classes"]
    assert set(classes) == set(SIDE_EFFECT_CLASSES)
    assert all(text.strip() for text in classes.values())


def test_as_json_preserves_table_order():
    """Order is part of the contract: `commands.ts` is compared verbatim, so a
    reordering must be a deliberate regeneration rather than an invisible diff.
    """
    payload = commands.as_json()
    assert [e["action"] for e in payload["commands"]] == [
        c.action for c in COMMANDS]


# ── The generated TypeScript mirror ───────────────────────────────────────

def test_the_generated_typescript_matches_the_table():
    """The drift guard that keeps `commands.ts` genuinely generated.

    Without a test that fails on any difference, a checked-in "generated" file
    is just a hand-maintained file with a misleading header — and it drifts the
    first time someone edits the table and forgets the script.

    Run `python scripts/gen_commands_ts.py` when this fails.

    `read_text` rather than `read_bytes` is load-bearing: this repo is checked
    out with `core.autocrlf=true`, so the file on a Windows disk has CRLF while
    the generator emits LF. Text mode applies universal-newline translation and
    the comparison holds; "tightening" this to a byte compare would fail on
    every Windows checkout and pass in Linux CI.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    checked_in = (root / commands.TYPESCRIPT_PATH).read_text(encoding="utf-8")
    assert checked_in == commands.as_typescript(), (
        "vscode-extension/src/commands.ts is stale - "
        "run python scripts/gen_commands_ts.py")


def test_the_generator_is_deterministic():
    """No timestamps, no set iteration — a no-op regeneration is a no-op diff."""
    assert commands.as_typescript() == commands.as_typescript()


def test_the_generated_file_carries_every_command():
    generated = commands.as_typescript()
    for command in COMMANDS:
        assert f'action: "{command.action}"' in generated


def test_the_generated_file_warns_against_editing_it():
    assert "GENERATED" in commands.as_typescript().splitlines()[1]


def test_pure_read_excludes_os_ui_effects_in_writing():
    """`focus` will foreground a window, which is a side effect on the desktop
    but not on any data this classification governs. The distinction is stated
    in the class's own description so the label is not read as a stronger
    claim than it makes.
    """
    assert "OS-level UI effects" in commands.SIDE_EFFECT_MEANING[PURE_READ]


def test_a_taskable_command_can_actually_be_run_as_a_task():
    """`task=True` has to mean "launchable with no further input".

    The VS Code TaskProvider builds one task per `task=True` row, filling in
    `--project ${workspaceFolder}` and nothing else. A row that carried no CLI
    subcommand, or that did not take `--project`, would produce a task that
    cannot be constructed — so marking, say, `focus` as taskable should fail
    here rather than ship a menu entry that errors when clicked.
    """
    for command in COMMANDS:
        if not command.task:
            continue
        assert command.cli, (
            f"{command.action} is taskable but has no CLI subcommand")
        assert command.requires_project, (
            f"{command.action} is taskable but takes no --project; the task "
            "provider has nothing to pass it")


def test_only_test_run_selects_individual_tests():
    """`accepts_tests` is about running tests, `accepts_paths` about scoping
    a report to files. They are different questions and a row answering the
    first without being able to run anything would be a contradiction.
    """
    selectors = [c.action for c in COMMANDS if c.accepts_tests]
    assert selectors == ["test-run"], selectors


def test_the_generated_file_carries_the_new_selector_flag():
    """A flag the extension reads has to survive the round trip."""
    generated = commands.as_typescript()
    assert "acceptsTests: true," in generated
    assert "acceptsTests: boolean;" in generated
