/**
 * mutations.js — break each property on purpose, and require the suite to
 * notice.
 *
 * A suite that passes on its first run is what a suite testing nothing looks
 * like. Booting a real editor makes it *look* convincing, and looking
 * convincing is not evidence. So for every property these tests claim to
 * protect, this removes it from the compiled output and requires a failure —
 * from the test written for it, not from whatever happened to trip.
 *
 * ## Four details that make it worth having
 *
 * **It mutates `out/`, never `src/`.** A crash mid-run then cannot leave the
 * repository edited. Bytes are hashed before the edit and re-hashed after
 * restoration, and restoration happens in a `finally`, so a timeout or a
 * ctrl-c cannot leave a mutated artifact behind either.
 *
 * **Each arm names the test that must object.** A mutation caught by an
 * unrelated test is not covered, it is lucky, and the two look identical in a
 * summary that only counts failures.
 *
 * **A pattern that no longer matches is reported NOT APPLIED, loudly.** A
 * refactor renames something, the mutation silently stops applying, and the
 * arm reads as a pass forever. That is the failure mode this file exists to
 * avoid, so it is an error rather than a warning.
 *
 * **Test counts are compared against a control.** An arm that errors early
 * runs fewer tests, and "no failures" then means "nothing ran".
 *
 * ## Tiers
 *
 * Every arm declares whether the property is reachable from the fast stub
 * suite, the live editor suite, or both. Spending a minute of Electron on a
 * severity-mapping mutation that `node --test` catches in a second is how a
 * mutation suite becomes too slow to run, which is how it dies.
 */
"use strict";

const cp = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const EXTENSION_ROOT = path.resolve(__dirname, "..", "..");
const OUT = path.join(EXTENSION_ROOT, "out");

const FAST = "fast";
const LIVE = "live";

/**
 * One mutation. `find` must appear exactly once in `file`, or the arm is
 * NOT APPLIED.
 *
 * `expect` is a substring of the name of the test that must object. `tier`
 * says which suite is run to find out.
 */
const MUTATIONS = [
  // ── reachable from the fast suite ──────────────────────────────────────
  {
    name: "an unknown status renders as clean",
    file: "status.js",
    find: 'return "$(database) TokenSave: ?";',
    replace: 'return "$(database) TokenSave: ok";',
    expect: "unreadable status is summarised as unknown",
    tier: FAST,
  },
  {
    name: "the stale-response guard accepts anything",
    file: "status.js",
    find: "if (token <= this.applied) {",
    replace: "if (false) {",
    expect: "overtaken",
    tier: FAST,
  },
  {
    name: "diagnostic lines stay 1-based",
    file: "diagnostics.js",
    find: "const line = Math.max(0, (finding.line ?? 1) - 1);",
    replace: "const line = Math.max(0, (finding.line ?? 1));",
    expect: "1-based",
    tier: FAST,
  },
  {
    name: "every nonce is the same",
    file: "savings.js",
    find: "out += alphabet.charAt(Math.floor(Math.random() * alphabet.length));",
    replace: 'out += "A";',
    expect: "each nonce is different",
    tier: FAST,
  },

  // ── only reachable with a real editor ──────────────────────────────────
  {
    name: "tree rows lose their label",
    file: "extension.js",
    find: 'const row = "  ".repeat(depth) + label +',
    replace: 'const row = "  ".repeat(depth) + "" +',
    expect: "offers every action under the folder",
    tier: LIVE,
  },
  {
    name: "un-run actions report a verdict instead of 'not run'",
    file: "tree.js",
    find: 'item.description = result ? (0, cli_1.summarise)(result) : "not run";',
    replace: 'item.description = result ? (0, cli_1.summarise)(result) : "";',
    expect: "not run",
    tier: LIVE,
  },
  {
    name: "the unavailable row loses its fix command",
    file: "tree.js",
    find: 'fixCommand: "tokensaveManager.setManagerPath",',
    replace: 'fixCommand: "tokensaveManager.refresh",',
    expect: "wired to the command that provides it",
    tier: LIVE,
  },
  {
    name: "a contributed command is never registered",
    file: "extension.js",
    find: 'vscode.commands.registerCommand("tokensaveManager.setManagerPath"',
    replace: 'vscode.commands.registerCommand("tokensaveManager.notRegistered"',
    expect: "registers every command the manifest declares",
    tier: LIVE,
  },
  {
    name: "the CSP meta tag is dropped",
    file: "savings.js",
    find: '<meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}">',
    replace: "",
    expect: "deny-by-default Content-Security-Policy",
    tier: LIVE,
  },
  {
    name: "the script nonce is not the one the CSP allows",
    file: "savings.js",
    find: '<script nonce="${escapeAttr(nonce)}">',
    replace: '<script nonce="${escapeAttr(makeNonce())}">',
    expect: "ties the script's nonce",
    tier: LIVE,
  },
  {
    name: "the status bar follows the active editor instead of its pin",
    file: "status.js",
    find: "const folder = this.pinned;",
    replace: "const folder = (vscode.workspace.workspaceFolders ?? [])[0] " +
             "&& vscode.window.activeTextEditor " +
             "? vscode.workspace.getWorkspaceFolder(" +
             "vscode.window.activeTextEditor.document.uri) : this.pinned;",
    expect: "stays about the pinned folder",
    tier: LIVE,
  },
  {
    name: "a per-file run clears every file's diagnostics",
    file: "diagnostics.js",
    find: "this.collection.delete(uri);",
    replace: "this.collection.clear();",
    expect: "never looked at",
    tier: LIVE,
  },
  {
    name: "the webview rewrites its document on every refresh",
    file: "savings.js",
    find: "void view.webview.postMessage({ type: \"loading\" });",
    replace: "view.webview.html = this.html(view.webview); this.renders++;",
    expect: "writes the document exactly once",
    tier: LIVE,
  },
];

// ── running a suite ──────────────────────────────────────────────────────────

function runSuite(tier) {
  // `node` directly rather than `npm run`. Three reasons, and the third is
  // the one that matters:
  //
  //   * Node 24 refuses to spawn `npm.cmd` without a shell (EINVAL, post
  //     CVE-2024-27980), and passing an args array *through* a shell
  //     concatenates without escaping — this checkout's own path has spaces
  //     in it, so that breaks here rather than theoretically.
  //   * One less layer between a failure and its output.
  //   * `npm test` runs `pretest`, which recompiles. That would overwrite the
  //     mutation with correct code and report every arm as SURVIVED — a
  //     mutation runner silently testing nothing, which is precisely the
  //     failure this file exists to detect in others.
  const args = tier === FAST
    ? ["--test", "test/cli.test.js", "test/diagnostics.test.js",
       "test/surfaces.test.js"]
    : ["test/integration/runTests.js"];
  const r = cp.spawnSync(process.execPath, args, {
    cwd: EXTENSION_ROOT,
    encoding: "utf8",
    env: { ...process.env, TOKENSAVE_TEST_FOCUS: "",
           TOKENSAVE_TEST_MINDER: "external" },
  });
  return { code: r.status, output: `${r.stdout || ""}${r.stderr || ""}` };
}

/** How many tests actually ran, from whichever runner produced the output. */
function testCount(output, tier) {
  if (tier === LIVE) {
    const m = /TOKENSAVE_TEST_COUNTS total=(\d+)/.exec(output);
    return m ? Number(m[1]) : null;
  }
  const m = /^. tests (\d+)$/m.exec(output);
  return m ? Number(m[1]) : null;
}

/**
 * Names of tests that failed, for the "was it the right one" check.
 *
 * Both runners mark a failure with `✖ <name>`: node's spec reporter and this
 * project's own harness happen to agree, so one pattern serves both. The
 * trailing form differs — the harness appends ` — <message>`, node appends
 * ` (1.27ms)` — so the name is taken up to whichever arrives first.
 *
 * An earlier version matched TAP's `not ok N - name` for the fast tier. node
 * --test only emits TAP with `--test-reporter=tap`, so nothing matched, every
 * fast arm reported "(no named failures)", and a genuinely caught mutation
 * was reported as CAUGHT BY THE WRONG TEST. Worth stating: the parser being
 * wrong looks exactly like the suite being wrong.
 */
function failedTests(output) {
  const names = new Set();
  const re = new RegExp("^[ \t]*✖ (.+?)(?: —| \\(\\d|$)", "gm");
  let m;
  while ((m = re.exec(output)) !== null) {
    const name = m[1].trim();
    // The reporter's own section header, not a test.
    if (name && name !== "failing tests:") names.add(name);
  }
  return [...names];
}

const sha = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

/**
 * One window minder for the whole run, started before the first arm.
 *
 * Per-arm minders were worse than useless: PowerShell compiles the `Add-Type`
 * interop at startup, so each of the nine live arms spent its first seconds
 * unwatched while the editor took the foreground. One warm process watches
 * every arm's window from the moment it appears.
 */
function startMinder() {
  if (process.platform !== "win32" || process.env.TOKENSAVE_TEST_FOCUS === "1") {
    return () => {};
  }
  // Each arm now launches its editor onto a private desktop, where there is
  // no window on your screen to put down. Starting a minder anyway would be a
  // PowerShell process watching for windows that never appear.
  if (require("./shared").desktopAvailable()) {
    console.log("  [focus] editors run on a private desktop; no minder needed");
    return () => {};
  }
  const child = cp.spawn("powershell.exe", [
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-File", path.join(__dirname, "keep-out-of-the-way.ps1"),
    "-TimeoutSeconds", "3600",
  ], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  child.stdout.on("data", (b) => process.stdout.write(`  [focus] ${b}`));
  child.on("error", (e) => console.warn(`window minder: ${e.message}`));
  return () => { try { child.kill(); } catch { /* already gone */ } };
}

async function main() {
  const only = process.argv[2];
  const arms = only
    ? MUTATIONS.filter((m) => m.name.includes(only))
    : MUTATIONS;
  if (!arms.length) {
    console.error(`no mutation matched ${JSON.stringify(only)}`);
    process.exitCode = 1;
  }

  const stopMinder = startMinder();
  try {
  // Control runs, so an arm's population can be compared against a known one.
  const control = {};
  for (const tier of [...new Set(arms.map((a) => a.tier))]) {
    const r = runSuite(tier);
    const count = testCount(r.output, tier);
    if (r.code !== 0 || !count) {
      console.error(`control run for tier '${tier}' is not green ` +
                    `(code ${r.code}, count ${count}). Fix that first — ` +
                    `mutation results are meaningless against a red baseline.`);
      console.error(r.output.slice(-3000));
      process.exit(1);
    }
    control[tier] = count;
    console.log(`control ${tier}: ${count} tests, green`);
  }

  const results = [];
  for (const arm of arms) {
    const target = path.join(OUT, arm.file);
    const original = fs.readFileSync(target);
    const before = sha(original);
    const text = original.toString("utf8");

    const occurrences = text.split(arm.find).length - 1;
    if (occurrences !== 1) {
      results.push({
        arm, verdict: "NOT APPLIED",
        detail: `pattern occurs ${occurrences} times in out/${arm.file} ` +
                `(expected exactly 1) — the code moved and this arm has been ` +
                `testing nothing`,
      });
      continue;
    }

    let outcome;
    try {
      fs.writeFileSync(target, text.replace(arm.find, arm.replace), "utf8");
      const r = runSuite(arm.tier);
      const count = testCount(r.output, arm.tier);
      const failed = failedTests(r.output);
      const caughtBy = failed.filter((n) =>
        n.toLowerCase().includes(arm.expect.toLowerCase()));

      if (r.code === 0) {
        outcome = { verdict: "SURVIVED",
                    detail: "the suite passed with the property removed" };
      } else if (count !== null && count < control[arm.tier]) {
        outcome = { verdict: "INCONCLUSIVE",
                    detail: `only ${count} of ${control[arm.tier]} tests ran; ` +
                            `the arm errored early rather than being caught` };
      } else if (!caughtBy.length) {
        outcome = { verdict: "CAUGHT BY THE WRONG TEST",
                    detail: `expected a failure matching ` +
                            `${JSON.stringify(arm.expect)}, got: ` +
                            `${failed.join(" | ") || "(no named failures)"}` };
      } else {
        outcome = { verdict: "CAUGHT", detail: caughtBy[0] };
      }
    } finally {
      // Unconditional, and verified. A mutation runner that can poison the
      // checkout on a timeout is worse than no mutation runner.
      fs.writeFileSync(target, original);
      const after = sha(fs.readFileSync(target));
      if (after !== before) {
        console.error(`FATAL: could not restore out/${arm.file} ` +
                      `(${before} -> ${after}). Run \`npm run compile\`.`);
        process.exit(2);
      }
    }
    results.push({ arm, ...outcome });
    console.log(`  ${outcome.verdict.padEnd(24)} ${arm.name}`);
  }

  console.log("\n── mutation report ───────────────────────────────────────");
  const bad = results.filter((r) => r.verdict !== "CAUGHT");
  for (const r of results) {
    console.log(`${r.verdict.padEnd(24)} [${r.arm.tier}] ${r.arm.name}`);
    if (r.verdict !== "CAUGHT") console.log(`  ${r.detail}`);
  }
  console.log(`\n${results.length - bad.length}/${results.length} caught by ` +
              `the test written for them`);
  if (bad.length) {
    console.log(
      "\nA surviving mutation means the test for that property is decorative. " +
      "Say which one, rather than adding an assertion to make it pass.");
    process.exit(1);
  }
  } finally {
    // Unconditional: a minder outliving its run would keep
    // minimising an editor the user opened deliberately after.
    stopMinder();
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
