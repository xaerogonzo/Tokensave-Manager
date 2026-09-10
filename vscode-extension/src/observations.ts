/**
 * observations.ts — the editor's Problems panel, captured for the Manager.
 *
 * **Separate from `diagnostics.ts`, and it has to be.** That file holds one
 * law — *"Python owns rules, positions and severity; the envelope is the
 * boundary; this file renders"* — and it describes the outbound direction.
 * What travels the other way is a verdict somebody else already rendered:
 * Pylance's `reportOptionalMemberAccess` is not a Manager rule and never will
 * be. Routing it through `findings` would break the one invariant that file
 * protects, using the feature that reuses it.
 *
 * ## The direction of travel, and there is no return arrow
 *
 *     findings ──> DiagnosticCollection ──> observations.json
 *
 * This module MAY read the collection and the store, and MAY write the
 * snapshot. It must never mutate the collection or any Manager finding, and
 * `observations` is deliberately absent from `DIAGNOSTIC_COMMANDS` so nothing
 * can push these back in.
 *
 * ## The echo loop
 *
 * `vscode.languages.getDiagnostics()` returns **every** collection's
 * diagnostics, including the `tokensave` collection this extension owns. A
 * naive capture therefore reads back the Manager's own findings and files them
 * as a *second source agreeing with the first*. Worse, if they were ever
 * rendered back, each capture would double the count.
 *
 * The fix is **multiset subtraction**, not a set difference and not a filter on
 * `source`:
 *
 *   - A `Set` is wrong because two genuinely identical diagnostics at one
 *     position are two diagnostics; subtracting a set would remove both when
 *     we own one.
 *   - Filtering by `source` is wrong because a real third-party pyflakes
 *     extension also sets `source: "pyflakes"`, and a name filter would
 *     silently drop an independent source — the exact thing a second opinion
 *     exists to provide.
 *
 * ## Write authority
 *
 * This extension writes exactly two files, both in the **gitignored**
 * `.tokensave-manager/` namespace: `commit_request.json` (a proposal) and
 * `observations.json` (a display cache). It never runs `git commit`, never
 * applies a proposal, never approves anything, and never writes a tracked file.
 */
import * as vscode from "vscode";
import { DiagnosticStore } from "./diagnostics";

/**
 * Bumped when this envelope changes incompatibly.
 *
 * A **third** version namespace, separate from the CLI envelope's
 * `schema_version` and the request inbox's `request_schema_version`. Three
 * formats, three producers, three compatibility windows. The Python reader
 * refuses an unknown version outright rather than parsing it best-effort.
 */
export const OBSERVATIONS_SCHEMA_VERSION = 1;

const SNAPSHOT_DIR = ".tokensave-manager";
const SNAPSHOT_FILE = "observations.json";

/** One already-rendered diagnostic, in the shape the envelope carries it. */
export interface Observation {
  file: string;
  /** 1-based. See `toObservation`. */
  line: number;
  column: number;
  end_line: number;
  end_column: number;
  severity: "error" | "warning" | "information" | "hint";
  message: string;
  /** The bare rule code, unqualified. `producer` carries who said it. */
  rule: string;
  producer: string;
}

function severityOf(value: vscode.DiagnosticSeverity): Observation["severity"] {
  switch (value) {
    case vscode.DiagnosticSeverity.Error:
      return "error";
    case vscode.DiagnosticSeverity.Information:
      return "information";
    case vscode.DiagnosticSeverity.Hint:
      return "hint";
    default:
      return "warning";
  }
}

/**
 * A diagnostic's rule code as a bare string.
 *
 * VS Code allows three shapes here — a string, a number, or an object with a
 * `value` and a link target. Reading only the first produces an empty code for
 * every diagnostic that used one of the others, which silently destroys the
 * dedup identity for exactly the richer sources worth comparing.
 *
 * **Unqualified on purpose.** Identity is `(file, line, column, code)`, and
 * pyright run headlessly and Pylance running in the editor report the same code
 * for the same defect. Prefixing the producer here would make them look like
 * different findings, which is the one comparison this whole feature exists to
 * make.
 */
export function ruleOf(diagnostic: vscode.Diagnostic): string {
  const code = diagnostic.code;
  if (code === undefined || code === null) {
    return "";
  }
  if (typeof code === "string" || typeof code === "number") {
    return String(code);
  }
  const value = (code as { value?: string | number }).value;
  return value === undefined || value === null ? "" : String(value);
}

/**
 * One diagnostic, converted at the boundary.
 *
 * **VS Code is 0-based; every Manager envelope is 1-based.** `diagnostics.ts`
 * does this conversion once in the outbound direction and this is its inbound
 * twin. Forwarding the raw position puts every row one line above the code it
 * describes, and the Python reader rejects a 0 rather than accepting it,
 * so getting this wrong fails loudly instead of drifting.
 */
export function toObservation(uri: vscode.Uri, folderPath: string,
                              diagnostic: vscode.Diagnostic): Observation {
  const rest = uri.fsPath.startsWith(folderPath)
    ? uri.fsPath.slice(folderPath.length) : uri.fsPath;
  return {
    file: rest.replace(/^[\\/]+/, "").split("\\").join("/"),
    line: diagnostic.range.start.line + 1,
    column: diagnostic.range.start.character + 1,
    end_line: diagnostic.range.end.line + 1,
    end_column: diagnostic.range.end.character + 1,
    severity: severityOf(diagnostic.severity),
    message: diagnostic.message ?? "",
    rule: ruleOf(diagnostic),
    producer: diagnostic.source ?? "",
  };
}

/** What makes two diagnostics the same OBJECT for subtraction purposes. */
function fingerprint(diagnostic: vscode.Diagnostic): string {
  const range = diagnostic.range;
  return [
    range.start.line, range.start.character,
    range.end.line, range.end.character,
    diagnostic.severity,
    diagnostic.source ?? "",
    ruleOf(diagnostic),
    diagnostic.message ?? "",
  ].join("\u0000");
}

/**
 * `editor` minus `owned`, **with multiplicity preserved**.
 *
 * Removes ONE occurrence per owned diagnostic. Exported because this is the
 * whole correctness argument and it is worth testing without an editor:
 *
 *     store   2 identical Manager diagnostics
 *     editor  those same 2, plus 1 from Pylance
 *     result  exactly 1
 *
 * A `Set`-based implementation returns 0 or 2 there, and both look plausible
 * until someone counts.
 */
export function subtractOwned(
  editor: readonly vscode.Diagnostic[],
  owned: readonly vscode.Diagnostic[],
): vscode.Diagnostic[] {
  const budget = new Map<string, number>();
  for (const diagnostic of owned) {
    const key = fingerprint(diagnostic);
    budget.set(key, (budget.get(key) ?? 0) + 1);
  }
  const kept: vscode.Diagnostic[] = [];
  for (const diagnostic of editor) {
    const key = fingerprint(diagnostic);
    const remaining = budget.get(key) ?? 0;
    if (remaining > 0) {
      budget.set(key, remaining - 1);      // one occurrence, not all of them
      continue;
    }
    kept.push(diagnostic);
  }
  return kept;
}

/** Coverage: what the source looked at, and what it cannot say. */
export interface Coverage {
  diagnostic_entries: number;
  files_with_diagnostics: number;
  /**
   * **Always null.** `getDiagnostics()` returns resources that HAVE
   * diagnostics; it does not enumerate what was analysed, so a clean file
   * Pylance examined and a file it never opened are indistinguishable here.
   * Reporting a number would be inventing one, and the Python side keeps this
   * three-valued precisely so it can stay unknown.
   */
  analyzed_files: null;
  /**
   * The configured analysis POLICY, read rather than assumed —
   * `openFilesOnly` is only the default. It is not evidence that anything in
   * the declared population was analysed at capture time.
   */
  diagnostic_mode: string;
  /** Knowable, and NOT the analyzed set. Reported so the two are not confused. */
  open_documents: number;
  scope: string;
}

export interface Envelope {
  observations_schema_version: number;
  captured_at: number;
  editor_label: string;
  coverage: Coverage;
  observations: Observation[];
}

/**
 * Build the envelope for one workspace folder. Pure apart from reading config.
 *
 * `entries` is what `vscode.languages.getDiagnostics()` returned; `store` is
 * ours, and is only ever read.
 */
export function buildEnvelope(
  folder: vscode.WorkspaceFolder,
  entries: ReadonlyArray<[vscode.Uri, vscode.Diagnostic[]]>,
  store: DiagnosticStore,
  now: number = Math.floor(Date.now() / 1000),
): Envelope {
  const folderPath = folder.uri.fsPath;
  const rows: Observation[] = [];
  const files = new Set<string>();

  for (const [uri, diagnostics] of entries) {
    if (!uri.fsPath.startsWith(folderPath)) {
      continue;                       // a sibling folder's problem, not ours
    }
    const foreign = subtractOwned(diagnostics, store.ownedFor(uri.fsPath));
    for (const diagnostic of foreign) {
      rows.push(toObservation(uri, folderPath, diagnostic));
      files.add(uri.fsPath);
    }
  }

  const analysis = vscode.workspace.getConfiguration(
    "python", folder.uri).get<string>("analysis.diagnosticMode", "");

  return {
    observations_schema_version: OBSERVATIONS_SCHEMA_VERSION,
    captured_at: now,
    editor_label: vscode.env?.appName ?? "editor",
    coverage: {
      diagnostic_entries: rows.length,
      files_with_diagnostics: files.size,
      analyzed_files: null,
      diagnostic_mode: analysis,
      open_documents: (vscode.workspace.textDocuments ?? []).length,
      scope: "files the editor has diagnostics for",
    },
    observations: rows,
  };
}

/**
 * Write the snapshot atomically.
 *
 * Temp-then-rename, for the reason `manager_ipc._atomic_write_json` already
 * records: the Manager reads while the extension writes, and without this it
 * observes a truncated file and reports a malformed snapshot that was never
 * malformed.
 */
async function writeSnapshot(folder: vscode.WorkspaceFolder,
                             envelope: Envelope): Promise<vscode.Uri> {
  const dir = vscode.Uri.joinPath(folder.uri, SNAPSHOT_DIR);
  await vscode.workspace.fs.createDirectory(dir);
  const target = vscode.Uri.joinPath(dir, SNAPSHOT_FILE);
  const temp = vscode.Uri.joinPath(dir, `${SNAPSHOT_FILE}.tmp`);
  const bytes = Buffer.from(JSON.stringify(envelope, null, 2), "utf8");
  await vscode.workspace.fs.writeFile(temp, bytes);
  await vscode.workspace.fs.rename(temp, target, { overwrite: true });
  return target;
}

/** Capture the Problems panel for one folder. Returns what was written. */
export async function captureProblems(
  folder: vscode.WorkspaceFolder, store: DiagnosticStore,
): Promise<Envelope> {
  const entries = vscode.languages.getDiagnostics();
  const envelope = buildEnvelope(folder, entries, store);
  await writeSnapshot(folder, envelope);
  return envelope;
}

export function registerObservations(
  context: vscode.ExtensionContext,
  store: DiagnosticStore,
  pickFolder: () => Promise<vscode.WorkspaceFolder | undefined>,
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "tokensaveManager.captureProblems", async () => {
        const folder = await pickFolder();
        if (!folder) {
          return;
        }
        try {
          const envelope = await captureProblems(folder, store);
          vscode.window.setStatusBarMessage(
            `TokenSave: captured ${envelope.coverage.diagnostic_entries} `
            + `observation(s) from ${envelope.coverage.files_with_diagnostics}`
            + " file(s)", 4000);
        } catch (err) {
          vscode.window.showWarningMessage(
            "TokenSave Manager: could not write the Problems snapshot: "
            + `${(err as Error).message}`);
        }
      }),
  );

  // Off by default, the same posture `checksOnSave` takes. A capture on every
  // save is useful to some people and noise to others, and the quiet default is
  // the one that cannot surprise anybody.
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((document) => {
      const settings = vscode.workspace.getConfiguration("tokensaveManager");
      if (!settings.get<boolean>("captureProblemsOnSave", false)) {
        return;
      }
      const folder = vscode.workspace.getWorkspaceFolder(document.uri);
      if (folder) {
        // Silent on failure: this runs on every save, and a notification per
        // failed save is how a helpful feature gets turned off.
        void captureProblems(folder, store).catch(() => undefined);
      }
    }),
  );
}
