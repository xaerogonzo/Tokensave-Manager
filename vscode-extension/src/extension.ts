/**
 * extension.ts — activation, commands, and the refresh contract.
 *
 * Two invariants are enforced here rather than merely intended.
 *
 * **Propose-only.** This extension may create or update
 * `.tokensave-manager/commit_request.json` through the CLI, and that is the
 * whole of its write authority over your repository. It never runs `git
 * commit`, never applies a proposal, and never drives the Manager's GUI to
 * approve anything. Approval happens in the Manager's Git tab, in front of a
 * person. There is deliberately no command here that could bypass that.
 *
 * **The project is always explicit.** Every command resolves a concrete
 * workspace folder before it runs, and passes it to the CLI. Nothing falls
 * back to "the active editor's folder" or a process cwd.
 */
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { CliResult, EXIT, runCli } from "./cli";
import { DiagnosticStore } from "./diagnostics";
import { ACTIONS, DIAGNOSTIC_COMMANDS, ProjectsProvider } from "./tree";

let output: vscode.OutputChannel;
let diagnostics: DiagnosticStore;

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("TokenSave Manager");
  context.subscriptions.push(output);

  const collection =
    vscode.languages.createDiagnosticCollection("tokensave");
  context.subscriptions.push(collection);
  diagnostics = new DiagnosticStore(collection);

  const provider = new ProjectsProvider(context);
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider(
      "tokensaveManager.projects", provider));

  context.subscriptions.push(
    vscode.commands.registerCommand("tokensaveManager.refresh",
      () => provider.refresh()),
    vscode.commands.registerCommand("tokensaveManager.runAction",
      (folder: vscode.WorkspaceFolder, actionId: string) =>
        runAction(context, provider, folder, actionId)),
  );

  // Palette entries. Each resolves a folder first — see pickFolder.
  for (const action of ACTIONS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(
        `tokensaveManager.${action.id}`, async () => {
          const folder = await pickFolder();
          if (folder) {
            await runAction(context, provider, folder, action.id);
          }
        }));
  }
  context.subscriptions.push(
    vscode.commands.registerCommand("tokensaveManager.sync", async () => {
      const folder = await pickFolder();
      if (folder) {
        await runAction(context, provider, folder, "sync", ["sync"]);
      }
    }));

  context.subscriptions.push(
    vscode.commands.registerCommand("tokensaveManager.setManagerPath",
      () => setManagerPath(provider)));

  registerRefreshTriggers(context, provider);
}

export function deactivate(): void { /* nothing to unwind */ }

/**
 * Ask for the Manager checkout and save it, with a folder picker.
 *
 * The alternative is telling the user to open Settings, find the right key and
 * paste a path — which is exactly the instruction that turned out to be too
 * vague in practice. The extension already knows what it needs; it should ask
 * for it directly, and verify the answer before saving.
 */
async function setManagerPath(provider: ProjectsProvider): Promise<void> {
  const picked = await vscode.window.showOpenDialog({
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "Use this Manager",
    title: "Select your TokenSave Manager folder (the one containing src/)",
  });
  if (!picked || picked.length === 0) {
    return;
  }

  const folder = picked[0].fsPath;
  if (!fs.existsSync(path.join(folder, "src", "cli.py"))) {
    // Saving an unusable path would just move the failure later, with the
    // setting looking correct.
    const retry = await vscode.window.showErrorMessage(
      `No src/cli.py under ${folder} — that does not look like a Manager `
      + "checkout.", "Choose again");
    if (retry) {
      await setManagerPath(provider);
    }
    return;
  }

  await vscode.workspace.getConfiguration("tokensaveManager")
    .update("managerPath", folder, vscode.ConfigurationTarget.Global);
  provider.refresh();
  vscode.window.showInformationMessage(
    `TokenSave Manager: using ${folder}`);
}

/**
 * When the tree redraws.
 *
 * A view that only refreshes on demand goes stale in exactly the situation
 * where its answer matters: right after the user changed the thing it was
 * reporting on. `.mcp.json` and the pending commit request are both files the
 * Manager or a Claude session can rewrite while VS Code is open.
 */
function registerRefreshTriggers(context: vscode.ExtensionContext,
                                 provider: ProjectsProvider): void {
  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders((event) => {
      // A folder that left the workspace must not keep contributing squiggles.
      for (const folder of event.removed) {
        diagnostics.forgetFolder(folder);
      }
      provider.refresh();
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("tokensaveManager")) {
        provider.refresh();
      }
    }),
  );

  const watcher = vscode.workspace.createFileSystemWatcher(
    "**/{.mcp.json,.tokensave-manager/commit_request.json}");
  context.subscriptions.push(
    watcher,
    watcher.onDidChange(() => provider.refresh()),
    watcher.onDidCreate(() => provider.refresh()),
    watcher.onDidDelete(() => provider.refresh()),
  );
}

/**
 * The folder a palette-invoked command applies to.
 *
 * With one folder open there is no ambiguity. With several there genuinely is,
 * so the user is asked rather than having one chosen for them.
 */
async function pickFolder(): Promise<vscode.WorkspaceFolder | undefined> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 0) {
    vscode.window.showWarningMessage(
      "TokenSave Manager: open a folder first.");
    return undefined;
  }
  if (folders.length === 1) {
    return folders[0];
  }
  const picked = await vscode.window.showQuickPick(
    folders.map((f) => ({ label: f.name, description: f.uri.fsPath, folder: f })),
    { title: "Which project?", placeHolder: "Select a workspace folder" });
  return picked?.folder;
}

async function runAction(context: vscode.ExtensionContext,
                         provider: ProjectsProvider,
                         folder: vscode.WorkspaceFolder,
                         actionId: string,
                         override?: string[]): Promise<void> {
  const action = ACTIONS.find((a) => a.id === actionId);
  const command = override?.[0] ?? action?.command;
  if (!command) {
    return;
  }

  const extra = command === "test-gaps"
    ? ["--base", vscode.workspace.getConfiguration("tokensaveManager")
        .get<string>("testGapsBase", "origin/master")]
    : [];

  output.appendLine(
    `[${new Date().toISOString()}] running ${command} in ${folder.uri.fsPath}`);

  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window,
      title: `TokenSave: ${command} — ${folder.name}` },
    () => runCli(context, command, folder, extra));

  provider.record(folder, actionId, result);
  // Swap diagnostics in only once there is a real answer. Clearing when the
  // command STARTED would empty the Problems panel for the length of the run
  // and show a clean bill of health nothing had earned yet.
  if (DIAGNOSTIC_COMMANDS.has(command) && result.envelope
      && !result.transportError) {
    diagnostics.replace(folder, command, result.envelope.findings ?? []);
  }
  report(command, folder, result);

  if (command === "test-gaps" && result.exitCode === EXIT.OK) {
    await offerToOpenSuggestion(folder, result);
  }
}

/**
 * Let the user jump to a suggested test.
 *
 * The list comes straight from the envelope; the extension does not decide
 * what a gap is, only where to put the cursor. Offered rather than opened
 * automatically — stealing focus after a background action is hostile.
 */
async function offerToOpenSuggestion(folder: vscode.WorkspaceFolder,
                                     result: CliResult): Promise<void> {
  const raw = (result.envelope?.data?.suggestions ?? []) as Array<{
    source?: string; test?: string; requires_automation?: boolean;
  }>;
  const items = raw
    .filter((s) => s.test || s.source)
    .map((s) => ({
      label: s.test || s.source!,
      description: s.test && s.source ? `for ${s.source}` : "",
      detail: s.requires_automation ? "automatable" : "needs a human",
      target: s.test || s.source!,
    }));
  if (items.length === 0) {
    return;
  }
  const picked = await vscode.window.showQuickPick(items, {
    title: `Test gaps — ${folder.name}`,
    placeHolder: "Open a suggested test file (Esc to skip)",
  });
  if (!picked) {
    return;
  }
  // Suggestions are repo-relative; joining them to the folder is our job
  // because only we know which project this result came from.
  const uri = vscode.Uri.joinPath(folder.uri, picked.target);
  try {
    await openAt(uri);
  } catch (err) {
    vscode.window.showWarningMessage(
      `TokenSave Manager: could not open ${picked.target} `
      + `(${(err as Error).message}). It may not exist yet.`);
  }
}

/**
 * Surface a result. Every failure gets a concrete message.
 *
 * A command that silently does nothing is the failure mode this guards
 * against — particularly exit code 3, which means a prerequisite is missing
 * and is fixable by the user once they are told which one.
 */
function report(command: string, folder: vscode.WorkspaceFolder,
                result: CliResult): void {
  const env = result.envelope;
  output.appendLine(
    `[${new Date().toISOString()}] ${command} ${folder.uri.fsPath} `
    + `→ exit ${result.exitCode}`);
  if (env) {
    output.appendLine(JSON.stringify(env, null, 2));
  }
  if (result.transportError) {
    output.appendLine(`transport: ${result.transportError}`);
  }

  if (result.transportError) {
    vscode.window.showErrorMessage(
      `TokenSave Manager: ${result.transportError}`, "Show Output")
      .then((choice) => { if (choice) { output.show(true); } });
    return;
  }
  if (result.exitCode === EXIT.PREREQUISITE) {
    vscode.window.showWarningMessage(
      `TokenSave Manager: ${env?.error ?? "a prerequisite is missing"}`,
      "Open Settings")
      .then((choice) => {
        if (choice) {
          vscode.commands.executeCommand(
            "workbench.action.openSettings", "tokensaveManager");
        }
      });
    return;
  }
  if (result.exitCode === EXIT.OK) {
    vscode.window.setStatusBarMessage(
      `TokenSave: ${command} ok (${folder.name})`, 4000);
    return;
  }
  vscode.window.showWarningMessage(
    `TokenSave Manager: ${command} reported problems in ${folder.name}.`,
    "Show Output")
    .then((choice) => { if (choice) { output.show(true); } });
}

/**
 * Open a file the Manager named, at an optional line.
 *
 * Uses the editor API rather than spawning `code --goto`: inside the extension
 * host there is already an editor, and shelling out would start a second
 * process to talk to the one we are running in. (`goto_argv` in
 * `helpers/vscode_tasks.py` remains correct for the Manager driving VS Code
 * from outside.)
 */
async function openAt(uri: vscode.Uri, line?: number): Promise<void> {
  const document = await vscode.workspace.openTextDocument(uri);
  const editor = await vscode.window.showTextDocument(document);
  if (line !== undefined && line > 0) {
    const position = new vscode.Position(line - 1, 0);  // findings are 1-indexed
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(new vscode.Range(position, position),
                       vscode.TextEditorRevealType.InCenter);
  }
}
