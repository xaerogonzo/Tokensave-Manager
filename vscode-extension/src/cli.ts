/**
 * cli.ts — the only place this extension talks to the Manager.
 *
 * The extension is deliberately thin: it renders what the CLI says and does
 * not reimplement any of it. tokensave project attribution, MCP scope
 * precedence, Doctor's rules and test discovery all live in Python, are tested
 * there, and were each the subject of an investigation that is not worth
 * repeating in TypeScript. A second implementation here would be a second
 * thing to be wrong.
 *
 * So this module does exactly four things: find the executable, spawn it with
 * an explicit argv, parse one JSON envelope from stdout, and translate the
 * exit code into something the UI can act on.
 */
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

/** Exit codes are semantic — see src/cli.py. */
export const EXIT = {
  OK: 0,
  FAILED: 1,          // the operation ran and reported problems
  USAGE: 2,           // we built a bad command line — always our bug
  PREREQUISITE: 3,    // a tool or path the CLI needs is missing
  VERIFY_FAILED: 4,   // it ran but could not be verified
} as const;

/** The envelope, exactly as src/cli.py emits it. */
export interface Envelope {
  schema_version: number;
  cli_version: string;
  command: string;
  ok: boolean;
  data: Record<string, unknown>;
  warnings: string[];
  error: string | null;
}

export interface CliResult {
  exitCode: number;
  envelope: Envelope | null;
  /** Set when the CLI could not be run or its output could not be parsed. */
  transportError: string | null;
}

/** The highest schema this extension knows how to read. */
export const SUPPORTED_SCHEMA = 1;

/** How the CLI gets invoked: a command plus any fixed leading arguments. */
export interface Runner {
  command: string;
  prefixArgs: string[];
  /** `source` runs your live `src/cli.py`; `bundled` runs a compiled exe. */
  kind: "source" | "bundled";
}

/**
 * How to invoke the Manager's CLI, or null if it cannot be found.
 *
 * Three modes, most specific first:
 *
 *   1. `cliPath` — an exact executable the user named. Most explicit, so it
 *      wins.
 *   2. `managerPath` — **source mode**, and the better option for anyone with
 *      a checkout. It runs `<managerPath>/src/cli.py` under `pythonPath`, so
 *      the extension always executes the *current* code rather than a snapshot
 *      taken at build time. It also means `manager-config.json` is found
 *      automatically (it sits beside the checkout), and `checks` works —
 *      that command needs a real interpreter, which the frozen build does not
 *      carry.
 *   3. The bundled exe, for users with no checkout.
 *
 * A compiled binary goes stale the moment the Manager changes, and requires a
 * rebuild-repackage-reinstall cycle to catch up. Source mode has none of that,
 * which is why it is documented as the default for developers.
 */
export function resolveRunner(context: vscode.ExtensionContext): Runner | null {
  const settings = vscode.workspace.getConfiguration("tokensaveManager");

  const explicitExe = settings.get<string>("cliPath", "").trim();
  if (explicitExe) {
    return fs.existsSync(explicitExe)
      ? { command: explicitExe, prefixArgs: [], kind: "bundled" }
      : null;
  }

  const managerPath = settings.get<string>("managerPath", "").trim();
  if (managerPath) {
    const script = path.join(managerPath, "src", "cli.py");
    if (!fs.existsSync(script)) {
      return null;
    }
    const python = settings.get<string>("pythonPath", "").trim() || "python";
    return { command: python, prefixArgs: [script], kind: "source" };
  }

  const bundled = path.join(
    context.extensionPath, "bin", "windows-x64", "tokensave-manager-cli.exe");
  return fs.existsSync(bundled)
    ? { command: bundled, prefixArgs: [], kind: "bundled" }
    : null;
}

/**
 * Why the CLI cannot be run, phrased for a human, or null when it can.
 *
 * Returned rather than thrown so the tree can render the reason as a node
 * instead of every command inventing its own error text.
 */
export function cliUnavailableReason(
  context: vscode.ExtensionContext): string | null {
  const settings = vscode.workspace.getConfiguration("tokensaveManager");
  const explicitExe = settings.get<string>("cliPath", "").trim();
  const managerPath = settings.get<string>("managerPath", "").trim();

  if (resolveRunner(context) !== null) {
    return null;
  }
  if (explicitExe) {
    return `Manager CLI not found at the configured path: ${explicitExe}`;
  }
  if (managerPath) {
    return `No src/cli.py under the configured manager path: ${managerPath}`;
  }
  if (process.platform !== "win32") {
    // Only reachable with nothing configured: the bundled binary is a Nuitka
    // Windows build, but source mode works anywhere Python does.
    return "No Manager configured. Set tokensaveManager.managerPath to your "
      + "checkout — the bundled CLI is a Windows-only build.";
  }
  return "No Manager configured — set tokensaveManager.managerPath to your "
    + "checkout (recommended), or tokensaveManager.cliPath to a built CLI.";
}

/**
 * Run one CLI command against one workspace folder.
 *
 * `--project` is always passed explicitly. The CLI requires it, and the reason
 * is the reason this whole integration exists: a process's working directory
 * is not a statement about which project the user means. In a multi-root
 * workspace the caller supplies the folder the tree item belongs to, never
 * "the active one".
 */
export async function runCli(
  context: vscode.ExtensionContext,
  command: string,
  folder: vscode.WorkspaceFolder,
  extraArgs: string[] = [],
): Promise<CliResult> {
  const runner = resolveRunner(context);
  if (runner === null) {
    return {
      exitCode: EXIT.PREREQUISITE,
      envelope: null,
      transportError: cliUnavailableReason(context)
        ?? "Manager CLI unavailable",
    };
  }

  const settings = vscode.workspace.getConfiguration("tokensaveManager");
  const configPath = settings.get<string>("configPath", "").trim();

  const args = [...runner.prefixArgs,
                command, "--project", folder.uri.fsPath, "--json", ...extraArgs];
  // Source mode finds manager-config.json beside the checkout on its own, so
  // --config is only needed when a relocated binary is being used.
  if (configPath) {
    args.push("--config", configPath);
  }

  return new Promise<CliResult>((resolve) => {
    // No shell. The Manager and its projects routinely live under paths with
    // spaces ("D:\\Claude Co worker\\..."), and a shell would re-split them —
    // the same trap the generated tasks.json avoids by using process tasks.
    const child = spawn(runner.command, args,
                        { shell: false, windowsHide: true });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

    child.on("error", (err) => {
      resolve({
        exitCode: EXIT.PREREQUISITE,
        envelope: null,
        transportError:
          `could not run ${path.basename(runner.command)}: ${err.message}`,
      });
    });

    child.on("close", (code) => {
      const exitCode = code ?? EXIT.FAILED;
      // A usage error deliberately leaves stdout empty, so an unparseable
      // stdout is expected there and is our bug, not the user's.
      if (!stdout.trim()) {
        resolve({
          exitCode,
          envelope: null,
          transportError: exitCode === EXIT.USAGE
            ? `the extension built an invalid command line: ${stderr.trim()}`
            : (stderr.trim() || "the CLI produced no output"),
        });
        return;
      }
      try {
        const envelope = JSON.parse(stdout) as Envelope;
        if (envelope.schema_version > SUPPORTED_SCHEMA) {
          resolve({
            exitCode,
            envelope,
            transportError:
              `Manager CLI ${envelope.cli_version} speaks envelope schema `
              + `${envelope.schema_version}; this extension understands `
              + `${SUPPORTED_SCHEMA}. Update the extension.`,
          });
          return;
        }
        resolve({ exitCode, envelope, transportError: null });
      } catch (err) {
        resolve({
          exitCode,
          envelope: null,
          transportError:
            `could not parse the CLI's output: ${(err as Error).message}`,
        });
      }
    });
  });
}

/**
 * A one-line summary of a result, for a tree item's description.
 *
 * Reads only fields the envelope guarantees. Anything richer belongs in the
 * Manager, which is still the full UI.
 */
export function summarise(result: CliResult): string {
  if (result.transportError) {
    return `⚠ ${result.transportError}`;
  }
  const env = result.envelope;
  if (!env) {
    return "⚠ no result";
  }
  if (env.error) {
    return `⚠ ${env.error}`;
  }
  switch (result.exitCode) {
    case EXIT.OK:
      return "✓ ok";
    case EXIT.VERIFY_FAILED:
      return "? could not verify";
    case EXIT.PREREQUISITE:
      return "⚠ prerequisite missing";
    default:
      return "⚠ problems found";
  }
}
