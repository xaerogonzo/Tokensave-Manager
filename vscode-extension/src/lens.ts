/**
 * lens.ts — the Manager's view of a file, above the file.
 *
 * The Test Manager already knows which source files have no matching test and
 * which test files look stale. That knowledge lives in a dialog you have to go
 * and open, which means it is consulted when you remember it exists rather
 * than when you are editing the file it is about.
 *
 * ## The wording is the feature
 *
 * `scan_coverage_gaps` is a **filename heuristic** — it looks for a
 * `tests/test_<name>.py` beside a `src/<name>.py` — and its own docstring says
 * so. It is not coverage measurement and cannot be. So this renders "no
 * filename-matched test", never "no tests":
 *
 *   * "no tests" is a claim about the code, and the data cannot support it —
 *     a module tested thoroughly from a differently-named file would be
 *     labelled untested.
 *   * "no filename-matched test" is a claim about the scan, which is exactly
 *     what was measured.
 *
 * The same applies to staleness. `detect_stale_tests` emits *signals* — an
 * import that no longer resolves, a symbol that has gone — and a signal is a
 * reason to look, not a verdict that the test is dead. The lens shows the
 * reason and lets the reader decide.
 *
 * This is not pedantry about phrasing. A lens that overstates gets switched
 * off, and then the accurate half goes with it.
 */
import * as vscode from "vscode";
import { Discovery, DiscoveryCache } from "./discovery";

/** What the `tokensaveManager.codeLens` setting may be. */
export type LensMode = "off" | "source" | "tests" | "both";

/** Repo-relative, forward slashes — the spelling the envelope uses. */
export function relativeTo(folder: vscode.WorkspaceFolder,
                           uri: vscode.Uri): string {
  const root = folder.uri.fsPath;
  const raw = uri.fsPath.startsWith(root)
    ? uri.fsPath.slice(root.length) : uri.fsPath;
  return raw.replace(/^[\\/]+/, "").split("\\").join("/");
}

/** Whether a repo-relative path is one of the project's test files. */
export function isTestFile(relative: string): boolean {
  return relative.startsWith("tests/");
}

/**
 * The lens lines for one file, as text.
 *
 * Pure, and returning strings rather than `CodeLens` objects, because the
 * words are the part worth asserting — a lens in the right place saying the
 * wrong thing is the failure this module is shaped around.
 */
export function lensText(relative: string, discovery: Discovery,
                         mode: LensMode): string[] {
  if (mode === "off" || discovery.problem !== null) {
    return [];
  }
  if (isTestFile(relative)) {
    if (mode !== "tests" && mode !== "both") {
      return [];
    }
    const lines: string[] = [];
    const count = discovery.cases.filter((c) => c.file === relative).length;
    if (count > 0) {
      lines.push(`$(beaker) ${count} test${count === 1 ? "" : "s"}`);
    }
    for (const signal of discovery.stale) {
      if (signal.test === relative.split("/").pop()) {
        // The signal, not a verdict. "looks stale" is what was measured;
        // "is dead" is not.
        lines.push(`$(warning) stale signal: ${signal.reason}`);
      }
    }
    return lines;
  }

  if (mode !== "source" && mode !== "both") {
    return [];
  }
  if (discovery.uncovered.includes(relative)) {
    // NOT "no tests". The scan is a filename heuristic and this says so.
    return ["$(beaker) no filename-matched test"];
  }
  return [];
}

export class ManagerLensProvider implements vscode.CodeLensProvider {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.changed.event;

  constructor(private readonly discovery: DiscoveryCache) {
    // Redraw when the shared cache is dropped — the same invalidation the
    // Test Explorer rides, so the two can never disagree about the file the
    // user is looking at.
    this.discovery.onDidChange(() => this.changed.fire());
  }

  dispose(): void {
    this.changed.dispose();
  }

  async provideCodeLenses(
    document: vscode.TextDocument): Promise<vscode.CodeLens[]> {
    const mode = vscode.workspace.getConfiguration("tokensaveManager")
      .get<LensMode>("codeLens", "both");
    if (mode === "off") {
      return [];
    }
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) {
      return [];
    }
    const relative = relativeTo(folder, document.uri);
    const discovery = await this.discovery.get(folder);

    const top = new vscode.Range(
      new vscode.Position(0, 0), new vscode.Position(0, 0));
    return lensText(relative, discovery, mode).map((title, index) => {
      const lens = new vscode.CodeLens(top);
      lens.command = index === 0 && !isTestFile(relative)
        ? {
          // The one actionable case: a source file with no matching test.
          // Test gaps for this file is the thing to do about it.
          title,
          command: "tokensaveManager.testGapsFile",
          arguments: [document.uri],
        }
        : { title, command: "" };
      return lens;
    });
  }
}

export function registerCodeLens(context: vscode.ExtensionContext,
                                 discovery: DiscoveryCache): void {
  const provider = new ManagerLensProvider(discovery);
  context.subscriptions.push(
    { dispose: () => provider.dispose() },
    vscode.languages.registerCodeLensProvider(
      { language: "python", scheme: "file" }, provider),
  );
}
