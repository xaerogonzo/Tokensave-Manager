/**
 * discovery.ts — one answer to "what tests does this project have".
 *
 * Two surfaces need it: the Test Explorer builds its tree from it, and the
 * CodeLens provider annotates source files with it. They are added in the same
 * release, which is exactly when two independent caches get written — each
 * correct, each refreshed on its own schedule, and each showing a different
 * number in a different part of the screen an hour after an edit.
 *
 * So there is one cache and one invalidation source. Callers subscribe to
 * `onDidChange` rather than polling, and nothing here decides *what* a test is
 * — that is `helpers/test_discovery.py`, reached through the CLI.
 *
 * **A cached miss is still cached.** When the CLI is unavailable or the
 * project has no suite, that answer is stored like any other. Retrying on
 * every keystroke because the last attempt found nothing is how a CodeLens
 * provider turns into a process-spawning loop.
 */
import * as vscode from "vscode";
import { CliResult, runCli } from "./cli";

/** One statically discovered test definition, as `tests --detail` reports it. */
export interface TestCase {
  nodeid: string;
  name: string;
  class_name: string;
  /** Repo-relative, forward slashes. Joined to the folder in one place. */
  file: string;
  /** 1-based. Converted to a 0-based Position at the VS Code boundary. */
  line: number;
  end_line: number;
  markers: string[];
}

/** A stale-test signal. A *signal*, not a verdict — see `lens.ts`. */
export interface StaleSignal {
  test: string;
  reason: string;
  detail: string;
}

/** What one folder's discovery produced. */
export interface Discovery {
  /** Every test definition found, or [] when discovery could not run. */
  cases: TestCase[];
  /**
   * Source files with no filename-matched test file.
   *
   * The underlying scan is a documented filename heuristic, not coverage
   * measurement, so this is "nothing named like a test for it was found" and
   * must never be rendered as "this code has no tests".
   */
  uncovered: string[];
  stale: StaleSignal[];
  /** Why discovery produced nothing, or null when it worked. */
  problem: string | null;
}

const EMPTY: Discovery = { cases: [], uncovered: [], stale: [], problem: null };

export class DiscoveryCache {
  private readonly cache = new Map<string, Discovery>();
  private readonly inFlight = new Map<string, Promise<Discovery>>();
  private readonly changed = new vscode.EventEmitter<vscode.WorkspaceFolder>();

  /** Fires when a folder's discovery has been dropped and should be re-read. */
  readonly onDidChange = this.changed.event;

  constructor(private readonly context: vscode.ExtensionContext) {}

  /**
   * This folder's discovery, running the CLI only when nothing is cached.
   *
   * Concurrent callers share one run: the Explorer and the CodeLens provider
   * both ask on activation, and two `tests --detail` processes over the same
   * tree would produce one answer at twice the cost.
   */
  async get(folder: vscode.WorkspaceFolder): Promise<Discovery> {
    const key = folder.uri.fsPath;
    const cached = this.cache.get(key);
    if (cached) {
      return cached;
    }
    const running = this.inFlight.get(key);
    if (running) {
      return running;
    }
    const promise = this.load(folder).then((result) => {
      this.cache.set(key, result);
      this.inFlight.delete(key);
      return result;
    });
    this.inFlight.set(key, promise);
    return promise;
  }

  /** What is already known, without running anything. */
  peek(folder: vscode.WorkspaceFolder): Discovery | undefined {
    return this.cache.get(folder.uri.fsPath);
  }

  /** Drop one folder's answer and tell subscribers to ask again. */
  invalidate(folder: vscode.WorkspaceFolder): void {
    this.cache.delete(folder.uri.fsPath);
    this.changed.fire(folder);
  }

  invalidateAll(): void {
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      this.invalidate(folder);
    }
  }

  /** Forget a folder entirely — it left the workspace. */
  forget(folder: vscode.WorkspaceFolder): void {
    this.cache.delete(folder.uri.fsPath);
    this.inFlight.delete(folder.uri.fsPath);
  }

  dispose(): void {
    this.changed.dispose();
  }

  private async load(folder: vscode.WorkspaceFolder): Promise<Discovery> {
    const result: CliResult = await runCli(
      this.context, "tests", folder, ["--detail"]);
    if (result.transportError || !result.envelope) {
      return { ...EMPTY, problem: result.transportError ?? "no result" };
    }
    if (result.envelope.error) {
      return { ...EMPTY, problem: result.envelope.error };
    }
    const data = result.envelope.data as {
      test_cases?: TestCase[];
      uncovered?: { items?: string[] };
      stale?: { items?: StaleSignal[] };
    };
    return {
      cases: data.test_cases ?? [],
      uncovered: data.uncovered?.items ?? [],
      stale: data.stale?.items ?? [],
      problem: null,
    };
  }
}

/**
 * Group definitions by the file they live in, preserving order.
 *
 * A `Map` rather than a plain object so file names that collide with
 * `Object.prototype` members cannot shadow anything.
 */
export function byFile(cases: TestCase[]): Map<string, TestCase[]> {
  const grouped = new Map<string, TestCase[]>();
  for (const testCase of cases) {
    const existing = grouped.get(testCase.file);
    if (existing) {
      existing.push(testCase);
    } else {
      grouped.set(testCase.file, [testCase]);
    }
  }
  return grouped;
}
