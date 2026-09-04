/**
 * manager.ts — asking the *running* Manager to open a dialog.
 *
 * Everything else in this extension talks to the Manager's CLI, which is a
 * fresh process that reads files and exits. This is the one channel to the
 * Manager a person actually has open, and it goes through the request inbox in
 * `helpers/manager_ipc.py` rather than a socket or a port: nothing to
 * firewall, nothing to leave listening, and every part of it testable without
 * a GUI.
 *
 * ## Propose-only still holds
 *
 * Every action here opens a dialog in front of a person. None commits, applies
 * or approves anything. Writing the request *is* a state change — that is what
 * an inbox is — so the invariant is about what the Manager does at the other
 * end, not a claim that nothing is written.
 *
 * ## The extension never computes a request id
 *
 * `manager_ipc.write_request` derives the id from a SHA-256 of the
 * canonicalised request, and `canonical_project` does `realpath` before
 * `normcase` for reasons that are security-relevant: two spellings of one
 * directory must not produce two authorization verdicts. Reproducing that in
 * TypeScript would mean a second implementation of a cryptographic identity,
 * "almost the same" — so requests are filed **through the CLI** and Python
 * stays the only thing that knows how an id is built.
 *
 * ## Absence is never success
 *
 * The ledger distinguishes five states and this file renders all five, plus a
 * transport failure and a timeout. In particular `pending` is not `unknown`
 * and neither of them is "the Manager is not running" — those are three
 * different situations with three different things for the user to do, and
 * collapsing them is how a queued request looks like a delivered one.
 */
import * as vscode from "vscode";
import { CliResult, EXIT, runCli } from "./cli";
import { MANAGER_ACTIONS } from "./commands";

/**
 * Gaps between polls, in ms. Widening, so a fast acknowledgement is still fast.
 *
 * The list is also the budget: the loop polls once immediately and then once
 * per entry, so it makes at most `POLL_BACKOFF_MS.length + 1` calls and waits
 * about six seconds in total. Bounding by attempts rather than by a wall-clock
 * deadline keeps it deterministic — a caller that supplies its own `sleep`
 * (the tests do) gets the same number of polls rather than a loop that spins
 * as fast as the machine allows until a timer it cannot control expires.
 */
const POLL_BACKOFF_MS = [150, 300, 600, 900, 1200, 1500, 1500];

/** What `request --status` reports. All five are rendered; none is inferred. */
type RequestState =
  | "pending" | "drained" | "rejected" | "quarantined" | "unknown";

interface FiledRequest {
  accepted: boolean;
  id: string;
  duplicate: boolean;
  manager_running: boolean;
}

/**
 * Monotonic ticket per invocation, so a slow poll cannot overwrite a fast one.
 *
 * A user who clicks "Open Doctor" twice gets two polling loops. Without this
 * the first one's timeout message can land after the second one has already
 * reported success, and the last message written is the one the user reads.
 */
let generation = 0;

export function registerManagerBridge(
  context: vscode.ExtensionContext,
  pickFolder: () => Promise<vscode.WorkspaceFolder | undefined>,
): void {
  for (const action of MANAGER_ACTIONS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(action.vscode, async () => {
        const folder = await pickFolder();
        if (folder) {
          await openDialog(context, folder, action.action, action.label);
        }
      }));
  }
}

/**
 * File a request and report honestly what became of it.
 *
 * Exported for the tests, which drive it directly rather than through the
 * command registry: what is worth asserting is the mapping from a ledger state
 * to what the user is told, and going through `executeCommand` would test the
 * registry instead.
 */
export async function openDialog(
  context: vscode.ExtensionContext,
  folder: vscode.WorkspaceFolder,
  action: string,
  label: string,
  ui: Ui = defaultUi,
  sleep: (ms: number) => Promise<void> = realSleep,
): Promise<void> {
  const ticket = ++generation;

  const filed = await runCli(context, "request", folder,
                             ["--action", action]);
  if (filed.transportError || !filed.envelope) {
    ui.error(`TokenSave Manager: ${filed.transportError ?? "no result"}`);
    return;
  }
  if (filed.exitCode === EXIT.USAGE) {
    // The protocol refused it — a bad action, or a project outside the
    // Manager's configured search roots. Its own words are better than ours.
    ui.warning(`TokenSave Manager: ${filed.envelope.error ?? "request refused"}`);
    return;
  }
  const written = filed.envelope.data as unknown as FiledRequest;
  if (!written?.id) {
    ui.error("TokenSave Manager: the request was not given an id.");
    return;
  }

  if (written.duplicate) {
    // `write_request` treats an identical pending request as a no-op, because
    // the id is a hash of what is being asked for. Saying "already queued"
    // beats pretending a second one was filed.
    ui.status(`TokenSave: ${label} is already queued`);
  }

  const outcome = await pollUntilSettled(
    context, folder, written.id, ticket, sleep);

  if (ticket !== generation) {
    // A later invocation has taken over. Its answer is the current one, and
    // ours would overwrite it with a message about an older request.
    return;
  }

  switch (outcome.state) {
    case "drained":
      ui.status(`TokenSave: ${label} — opened in the Manager`);
      return;
    case "rejected":
      ui.warning(
        `TokenSave Manager refused the request: ${outcome.detail || "no reason given"}`);
      return;
    case "quarantined":
      // Not a slow request. The Manager tried, failed repeatedly, and filed it
      // away — which is a defect to look at, not something to wait longer for.
      ui.error(
        `TokenSave Manager quarantined the request: ${outcome.detail || "no reason given"}`);
      return;
    case "unknown":
      ui.warning(
        "TokenSave Manager has no record of that request for this project.");
      return;
    default:
      // Still pending when we stopped waiting. That is not a failure and not
      // an unknown — the request is sitting in the inbox and will be picked
      // up. Whether the Manager is running is a SEPARATE fact, reported as a
      // separate sentence, because "queued" and "nothing is listening" are
      // different situations with different things to do about them.
      await reportStillPending(ui, label, written.manager_running);
  }
}

interface Settled {
  state: RequestState;
  detail: string;
}

async function pollUntilSettled(
  context: vscode.ExtensionContext,
  folder: vscode.WorkspaceFolder,
  id: string,
  ticket: number,
  sleep: (ms: number) => Promise<void>,
): Promise<Settled> {
  for (let attempt = 0; attempt <= POLL_BACKOFF_MS.length; attempt += 1) {
    const result: CliResult = await runCli(
      context, "request", folder, ["--status", id]);
    if (ticket !== generation) {
      return { state: "pending", detail: "" };
    }
    if (result.transportError || !result.envelope) {
      // A transport failure mid-poll says nothing about the request, which is
      // still filed. Reporting it as pending is the truthful reading; saying
      // "no record" would be inventing an answer from a failure to ask.
      return { state: "pending", detail: "" };
    }
    const data = result.envelope.data as { state?: string; detail?: string };
    const state = (data.state ?? "unknown") as RequestState;
    if (state !== "pending") {
      return { state, detail: data.detail ?? "" };
    }
    if (attempt < POLL_BACKOFF_MS.length) {
      await sleep(POLL_BACKOFF_MS[attempt]);
    }
  }
  return { state: "pending", detail: "" };
}

async function reportStillPending(ui: Ui, label: string,
                                  managerRunning: boolean): Promise<void> {
  const queued = `TokenSave: ${label} is queued; the Manager has not `
    + "acknowledged it yet.";
  if (managerRunning) {
    ui.status(queued);
    return;
  }
  const choice = await ui.warningWithAction(
    `${queued} The Manager does not appear to be running, so it will open `
    + "the next time it starts.", "Open Manager");
  if (choice) {
    await vscode.commands.executeCommand("tokensaveManager.focus");
  }
}

/**
 * How this file talks to the user.
 *
 * Injected so the tests can read what would have been shown. Every branch
 * above produces exactly one message, and the tests assert which one — the
 * whole point being that five ledger states must not collapse into two.
 */
export interface Ui {
  status(text: string): void;
  warning(text: string): void;
  error(text: string): void;
  warningWithAction(text: string, action: string): Promise<string | undefined>;
}

const defaultUi: Ui = {
  status: (text) => vscode.window.setStatusBarMessage(text, 4000),
  warning: (text) => { void vscode.window.showWarningMessage(text); },
  error: (text) => { void vscode.window.showErrorMessage(text); },
  warningWithAction: (text, action) =>
    Promise.resolve(vscode.window.showWarningMessage(text, action)) as
      Promise<string | undefined>,
};

const realSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Reset the invocation counter. Tests only. */
export function resetGeneration(): void {
  generation = 0;
}
