/**
 * diagnostics.ts — envelope `findings` rendered as VS Code Diagnostics.
 *
 * This is the consumer half of a contract whose producing half lives in
 * Python. The division is deliberate and worth restating, because it is the
 * thing most likely to erode: **Python owns rules, positions and severity; the
 * envelope is the boundary; this file renders.** There is exactly one
 * transformation here that is legitimately TypeScript's, and it is converting
 * 1-based coordinates to VS Code's 0-based `Position`.
 *
 * If a rule ever seems to belong here — "warnings from X should really be
 * errors", "these two findings are duplicates" — that is drift. It belongs in
 * the producer, where it can be tested against the tool's real output.
 */
import * as vscode from "vscode";
import { Finding } from "./cli";

/**
 * The replacement unit: one workspace folder, one command.
 *
 * A command replaces its own results wholesale and touches nobody else's, so
 * `scout` finishing leaves `checks` in the same folder — and `scout` in a
 * sibling folder — exactly as they were.
 *
 * Note this is deliberately NOT keyed by producer as well. `checks` emits both
 * `pyflakes` and `compileall` findings from a single run, so a producer-level
 * key would be finer-grained than any replacement that actually happens, and
 * the only thing it could add is a bug: a producer that reported findings on
 * one run and none on the next would leave its old partition stranded, because
 * nothing in the new result would name it. The producer still travels on each
 * diagnostic, as `source`.
 */
function partitionKey(folder: vscode.WorkspaceFolder, command: string): string {
  return `${folder.uri.toString()}|${command}`;
}

/** `pyflakes` out of `pyflakes`, `compileall` out of `compileall/SyntaxError`. */
function producerOf(rule: string): string {
  return (rule || "manager").split("/")[0] || "manager";
}

function severityOf(finding: Finding): vscode.DiagnosticSeverity {
  // A closed set, chosen by the producer. The default is only reached if the
  // CLI ever emits a severity this extension has not been taught, and warning
  // is the right place to land: visible, but not claiming to be an error.
  switch (finding.severity) {
    case "error":
      return vscode.DiagnosticSeverity.Error;
    case "information":
      return vscode.DiagnosticSeverity.Information;
    case "hint":
      return vscode.DiagnosticSeverity.Hint;
    case "warning":
    default:
      return vscode.DiagnosticSeverity.Warning;
  }
}

/**
 * The one coordinate conversion in the whole integration.
 *
 * The envelope is 1-based throughout — Python's convention, and what
 * `File "...", line N` and pyflakes both report. VS Code is 0-based. Doing
 * this in exactly one place is what stops a finding drifting a line every time
 * it passes through another function.
 *
 * `end_*` are always present (the producer fills them from the start when it
 * only knows a point), but they are defended here anyway: a zero-width range
 * is a legitimate value and must not be turned into a negative one by clamping
 * in the wrong direction.
 */
function rangeOf(finding: Finding): vscode.Range {
  const line = Math.max(0, (finding.line ?? 1) - 1);
  const column = Math.max(0, (finding.column ?? 1) - 1);
  const endLine = Math.max(line, (finding.end_line ?? finding.line ?? 1) - 1);
  const endColumnRaw = (finding.end_column ?? finding.column ?? 1) - 1;
  // Only clamp against the start when the range is on a single line; on a
  // multi-line range an end column smaller than the start is normal.
  const endColumn = endLine === line
    ? Math.max(column, endColumnRaw)
    : Math.max(0, endColumnRaw);
  return new vscode.Range(line, column, endLine, endColumn);
}

/** Build the diagnostic for one finding. Exported for testing. */
export function toDiagnostic(finding: Finding): vscode.Diagnostic {
  const diagnostic = new vscode.Diagnostic(
    rangeOf(finding), finding.message ?? "", severityOf(finding));
  // `source` is what the Problems panel shows in parentheses, so it should be
  // the thing that produced the finding, not the command that asked for it.
  diagnostic.source = producerOf(finding.rule);
  if (finding.rule) {
    diagnostic.code = finding.rule;
  }
  return diagnostic;
}

/**
 * Holds every partition and keeps the editor's view of them consistent.
 *
 * One VS Code collection; the partitioning is ours. A file can carry findings
 * from several commands at once, so the collection entry for a file is always
 * the union across partitions, recomputed whenever one of them changes.
 */
export class DiagnosticStore {
  private readonly collection: vscode.DiagnosticCollection;
  /** partition key → (file fsPath → diagnostics) */
  private readonly partitions = new Map<string, Map<string, vscode.Diagnostic[]>>();

  constructor(collection: vscode.DiagnosticCollection) {
    this.collection = collection;
  }

  /**
   * Swap in the results of one completed command.
   *
   * **Called on completion, never on start.** Clearing when a command begins
   * would empty the Problems panel for as long as the run takes — worst
   * exactly where runs are longest — and leave the user staring at a clean
   * bill of health that has not been earned yet. The previous results stay
   * visible until there is something truer to replace them with.
   *
   * Findings carry repo-relative paths; joining them to the folder is this
   * side's job, because only the caller knows which project the result came
   * from. That is the same reason the CLI insists on an explicit `--project`.
   */
  replace(folder: vscode.WorkspaceFolder, command: string,
          findings: readonly Finding[]): void {
    const next = new Map<string, vscode.Diagnostic[]>();
    for (const finding of findings) {
      if (!finding?.file) {
        continue;                 // nothing to attach a squiggle to
      }
      const uri = vscode.Uri.joinPath(folder.uri, finding.file);
      const key = uri.fsPath;
      const list = next.get(key) ?? [];
      list.push(toDiagnostic(finding));
      next.set(key, list);
    }

    const key = partitionKey(folder, command);
    const previous = this.partitions.get(key);
    if (next.size === 0) {
      this.partitions.delete(key);
    } else {
      this.partitions.set(key, next);
    }

    // Only files this partition touched — before or after — can have changed.
    const affected = new Set<string>([
      ...(previous?.keys() ?? []),
      ...next.keys(),
    ]);
    for (const fsPath of affected) {
      this.rebuild(fsPath);
    }
  }

  /** Drop everything for one folder, e.g. when it leaves the workspace. */
  forgetFolder(folder: vscode.WorkspaceFolder): void {
    const prefix = `${folder.uri.toString()}|`;
    const affected = new Set<string>();
    for (const [key, files] of this.partitions) {
      if (key.startsWith(prefix)) {
        for (const fsPath of files.keys()) {
          affected.add(fsPath);
        }
        this.partitions.delete(key);
      }
    }
    for (const fsPath of affected) {
      this.rebuild(fsPath);
    }
  }

  clear(): void {
    this.partitions.clear();
    this.collection.clear();
  }

  /** Diagnostics currently shown for a file, across every partition. */
  private rebuild(fsPath: string): void {
    const merged: vscode.Diagnostic[] = [];
    for (const files of this.partitions.values()) {
      const list = files.get(fsPath);
      if (list) {
        merged.push(...list);
      }
    }
    const uri = vscode.Uri.file(fsPath);
    if (merged.length === 0) {
      this.collection.delete(uri);
      return;
    }
    this.collection.set(uri, merged);
  }
}
