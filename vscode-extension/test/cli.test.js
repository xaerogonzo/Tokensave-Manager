/**
 * test/cli.test.js — the extension's decision logic, without an extension host.
 *
 * `cli.ts` is where this extension decides things: which executable to run,
 * whether it can run at all, what an exit code means, and whether the CLI is
 * speaking a dialect it understands. Everything else in the extension renders
 * what those decisions produce. So this is the part worth testing, and it is
 * testable without VS Code — the `vscode` module is stubbed through
 * `Module._load`, which is far cheaper than downloading an editor to run an
 * extension host.
 *
 * Node's built-in test runner is used deliberately: the extension ships no
 * runtime dependencies, and a test framework would be the only thing in
 * `node_modules` that mattered at review time.
 *
 * Run with:  npm test   (after npm run compile)
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const Module = require("node:module");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

// ── the vscode stub ─────────────────────────────────────────────────────────

let settings = {};

const vscodeStub = {
  workspace: {
    getConfiguration() {
      return {
        get(key, fallback) {
          return key in settings ? settings[key] : fallback;
        },
      };
    },
  },
};

const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, ...rest);
};

const cli = require("../out/cli.js");

/** A fake ExtensionContext — only `extensionPath` is ever read. */
function context(extensionPath) {
  return { extensionPath };
}

/** A temp dir containing a fake bundled CLI at the expected relative path. */
function withBundledExe() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-ext-"));
  const binDir = path.join(root, "bin", "windows-x64");
  fs.mkdirSync(binDir, { recursive: true });
  fs.writeFileSync(path.join(binDir, "tokensave-manager-cli.exe"), "");
  return root;
}

test.beforeEach(() => { settings = {}; });

// ── how the CLI gets invoked ────────────────────────────────────────────────

/** A temp dir shaped like a Manager checkout. */
function withCheckout() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-src-"));
  fs.mkdirSync(path.join(root, "src"), { recursive: true });
  fs.writeFileSync(path.join(root, "src", "cli.py"), "");
  return root;
}

test("a checkout runs the live cli.py, not a snapshot", () => {
  // The point of source mode: a compiled binary is a copy of the Manager as
  // it was at build time, and goes stale the moment the Manager changes.
  const checkout = withCheckout();
  settings = { managerPath: checkout, pythonPath: "C:/py/python.exe" };

  const runner = cli.resolveRunner(context(withBundledExe()));
  assert.strictEqual(runner.kind, "source");
  assert.strictEqual(runner.command, "C:/py/python.exe");
  assert.deepStrictEqual(runner.prefixArgs,
    [path.join(checkout, "src", "cli.py")]);
});

test("source mode falls back to python on PATH", () => {
  settings = { managerPath: withCheckout() };
  assert.strictEqual(cli.resolveRunner(context("/nowhere")).command, "python");
});

test("a managerPath with no src/cli.py is refused, not silently downgraded", () => {
  // Falling through to the bundled exe here would run a different, older
  // Manager than the one the user pointed at.
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-nosrc-"));
  settings = { managerPath: empty };
  assert.strictEqual(cli.resolveRunner(context(withBundledExe())), null);
});

test("an explicit cliPath outranks a checkout", () => {
  const root = withBundledExe();
  const custom = path.join(root, "elsewhere.exe");
  fs.writeFileSync(custom, "");
  settings = { cliPath: custom, managerPath: withCheckout() };

  const runner = cli.resolveRunner(context(root));
  assert.strictEqual(runner.kind, "bundled");
  assert.strictEqual(runner.command, custom);
});

test("the bundled copy is the last resort", () => {
  const root = withBundledExe();
  const runner = cli.resolveRunner(context(root));
  assert.strictEqual(runner.kind, "bundled");
  assert.strictEqual(runner.command,
    path.join(root, "bin", "windows-x64", "tokensave-manager-cli.exe"));
});

test("a configured exe that does not exist resolves to null", () => {
  const root = withBundledExe();
  settings = { cliPath: path.join(root, "no-such.exe") };
  assert.strictEqual(cli.resolveRunner(context(root)), null);
});

test("nothing configured and no bundle resolves to null", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-empty-"));
  assert.strictEqual(cli.resolveRunner(context(empty)), null);
});

// ── why it cannot run, phrased for a human ──────────────────────────────────

test("a resolvable runner reports no reason", () => {
  assert.strictEqual(cliUnavailableOnWin32(withBundledExe()), null);
});

test("with nothing configured the advice names the recommended setting", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-empty2-"));
  assert.match(cliUnavailableOnWin32(empty), /managerPath/);
});

test("a bad checkout path is quoted back rather than replaced by advice", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-empty3-"));
  const bogus = path.join(empty, "not-a-checkout");
  settings = { managerPath: bogus };
  const reason = cliUnavailableOnWin32(empty);
  assert.ok(reason.includes(bogus));
  assert.match(reason, /src\/cli\.py/);
});

test("a bad configured exe is quoted back too", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-empty4-"));
  const bogus = path.join(empty, "typo.exe");
  settings = { cliPath: bogus };
  assert.ok(cliUnavailableOnWin32(empty).includes(bogus));
});

test("source mode is not refused off Windows", () => {
  // Only the bundled binary is Windows-only; cli.py runs anywhere Python does.
  settings = { managerPath: withCheckout() };
  const reason = onPlatform("linux",
    () => cli.cliUnavailableReason(context("/nowhere")));
  assert.strictEqual(reason, null);
});

test("with nothing configured off Windows, the message says why", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-empty5-"));
  const reason = onPlatform("linux",
    () => cli.cliUnavailableReason(context(empty)));
  assert.match(reason, /Windows-only/);
});

// ── what an exit code means ─────────────────────────────────────────────────

const envelope = (over = {}) => ({
  schema_version: 1, cli_version: "2.2.1", command: "doctor",
  ok: true, data: {}, warnings: [], error: null, ...over,
});

test("a transport failure outranks anything in the envelope", () => {
  const summary = cli.summarise({
    exitCode: 3, envelope: envelope(), transportError: "exe missing" });
  assert.match(summary, /exe missing/);
});

test("an envelope error is shown verbatim", () => {
  const summary = cli.summarise({
    exitCode: 1, envelope: envelope({ error: "no tokensave_exe configured" }),
    transportError: null });
  assert.match(summary, /no tokensave_exe configured/);
});

test("exit 4 reads as unverified, never as clean", () => {
  const summary = cli.summarise({
    exitCode: cli.EXIT.VERIFY_FAILED, envelope: envelope({ ok: false }),
    transportError: null });
  assert.match(summary, /verify/i);
  assert.doesNotMatch(summary, /ok/i);
});

test("exit 0 reads as ok", () => {
  assert.match(
    cli.summarise({ exitCode: 0, envelope: envelope(), transportError: null }),
    /ok/);
});

test("exit 1 reads as problems found, not as a crash", () => {
  const summary = cli.summarise({
    exitCode: 1, envelope: envelope({ ok: false }), transportError: null });
  assert.match(summary, /problems/);
});

test("a result with no envelope at all still summarises", () => {
  const summary = cli.summarise({
    exitCode: 1, envelope: null, transportError: null });
  assert.ok(summary.length > 0);
});

// ── the contract's own version guard ────────────────────────────────────────

test("the extension declares which envelope schema it understands", () => {
  assert.strictEqual(typeof cli.SUPPORTED_SCHEMA, "number");
  assert.ok(cli.SUPPORTED_SCHEMA >= 1);
});

test("the exit-code table matches the CLI's documented contract", () => {
  // Drifting from src/cli.py would make every failure render as the wrong kind.
  assert.deepStrictEqual(cli.EXIT, {
    OK: 0, FAILED: 1, USAGE: 2, PREREQUISITE: 3, VERIFY_FAILED: 4 });
});

// ── helpers ─────────────────────────────────────────────────────────────────

function cliUnavailableOnWin32(root) {
  return onPlatform("win32", () => cli.cliUnavailableReason(context(root)));
}

/** Run `fn` with `process.platform` temporarily reported as `value`. */
function onPlatform(value, fn) {
  const original = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", original);
  }
}

// ── the settings the README promises must be the settings that exist ────────

test("every declared setting is documented in the README", () => {
  // This guard exists because it already went wrong: a VSIX shipped whose
  // README told the user to set `configPath` when the recommended setting had
  // become `managerPath`. The code was right and the docs were stale, which is
  // the worst combination — the user follows the instructions and it fails.
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  const readme = fs.readFileSync(
    path.join(__dirname, "..", "README.md"), "utf8");

  const declared = Object.keys(
    manifest.contributes.configuration.properties);
  assert.ok(declared.length > 0, "no settings declared");

  for (const full of declared) {
    const short = full.replace(/^tokensaveManager\./, "");
    assert.ok(readme.includes(full) || readme.includes(short),
      `${full} is declared but never mentioned in README.md`);
  }
});

test("the README does not promise settings that do not exist", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  const readme = fs.readFileSync(
    path.join(__dirname, "..", "README.md"), "utf8");

  const declared = new Set(
    Object.keys(manifest.contributes.configuration.properties));
  const mentioned = readme.match(/tokensaveManager\.[A-Za-z]+/g) ?? [];

  for (const name of new Set(mentioned)) {
    assert.ok(declared.has(name),
      `README.md documents ${name}, which package.json does not declare`);
  }
});

test("every contributed command has a declared handler id", () => {
  // A command in the palette that activates nothing is the same silent
  // failure in a different place.
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "extension.ts"), "utf8");
  const tree = fs.readFileSync(
    path.join(__dirname, "..", "src", "tree.ts"), "utf8");

  for (const { command } of manifest.contributes.commands) {
    const short = command.replace(/^tokensaveManager\./, "");
    const registered = source.includes(`"${command}"`)
      // Action commands are registered from a loop over ACTIONS.
      || tree.includes(`id: "${short}"`);
    assert.ok(registered, `${command} is contributed but never registered`);
  }
});
