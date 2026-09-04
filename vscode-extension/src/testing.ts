/**
 * testing.ts — the suite, in VS Code's own Test Explorer.
 *
 * Until now this extension could run the whole suite and report four numbers.
 * A `TestController` is the difference between "2779 passed" in an output
 * channel and a red dot beside the `def` that failed.
 *
 * ## What this deliberately does not do
 *
 * **It does not discover tests itself.** `tests --detail` does, by walking the
 * AST in Python where it is tested. A second discovery implementation in
 * TypeScript would be a second thing to be wrong, and it would disagree with
 * the number the Manager's own Test Manager shows.
 *
 * **It does not attribute results itself.** A parametrised test reports one
 * result per case against a single discovered definition, and mapping those is
 * where an implementation quietly guesses. `test-run` returns a `requested`
 * field per result; this file looks that up in a map and nothing more. A
 * result marked `ambiguous` is reported as *not attributed*, never as a
 * best-effort match.
 *
 * **There is no Debug profile.** Debugging pytest means launching debugpy with
 * the right interpreter, cwd, path and port — which is `ms-python`'s job, done
 * properly, already installed by anyone debugging Python in this editor.
 * Reimplementing it here would be a worse copy of a solved problem. Both
 * controllers can coexist: ours is labelled "TokenSave" and adds the run lock,
 * the gate profile, and the Manager's own view of the suite.
 *
 * ## The gate profile
 *
 * `-m "not tk"` is what CI enforces and what `smoke_runner.run_gate` runs, so
 * it is offered as a first-class profile rather than left for the user to
 * remember. It is not merely a convenience on Windows: a markerless run opens
 * real Tk windows over the editor.
 */
import * as vscode from "vscode";
import { EXIT, runCli } from "./cli";
import { DiscoveryCache, TestCase, byFile } from "./discovery";

/** One per-test record from `test-run`'s envelope. */
interface TestResult {
  nodeid: string;
  outcome: string;
  duration_seconds: number | null;
  message: string;
  /** The requested nodeid this belongs to, or "" when it belongs to none. */
  requested: string;
  /** True when it could belong to more than one. Never rendered as a result. */
  ambiguous: boolean;
}

/**
 * Item ids are `<encoded folder path>/<encoded nodeid>`.
 *
 * The folder has to be part of the identity: two projects in one multi-root
 * workspace routinely have a `tests/test_cli.py::test_status`, and an id that
 * was only the nodeid would make one folder's result land on the other's item.
 *
 * Joining them needs a separator that cannot occur in either half, and there
 * isn't one. A NUL looked like the answer and **VS Code rejects it** — "Test
 * IDs may not include the ... symbol" — which the stub-backed tests could not
 * see, because the stub stored whatever it was handed. The live suite caught
 * it, and the stub now refuses NUL too.
 *
 * Percent-encoding each half sidesteps the question: `encodeURIComponent`
 * escapes `/`, so `/` is safe as the join, and the result is printable ASCII
 * that survives being persisted between sessions.
 */
function itemId(folder: vscode.WorkspaceFolder, nodeid: string): string {
  return `${encodeURIComponent(folder.uri.fsPath)}`
    + `/${encodeURIComponent(nodeid)}`;
}

export class TestExplorer {
  private readonly controller: vscode.TestController;
  /** item id → the folder and nodeid it stands for. */
  private readonly known =
    new Map<string, { folder: vscode.WorkspaceFolder; nodeid: string }>();

  constructor(private readonly context: vscode.ExtensionContext,
              private readonly discovery: DiscoveryCache) {
    this.controller = vscode.tests.createTestController(
      "tokensaveManager", "TokenSave");
    context.subscriptions.push(this.controller);

    this.controller.resolveHandler = async (item) => {
      if (item === undefined) {
        await this.discoverAll();
      }
    };
    this.controller.refreshHandler = async () => {
      this.discovery.invalidateAll();
      await this.discoverAll();
    };

    this.controller.createRunProfile(
      "Run", vscode.TestRunProfileKind.Run,
      (request, token) => this.run(request, token, ""), true);
    this.controller.createRunProfile(
      "Gate (not tk)", vscode.TestRunProfileKind.Run,
      (request, token) => this.run(request, token, "not tk"), false);

    // One invalidation source, shared with the CodeLens provider. Without
    // this the tree is correct exactly once — at activation — and then
    // silently drifts from the file the user is editing.
    context.subscriptions.push(
      this.discovery.onDidChange(() => void this.discoverAll()));
  }

  /** For the live suite: the tree as rows, which is what a person reads. */
  renderTests(): string[] {
    const rows: string[] = [];
    const walk = (items: vscode.TestItemCollection, depth: number): void => {
      const collected: vscode.TestItem[] = [];
      items.forEach((child) => collected.push(child));
      for (const child of collected) {
        rows.push("  ".repeat(depth) + child.label);
        walk(child.children, depth + 1);
      }
    };
    walk(this.controller.items, 0);
    return rows;
  }

  async discoverAll(): Promise<void> {
    const folders = vscode.workspace.workspaceFolders ?? [];
    this.known.clear();
    this.controller.items.replace([]);
    for (const folder of folders) {
      const result = await this.discovery.get(folder);
      if (result.cases.length === 0) {
        continue;
      }
      this.controller.items.add(this.buildFolder(folder, result.cases));
    }
  }

  private buildFolder(folder: vscode.WorkspaceFolder,
                      cases: TestCase[]): vscode.TestItem {
    const root = this.controller.createTestItem(
      folder.uri.fsPath, folder.name, folder.uri);
    for (const [file, inFile] of byFile(cases)) {
      const uri = vscode.Uri.joinPath(folder.uri, file);
      const fileItem = this.controller.createTestItem(
        itemId(folder, file), file, uri);
      for (const testCase of inFile) {
        const item = this.controller.createTestItem(
          itemId(folder, testCase.nodeid), this.labelFor(testCase), uri);
        // 1-based in the envelope, 0-based here. This conversion happens at
        // the boundary and nowhere else, matching diagnostics.ts.
        item.range = new vscode.Range(
          new vscode.Position(Math.max(0, testCase.line - 1), 0),
          new vscode.Position(Math.max(0, testCase.end_line - 1), 0));
        if (testCase.markers.length > 0) {
          item.description = testCase.markers.join(", ");
        }
        this.known.set(item.id, { folder, nodeid: testCase.nodeid });
        fileItem.children.add(item);
      }
      fileItem.children.size > 0 && root.children.add(fileItem);
    }
    return root;
  }

  /** `TestClass::test_name` for a method, the bare name otherwise. */
  private labelFor(testCase: TestCase): string {
    return testCase.class_name
      ? `${testCase.class_name}::${testCase.name}`
      : testCase.name;
  }

  /**
   * Run what was asked for.
   *
   * An empty `request.include` means "everything", which is run as a whole
   * suite rather than as several thousand node ids on one command line —
   * Windows has a command-line length limit and a full selection would exceed
   * it.
   */
  private async run(request: vscode.TestRunRequest,
                    token: vscode.CancellationToken,
                    markers: string): Promise<void> {
    const run = this.controller.createTestRun(request);
    try {
      for (const [folder, items] of this.groupByFolder(request)) {
        await this.runFolder(run, folder, items, markers, token);
      }
    } finally {
      run.end();
    }
  }

  /**
   * Which folder each selected item belongs to.
   *
   * A run in a multi-root workspace is one CLI invocation per project: the CLI
   * takes a single `--project`, and there is no such thing as a node id that
   * spans two repositories.
   */
  private groupByFolder(request: vscode.TestRunRequest):
      Map<vscode.WorkspaceFolder, vscode.TestItem[]> {
    const grouped = new Map<vscode.WorkspaceFolder, vscode.TestItem[]>();
    const add = (item: vscode.TestItem): void => {
      const entry = this.known.get(item.id);
      if (entry) {
        const existing = grouped.get(entry.folder);
        existing ? existing.push(item) : grouped.set(entry.folder, [item]);
        return;
      }
      // A folder or file node: descend to the leaves it stands for.
      item.children.forEach(add);
    };

    if (request.include && request.include.length > 0) {
      request.include.forEach(add);
    } else {
      this.controller.items.forEach(add);
    }

    if (request.exclude && request.exclude.length > 0) {
      const excluded = new Set(request.exclude.map((item) => item.id));
      for (const [folder, items] of grouped) {
        grouped.set(folder, items.filter((item) => !excluded.has(item.id)));
      }
    }
    return grouped;
  }

  private async runFolder(run: vscode.TestRun,
                          folder: vscode.WorkspaceFolder,
                          items: vscode.TestItem[],
                          markers: string,
                          token: vscode.CancellationToken): Promise<void> {
    if (items.length === 0) {
      return;
    }
    for (const item of items) {
      run.enqueued(item);
      run.started(item);
    }

    const byNodeid = new Map<string, vscode.TestItem>();
    for (const item of items) {
      const entry = this.known.get(item.id);
      if (entry) {
        byNodeid.set(entry.nodeid, item);
      }
    }

    // Selecting every test explicitly would build a command line with
    // thousands of node ids on it. A whole-folder run is expressed by passing
    // no selector at all, which is also what makes the gate profile possible:
    // `--tests` and `--markers` are mutually exclusive at the CLI.
    const whole = byNodeid.size ===
      (this.discovery.peek(folder)?.cases.length ?? -1);
    const args = markers
      ? ["--markers", markers]
      : whole ? [] : ["--tests", ...byNodeid.keys()];

    const result = await runCli(this.context, "test-run", folder, args, token);

    if (result.cancelled) {
      // Cancelled is not failed. Leaving the items in their started state
      // shows them as un-run, which is what happened.
      for (const item of items) {
        run.skipped(item);
      }
      run.appendOutput(`\r\nRun cancelled in ${folder.name}.\r\n`);
      return;
    }
    if (result.transportError || !result.envelope) {
      for (const item of items) {
        run.errored(item, new vscode.TestMessage(
          result.transportError ?? "the Manager CLI produced no result"));
      }
      return;
    }

    const data = result.envelope.data as {
      tests?: TestResult[]; run_state?: string; output?: string;
    };
    if (data.output) {
      run.appendOutput(data.output.replace(/\r?\n/g, "\r\n"));
    }

    if (result.exitCode === EXIT.PREREQUISITE || data.run_state === "busy") {
      // "A run is already in progress" is not a test outcome. Reporting these
      // as errored with the reason beats showing them as failures the user
      // will go looking for a cause of.
      for (const item of items) {
        run.errored(item, new vscode.TestMessage(
          result.envelope.error ?? "the suite could not be run"));
      }
      return;
    }

    this.report(run, data.tests ?? [], byNodeid, items);
  }

  /**
   * Turn per-test records into Explorer state.
   *
   * Anything not attributed to a requested test is skipped rather than
   * guessed at, and anything the CLI marked ambiguous is reported as
   * unattributable — a green tick on the wrong test is worse than no tick.
   */
  private report(run: vscode.TestRun, results: TestResult[],
                 byNodeid: Map<string, vscode.TestItem>,
                 items: vscode.TestItem[]): void {
    const answered = new Set<string>();
    const ambiguous: TestResult[] = [];

    for (const result of results) {
      if (result.ambiguous) {
        ambiguous.push(result);
        continue;
      }
      const item = byNodeid.get(result.requested);
      if (!item) {
        continue;
      }
      answered.add(item.id);
      const ms = (result.duration_seconds ?? 0) * 1000;
      switch (result.outcome) {
        case "passed":
        case "xfailed":
          run.passed(item, ms);
          break;
        case "skipped":
          run.skipped(item);
          break;
        case "error":
          run.errored(item, this.messageFor(result), ms);
          break;
        default:
          // failed, xpassed, and anything a future pytest invents. Defaulting
          // to failed is the safe direction: an unrecognised outcome shown as
          // a pass is a false green.
          run.failed(item, this.messageFor(result), ms);
      }
    }

    for (const result of ambiguous) {
      run.appendOutput(
        `\r\ncould not attribute ${result.nodeid} to a single test\r\n`);
    }

    // A requested test with no result did not run. Marking it skipped says
    // that; leaving it started would leave a spinner behind forever.
    for (const item of items) {
      if (!answered.has(item.id)) {
        run.skipped(item);
      }
    }
  }

  private messageFor(result: TestResult): vscode.TestMessage {
    return new vscode.TestMessage(
      result.message || `${result.outcome} (no detail reported)`);
  }
}
