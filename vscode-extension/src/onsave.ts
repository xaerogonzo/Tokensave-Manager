/**
 * onsave.ts — re-check a file when you save it.
 *
 * `checks --paths <file>` already exists and the Problems panel already knows
 * how to hold its findings per file and per producer. The only new thing here
 * is *when*, and the only hard part is that "when" now has several answers at
 * once.
 *
 * ## Two ways this quietly goes wrong
 *
 * **A slow run finishing after a fast one.** Save, edit, save again: two runs
 * are in flight over the same file, and if the first finishes last it puts
 * back findings the second already knows are fixed. The squiggle reappears
 * with nothing to click on. Each run therefore carries a generation number and
 * a stale one drops its result on the floor.
 *
 * **A whole-workspace run and a per-file run overlapping.** `DiagnosticStore`
 * already scopes replacement by producer *and* by file, which is what keeps a
 * per-file run from wiping the rest of the folder. That property is load-
 * bearing here rather than incidental, so it is tested from this side too.
 *
 * ## Off by default, and honest when it cannot work
 *
 * `checks` runs `sys.executable -m compileall`, which under a Nuitka onefile
 * build is the extracted binary rather than an interpreter. With a bundled
 * runner the command can only fail — so it is refused once, with the reason,
 * instead of producing an error notification on every single save.
 */
import * as vscode from "vscode";
import { CliResult, EXIT, resolveRunner, runCli } from "./cli";

/** Per-file generation counters, so a slow run cannot outlive a fast one. */
const generations = new Map<string, number>();

/** Pending debounce timers, keyed by file. */
const timers = new Map<string, NodeJS.Timeout>();

/** Said once per session, not once per save. */
let warnedAboutFrozen = false;

/** What a completed run should do with its findings. */
export interface SaveHandler {
  apply(folder: vscode.WorkspaceFolder, relative: string,
        result: CliResult): void;
}

/**
 * Decide whether a result may still be applied.
 *
 * Exported because this is the whole of the correctness argument and it is
 * worth testing without an editor: a result is applied only when no newer run
 * for the same file has started since it began.
 */
export function isCurrent(key: string, generation: number): boolean {
  return generations.get(key) === generation;
}

/** Begin a run for *key* and return its generation. */
export function beginRun(key: string): number {
  const next = (generations.get(key) ?? 0) + 1;
  generations.set(key, next);
  return next;
}

/** Reset all counters. Tests only. */
export function resetRuns(): void {
  generations.clear();
  for (const timer of timers.values()) {
    clearTimeout(timer);
  }
  timers.clear();
  warnedAboutFrozen = false;
}

export function registerChecksOnSave(context: vscode.ExtensionContext,
                                     handler: SaveHandler): void {
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((document) => {
      const settings = vscode.workspace.getConfiguration("tokensaveManager");
      if (!settings.get<boolean>("checksOnSave", false)) {
        return;
      }
      if (document.languageId !== "python" || document.uri.scheme !== "file") {
        return;
      }
      const folder = vscode.workspace.getWorkspaceFolder(document.uri);
      if (!folder) {
        return;
      }

      const runner = resolveRunner(context);
      if (runner?.kind === "bundled") {
        if (!warnedAboutFrozen) {
          warnedAboutFrozen = true;
          void vscode.window.showWarningMessage(
            "TokenSave Manager: checks-on-save needs a Manager checkout — the "
            + "bundled CLI has no Python interpreter to run them with. Set "
            + "tokensaveManager.managerPath, or turn checksOnSave off.");
        }
        return;
      }

      const key = document.uri.fsPath;
      const existing = timers.get(key);
      if (existing) {
        clearTimeout(existing);
      }
      const delay = settings.get<number>("statusDebounceMs", 750);
      timers.set(key, setTimeout(() => {
        timers.delete(key);
        void runChecks(context, folder, document.uri, handler);
      }, delay));
    }),
  );
}

async function runChecks(context: vscode.ExtensionContext,
                         folder: vscode.WorkspaceFolder,
                         uri: vscode.Uri,
                         handler: SaveHandler): Promise<void> {
  const key = uri.fsPath;
  const generation = beginRun(key);

  const result = await runCli(context, "checks", folder,
                              ["--paths", uri.fsPath]);

  if (!isCurrent(key, generation)) {
    // A newer save has already started or finished. Applying this now would
    // resurrect findings the newer run has superseded.
    return;
  }
  if (result.transportError || result.exitCode === EXIT.PREREQUISITE
      || !result.envelope) {
    // Silent on failure by design. This runs on every save; a notification per
    // failed save is how a helpful feature becomes one people disable. The
    // manually-invoked commands still report loudly.
    return;
  }
  handler.apply(folder, relativeTo(folder, uri), result);
}

function relativeTo(folder: vscode.WorkspaceFolder, uri: vscode.Uri): string {
  const root = folder.uri.fsPath;
  const raw = uri.fsPath.startsWith(root)
    ? uri.fsPath.slice(root.length) : uri.fsPath;
  return raw.replace(/^[\\/]+/, "").split("\\").join("/");
}
