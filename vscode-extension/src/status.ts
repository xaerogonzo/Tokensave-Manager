/**
 * status.ts — one status-bar item, and the rules that keep it honest and cheap.
 *
 * Three decisions here are deliberate, and each one rejects the obvious
 * alternative for a stated reason.
 *
 * **It is pinned to a folder, and does not follow the active editor.** In a
 * multi-root workspace "the current project" is a guess, and an item whose
 * subject silently changes as you navigate is worse than one that is
 * occasionally about the wrong project — because it is never *known* to be
 * about the wrong one. The pin is visible in the tooltip and changed only by
 * an explicit choice from the quick pick.
 *
 * **Refreshes are bounded.** File-system watchers fire in bursts — a branch
 * switch or a `git checkout` can produce a dozen events in a second — and one
 * subprocess per event is both wasteful and pointless, since they would all
 * report the same answer. Events are debounced into a single call, and the
 * periodic poll is slow.
 *
 * **`status` and `focus` stay decoupled.** `status` is cheap project state;
 * `focus` is the Manager's process state. They change for unrelated reasons
 * and are read on unrelated cadences, so bundling them into one call would
 * make the cheap one as expensive as the expensive one.
 *
 * The request indicator reflects the acknowledgement ledger's states rather
 * than mere file existence: knowing whether the Manager actually *received* a
 * handoff is the entire point of having a live channel.
 */
import * as vscode from "vscode";
import { CliResult, runCli, runProjectlessCli } from "./cli";

/** What `status` says about a project, reduced to what a 40-column item can show. */
export interface StatusSummary {
  branch: string;
  dirty: boolean | null;
  changedFiles: number | null;
  pendingRequest: boolean;
  /** Whether `mcp.configured` was a *probe* or a reading of a file on disk. */
  mcpConfigured: boolean;
  mcpProbed: boolean;
}

/**
 * Reduce a `status` envelope to what the bar renders.
 *
 * Returns null when the envelope could not be read, which the caller shows as
 * an unknown state rather than a clean one — an unreadable project is not a
 * healthy project, and the CLI is careful to send `null` rather than `false`
 * for exactly that reason.
 */
export function summarise(result: CliResult): StatusSummary | null {
  const data = result.envelope?.data as
    | { git?: Record<string, unknown>; mcp?: Record<string, unknown>;
        commit_request?: Record<string, unknown> }
    | undefined;
  if (!data || result.transportError) {
    return null;
  }
  const git = data.git ?? {};
  const changed = git.changed_files;
  return {
    branch: typeof git.branch === "string" ? git.branch : "",
    dirty: typeof git.dirty === "boolean" ? git.dirty : null,
    changedFiles: Array.isArray(changed) ? changed.length : null,
    pendingRequest: Boolean(data.commit_request?.pending),
    mcpConfigured: Boolean(data.mcp?.configured),
    // `status` never probes — probing spawns `claude mcp get`, which CREATES a
    // `~/.claude.json` entry. So this is a fact about a file, and the label
    // below has to say so rather than implying a verdict.
    mcpProbed: Boolean(data.mcp?.probed),
  };
}

/** The item's text. Short on purpose: the bar is not a dashboard. */
export function renderText(summary: StatusSummary | null): string {
  if (summary === null) {
    return "$(database) TokenSave: ?";
  }
  const parts: string[] = [];
  if (summary.branch) {
    parts.push(summary.branch);
  }
  if (summary.dirty === null) {
    // Unknown, not clean. The CLI distinguishes these and so must the bar.
    parts.push("?");
  } else if (summary.dirty) {
    parts.push(summary.changedFiles !== null
      ? `●${summary.changedFiles}` : "●");
  }
  if (summary.pendingRequest) {
    parts.push("$(inbox)");
  }
  return `$(database) ${parts.join(" ") || "clean"}`;
}

/** The tooltip. This is where the detail the text cannot carry belongs. */
export function renderTooltip(summary: StatusSummary | null,
                              folderName: string,
                              managerRunning: boolean | null): string {
  const lines = [`TokenSave Manager — ${folderName}`];
  if (summary === null) {
    lines.push("Could not read this project's status.");
  } else {
    lines.push(`Branch: ${summary.branch || "unknown"}`);
    lines.push(summary.dirty === null
      ? "Working tree: could not be read"
      : summary.dirty
        ? `Working tree: ${summary.changedFiles ?? "?"} changed file(s)`
        : "Working tree: clean");
    // "Config present" rather than a tick: what was checked is the file, and
    // saying more than that is the mistake this project has already made once.
    lines.push(summary.mcpConfigured
      ? `MCP: config present${summary.mcpProbed ? "" : " (not probed)"}`
      : "MCP: no project config");
    if (summary.pendingRequest) {
      lines.push("A commit request is waiting for approval in the Manager.");
    }
  }
  lines.push(managerRunning === null
    ? "Manager: unknown"
    : managerRunning ? "Manager: running" : "Manager: not running");
  lines.push("Click for actions.");
  return lines.join("\n");
}

/**
 * A debouncer that collapses a burst into one trailing call.
 *
 * Split out and exported because it is the part worth testing: the failure it
 * prevents — a dozen watcher events becoming a dozen subprocesses — is
 * invisible in normal use and obvious only under load.
 */
export class Debouncer {
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(private readonly delayMs: number,
              private readonly run: () => void) {}

  schedule(): void {
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
    }
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.run();
    }, this.delayMs);
  }

  dispose(): void {
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }
}

/**
 * Drops responses that have been overtaken by a newer request.
 *
 * The same race the Manager's own Savings dialog has: two `status` calls in
 * flight, the older one finishing last and overwriting a newer answer. Cheap
 * to prevent, and impossible to notice once it happens.
 */
export class Sequence {
  private issued = 0;
  private applied = 0;

  next(): number {
    this.issued += 1;
    return this.issued;
  }

  /** True when `token` is the newest result seen so far. */
  accept(token: number): boolean {
    if (token <= this.applied) {
      return false;
    }
    this.applied = token;
    return true;
  }
}

export class StatusBar {
  private readonly item: vscode.StatusBarItem;
  private readonly debouncer: Debouncer;
  private readonly sequence = new Sequence();
  private timer: ReturnType<typeof setInterval> | undefined;
  private pinned: vscode.WorkspaceFolder | undefined;
  private managerRunning: boolean | null = null;

  constructor(private readonly context: vscode.ExtensionContext) {
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left, 100);
    this.item.command = "tokensaveManager.statusBarMenu";
    this.debouncer = new Debouncer(this.debounceMs(), () => void this.refresh());
    this.pinned = (vscode.workspace.workspaceFolders ?? [])[0];
    this.render(null);
  }

  private settings(): vscode.WorkspaceConfiguration {
    return vscode.workspace.getConfiguration("tokensaveManager");
  }

  private debounceMs(): number {
    return this.settings().get<number>("statusDebounceMs", 750);
  }

  private pollMs(): number {
    return this.settings().get<number>("statusPollSeconds", 300) * 1000;
  }

  /** The folder this item is about. Never inferred from the active editor. */
  pinnedFolder(): vscode.WorkspaceFolder | undefined {
    return this.pinned;
  }

  /**
   * What the item currently reads, as a person sees it.
   *
   * `renderText` is already unit-tested against a summary; this is the other
   * half — that the string actually reached the StatusBarItem. A pure
   * function returning the right text into a widget nobody updated is a bug
   * no test of that function can see.
   */
  currentText(): string {
    return this.item.text;
  }

  pin(folder: vscode.WorkspaceFolder): void {
    this.pinned = folder;
    void this.refresh();
  }

  start(): void {
    this.item.show();
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), this.pollMs());
  }

  /** Coalesce a burst of watcher events into one refresh. */
  schedule(): void {
    this.debouncer.schedule();
  }

  async refresh(): Promise<void> {
    const folder = this.pinned;
    if (!folder) {
      this.render(null);
      return;
    }
    const token = this.sequence.next();
    const result = await runCli(this.context, "status", folder);
    // An older reply must never replace a newer one.
    if (!this.sequence.accept(token)) {
      return;
    }
    this.render(summarise(result));
  }

  /** Ask separately, on its own cadence — see the module note on decoupling. */
  async refreshManagerState(): Promise<void> {
    const result = await runProjectlessCli(this.context, "focus", ["--probe"]);
    const data = result.envelope?.data as { running?: boolean } | undefined;
    this.managerRunning = typeof data?.running === "boolean"
      ? data.running : null;
    this.render(this.lastSummary);
  }

  private lastSummary: StatusSummary | null = null;

  private render(summary: StatusSummary | null): void {
    this.lastSummary = summary;
    this.item.text = renderText(summary);
    this.item.tooltip = renderTooltip(
      summary, this.pinned?.name ?? "no folder", this.managerRunning);
  }

  dispose(): void {
    this.debouncer.dispose();
    if (this.timer !== undefined) {
      clearInterval(this.timer);
    }
    this.item.dispose();
  }
}
