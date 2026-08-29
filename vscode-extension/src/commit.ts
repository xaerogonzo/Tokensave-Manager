/**
 * commit.ts — propose a commit from the editor, for a person to approve.
 *
 * This is the loop the propose-only invariant left open: the extension could
 * write a commit request, but nothing told the user it had arrived, and
 * nothing here could bring the Manager forward to show them. It still cannot
 * commit anything — the request is a proposal, the Manager's Git tab is where
 * a human reviews it, and there is deliberately no path from this file to
 * `git commit`.
 *
 * Two behaviours are worth stating because the convenient version of each is
 * wrong.
 *
 * **A pending request is never silently replaced.** The CLI refuses to
 * overwrite a *different* pending request, and that refusal protects work the
 * user has not yet approved. So a conflict is shown as a choice rather than
 * quietly resolved by passing `--replace`.
 *
 * **Delivery is reported, not assumed.** Writing a file is not the same as the
 * Manager receiving it. The acknowledgement ledger is what makes the
 * difference observable, and this asks it rather than saying "sent".
 */
import * as path from "path";
import * as vscode from "vscode";
import { CliResult, EXIT, runCli, runProjectlessCli } from "./cli";

/** One changed file, exactly as `status` reports it. */
export interface ChangedFile {
  path: string;
  status: string;
  old_path?: string;
}

/**
 * The changed files in a `status` envelope, or null when it could not say.
 *
 * Null and empty are different answers and must stay that way: an unreadable
 * repository is not a clean one, and offering "nothing to commit" for a
 * repository we failed to read would be inventing a result. The CLI sends
 * `null` for exactly this reason.
 */
export function changedFiles(result: CliResult): ChangedFile[] | null {
  const git = (result.envelope?.data as { git?: Record<string, unknown> })
    ?.git;
  const files = git?.changed_files;
  if (!Array.isArray(files)) {
    return null;
  }
  return files.filter((f): f is ChangedFile =>
    Boolean(f) && typeof (f as ChangedFile).path === "string");
}

/** A quick-pick row. Renames show where the file came from. */
export function toPickItem(file: ChangedFile): vscode.QuickPickItem {
  return {
    label: file.path,
    description: file.status,
    detail: file.old_path ? `renamed from ${file.old_path}` : undefined,
  };
}

/** What the CLI said about a filed request, reduced to what the UI needs. */
export interface FiledRequest {
  id: string;
  duplicate: boolean;
  managerRunning: boolean;
}

export function readFiled(result: CliResult): FiledRequest | null {
  const data = result.envelope?.data as
    | { id?: unknown; duplicate?: unknown; manager_running?: unknown }
    | undefined;
  if (!data || typeof data.id !== "string") {
    return null;
  }
  return {
    id: data.id,
    duplicate: Boolean(data.duplicate),
    managerRunning: Boolean(data.manager_running),
  };
}

/**
 * Whether a `commit-request` failure is the "something else is pending" one.
 *
 * Distinguished from a general failure because the remedy is different: this
 * is a choice for the user, not an error to report.
 */
export function isPendingConflict(result: CliResult): boolean {
  return result.exitCode === EXIT.FAILED
    && typeof result.envelope?.error === "string"
    && result.envelope.error.includes("already pending");
}

/**
 * Ask which changed files to propose, then file the request.
 *
 * `seed` pre-selects a file — used by the explorer context menu, where the
 * user has already pointed at one.
 */
export async function proposeCommit(
  context: vscode.ExtensionContext,
  folder: vscode.WorkspaceFolder,
  seed?: vscode.Uri,
): Promise<void> {
  const status = await runCli(context, "status", folder);
  const files = changedFiles(status);

  if (files === null) {
    vscode.window.showWarningMessage(
      "TokenSave Manager: could not read this repository's status, so there "
      + "is nothing to propose from.");
    return;
  }
  if (files.length === 0) {
    vscode.window.showInformationMessage(
      "TokenSave Manager: no changed files to propose.");
    return;
  }

  const items = files.map(toPickItem);
  if (seed) {
    // Relative, forward-slashed — the same spelling `status` reports.
    const wanted = path.relative(folder.uri.fsPath, seed.fsPath)
      .split(path.sep).join("/");
    for (const item of items) {
      if (item.label === wanted) {
        (item as vscode.QuickPickItem & { picked?: boolean }).picked = true;
      }
    }
  }

  const picked = await vscode.window.showQuickPick(items, {
    canPickMany: true,
    title: `Propose a commit — ${folder.name}`,
    placeHolder: "Choose the files this commit should contain",
  });
  // An empty selection produces no request: an empty proposal becomes a
  // confusing dialog in the Manager with nothing in it.
  if (!picked || picked.length === 0) {
    return;
  }

  const note = await vscode.window.showInputBox({
    title: "What is this change?",
    prompt: "Shown in the Manager's Git tab. Optional.",
  });
  if (note === undefined) {
    return;                       // Escape means cancel, not "empty note".
  }
  const scope = await vscode.window.showInputBox({
    title: "Suggested scope (optional)",
    placeHolder: "feat(app), fix(cli), …",
  });
  if (scope === undefined) {
    return;
  }

  await file(context, folder, picked.map((p) => p.label), note, scope, false);
}

async function file(
  context: vscode.ExtensionContext,
  folder: vscode.WorkspaceFolder,
  files: string[],
  note: string,
  scope: string,
  replace: boolean,
): Promise<void> {
  const args = ["--files", ...files, "--note", note, "--scope", scope];
  if (replace) {
    args.push("--replace");
  }
  const result = await runCli(context, "commit-request", folder, args);

  if (isPendingConflict(result)) {
    // Offered rather than resolved: the pending request is work the user has
    // not approved yet, and discarding it for them is not this extension's
    // call to make.
    const choice = await vscode.window.showWarningMessage(
      "A different commit request is already pending in the Manager.",
      { modal: false },
      "Replace it", "Show the pending one", "Cancel");
    if (choice === "Replace it") {
      await file(context, folder, files, note, scope, true);
    } else if (choice === "Show the pending one") {
      await runProjectlessCli(context, "focus");
    }
    return;
  }

  if (result.exitCode !== EXIT.OK) {
    vscode.window.showErrorMessage(
      `TokenSave Manager: ${result.envelope?.error ?? result.transportError
        ?? "the request could not be filed"}`);
    return;
  }

  const filed = readFiled(result);
  if (!filed) {
    vscode.window.showWarningMessage(
      "TokenSave Manager: the request was filed but the CLI's reply could "
      + "not be read.");
    return;
  }

  // Says what is known, not what is hoped. "Queued" is a fact; "the Manager
  // has it" would be a guess until the ledger says so.
  const message = filed.managerRunning
    ? `Queued commit request ${filed.id} — the Manager should show it shortly.`
    : `Queued commit request ${filed.id}. The Manager is not running; it will `
      + "be picked up when it next starts.";

  const action = await vscode.window.showInformationMessage(
    message, ...(filed.managerRunning ? ["Open Manager"] : []));
  if (action === "Open Manager") {
    await runProjectlessCli(context, "focus");
  }
}
