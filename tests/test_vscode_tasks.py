"""tests/test_vscode_tasks.py — the VS Code files the Manager generates.

These are files other tools execute and people commit, so the things pinned
here are the ones that fail quietly rather than loudly.

  * **`"type": "process"`, never `"shell"`.** A shell task re-parses its
    command line, so an exe under `D:\\Claude Co worker\\...` is split at the
    space. This project has already paid for that lesson once, as an upstream
    bug report reading `bash: D:/Claude: No such file or directory`.
  * **`--project "${workspaceFolder}"` on every task.** A task's working
    directory is not a statement about which project the user means; assuming
    it is, is the mistake behind the whole MCP scope investigation.
  * **No `checks` task for a packaged runner**, because it can only exit 3
    there — `sys.executable` under a onefile build is the extracted binary,
    not an interpreter.
  * **No machine paths and no MCP config in a `.code-workspace`.** It is a
    file people commit and copy between checkouts.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers.vscode_tasks import (
    FROZEN_UNSUPPORTED,
    TASKS,
    WORKSPACE_FOLDER,
    Runner,
    applicable_tasks,
    build_tasks_json,
    build_workspace_json,
    default_runner,
    goto_argv,
    preview_workspace,
    write_tasks_json,
    write_workspace_file,
)

#: A path with a space, which is the reference machine's actual layout.
SPACED = "D:/Claude Co worker/Token Save Manager Source/src/cli.py"

SOURCE_RUNNER = Runner(["python", SPACED])
FROZEN_RUNNER = Runner(["C:/Program Files/Manager/tokensave-manager-cli.exe"],
                       frozen=True)


def _tasks(runner=SOURCE_RUNNER) -> list:
    return json.loads(build_tasks_json(runner))["tasks"]


# ── the shell-quoting trap ──────────────────────────────────────────────────

def test_every_task_is_a_process_task_not_a_shell_task():
    for task in _tasks():
        assert task["type"] == "process", (
            f"{task['label']} is a shell task; a spaced path would be re-split")


def test_a_spaced_path_survives_as_one_argv_element():
    args = _tasks()[0]["args"]
    assert SPACED in args, "the spaced path was split or mangled"


def test_a_spaced_executable_survives_as_the_command():
    task = json.loads(build_tasks_json(FROZEN_RUNNER))["tasks"][0]
    assert task["command"] == FROZEN_RUNNER.argv[0]
    assert " " in task["command"], "this case only matters with a space in it"


# ── the project must always be explicit ─────────────────────────────────────

def test_every_task_passes_the_workspace_folder_explicitly():
    for task in _tasks():
        args = task["args"]
        assert "--project" in args, f"{task['label']} relies on the cwd"
        assert args[args.index("--project") + 1] == WORKSPACE_FOLDER


def test_no_task_relies_on_a_cwd_setting():
    """A task with its own `options.cwd` would reintroduce the inference."""
    for task in _tasks():
        assert "options" not in task or "cwd" not in task.get("options", {})


# ── frozen-runner filtering ─────────────────────────────────────────────────

def test_a_source_runner_gets_every_task():
    assert len(applicable_tasks(SOURCE_RUNNER)) == len(TASKS)


def test_a_frozen_runner_loses_exactly_the_unsupported_commands():
    commands = {t.command for t in applicable_tasks(FROZEN_RUNNER)}
    assert commands == {t.command for t in TASKS} - FROZEN_UNSUPPORTED
    assert "checks" not in commands


def test_the_unsupported_set_names_only_real_commands():
    """A typo here would silently stop filtering anything."""
    assert FROZEN_UNSUPPORTED <= {t.command for t in TASKS}


# ── the file itself ─────────────────────────────────────────────────────────

def test_tasks_json_declares_the_schema_version():
    assert json.loads(build_tasks_json(SOURCE_RUNNER))["version"] == "2.0.0"


def test_labels_are_unique_so_vs_code_can_address_them():
    labels = [t["label"] for t in _tasks()]
    assert len(labels) == len(set(labels))


def test_every_task_carries_a_detail_line():
    """The label is a name; `detail` is what the command palette shows."""
    for task in _tasks():
        assert task["detail"].strip()


def test_write_creates_the_vscode_directory(tmp_path):
    ok, message = write_tasks_json(str(tmp_path), SOURCE_RUNNER)
    assert ok, message
    written = tmp_path / ".vscode" / "tasks.json"
    assert written.is_file()
    json.loads(written.read_text(encoding="utf-8"))


def test_write_reports_what_a_frozen_runner_omitted(tmp_path):
    ok, message = write_tasks_json(str(tmp_path), FROZEN_RUNNER)
    assert ok
    assert "omitted" in message, "silently dropping a task would be worse"


def test_write_is_idempotent(tmp_path):
    write_tasks_json(str(tmp_path), SOURCE_RUNNER)
    first = (tmp_path / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    write_tasks_json(str(tmp_path), SOURCE_RUNNER)
    second = (tmp_path / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    assert first == second


# ── the runner is derived, not configured ───────────────────────────────────

def test_a_frozen_install_points_at_the_sibling_console_exe():
    runner = default_runner("C:/Program Files/Manager", frozen=True)
    assert runner.frozen is True
    assert runner.argv == ["C:/Program Files/Manager/tokensave-manager-cli.exe"]


def test_a_source_checkout_runs_cli_py_under_the_current_interpreter():
    runner = default_runner("D:/repo", frozen=False, python_exe="C:/py/python.exe")
    assert runner.frozen is False
    assert runner.argv == ["C:/py/python.exe", "D:/repo/src/cli.py"]


def test_a_source_checkout_falls_back_to_bare_python():
    assert default_runner("D:/repo", frozen=False).argv[0] == "python"


def test_the_derived_frozen_runner_drops_the_unsupported_task():
    """The derivation and the filtering must agree, or a frozen install
    generates a task that can only ever exit 3."""
    commands = {t.command
                for t in applicable_tasks(default_runner("C:/x", frozen=True))}
    assert "checks" not in commands


def test_a_derived_runner_survives_a_spaced_install_path():
    runner = default_runner("D:/Claude Co worker/Manager", frozen=True)
    task = json.loads(build_tasks_json(runner))["tasks"][0]
    assert task["command"] == "D:/Claude Co worker/Manager/tokensave-manager-cli.exe"
    assert task["type"] == "process"


# ── .code-workspace ─────────────────────────────────────────────────────────

def test_folders_are_relative_to_the_descriptor(tmp_path):
    out = tmp_path / "all.code-workspace"
    folders = [str(tmp_path / "repo-a"), str(tmp_path / "repo-b")]
    entries = preview_workspace(folders, str(out))
    assert [e["path"] for e in entries] == ["repo-a", "repo-b"]


def test_a_relative_workspace_survives_being_moved(tmp_path):
    """The portability point: copy the checkout, the descriptor still works."""
    out = tmp_path / "ws" / "all.code-workspace"
    folders = [str(tmp_path / "ws" / "repo-a")]
    entries = preview_workspace(folders, str(out))
    assert entries == [{"path": "repo-a"}]
    assert not os.path.isabs(entries[0]["path"])


def test_an_unreachable_folder_falls_back_to_absolute():
    """Windows offers no relative path across drives; absolute beats crashing."""
    entries = preview_workspace(["Z:/elsewhere/repo"], "C:/ws/all.code-workspace")
    assert entries[0]["path"] == "Z:/elsewhere/repo"


def test_the_workspace_carries_no_mcp_configuration(tmp_path):
    """Membership and MCP config are separate decisions - see Phase A."""
    out = tmp_path / "all.code-workspace"
    doc = json.loads(build_workspace_json([str(tmp_path / "a")], str(out)))
    assert set(doc) == {"folders", "settings"}
    blob = json.dumps(doc).lower()
    assert "mcp" not in blob and "tokensave" not in blob


def test_settings_pass_through_untouched(tmp_path):
    out = tmp_path / "all.code-workspace"
    doc = json.loads(build_workspace_json(
        [str(tmp_path / "a")], str(out), {"files.autoSave": "off"}))
    assert doc["settings"] == {"files.autoSave": "off"}


def test_writing_an_empty_workspace_is_refused(tmp_path):
    ok, message = write_workspace_file(str(tmp_path / "x.code-workspace"), [])
    assert not ok
    assert "nothing to write" in message


def test_the_preview_matches_what_gets_written(tmp_path):
    """The UI shows `preview_workspace`; it must not differ from the file."""
    out = tmp_path / "all.code-workspace"
    folders = [str(tmp_path / "a"), str(tmp_path / "b")]
    previewed = preview_workspace(folders, str(out))
    ok, _ = write_workspace_file(str(out), folders)
    assert ok
    assert json.loads(out.read_text(encoding="utf-8"))["folders"] == previewed


# ── jump-to-location ────────────────────────────────────────────────────────

def test_no_line_means_no_goto_flag():
    """`editor_cmd` is user-configurable and may not be VS Code."""
    assert goto_argv(["code"], "D:/x/y.py") == ["code", "D:/x/y.py"]


def test_a_line_produces_the_documented_goto_form():
    assert goto_argv(["code"], "D:/x/y.py", 42) == \
        ["code", "--goto", "D:/x/y.py:42"]


def test_a_column_is_appended_after_the_line():
    assert goto_argv(["code"], "D:/x/y.py", 42, 7) == \
        ["code", "--goto", "D:/x/y.py:42:7"]


def test_configured_editor_flags_are_preserved():
    argv = goto_argv(["code", "--new-window"], "D:/x/y.py", 3)
    assert argv[:2] == ["code", "--new-window"]


def test_the_path_stays_a_single_argv_element_even_with_spaces():
    argv = goto_argv(["code"], "D:/Claude Co worker/a b.py", 9)
    assert argv[-1] == "D:/Claude Co worker/a b.py:9"


@pytest.mark.parametrize("line", ["42", 42.0])
def test_a_non_integer_line_is_coerced_not_interpolated_raw(line):
    """Guards against `file.py:42.0` reaching the editor."""
    assert goto_argv(["code"], "x.py", line)[-1] == "x.py:42"
