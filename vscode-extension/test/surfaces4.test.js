/**
 * test/surfaces4.test.js — tasks, lenses, save-triggered checks, and setup.
 *
 * Four small surfaces, and each one has exactly one property that is easy to
 * get wrong and invisible when you do:
 *
 *   * a task built with a **shell** execution re-splits `D:\Claude Co worker`
 *     on the space;
 *   * a CodeLens saying **"no tests"** makes a claim the filename heuristic
 *     underneath it cannot support;
 *   * a save-triggered run finishing **after** a newer one puts back findings
 *     that are already fixed;
 *   * a setup check that only proves the happy path leaves **configured but
 *     broken** looking like success.
 *
 * So that is mostly what is asserted here.
 */
"use strict";

const assert = require("node:assert");
const { test } = require("node:test");

const { folder, setSettings, taskProviders } = require("./vscode-stub.js");

const tasks = require("../out/tasks.js");
const lens = require("../out/lens.js");
const onsave = require("../out/onsave.js");
const setup = require("../out/setup.js");

const ROOT = process.platform === "win32" ? "C:\\proj" : "/proj";
const SPACED = process.platform === "win32"
  ? "D:\\Claude Co worker\\Token Save Manager Source"
  : "/home/a b/Token Save Manager Source";

test.beforeEach(() => {
  setSettings({ managerPath: ROOT });
  taskProviders.length = 0;
  onsave.resetRuns();
});

// ── 4a. tasks ───────────────────────────────────────────────────────────────

test("only taskable commands become tasks", () => {
  const { COMMANDS } = require("../out/commands.js");
  const offered = tasks.taskableCommands("source").map((c) => c.action);
  const expected = COMMANDS.filter((c) => c.task).map((c) => c.action);
  assert.deepStrictEqual(offered, expected);
  assert.ok(offered.length > 0, "the table advertises no tasks at all");
});

test("a frozen runner is not offered checks", () => {
  // `checks` shells out to `sys.executable -m compileall`, and under a Nuitka
  // onefile build sys.executable is the extracted binary. The task could only
  // ever fail, which is worse than one fewer task.
  const source = tasks.taskableCommands("source").map((c) => c.cli);
  const bundled = tasks.taskableCommands("bundled").map((c) => c.cli);
  assert.ok(source.includes("checks"));
  assert.ok(!bundled.includes("checks"));
});

test("the frozen exclusion matches the Python side exactly", () => {
  // helpers/vscode_tasks.FROZEN_UNSUPPORTED. Two lists that drift would mean
  // the generated tasks.json and the provider offering different menus.
  assert.deepStrictEqual([...tasks.FROZEN_UNSUPPORTED], ["checks"]);
});

test("a task passes the project as its own argv entry", () => {
  const runner = { command: "python", prefixArgs: ["/m/src/cli.py"],
                   kind: "source" };
  const command = tasks.taskableCommands("source")[0];
  const args = tasks.taskArgs(runner, command, folder(SPACED));

  // The path with spaces must arrive as ONE argument. A shell task would
  // re-split it — the same failure as `bash: D:/Claude: No such file`.
  assert.ok(args.includes(SPACED), args.join(" | "));
  assert.strictEqual(args[args.indexOf("--project") + 1], SPACED);
});

test("the task provider is registered under the contributed type", () => {
  const pkg = require("../package.json");
  const declared = pkg.contributes.taskDefinitions.map((d) => d.type);
  assert.ok(declared.includes(tasks.TASK_TYPE));
  assert.deepStrictEqual(
    pkg.contributes.taskDefinitions[0].required, ["action"]);
});

test("a task is keyed by the stable action, not the label", () => {
  // Labels are free to change; `action` is what generated files reference.
  const pkg = require("../package.json");
  const properties = pkg.contributes.taskDefinitions[0].properties;
  assert.ok("action" in properties);
  assert.ok(!("label" in properties));
});

// ── 4b. code lenses ─────────────────────────────────────────────────────────

const noProblem = { cases: [], uncovered: [], stale: [], problem: null };

test("an uncovered source file says filename-matched, not 'no tests'", () => {
  const text = lens.lensText("src/helpers/foo.py",
                             { ...noProblem, uncovered: ["src/helpers/foo.py"] },
                             "both");
  assert.strictEqual(text.length, 1);
  // The scan is a filename heuristic. "no tests" would be a claim about the
  // code; this is a claim about the scan, which is what was measured.
  assert.match(text[0], /no filename-matched test/);
  assert.doesNotMatch(text[0], /no tests\b/);
});

test("a covered source file gets no lens at all", () => {
  assert.deepStrictEqual(lens.lensText("src/a.py", noProblem, "both"), []);
});

test("a test file reports its count", () => {
  const discovery = { ...noProblem, cases: [
    { file: "tests/test_a.py" }, { file: "tests/test_a.py" },
    { file: "tests/test_b.py" },
  ] };
  const text = lens.lensText("tests/test_a.py", discovery, "both");
  assert.match(text[0], /2 tests/);
});

test("one test reads as singular", () => {
  const discovery = { ...noProblem, cases: [{ file: "tests/test_a.py" }] };
  assert.match(lens.lensText("tests/test_a.py", discovery, "both")[0],
               /1 test\b/);
});

test("a stale signal is shown as a signal, not a verdict", () => {
  const discovery = { ...noProblem,
    cases: [{ file: "tests/test_a.py" }],
    stale: [{ test: "test_a.py", reason: "imports helpers.gone", detail: "" }] };
  const text = lens.lensText("tests/test_a.py", discovery, "both");
  const stale = text.find((line) => line.includes("stale"));
  // `detect_stale_tests` emits reasons to look, not conclusions.
  assert.match(stale, /stale signal: imports helpers\.gone/);
  assert.doesNotMatch(stale, /dead|unused|delete/i);
});

test("the mode setting really narrows what is shown", () => {
  const uncovered = { ...noProblem, uncovered: ["src/a.py"] };
  const testFile = { ...noProblem, cases: [{ file: "tests/test_a.py" }] };

  assert.strictEqual(lens.lensText("src/a.py", uncovered, "off").length, 0);
  assert.strictEqual(lens.lensText("src/a.py", uncovered, "tests").length, 0);
  assert.strictEqual(lens.lensText("src/a.py", uncovered, "source").length, 1);
  assert.strictEqual(
    lens.lensText("tests/test_a.py", testFile, "source").length, 0);
  assert.strictEqual(
    lens.lensText("tests/test_a.py", testFile, "tests").length, 1);
});

test("a failed discovery shows nothing rather than a wrong answer", () => {
  // "no filename-matched test" when discovery never ran would be a finding
  // invented from a failure to look.
  const broken = { ...noProblem, problem: "Manager CLI unavailable" };
  assert.deepStrictEqual(lens.lensText("src/a.py", broken, "both"), []);
});

test("paths are made repo-relative with forward slashes", () => {
  const uri = { fsPath: process.platform === "win32"
    ? `${ROOT}\\src\\helpers\\foo.py` : `${ROOT}/src/helpers/foo.py` };
  assert.strictEqual(lens.relativeTo(folder(ROOT), uri),
                     "src/helpers/foo.py");
});

test("test files are recognised by their repo-relative prefix", () => {
  assert.ok(lens.isTestFile("tests/test_a.py"));
  assert.ok(!lens.isTestFile("src/tests_helper.py"));
});

// ── 4c. checks on save ──────────────────────────────────────────────────────

test("a newer run retires an older one", () => {
  const first = onsave.beginRun("a.py");
  const second = onsave.beginRun("a.py");
  // Save, edit, save again. If the first finishes last it would put back
  // findings the second already knows are fixed.
  assert.ok(!onsave.isCurrent("a.py", first));
  assert.ok(onsave.isCurrent("a.py", second));
});

test("runs on different files do not retire each other", () => {
  const a = onsave.beginRun("a.py");
  const b = onsave.beginRun("b.py");
  assert.ok(onsave.isCurrent("a.py", a));
  assert.ok(onsave.isCurrent("b.py", b));
});

test("a run whose generation was never issued is not current", () => {
  assert.ok(!onsave.isCurrent("a.py", 99));
});

test("checks-on-save is off by default", () => {
  const pkg = require("../package.json");
  const setting =
    pkg.contributes.configuration.properties["tokensaveManager.checksOnSave"];
  assert.strictEqual(setting.default, false);
  // The bundled CLI cannot run checks at all, and the setting says so rather
  // than leaving the user to discover it one failed save at a time.
  assert.match(setting.markdownDescription, /managerPath/);
});

test("the code-lens setting documents that the scan is a heuristic", () => {
  const pkg = require("../package.json");
  const setting =
    pkg.contributes.configuration.properties["tokensaveManager.codeLens"];
  assert.deepStrictEqual(setting.enum, ["off", "source", "tests", "both"]);
  assert.match(setting.markdownDescription, /heuristic/);
});

// ── 4d. setup ───────────────────────────────────────────────────────────────

/**
 * A resolvable runner plus a canned `commands` reply.
 *
 * `resolveRunner` has to be patched as well: it checks the filesystem for
 * `src/cli.py`, so against a fictional root `verifySetup` returns "broken"
 * before it ever reaches the CLI — and the two tests below would have passed
 * for entirely the wrong reason.
 */
function contextWith(reply) {
  const cli = require("../out/cli.js");
  const originalRun = cli.runProjectlessCli;
  const originalResolve = cli.resolveRunner;
  cli.runProjectlessCli = async () => reply;
  cli.resolveRunner = () => ({
    command: "python", prefixArgs: [`${ROOT}/src/cli.py`], kind: "source" });
  return {
    restore: () => {
      cli.runProjectlessCli = originalRun;
      cli.resolveRunner = originalResolve;
    },
  };
}

test("nothing configured is unconfigured, not broken", async () => {
  setSettings({});
  const report = await setup.verifySetup(
    { subscriptions: [], extensionPath: "/nowhere" });
  assert.strictEqual(report.state, "unconfigured");
});

test("a path that is set but does not resolve is BROKEN", async () => {
  // The state the whole walkthrough exists for: it looks configured, and
  // every command fails separately with a different message. Telling this
  // user to "configure the extension" is how a setup flow loses them.
  setSettings({ managerPath: "/does/not/exist" });
  const report = await setup.verifySetup(
    { subscriptions: [], extensionPath: "/nowhere" });
  assert.strictEqual(report.state, "broken");
  assert.match(report.message, /No src\/cli\.py/);
});

test("a configured Manager that does not answer is broken", async () => {
  const patched = contextWith(
    { exitCode: 3, envelope: null, transportError: "python: not found" });
  const report = await setup.verifySetup(
    { subscriptions: [], extensionPath: ROOT });
  patched.restore();
  assert.strictEqual(report.state, "broken");
  assert.match(report.message, /did not answer/);
});

test("an answer with no commands is broken, not working", async () => {
  // Something replied with parseable JSON that is not a Manager vocabulary.
  const patched = contextWith({
    exitCode: 0, transportError: null,
    envelope: { schema_version: 1, cli_version: "x", command: "commands",
                ok: true, findings: [], warnings: [], error: null,
                data: { commands: [] } },
  });
  const report = await setup.verifySetup(
    { subscriptions: [], extensionPath: ROOT });
  patched.restore();
  assert.strictEqual(report.state, "broken");
});

test("a Manager that answers with its vocabulary is working", async () => {
  const patched = contextWith({
    exitCode: 0, transportError: null,
    envelope: { schema_version: 1, cli_version: "2.6.0", command: "commands",
                ok: true, findings: [], warnings: [], error: null,
                data: { commands: [{ action: "status" }, { action: "doctor" }] } },
  });
  const report = await setup.verifySetup(
    { subscriptions: [], extensionPath: ROOT });
  patched.restore();
  assert.strictEqual(report.state, "working");
  assert.match(report.message, /2 commands/);
  assert.match(report.message, /2\.6\.0/);
});

test("the welcome view offers the picker and keys on readiness", () => {
  const pkg = require("../package.json");
  const welcome = pkg.contributes.viewsWelcome[0];
  assert.strictEqual(welcome.view, "tokensaveManager.projects");
  assert.match(welcome.contents, /command:tokensaveManager\.setManagerPath/);
  assert.strictEqual(welcome.when, "!tokensaveManager.ready");
});

test("the walkthrough's three steps each offer a command to run", () => {
  const pkg = require("../package.json");
  const walkthrough = pkg.contributes.walkthroughs[0];
  assert.strictEqual(walkthrough.steps.length, 3);
  for (const step of walkthrough.steps) {
    assert.match(step.description, /\(command:/,
                 `${step.id} describes a step with nothing to click`);
  }
});

test("every walkthrough media file referenced actually exists", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const pkg = require("../package.json");
  for (const step of pkg.contributes.walkthroughs[0].steps) {
    const file = path.join(__dirname, "..", step.media.markdown);
    assert.ok(fs.existsSync(file), `${step.media.markdown} is missing`);
  }
});
