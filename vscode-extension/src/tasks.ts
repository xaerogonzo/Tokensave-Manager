/**
 * tasks.ts — the Manager's operations as real VS Code tasks.
 *
 * `helpers/vscode_tasks.py` already writes a `.vscode/tasks.json`, and it
 * stays: it serves people driving the Manager without this extension
 * installed. But writing a file into someone's repository to get a menu entry
 * is a heavy way to get a menu entry — it has to be regenerated when the
 * Manager moves, it shows up in `git status`, and it hard-codes an interpreter
 * path that is wrong on the next machine.
 *
 * A `TaskProvider` needs none of that. The tasks exist while the extension is
 * installed, they resolve the runner the same way every other command does,
 * and nothing is written to disk.
 *
 * **`ProcessExecution`, never `ShellExecution`.** This is the trap
 * `vscode_tasks.py` documents at length and it is not hypothetical here: the
 * Manager and its projects live under `D:\Claude Co worker\...`, and a shell
 * task re-splits that on the space. The same class of bug produced
 * `bash: D:/Claude: No such file or directory` in a tokensave hook.
 *
 * **A frozen runner gets fewer tasks.** `checks` shells out to
 * `sys.executable -m compileall`, and under a Nuitka onefile build
 * `sys.executable` is the extracted binary rather than an interpreter, so the
 * task could only ever fail. `applicable_tasks` omits it on the Python side
 * for the same reason; this is that rule, applied to the runner this extension
 * actually resolved.
 */
import * as vscode from "vscode";
import { Runner, resolveRunner } from "./cli";
import { COMMANDS, ManagerCommand } from "./commands";

/** The task type contributed in package.json. */
export const TASK_TYPE = "tokensave";

/**
 * Commands a frozen runner cannot perform.
 *
 * Mirrors `helpers/vscode_tasks.FROZEN_UNSUPPORTED`. Offering a task that can
 * only ever fail is worse than offering one fewer task.
 */
export const FROZEN_UNSUPPORTED: ReadonlySet<string> = new Set(["checks"]);

/**
 * Which operations become tasks for this runner.
 *
 * `task: true` comes from the generated table, whose invariant — asserted in
 * `tests/test_commands_table.py` — is that a taskable command has a CLI
 * subcommand and takes `--project`. Without that, this function would happily
 * build a task with nothing to pass it.
 */
export function taskableCommands(kind: Runner["kind"]): ManagerCommand[] {
  return COMMANDS.filter((command) =>
    command.task
    && !(kind === "bundled" && FROZEN_UNSUPPORTED.has(command.cli)));
}

/** Build the argv for one command against one folder. */
export function taskArgs(runner: Runner, command: ManagerCommand,
                         folder: vscode.WorkspaceFolder): string[] {
  return [...runner.prefixArgs, command.cli,
          "--project", folder.uri.fsPath];
}

export class ManagerTaskProvider implements vscode.TaskProvider {
  constructor(private readonly context: vscode.ExtensionContext) {}

  provideTasks(): vscode.Task[] {
    const runner = resolveRunner(this.context);
    if (runner === null) {
      // No tasks rather than tasks that cannot run. The tree already explains
      // the missing configuration and offers the fix; a task list full of
      // entries that fail on click would be a second, worse explanation.
      return [];
    }
    const folders = vscode.workspace.workspaceFolders ?? [];
    const tasks: vscode.Task[] = [];
    for (const folder of folders) {
      for (const command of taskableCommands(runner.kind)) {
        tasks.push(this.build(runner, command, folder));
      }
    }
    return tasks;
  }

  /**
   * Fill in a task the user wrote by hand in their own `tasks.json`.
   *
   * Returning undefined for an unknown action is deliberate: a hand-written
   * task naming an operation that does not exist should fail visibly rather
   * than be quietly turned into some other one.
   */
  resolveTask(task: vscode.Task): vscode.Task | undefined {
    const runner = resolveRunner(this.context);
    const action = (task.definition as { action?: string }).action;
    if (runner === null || !action) {
      return undefined;
    }
    const command = taskableCommands(runner.kind)
      .find((c) => c.action === action);
    const folder = task.scope as vscode.WorkspaceFolder;
    if (!command || !folder?.uri) {
      return undefined;
    }
    return this.build(runner, command, folder);
  }

  private build(runner: Runner, command: ManagerCommand,
                folder: vscode.WorkspaceFolder): vscode.Task {
    const task = new vscode.Task(
      { type: TASK_TYPE, action: command.action },
      folder,
      command.label,
      "TokenSave",
      // ProcessExecution: argv stays an array all the way to CreateProcess, so
      // a path with a space in it arrives as one argument.
      new vscode.ProcessExecution(
        runner.command, taskArgs(runner, command, folder)),
    );
    task.detail = command.detail;
    // Nothing here is a build or a test in VS Code's sense, and mislabelling
    // them would put `doctor` on Ctrl+Shift+B.
    task.group = undefined;
    task.presentationOptions = {
      reveal: vscode.TaskRevealKind.Always,
      panel: vscode.TaskPanelKind.Dedicated,
      clear: true,
    };
    return task;
  }
}

export function registerTaskProvider(
  context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.tasks.registerTaskProvider(
      TASK_TYPE, new ManagerTaskProvider(context)));
}
