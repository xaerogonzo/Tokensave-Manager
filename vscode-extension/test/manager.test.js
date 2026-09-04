/**
 * test/manager.test.js — five ledger states, five different things to say.
 *
 * The request inbox distinguishes `pending`, `drained`, `rejected`,
 * `quarantined` and `unknown`, and the reason it bothers is that a missing
 * file cannot tell "delivered" from "never existed" from "refused". That
 * distinction survives only as far as the code rendering it, so most of this
 * file is one test per state asserting the user is told the right one.
 *
 * The two that matter most:
 *
 *   * `pending` after a timeout must NOT read as "the Manager is not
 *     running". Those are different situations — a queued request with the
 *     Manager busy, versus nothing listening at all — and only one of them is
 *     worth acting on.
 *   * `quarantined` must not read as `drained`. It is the state the ledger
 *     exists to preserve.
 */
"use strict";

const assert = require("node:assert");
const { test } = require("node:test");

const { folder, setSettings } = require("./vscode-stub.js");

const { openDialog, resetGeneration } = require("../out/manager.js");
const { MANAGER_ACTIONS } = require("../out/commands.js");

const ROOT = process.platform === "win32" ? "C:\\proj" : "/proj";

/** Collects everything that would have been shown to the user. */
function recordingUi() {
  const shown = [];
  return {
    shown,
    status: (text) => shown.push(["status", text]),
    warning: (text) => shown.push(["warning", text]),
    error: (text) => shown.push(["error", text]),
    warningWithAction: async (text) => {
      shown.push(["warningWithAction", text]);
      return undefined;              // the user dismissed it
    },
  };
}

function filed(overrides) {
  return {
    exitCode: 0,
    transportError: null,
    envelope: {
      schema_version: 1, cli_version: "x", command: "request", ok: true,
      findings: [], warnings: [], error: null,
      data: Object.assign(
        { accepted: true, id: "abc123", duplicate: false,
          manager_running: true },
        overrides || {}),
    },
  };
}

function status(state, detail) {
  return {
    exitCode: 0,
    transportError: null,
    envelope: {
      schema_version: 1, cli_version: "x", command: "request", ok: true,
      findings: [], warnings: [], error: null,
      data: { state, detail: detail || "" },
    },
  };
}

/**
 * Drive `openDialog` against a scripted CLI.
 *
 * `replies` is consumed in order; the first is the filing, the rest are
 * status polls. Sleeping is replaced with a no-op so a backoff loop does not
 * make the suite wait for it.
 */
async function run(replies, options) {
  const cli = require("../out/cli.js");
  const original = cli.runCli;
  const calls = [];
  let index = 0;
  cli.runCli = async (context, command, wsFolder, args) => {
    calls.push({ command, args });
    const reply = replies[Math.min(index, replies.length - 1)];
    index += 1;
    return typeof reply === "function" ? reply(calls.length) : reply;
  };
  const ui = (options && options.ui) || recordingUi();
  try {
    await openDialog({ subscriptions: [], extensionPath: ROOT },
                     folder(ROOT),
                     (options && options.action) || "doctor",
                     (options && options.label) || "Open Doctor",
                     ui, async () => {});
  } finally {
    cli.runCli = original;
  }
  return { ui, calls };
}

test.beforeEach(() => {
  setSettings({ managerPath: ROOT });
  resetGeneration();
});

// ── the table drives the commands ───────────────────────────────────────────

test("every dialog in the table is contributed in package.json", () => {
  const pkg = require("../package.json");
  const contributed = new Set(
    pkg.contributes.commands.map((c) => c.command));
  for (const action of MANAGER_ACTIONS) {
    assert.ok(contributed.has(action.vscode),
              `${action.vscode} is in the table but not contributed`);
  }
});

test("open-project is a different command from focus", () => {
  const pkg = require("../package.json");
  const ids = pkg.contributes.commands.map((c) => c.command);
  // "Open Manager" raises the window; "Open in Manager" names a project.
  // One command doing both would lose the half that names the project.
  assert.ok(ids.includes("tokensaveManager.focus"));
  assert.ok(ids.includes("tokensaveManager.openInManager"));
});

// ── filing ──────────────────────────────────────────────────────────────────

test("the request is filed through the CLI, never built here", async () => {
  const { calls } = await run([filed(), status("drained")]);
  assert.strictEqual(calls[0].command, "request");
  assert.deepStrictEqual(calls[0].args, ["--action", "doctor"]);
  // No id is computed on this side: `write_request` hashes the canonicalised
  // request, and `canonical_project` resolves symlinks before case-folding
  // for reasons that decide authorization.
  assert.ok(!calls[0].args.some((a) => a.includes("abc123")));
});

test("the poll asks about the id the filing returned", async () => {
  const { calls } = await run([filed(), status("drained")]);
  assert.deepStrictEqual(calls[1].args, ["--status", "abc123"]);
});

test("a refused request reports the protocol's own reason", async () => {
  const refusal = {
    exitCode: 2,
    transportError: null,
    envelope: {
      schema_version: 1, cli_version: "x", command: "request", ok: false,
      findings: [], warnings: [],
      error: "project is outside the Manager's configured search roots",
      data: { accepted: false },
    },
  };
  const { ui } = await run([refusal]);
  assert.strictEqual(ui.shown[0][0], "warning");
  assert.match(ui.shown[0][1], /outside the Manager's configured search roots/);
});

test("a transport failure is an error, not a queued request", async () => {
  const { ui } = await run([
    { exitCode: 3, envelope: null, transportError: "could not run python" },
  ]);
  assert.strictEqual(ui.shown[0][0], "error");
  assert.match(ui.shown[0][1], /could not run python/);
});

test("re-filing the same request says already queued", async () => {
  // `write_request` makes an identical pending request a no-op, because the
  // id is a hash of what is being asked for. Claiming a second was filed
  // would be a lie about state the user can go and look at.
  const { ui } = await run([filed({ duplicate: true }), status("drained")]);
  assert.match(ui.shown[0][1], /already queued/);
});

// ── one branch per ledger state ─────────────────────────────────────────────

test("drained reports quietly that the dialog opened", async () => {
  const { ui } = await run([filed(), status("drained")]);
  assert.strictEqual(ui.shown[0][0], "status");
  assert.match(ui.shown[0][1], /opened in the Manager/);
});

test("rejected reports the reason as a warning", async () => {
  const { ui } = await run([filed(), status("rejected", "unknown action")]);
  assert.strictEqual(ui.shown[0][0], "warning");
  assert.match(ui.shown[0][1], /unknown action/);
});

test("quarantined is an error and never reads as delivered", async () => {
  const { ui } = await run([
    filed(), status("quarantined", "five attempts failed")]);
  assert.strictEqual(ui.shown[0][0], "error");
  assert.match(ui.shown[0][1], /quarantined/);
  assert.doesNotMatch(ui.shown[0][1], /opened in the Manager/);
});

test("unknown says there is no record, not that it is still waiting",
     async () => {
       const { ui } = await run([filed(), status("unknown")]);
       assert.strictEqual(ui.shown[0][0], "warning");
       assert.match(ui.shown[0][1], /no record/);
     });

test("pending with the Manager running says queued and nothing more",
     async () => {
       const { ui } = await run([filed({ manager_running: true }),
                                 status("pending")]);
       const last = ui.shown[ui.shown.length - 1];
       assert.strictEqual(last[0], "status");
       assert.match(last[1], /queued/);
       // Crucially NOT a claim about the Manager being absent.
       assert.doesNotMatch(last[1], /not appear to be running/);
     });

test("pending with the Manager absent adds that as a separate sentence",
     async () => {
       const { ui } = await run([filed({ manager_running: false }),
                                 status("pending")]);
       const last = ui.shown[ui.shown.length - 1];
       assert.strictEqual(last[0], "warningWithAction");
       // Both facts, kept apart: the request IS queued, and separately there
       // is nothing listening yet.
       assert.match(last[1], /queued/);
       assert.match(last[1], /does not appear to be running/);
     });

test("a mid-poll transport failure reports pending, not unknown", async () => {
  // The request is filed either way; a failed status call says nothing about
  // it. Reporting "no record" there would be inventing an answer.
  const { ui } = await run([
    filed(),
    { exitCode: 3, envelope: null, transportError: "spawn failed" },
  ]);
  const last = ui.shown[ui.shown.length - 1];
  assert.match(last[1], /queued/);
  assert.doesNotMatch(last[1], /no record/);
});

test("polling stops as soon as the ledger settles", async () => {
  const { calls } = await run([filed(), status("drained"), status("drained")]);
  // one filing plus one status call, not a full backoff sequence
  assert.strictEqual(calls.length, 2);
});

test("polling gives up rather than looping forever", async () => {
  const { calls, ui } = await run([filed(), status("pending")]);
  assert.ok(calls.length > 2, "it should have polled more than once");
  assert.ok(calls.length < 40, `polled ${calls.length} times without stopping`);
  assert.match(ui.shown[ui.shown.length - 1][1], /queued/);
});

// ── the stale-response guard ────────────────────────────────────────────────

test("a slower earlier invocation does not overwrite a later one", async () => {
  const cli = require("../out/cli.js");
  const original = cli.runCli;
  const first = recordingUi();
  const second = recordingUi();

  // ONE instrumented runCli for both invocations, rather than swapping it
  // mid-test: a counter attached to a function that gets replaced records
  // nothing, which is how the first version of this test "passed" its
  // incidental assertion by never running it.
  //
  // The first invocation's polls stay `pending`; the second settles at once.
  // Which is which is decided by how many requests have been filed so far.
  let filings = 0;
  let polls = 0;
  cli.runCli = async (context, command, wsFolder, args) => {
    if (args[0] === "--action") {
      filings += 1;
      return filed();
    }
    polls += 1;
    return filings >= 2 ? status("drained") : status("pending");
  };

  const slow = openDialog({ subscriptions: [], extensionPath: ROOT },
                          folder(ROOT), "doctor", "Open Doctor", first,
                          async () => {});
  // Starting the second bumps the generation, retiring the first.
  await openDialog({ subscriptions: [], extensionPath: ROOT },
                   folder(ROOT), "doctor", "Open Doctor", second,
                   async () => {});
  await slow;
  cli.runCli = original;

  assert.ok(polls >= 2, `only ${polls} status polls were made`);
  assert.match(second.shown[0][1], /opened in the Manager/);
  // The retired invocation says nothing at all. Otherwise its "still queued"
  // message lands after the successful one, and the last message written is
  // the one the user reads.
  assert.deepStrictEqual(first.shown, [], JSON.stringify(first.shown));
});
