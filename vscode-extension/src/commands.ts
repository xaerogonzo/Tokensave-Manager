/**
 * commands.ts - GENERATED. Do not edit.
 *
 * Mirrors `src/helpers/commands.py`, which is the single source of
 * truth for the Manager's command vocabulary. Regenerate with:
 *
 *     python scripts/gen_commands_ts.py
 *
 * `tests/test_commands_table.py` fails if this file drifts from the
 * table, so an edit here is reverted by CI rather than merged.
 */

/** What a command is permitted to change. */
export type SideEffect =
  | "pure_read"
  | "observe_refresh"
  | "mutating";

/** Plain-English meaning of each class, for anything that renders one. */
export const SIDE_EFFECT_MEANING: Record<SideEffect, string> = {
  "pure_read":
    "No filesystem, project or database mutation. OS-level UI effects (window focus) are excluded.",
  "observe_refresh":
    "Reads the project, but may refresh tokensave's own bookkeeping under ~/.tokensave.",
  "mutating":
    "Changes project or Manager state.",
};

export interface ManagerCommand {
  /** Stable identifier. Safe to reference; labels are not. */
  action: string;
  /** The `cli.py` subcommand, or "" when there is none. */
  cli: string;
  /** The VS Code command id, or "" when unexposed. */
  vscode: string;
  label: string;
  detail: string;
  sideEffect: SideEffect;
  requiresProject: boolean;
  acceptsPaths: boolean;
  /** Whether `--tests` may select individual node ids. */
  acceptsTests: boolean;
  task: boolean;
}

export const COMMANDS: readonly ManagerCommand[] = [
  {
    action: "status",
    cli: "status",
    vscode: "tokensaveManager.status",
    label: "Status",
    detail: "Branch, index, MCP binding, pending request \u2014 one cheap look.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "checks",
    cli: "checks",
    vscode: "tokensaveManager.checks",
    label: "Run checks",
    detail: "Syntax + pyflakes, reported into the Problems panel.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: true,
    acceptsTests: false,
    task: true,
  },
  {
    action: "doctor",
    cli: "doctor",
    vscode: "tokensaveManager.doctor",
    label: "Doctor",
    detail: "Scan for stale tokensave entries. Read-only \u2014 never fixes.",
    sideEffect: "observe_refresh",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: true,
  },
  {
    action: "scout",
    cli: "scout",
    vscode: "tokensaveManager.scout",
    label: "Scout",
    detail: "Refactor candidates from the tokensave index. No LLM.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "tests",
    cli: "tests",
    vscode: "tokensaveManager.tests",
    label: "Tests",
    detail: "What exists, what is uncovered, what looks stale.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "test-gaps",
    cli: "test-gaps",
    vscode: "tokensaveManager.testGaps",
    label: "Test gaps",
    detail: "Tests suggested for what changed against a base ref.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: true,
    acceptsTests: false,
    task: true,
  },
  {
    action: "test-run",
    cli: "test-run",
    vscode: "tokensaveManager.testRun",
    label: "Run tests",
    detail: "Run the suite once and report the counts.",
    sideEffect: "observe_refresh",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: true,
    task: false,
  },
  {
    action: "mcp-status",
    cli: "mcp-status",
    vscode: "tokensaveManager.mcpStatus",
    label: "MCP status",
    detail: "Which tokensave server serves this project.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: true,
  },
  {
    action: "graph-trust",
    cli: "graph-trust",
    vscode: "tokensaveManager.graphTrust",
    label: "Graph trust",
    detail: "How much of tokensave's call graph is real.",
    sideEffect: "pure_read",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: true,
  },
  {
    action: "sync",
    cli: "sync",
    vscode: "tokensaveManager.sync",
    label: "Sync tokensave",
    detail: "Refresh shadow links, then re-index this project.",
    sideEffect: "mutating",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: true,
  },
  {
    action: "commit-request",
    cli: "commit-request",
    vscode: "tokensaveManager.commitRequest",
    label: "Commit request",
    detail: "Read, or file, a request awaiting approval in the Manager.",
    sideEffect: "mutating",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: true,
  },
  {
    action: "cost",
    cli: "cost",
    vscode: "tokensaveManager.savings",
    label: "Savings & spend",
    detail: "Savings from `gain`, spend from `cost`, opportunity from `discover`.",
    sideEffect: "observe_refresh",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "focus",
    cli: "focus",
    vscode: "tokensaveManager.focus",
    label: "Open Manager",
    detail: "Raise the running Manager window, if there is one.",
    sideEffect: "pure_read",
    requiresProject: false,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "request",
    cli: "request",
    vscode: "",
    label: "Manager request",
    detail: "File a request for the running Manager to open a dialog, or ask what became of one.",
    sideEffect: "mutating",
    requiresProject: true,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
  {
    action: "commands",
    cli: "commands",
    vscode: "",
    label: "Command vocabulary",
    detail: "Emit this table, so no consumer has to restate it.",
    sideEffect: "pure_read",
    requiresProject: false,
    acceptsPaths: false,
    acceptsTests: false,
    task: false,
  },
];

/** Look one up by its stable identifier. */
export function commandByAction(
  action: string): ManagerCommand | undefined {
  return COMMANDS.find((c) => c.action === action);
}
