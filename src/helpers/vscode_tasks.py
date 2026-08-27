"""helpers/vscode_tasks.py — the VS Code project files the Manager writes.

Two generators, both following the repo's pure-builder + IO-wrapper pattern
(`helpers/ci_workflow.py` is the model): `.vscode/tasks.json`, which surfaces
the headless CLI as VS Code tasks, and a `.code-workspace` descriptor for the
multi-root case.

Four decisions here are load-bearing, and each comes from something this
project measured rather than assumed.

**Tasks use ``"type": "process"``, never ``"shell"``.** A shell task re-parses
the command line, so an interpreter or exe living under a path with spaces —
``D:\\Claude Co worker\\...`` on the reference machine — gets split at the
space. That exact failure has already cost this project one upstream bug
report (`bash: D:/Claude: No such file or directory`). A process task passes
argv through verbatim.

**Every task passes ``--project "${workspaceFolder}"``.** The CLI requires it,
and the reason is the same one that produced the MCP scope collision: a task's
working directory is not a reliable statement about which project the user
means. VS Code substitutes the variable, so the value is always explicit and
always right for the folder the task was launched from.

**A frozen runner does not get a ``checks`` task.** `checks` shells out to
``sys.executable -m pyflakes``, which under a Nuitka onefile build is the
extracted binary rather than an interpreter. Offering a task that can only ever
exit 3 is worse than not offering it.

**The workspace file carries folders and settings only — never MCP config.**
Workspace *membership* and MCP *server configuration* are separate decisions,
and Phase A measured that a project's own root `.mcp.json` already serves both
the Claude Code extension and Copilot. Writing MCP config here would duplicate
that, in a second place, silently.

No Tk. Safe to call from any thread.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess

from constants import CREATE_NO_WINDOW

#: Commands the packaged CLI cannot run. See the module docstring.
FROZEN_UNSUPPORTED = frozenset({"checks"})

#: VS Code substitutes this to the folder the task was launched from.
WORKSPACE_FOLDER = "${workspaceFolder}"


@dataclasses.dataclass(frozen=True)
class Runner:
    """How a task invokes the Manager's CLI.

    ``argv`` is a full prefix — ``["python", "<repo>/src/cli.py"]`` in a source
    checkout, or ``["<install>/tokensave-manager-cli.exe"]`` for the packaged
    build. Kept as a list because the split between command and arguments is
    VS Code's concern, not the caller's.
    """
    argv: list
    frozen: bool = False

    @property
    def command(self) -> str:
        return self.argv[0]

    @property
    def prefix_args(self) -> list:
        return list(self.argv[1:])


@dataclasses.dataclass(frozen=True)
class TaskSpec:
    """One entry in the generated tasks.json."""
    label: str
    command: str                  # the CLI subcommand
    detail: str
    extra_args: tuple = ()


#: The task catalogue. Deliberately small: the Manager stays the rich UI and
#: these are the operations worth reaching without leaving the editor.
TASKS: tuple = (
    TaskSpec("Manager: Doctor", "doctor",
             "Scan for stale tokensave entries. Read-only - never applies a fix."),
    TaskSpec("Manager: Sync tokensave", "sync",
             "Refresh shadow links, then re-index this project."),
    TaskSpec("Manager: Test gaps", "test-gaps",
             "Suggest tests for what changed against the base ref."),
    TaskSpec("Manager: MCP status", "mcp-status",
             "Report the MCP binding for this project, layer by layer."),
    TaskSpec("Manager: Run checks", "checks",
             "Syntax + pyflakes over src/."),
    TaskSpec("Manager: Pending commit request", "commit-request",
             "Show the commit request waiting for approval in the Manager."),
)


def applicable_tasks(runner: Runner) -> list:
    """The tasks this runner can actually perform.

    Filtering here rather than at write time so a caller can preview the same
    list the file will contain.
    """
    if not runner.frozen:
        return list(TASKS)
    return [t for t in TASKS if t.command not in FROZEN_UNSUPPORTED]


def build_tasks_json(runner: Runner, tasks: "list | None" = None) -> str:
    """Render tasks.json. Pure — no filesystem access.

    ``--json`` is deliberately NOT passed: without it the CLI also writes its
    one-line human summary to stderr, which is what makes the task readable in
    the terminal. The JSON envelope still goes to stdout, so the same command
    remains usable programmatically.
    """
    entries = []
    for spec in (applicable_tasks(runner) if tasks is None else tasks):
        entries.append({
            "label": spec.label,
            "detail": spec.detail,
            "type": "process",          # never "shell" - see the module docstring
            "command": runner.command,
            "args": runner.prefix_args + [spec.command,
                                          "--project", WORKSPACE_FOLDER,
                                          *spec.extra_args],
            "problemMatcher": [],
            "presentation": {
                "reveal": "always",
                "panel": "shared",
                "clear": True,
            },
        })
    return json.dumps({"version": "2.0.0", "tasks": entries},
                      indent=2) + "\n"


def write_tasks_json(project_path: str, runner: Runner) -> tuple:
    """Write ``<project>/.vscode/tasks.json``. Returns (ok, message).

    Overwrites: the file is generated, and a caller wanting to preserve hand
    edits should diff first — the Manager's proposal flow exists for that.
    """
    vscode_dir = os.path.join(project_path, ".vscode")
    try:
        os.makedirs(vscode_dir, exist_ok=True)
    except OSError as exc:
        return False, f"could not create {vscode_dir}: {exc}"

    out_path = os.path.join(vscode_dir, "tasks.json")
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(build_tasks_json(runner))
    except OSError as exc:
        return False, f"could not write {out_path}: {exc}"

    n = len(applicable_tasks(runner))
    skipped = "" if not runner.frozen else \
        f" ({len(TASKS) - n} omitted - unavailable in the packaged CLI)"
    return True, f"written: .vscode/tasks.json - {n} task(s){skipped}"


def default_runner(base_dir: str, frozen: bool,
                   python_exe: str = "") -> Runner:
    """The Runner for however this Manager is currently installed.

    Derived rather than configured, deliberately. Phase D's brief was to add a
    Settings knob only if measurement showed one was needed, and it is not: the
    Manager already knows whether it is frozen and where it lives, so a knob
    here would be a second source of truth for a question that has one answer.

    Frozen installs point at the sibling console exe (see
    docs/ARCHITECTURE.md on why that is a separate build target); a source
    checkout runs `src/cli.py` under the interpreter running the Manager.
    """
    if frozen:
        exe = os.path.join(base_dir, "tokensave-manager-cli.exe")
        return Runner([exe.replace("\\", "/")], frozen=True)
    cli_py = os.path.join(base_dir, "src", "cli.py")
    return Runner([python_exe or "python", cli_py.replace("\\", "/")],
                  frozen=False)


# ── .code-workspace ──────────────────────────────────────────────────────────

def _folder_entry(folder: str, anchor_dir: str) -> dict:
    """One `folders` entry, relative to the descriptor where possible.

    An absolute path bakes this machine's layout into a file people may commit
    or copy — the same portability rule that keeps `_canonical_project_entry`
    from interpolating a project root. Falls back to absolute only when no
    relative path exists (a different drive on Windows).
    """
    try:
        rel = os.path.relpath(folder, anchor_dir)
    except ValueError:                       # different drive
        return {"path": folder.replace("\\", "/")}
    return {"path": rel.replace("\\", "/")}


def preview_workspace(folders: list, out_path: str) -> list:
    """Exactly what `write_workspace_file` would put in `folders`.

    Exists so the UI can show the list before writing. The Manager's registry
    of known projects is a discovery result, not an assertion that those repos
    belong in one workspace, and a blind generation across every search root
    produces a workspace of unrelated code.
    """
    anchor = os.path.dirname(os.path.abspath(out_path))
    return [_folder_entry(f, anchor) for f in folders]


def build_workspace_json(folders: list, out_path: str,
                         settings: "dict | None" = None) -> str:
    """Render a `.code-workspace` descriptor. Pure.

    Folders and settings only. See the module docstring on why MCP config is
    deliberately absent.
    """
    return json.dumps({
        "folders": preview_workspace(folders, out_path),
        "settings": dict(settings or {}),
    }, indent=2) + "\n"


def write_workspace_file(out_path: str, folders: list,
                         settings: "dict | None" = None) -> tuple:
    """Write a `.code-workspace`. Returns (ok, message)."""
    if not folders:
        return False, "no folders selected - nothing to write"
    parent = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(build_workspace_json(folders, out_path, settings))
    except OSError as exc:
        return False, f"could not write {out_path}: {exc}"
    return True, f"written: {os.path.basename(out_path)} - {len(folders)} folder(s)"


# ── jump-to-location ─────────────────────────────────────────────────────────

def goto_argv(editor_cmd_parts: list, path: str,
              line: "int | None" = None, column: "int | None" = None) -> list:
    """argv that opens *path*, optionally at a line, in a VS Code-like editor.

    `code --goto file:line:col` is the documented form. The flag is only added
    when a line is actually known: passing `--goto` with a bare path is
    harmless in VS Code but meaningless in editors that do not implement it,
    and `editor_cmd` is user-configurable — it may not be VS Code at all.

    Pure, so the exact argv can be asserted without spawning anything.
    """
    argv = list(editor_cmd_parts)
    if line is None:
        argv.append(path)
        return argv
    target = f"{path}:{int(line)}"
    if column is not None:
        target += f":{int(column)}"
    argv.append("--goto")
    argv.append(target)
    return argv


def open_in_editor(editor_cmd: str, path: str, line: "int | None" = None,
                   column: "int | None" = None) -> tuple:
    """Spawn the configured editor on *path*, optionally at a line.

    Returns ``(ok, error)`` rather than raising, because both callers have to
    render a failure either way — the Projects tab as a messagebox, the scout
    dialog as a log line — and a helper that raised would leave each of them
    inventing its own message for the same condition.

    One implementation on purpose: the argv construction (`--goto` only when a
    line is known) and the spawn flags belong together, and a second copy is
    how "open at line" starts behaving differently depending on which button
    the user pressed.
    """
    try:
        argv = goto_argv(shlex.split(editor_cmd), path, line, column)
    except ValueError as exc:                      # unbalanced quotes in cfg
        return False, f"could not parse editor command {editor_cmd!r}: {exc}"
    try:
        subprocess.Popen(argv, creationflags=CREATE_NO_WINDOW)
    except (OSError, ValueError) as exc:
        return False, f"could not launch {editor_cmd!r}: {exc}"
    return True, ""
