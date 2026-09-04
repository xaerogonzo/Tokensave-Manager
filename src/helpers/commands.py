"""The Manager's command vocabulary — one table, four consumers.

There are now four places that name the same operations: `cli.py`'s
subcommands, the VS Code tasks in `helpers/vscode_tasks.py`, the extension's
command ids, and the Manager IPC request actions. Four hand-maintained lists of
the same thing drift, and they drift silently — nothing fails when a label is
updated in one and not the others, so the first symptom is a user reading two
different names for one operation.

So the table lives here, and each consumer derives from it. The extension cannot
import Python, so `manager-cli commands --json` emits this table and a
checked-in generated `commands.ts` is verified against that output by a test
that fails on drift. That is deliberate: a "generated" file nobody checks
becomes a fifth hand-maintained table within a release or two.

**`action` is an identifier, not a label.** Generated task files and extension
command registrations reference it, so it survives display-text changes and is
not renamed casually. `label` is the part that may change freely.

**Side-effect classes describe what a command is permitted to change, not how
small the change is.** The three-way split replaced a `READ_ONLY_COMMANDS`
frozenset that made a promise two of its members did not keep — every class
below was measured rather than assumed:

    PURE_READ         no filesystem, project or database mutation
    OBSERVE_REFRESH   reads the project; may refresh tokensave's OWN bookkeeping
    MUTATING          changes project or Manager state

`cost` and `discover` ingest accounting rows into `~/.tokensave/global.db`, so
neither is a read. `doctor` was the interesting one: it does not touch the
database or its WAL at all, which is exactly why a WAL-only check would have
cleared it — it rewrites `~/.tokensave/state.toml` with byte-identical content,
moving only the mtime. Measured against an idle control run, so the write is
doctor's and not a background MCP server's. That is upstream bookkeeping rather
than project or Manager config, which puts it in `OBSERVE_REFRESH`.

**`PURE_READ` means no *data* mutation.** `focus` calls `SetForegroundWindow`,
which is a side effect on the desktop but not on anything this classification
governs. Stated here so the label is not read as a stronger claim than it makes.

No Tk, no controllers — importable from `cli.py`, which may only reach the
stdlib, `constants`, and `helpers/`.
"""

from __future__ import annotations

import dataclasses
import json

# ── Side-effect classes ──────────────────────────────────────────────────────

PURE_READ = "pure_read"
OBSERVE_REFRESH = "observe_refresh"
MUTATING = "mutating"

SIDE_EFFECT_CLASSES = (PURE_READ, OBSERVE_REFRESH, MUTATING)

#: What each class promises, for `commands --json` consumers and for humans.
SIDE_EFFECT_MEANING = {
    PURE_READ: "No filesystem, project or database mutation. "
               "OS-level UI effects (window focus) are excluded.",
    OBSERVE_REFRESH: "Reads the project, but may refresh tokensave's own "
                     "bookkeeping under ~/.tokensave.",
    MUTATING: "Changes project or Manager state.",
}


@dataclasses.dataclass(frozen=True)
class Command:
    """One operation, named once for every surface that exposes it."""

    #: Stable identifier. Referenced by generated files; not renamed casually.
    action: str
    #: The `cli.py` subcommand, or "" for an operation with no CLI form.
    cli: str
    #: The VS Code command id, or "" when the extension does not expose it.
    vscode: str
    #: Display text. Free to change — that is what `action` protects against.
    label: str
    detail: str
    side_effect: str
    #: Whether `--project` is required. Everything project-scoped says True,
    #: so the extension can check before it builds a command line rather than
    #: discovering it from an exit code.
    requires_project: bool = True
    #: Whether the command accepts `--paths` to scope its findings to files.
    accepts_paths: bool = False
    #: Whether this operation is offered as a generated VS Code task.
    task: bool = False

    def __post_init__(self) -> None:
        if self.side_effect not in SIDE_EFFECT_CLASSES:
            raise ValueError(
                f"{self.action}: unknown side-effect class {self.side_effect!r}")


COMMANDS: tuple = (
    Command(
        action="status", cli="status", vscode="tokensaveManager.status",
        label="Status",
        detail="Branch, index, MCP binding, pending request — one cheap look.",
        side_effect=PURE_READ),
    Command(
        action="checks", cli="checks", vscode="tokensaveManager.checks",
        label="Run checks",
        detail="Syntax + pyflakes, reported into the Problems panel.",
        side_effect=PURE_READ, accepts_paths=True, task=True),
    Command(
        action="doctor", cli="doctor", vscode="tokensaveManager.doctor",
        label="Doctor",
        detail="Scan for stale tokensave entries. Read-only — never fixes.",
        # Measured: rewrites ~/.tokensave/state.toml (content-identical, mtime
        # moves) and touches neither global.db nor its WAL.
        side_effect=OBSERVE_REFRESH, task=True),
    Command(
        action="scout", cli="scout", vscode="tokensaveManager.scout",
        label="Scout",
        detail="Refactor candidates from the tokensave index. No LLM.",
        side_effect=PURE_READ),
    Command(
        action="tests", cli="tests", vscode="tokensaveManager.tests",
        label="Tests",
        detail="What exists, what is uncovered, what looks stale.",
        side_effect=PURE_READ),
    Command(
        action="test-gaps", cli="test-gaps", vscode="tokensaveManager.testGaps",
        label="Test gaps",
        detail="Tests suggested for what changed against a base ref.",
        side_effect=PURE_READ, accepts_paths=True, task=True),
    Command(
        action="test-run", cli="test-run", vscode="tokensaveManager.testRun",
        label="Run tests",
        detail="Run the suite once and report the counts.",
        # Runs pytest, which writes .pytest_cache and coverage artefacts.
        side_effect=OBSERVE_REFRESH),
    Command(
        action="mcp-status", cli="mcp-status",
        vscode="tokensaveManager.mcpStatus", label="MCP status",
        detail="Which tokensave server serves this project.",
        side_effect=PURE_READ, task=True),
    Command(
        action="graph-trust", cli="graph-trust",
        vscode="tokensaveManager.graphTrust", label="Graph trust",
        detail="How much of tokensave's call graph is real.",
        side_effect=PURE_READ, task=True),
    Command(
        action="sync", cli="sync", vscode="tokensaveManager.sync",
        label="Sync tokensave",
        detail="Refresh shadow links, then re-index this project.",
        side_effect=MUTATING, task=True),
    Command(
        action="commit-request", cli="commit-request",
        vscode="tokensaveManager.commitRequest", label="Commit request",
        detail="Read, or file, a request awaiting approval in the Manager.",
        side_effect=MUTATING, task=True),
    Command(
        action="cost", cli="cost", vscode="tokensaveManager.savings",
        label="Savings & spend",
        detail="Savings from `gain`, spend from `cost`, opportunity from "
               "`discover`.",
        # `cost` and `discover` both ingest rows into ~/.tokensave/global.db.
        side_effect=OBSERVE_REFRESH),
    Command(
        action="focus", cli="focus", vscode="tokensaveManager.focus",
        label="Open Manager",
        detail="Raise the running Manager window, if there is one.",
        # No data is touched. Foregrounding a window is an OS-level effect on
        # the desktop, which this classification explicitly does not govern —
        # see SIDE_EFFECT_MEANING[PURE_READ].
        side_effect=PURE_READ, requires_project=False),
    Command(
        action="request", cli="request", vscode="",
        label="Manager request",
        detail="File a request for the running Manager to open a dialog, or "
               "ask what became of one.",
        # Writes into the project's own .tokensave-manager/requests inbox.
        side_effect=MUTATING),
    Command(
        action="commands", cli="commands", vscode="",
        label="Command vocabulary",
        detail="Emit this table, so no consumer has to restate it.",
        # Describes the Manager rather than acting on a repository. Demanding
        # a project would mean an editor had to open a folder before it could
        # ask what it may invoke.
        side_effect=PURE_READ, requires_project=False),
)


#: Lookup by stable identifier.
BY_ACTION = {c.action: c for c in COMMANDS}

#: Lookup by CLI subcommand, for commands that have one.
BY_CLI = {c.cli: c for c in COMMANDS if c.cli}


def by_side_effect(side_effect: str) -> tuple:
    """Every command in one class. Raises on an unknown class name."""
    if side_effect not in SIDE_EFFECT_CLASSES:
        raise ValueError(f"unknown side-effect class {side_effect!r}")
    return tuple(c for c in COMMANDS if c.side_effect == side_effect)


#: Where the generated TypeScript mirror lives, relative to the repo root.
TYPESCRIPT_PATH = "vscode-extension/src/commands.ts"


def as_typescript() -> str:
    """The table as a TypeScript module, byte-for-byte reproducible.

    The extension cannot import Python, and restating eleven commands in
    TypeScript by hand is how the fifth hand-maintained list would appear. So
    it is generated, checked in, and compared against fresh output by a test
    that fails on any difference — a "generated" file nobody verifies is just a
    hand-maintained file with a misleading header.

    Deterministic by construction: the table's own order, no timestamps, no
    interpreter-dependent formatting. A regeneration that changes nothing
    produces an identical file, so a real diff always means a real change.
    """
    lines = [
        "/**",
        " * commands.ts - GENERATED. Do not edit.",
        " *",
        " * Mirrors `src/helpers/commands.py`, which is the single source of",
        " * truth for the Manager's command vocabulary. Regenerate with:",
        " *",
        " *     python scripts/gen_commands_ts.py",
        " *",
        " * `tests/test_commands_table.py` fails if this file drifts from the",
        " * table, so an edit here is reverted by CI rather than merged.",
        " */",
        "",
        "/** What a command is permitted to change. */",
        "export type SideEffect =",
    ]
    for i, name in enumerate(SIDE_EFFECT_CLASSES):
        tail = ";" if i == len(SIDE_EFFECT_CLASSES) - 1 else ""
        lines.append(f'  | "{name}"{tail}')
    lines += [
        "",
        "/** Plain-English meaning of each class, for anything that renders one. */",
        "export const SIDE_EFFECT_MEANING: Record<SideEffect, string> = {",
    ]
    for name in SIDE_EFFECT_CLASSES:
        lines.append(f'  "{name}":')
        lines.append(f'    {json.dumps(SIDE_EFFECT_MEANING[name])},')
    lines += [
        "};",
        "",
        "export interface ManagerCommand {",
        "  /** Stable identifier. Safe to reference; labels are not. */",
        "  action: string;",
        "  /** The `cli.py` subcommand, or \"\" when there is none. */",
        "  cli: string;",
        "  /** The VS Code command id, or \"\" when unexposed. */",
        "  vscode: string;",
        "  label: string;",
        "  detail: string;",
        "  sideEffect: SideEffect;",
        "  requiresProject: boolean;",
        "  acceptsPaths: boolean;",
        "  task: boolean;",
        "}",
        "",
        "export const COMMANDS: readonly ManagerCommand[] = [",
    ]
    for c in COMMANDS:
        lines += [
            "  {",
            f'    action: {json.dumps(c.action)},',
            f'    cli: {json.dumps(c.cli)},',
            f'    vscode: {json.dumps(c.vscode)},',
            f'    label: {json.dumps(c.label)},',
            f'    detail: {json.dumps(c.detail)},',
            f'    sideEffect: {json.dumps(c.side_effect)},',
            f'    requiresProject: {"true" if c.requires_project else "false"},',
            f'    acceptsPaths: {"true" if c.accepts_paths else "false"},',
            f'    task: {"true" if c.task else "false"},',
            "  },",
        ]
    lines += [
        "];",
        "",
        "/** Look one up by its stable identifier. */",
        "export function commandByAction(",
        "  action: string): ManagerCommand | undefined {",
        "  return COMMANDS.find((c) => c.action === action);",
        "}",
        "",
    ]
    return chr(10).join(lines)


def as_json() -> dict:
    """The table, for `manager-cli commands --json` and the generated TS.

    Ordering is the table's own, and is part of the contract: the generated
    `commands.ts` is compared verbatim, so a reordering has to be a deliberate
    regeneration rather than an invisible diff.
    """
    return {
        "side_effect_classes": {
            name: SIDE_EFFECT_MEANING[name] for name in SIDE_EFFECT_CLASSES
        },
        "commands": [dataclasses.asdict(c) for c in COMMANDS],
    }
