/**
 * setup.ts — getting configured, and knowing when you are not.
 *
 * An unconfigured install used to show one row reading "Manager CLI
 * unavailable". That is accurate and nearly useless: it names a state without
 * naming a next step, on a screen the user reached by installing something
 * that promised to do things.
 *
 * ## Three states, not two
 *
 * The interesting one is the middle:
 *
 *   * **unconfigured** — no `managerPath`, no `cliPath`, no bundled binary.
 *     Obvious, and the welcome view offers the picker.
 *   * **configured but broken** — a path is set and it does not work: the
 *     checkout moved, `python` is not on PATH, the interpreter cannot import
 *     what `cli.py` needs. Everything *looks* set up, and every command fails
 *     with a different message.
 *   * **working**.
 *
 * A setup flow that only proves the happy path leaves the middle state looking
 * like success, which is the one where a user reasonably concludes the
 * extension is broken. So verification actually **runs** the CLI rather than
 * checking that a string is non-empty.
 *
 * `commands` is what it runs: it needs no project, so it works before a folder
 * is open, and it is the command whose entire job is to answer "what can you
 * do" — which is exactly the question being asked.
 */
import * as vscode from "vscode";
import { cliUnavailableReason, resolveRunner, runProjectlessCli } from "./cli";

/** What a verification found. */
export type SetupState = "unconfigured" | "broken" | "working";

export interface SetupReport {
  state: SetupState;
  /** One sentence a person can act on. */
  message: string;
}

/**
 * Ask the configured Manager what it can do, and classify the answer.
 *
 * Exported separately from the command so the classification can be tested
 * without an editor — it is the part with three branches.
 */
export async function verifySetup(
  context: vscode.ExtensionContext): Promise<SetupReport> {
  const unavailable = cliUnavailableReason(context);
  const runner = resolveRunner(context);
  if (runner === null) {
    const settings = vscode.workspace.getConfiguration("tokensaveManager");
    const configured = (settings.get<string>("managerPath", "").trim()
      || settings.get<string>("cliPath", "").trim());
    return {
      // A path that is set but does not resolve is BROKEN, not unconfigured.
      // Telling someone to configure what they have already configured is how
      // a setup flow loses their trust.
      state: configured ? "broken" : "unconfigured",
      message: unavailable ?? "No Manager configured.",
    };
  }

  const result = await runProjectlessCli(context, "commands");
  if (result.transportError || !result.envelope) {
    return {
      state: "broken",
      message: `The Manager is configured but did not answer: `
        + `${result.transportError ?? "no output"}`,
    };
  }
  const data = result.envelope.data as { commands?: unknown[] };
  const count = data.commands?.length ?? 0;
  if (count === 0) {
    return {
      state: "broken",
      message: "The Manager answered but listed no commands, which means the "
        + "reply was not a Manager vocabulary.",
    };
  }
  return {
    state: "working",
    message: `Connected: ${count} commands available, `
      + `Manager CLI ${result.envelope.cli_version} `
      + `(${runner.kind} mode).`,
  };
}

export function registerSetup(context: vscode.ExtensionContext,
                              onChanged: () => void): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("tokensaveManager.verifySetup",
      async () => {
        const report = await vscode.window.withProgress(
          { location: vscode.ProgressLocation.Notification,
            title: "TokenSave: asking the Manager what it can do…" },
          () => verifySetup(context));
        await announce(report);
        onChanged();
      }),
    vscode.commands.registerCommand("tokensaveManager.openWalkthrough",
      () => vscode.commands.executeCommand(
        "workbench.action.openWalkthrough",
        "tokensave.tokensave-manager#tokensaveManager.setup", false)),
  );
  void refreshReadyContext(context);
}

async function announce(report: SetupReport): Promise<void> {
  if (report.state === "working") {
    void vscode.window.showInformationMessage(
      `TokenSave Manager — ${report.message}`);
    return;
  }
  const choice = await vscode.window.showWarningMessage(
    `TokenSave Manager — ${report.message}`, "Select the Manager folder");
  if (choice) {
    await vscode.commands.executeCommand("tokensaveManager.setManagerPath");
  }
}

/**
 * Set the context key the welcome view keys on.
 *
 * Based on whether a runner resolves, not on whether it answers: this runs on
 * every activation and settings change, and spawning a process each time to
 * decide whether to draw a welcome message would be a poor trade. The
 * "configured but broken" case is what `verifySetup` is for.
 */
export async function refreshReadyContext(
  context: vscode.ExtensionContext): Promise<void> {
  await vscode.commands.executeCommand(
    "setContext", "tokensaveManager.ready", resolveRunner(context) !== null);
}
