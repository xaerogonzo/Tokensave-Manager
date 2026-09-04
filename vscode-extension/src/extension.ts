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
import { CliResult, EXIT, runCli, runProjectlessCli } from "./cli";
import { commandByAction } from "./commands";
import { proposeCommit } from "./commit";
import { DiagnosticStore } from "./diagnostics";
import { SavingsViewProvider } from "./savings";
import { StatusBar } from "./status";
import { ACTIONS, DIAGNOSTIC_COMMANDS, ProjectsProvider } from "./tree";

let output: vscode.OutputChannel;
let diagnostics: DiagnosticStore;
let statusBar: StatusBar | undefined;
let savingsProvider: SavingsViewProvider | undefined;

/**
 * What `activate` hands back, for the live integration suite.
 *
 * Tree providers are not reachable from outside the extension; `activate`'s
 * return value is the documented seam, read via
 * `extensions.getExtension(id).exports`. This is deliberately the whole of
 * it.
 *
 * **Rendered strings, not the nodes behind them.** A tree whose nodes are
 * correct and whose labels are wrong is a broken tree, and only one of those
 * two is what a person sees — so asserting against a node graph produces
 * tests that pass while the UI is visibly broken.
 *
 * `treeCommandFor` is the single deliberate exception, and it earns its
 * place: the "Manager CLI unavailable" row's whole value is that clicking it
 * opens the fix, and a row that renders "click to fix" while wired to the
 * wrong command is a defect no string assertion can see. It returns one
 * command id per row and nothing else.
 *
 * Resist widening this further. An API that can reach past the UI and assert
 * something a user could never observe eventually will — and will then pass
 * while the user's experience is broken.
 */
export interface TestApi {
  whenReady(): Promise<void>;
  renderTree(): Promise<string[]>;
  treeCommandFor(row: string): string | undefined;
  webviewHtml(): string | undefined;
  webviewRenderCount(): number;
  statusBarText(): string;
  pinnedFolderName(): string | undefined;
}

/**
 * Render a `TreeDataProvider` the way the sidebar does.
 *
 * Walks children depth-first and returns one string per row: indentation,
 * label, and the description in brackets when there is one. That is what a
 * person reads, so it is what the tests assert on.
 */
async function renderTree(
  provider: vscode.TreeDataProvider<any>,
): Promise<{ rows: string[]; commands: Map<string, string | undefined> }> {
  const rows: string[] = [];
  const commands = new Map<string, string | undefined>();
  const walk = async (node: any, depth: number): Promise<void> => {
    const children = (await provider.getChildren(node)) ?? [];
    for (const child of children) {
      const item = await provider.getTreeItem(child);
      const label = typeof item.label === "string"
        ? item.label : (item.label as any)?.label ?? "";
      const description = typeof item.description === "string"
        ? item.description : "";
      const row = "  ".repeat(depth) + label +
        (description ? `  [${description}]` : "");
      rows.push(row);
      commands.set(row, item.command?.command);
      await walk(child, depth + 1);
    }
  };
  await walk(undefined, 0);
  return { rows, commands };
}

export function activate(context: vscode.ExtensionContext): TestApi {
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

  registerStatusBar(context);
  registerSavingsView(context);
  registerCommitComposer(context);
  registerFileScopedActions(context);
  registerRefreshTriggers(context, provider);

  return {
    // Activation does no async work of its own beyond constructing the
    // status bar, whose first read is fired-and-forgotten. Tests that need
    // that read to have landed wait on the value with `until`, rather than
    // having this resolve on a timer and pretend to be a barrier.
    whenReady: async () => { await Promise.resolve(); },
    renderTree: async () => {
      const { rows, commands } = await renderTree(provider);
      // Recorded on every render so `treeCommandFor` answers about the rows
      // the caller just received, rather than about a stale walk.
      lastTreeCommands.clear();
      for (const [row, command] of commands) {
        lastTreeCommands.set(row, command);
      }
      return rows;
    },
    treeCommandFor: (row: string) => lastTreeCommands.get(row),
    webviewHtml: () => savingsProvider?.currentHtml(),
    webviewRenderCount: () => savingsProvider?.renderCount() ?? 0,
    statusBarText: () => statusBar?.currentText() ?? "",
    pinnedFolderName: () => statusBar?.pinnedFolder()?.name,
  };
}

/** Command ids from the most recent `renderTree`, keyed by rendered row. */
const lastTreeCommands = new Map<string, string | undefined>();

export function deactivate(): void {
  statusBar?.dispose();
  statusBar = undefined;
}

/**
 * The status bar, and the commands that drive it.
 *
 * Pinned to a folder rather than following the active editor — see status.ts
 * for why an item whose subject changes as you navigate is the worse of the
 * two options.
 */
function registerStatusBar(context: vscode.ExtensionContext): void {
  statusBar = new StatusBar(context);
  context.subscriptions.push(
    { dispose: () => statusBar?.dispose() },
    vscode.commands.registerCommand("tokensaveManager.statusBarMenu",
      () => showStatusMenu(context)),
    vscode.commands.registerCommand("tokensaveManager.focus",
      () => openManager(context)),
  );
  statusBar.start();
  void statusBar.refreshManagerState();
}

/** Bring the Manager forward, and say plainly when it is not there. */
async function openManager(context: vscode.ExtensionContext): Promise<void> {
  const result = await runProjectlessCli(context, "focus");
  if (result.exitCode === EXIT.PREREQUISITE) {
    vscode.window.showWarningMessage(
      "TokenSave Manager: the Manager is not running — start it first.");
    return;
  }
  const data = result.envelope?.data as
    | { focused?: boolean; reason?: string } | undefined;
  // Running-but-not-raised is a normal outcome, not a failure: Windows
  // routinely refuses SetForegroundWindow to a background process. Saying so
  // beats reporting the Manager as absent.
  if (data && data.focused === false) {
    vscode.window.setStatusBarMessage(
      `TokenSave: Manager is running (${data.reason ?? "not raised"})`, 4000);
  }
}

/** Click the status bar: every action, plus the pin and the Manager. */
async function showStatusMenu(context: vscode.ExtensionContext): Promise<void> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  const items: Array<vscode.QuickPickItem & { run: () => Promise<void> }> = [];

  const pinned = statusBar?.pinnedFolder();
  if (pinned) {
    for (const action of ACTIONS) {
      items.push({
        label: action.label,
        description: action.detail,
        run: async () => {
          await vscode.commands.executeCommand(
            "tokensaveManager.runAction", pinned, action.id);
        },
      });
    }
    items.push({
      label: "$(git-commit) Propose a commit…",
      description: "Pick changed files and file a request",
      run: async () => proposeCommit(context, pinned),
    });
  }
  items.push({
    label: "$(window) Open Manager",
    description: "Bring the running Manager forward",
    run: async () => openManager(context),
  });
  if (folders.length > 1) {
    for (const folder of folders) {
      items.push({
        label: `$(pin) Track ${folder.name}`,
        description: folder.uri.fsPath,
        // Explicit, because the alternative — following the active editor —
        // makes the item's subject change without the user asking.
        run: async () => statusBar?.pin(folder),
      });
    }
  }

  const picked = await vscode.window.showQuickPick(items, {
    title: pinned ? `TokenSave — ${pinned.name}` : "TokenSave Manager",
    placeHolder: "Choose an action",
  });
  await picked?.run();
}

function registerSavingsView(context: vscode.ExtensionContext): void {
  const provider = new SavingsViewProvider(
    context, () => statusBar?.pinnedFolder()
      ?? (vscode.workspace.workspaceFolders ?? [])[0]);
  savingsProvider = provider;
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      SavingsViewProvider.viewType, provider),
    vscode.commands.registerCommand("tokensaveManager.savings", async () => {
      await vscode.commands.executeCommand(
        `${SavingsViewProvider.viewType}.focus`);
      await provider.refresh();
    }),
  );
}

function registerCommitComposer(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("tokensaveManager.proposeCommit",
      async (resource?: vscode.Uri) => {
        const folder = resource
          ? vscode.workspace.getWorkspaceFolder(resource)
          : (statusBar?.pinnedFolder() ?? await pickFolder());
        if (folder) {
          await proposeCommit(context, folder, resource);
        }
      }),
  );
}

/**
 * "Checks this file" / "Test gaps for this file".
 *
 * `--paths` is what makes these honest: without it they would run
 * whole-workspace work under a per-file label. The CLI refuses a path outside
 * the project rather than returning an empty result, so a stale URI reports an
 * error instead of a clean bill of health.
 */
function registerFileScopedActions(context: vscode.ExtensionContext): void {
  const scoped: Array<[string, string]> = [
    ["tokensaveManager.checksFile", "checks"],
    ["tokensaveManager.testGapsFile", "test-gaps"],
  ];
  for (const [id, command] of scoped) {
    context.subscriptions.push(
      vscode.commands.registerCommand(id, async (resource?: vscode.Uri) => {
        const uri = resource ?? vscode.window.activeTextEditor?.document.uri;
        if (!uri) {
          vscode.window.showWarningMessage(
            "TokenSave Manager: open or select a file first.");
          return;
        }
        const folder = vscode.workspace.getWorkspaceFolder(uri);
        if (!folder) {
          vscode.window.showWarningMessage(
            "TokenSave Manager: that file is not in an open workspace folder.");
          return;
        }
        // Only at the CLI boundary does a Uri become a string.
        await runScoped(context, command, folder, uri.fsPath);
      }));
  }
}

async function runScoped(context: vscode.ExtensionContext, command: string,
                         folder: vscode.WorkspaceFolder,
                         file: string): Promise<void> {
  const entry = commandByAction(command);
  if (entry && !entry.acceptsPaths) {
    // The generated table says which commands may be scoped, so this cannot
    // drift from what the CLI actually accepts.
    return;
  }
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window,
      title: `TokenSave: ${command} — ${file}` },
    () => runCli(context, command, folder, ["--paths", file]));

  if (result.transportError || result.exitCode === EXIT.PREREQUISITE) {
    vscode.window.showWarningMessage(
      `TokenSave Manager: ${result.envelope?.error ?? result.transportError}`);
    return;
  }
  if (DIAGNOSTIC_COMMANDS.has(command) && result.envelope) {
    // Replace only this file's findings for this producer — a scoped run must
    // not wipe the rest of the folder's diagnostics.
    diagnostics.replace(folder, command, result.envelope.findings ?? [],
                        [toRelative(folder, file)]);
  }
  const data = result.envelope?.data as
    | { matched_paths?: unknown[] } | undefined;
  if (Array.isArray(data?.matched_paths) && data.matched_paths.length === 0) {
    // "clean" and "not part of this project" both render as zero findings, so
    // the CLI reports which one it was and this says so.
    vscode.window.showWarningMessage(
      `TokenSave Manager: ${file} is not part of this project's sources, `
      + "so nothing was checked.");
    return;
  }
  const count = result.envelope?.findings?.length ?? 0;
  vscode.window.setStatusBarMessage(
    count ? `TokenSave: ${count} finding(s) in this file`
          : "TokenSave: no findings in this file", 4000);
}

function toRelative(folder: vscode.WorkspaceFolder, file: string): string {
  const root = folder.uri.fsPath;
  const rest = file.startsWith(root) ? file.slice(root.length) : file;
  return rest.replace(/^[\\/]+/, "").split("\\").join("/");
}

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
  // The status bar rides the same watcher, but through its debouncer: a
  // branch switch fires a dozen events in a second, and one subprocess per
  // event would all report the same answer.
  const touched = () => {
    provider.refresh();
    statusBar?.schedule();
  };
  context.subscriptions.push(
    watcher,
    watcher.onDidChange(touched),
    watcher.onDidCreate(touched),
    watcher.onDidDelete(touched),
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
