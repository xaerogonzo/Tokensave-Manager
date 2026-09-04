/**
 * test/testing.test.js — the Test Explorer's decisions, headless.
 *
 * Three things are worth asserting here and are not worth booting an editor
 * for: which node ids a run puts on the command line, how a per-test record
 * becomes Explorer state, and what happens when a run is cancelled. The tree
 * itself — that a person sees rows in the right order — is checked in the live
 * suite, because a rendered tree is the only honest evidence of that.
 *
 * The recurring theme is that **an unattributable result must not become a
 * result**. A green tick on the wrong test is worse than no tick, and it is
 * invisible: the run passes, the count is right, and one assertion in the
 * suite is now reporting about a different test than the user thinks.
 */
"use strict";

const assert = require("node:assert");
const { test } = require("node:test");

const {
  cancellation, controllers, folder, setFolders, setSettings,
} = require("./vscode-stub.js");

const { TestExplorer } = require("../out/testing.js");
const { byFile } = require("../out/discovery.js");

const ROOT = process.platform === "win32" ? "C:\\proj" : "/proj";

/**
 * The id `testing.ts` builds for a folder-plus-nodeid.
 *
 * Percent-encoded rather than joined with a separator, because there is no
 * character that cannot appear in a path AND cannot appear in a node id — and
 * the obvious candidate, NUL, is rejected by the real editor.
 */
function id(nodeid) {
  return `${encodeURIComponent(ROOT)}/${encodeURIComponent(nodeid)}`;
}

function testCase(nodeid, overrides) {
  return Object.assign({
    nodeid,
    name: nodeid.split("::").pop(),
    class_name: "",
    file: nodeid.split("::")[0],
    line: 1,
    end_line: 2,
    markers: [],
  }, overrides || {});
}

/**
 * An Explorer wired to canned discovery and a canned `test-run`.
 *
 * `runCli` is replaced on the compiled module rather than mocked globally:
 * the argv it is handed is half of what these tests assert, and a global
 * subprocess mock would also swallow calls these assertions do not own.
 */
function explorerWith(cases, runResult) {
  const cli = require("../out/cli.js");
  const calls = [];
  const originalRun = cli.runCli;
  cli.runCli = async (context, command, wsFolder, args, token) => {
    calls.push({ command, folder: wsFolder, args, token });
    return typeof runResult === "function"
      ? runResult({ command, args, token })
      : runResult;
  };

  const discovery = {
    get: async () => ({ cases, uncovered: [], stale: [], problem: null }),
    peek: () => ({ cases, uncovered: [], stale: [], problem: null }),
    onDidChange: () => ({ dispose() {} }),
    invalidateAll() {},
  };
  const context = { subscriptions: [], extensionPath: ROOT };
  const explorer = new TestExplorer(context, discovery);
  return { explorer, calls, restore: () => { cli.runCli = originalRun; } };
}

function envelope(tests, extra) {
  return {
    exitCode: 0,
    transportError: null,
    envelope: Object.assign({
      schema_version: 1, cli_version: "x", command: "test-run", ok: true,
      findings: [], warnings: [], error: null,
      data: { tests, run_state: "completed", output: "" },
    }, extra || {}),
  };
}

function result(nodeid, outcome, requested, ambiguous) {
  return {
    nodeid,
    outcome,
    duration_seconds: 0.01,
    message: outcome === "failed" ? "assert 1 == 2" : "",
    requested: requested === undefined ? nodeid : requested,
    ambiguous: Boolean(ambiguous),
  };
}

test.beforeEach(() => {
  setSettings({ managerPath: ROOT });
  setFolders([folder(ROOT)]);
  controllers.length = 0;
});

// ── discovery → tree ────────────────────────────────────────────────────────

test("byFile groups definitions without losing their order", () => {
  const grouped = byFile([
    testCase("tests/a.py::test_one"),
    testCase("tests/b.py::test_two"),
    testCase("tests/a.py::test_three"),
  ]);
  assert.deepStrictEqual([...grouped.keys()], ["tests/a.py", "tests/b.py"]);
  assert.deepStrictEqual(
    grouped.get("tests/a.py").map((c) => c.name),
    ["test_one", "test_three"]);
});

test("a discovered definition becomes an item with a 0-based range", async () => {
  const { explorer, restore } = explorerWith(
    [testCase("tests/a.py::test_one", { line: 12, end_line: 15 })]);
  await explorer.discoverAll();
  restore();

  const controller = controllers[0];
  const root = controller.items.get(ROOT);
  const file = root.children.get(id("tests/a.py"));
  const item = file.children.get(id("tests/a.py::test_one"));
  // 1-based in the envelope, 0-based here, converted at this boundary only.
  assert.strictEqual(item.range.start.line, 11);
  assert.strictEqual(item.range.end.line, 14);
});

test("a class method is labelled with its class", async () => {
  const { explorer, restore } = explorerWith([
    testCase("tests/a.py::TestC::test_m", { class_name: "TestC", name: "test_m" }),
  ]);
  await explorer.discoverAll();
  restore();
  assert.ok(explorer.renderTests().includes("    TestC::test_m"),
            explorer.renderTests().join(" | "));
});

test("markers are shown as a description, not baked into the label", async () => {
  const { explorer, restore } = explorerWith(
    [testCase("tests/a.py::test_one", { markers: ["tk", "parametrize"] })]);
  await explorer.discoverAll();
  restore();
  const item = controllers[0].items.get(ROOT)
    .children.get(id("tests/a.py"))
    .children.get(id("tests/a.py::test_one"));
  assert.strictEqual(item.description, "tk, parametrize");
  assert.strictEqual(item.label, "test_one");
});

test("item ids carry the folder, so two projects cannot collide", async () => {
  const { explorer, restore } = explorerWith(
    [testCase("tests/test_cli.py::test_status")]);
  await explorer.discoverAll();
  restore();
  const ids = [];
  controllers[0].items.forEach((root) => {
    root.children.forEach((file) => file.children.forEach((i) => ids.push(i.id)));
  });
  // The encoded folder path, since the id is percent-encoded — the property
  // that matters is that the project is IN the id, not how it is spelled.
  assert.ok(ids[0].startsWith(encodeURIComponent(ROOT)),
            `${ids[0]} does not identify its project`);
  // And a second folder would produce a different prefix for the same test.
  const other = process.platform === "win32" ? "C:' + chr(92)*2 + 'other" : "/other";
  assert.notStrictEqual(encodeURIComponent(other), encodeURIComponent(ROOT));
});

// ── two run profiles ────────────────────────────────────────────────────────

test("there are two Run profiles and no Debug profile", async () => {
  const { restore } = explorerWith([]);
  restore();
  const kinds = controllers[0].profiles.map((p) => p.kind);
  const labels = controllers[0].profiles.map((p) => p.label);
  assert.deepStrictEqual(labels, ["Run", "Gate (not tk)"]);
  // Debugging pytest is ms-python's job; a worse copy of it here would be a
  // second thing to be wrong.
  assert.ok(kinds.every((k) => k === 1), "a Debug profile appeared");
});

test("the gate profile passes a marker expression, never node ids", async () => {
  const cases = [testCase("tests/a.py::test_one"), testCase("tests/a.py::test_two")];
  const { explorer, calls, restore } = explorerWith(cases, envelope([]));
  await explorer.discoverAll();
  const item = controllers[0].items.get(ROOT).children
    .get(id("tests/a.py")).children.get(id("tests/a.py::test_one"));

  const gate = controllers[0].profiles[1];
  await gate.handler({ include: [item], exclude: [] }, cancellation().token);
  restore();

  assert.deepStrictEqual(calls[0].args, ["--markers", "not tk"]);
  assert.ok(!calls[0].args.includes("--tests"),
            "--tests and --markers are mutually exclusive at the CLI");
});

test("running the whole folder passes no selector at all", async () => {
  const cases = [testCase("tests/a.py::test_one"), testCase("tests/a.py::test_two")];
  const { explorer, calls, restore } = explorerWith(cases, envelope([]));
  await explorer.discoverAll();

  const run = controllers[0].profiles[0];
  await run.handler({ include: [], exclude: [] }, cancellation().token);
  restore();

  // Selecting every test explicitly would put thousands of node ids on one
  // command line, which Windows will not accept.
  assert.deepStrictEqual(calls[0].args, []);
});

test("running a subset passes exactly those node ids", async () => {
  const cases = [
    testCase("tests/a.py::test_one"),
    testCase("tests/a.py::test_two"),
    testCase("tests/a.py::test_three"),
  ];
  const { explorer, calls, restore } = explorerWith(cases, envelope([]));
  await explorer.discoverAll();
  const file = controllers[0].items.get(ROOT).children.get(id("tests/a.py"));
  const one = file.children.get(id("tests/a.py::test_one"));

  await controllers[0].profiles[0].handler(
    { include: [one], exclude: [] }, cancellation().token);
  restore();

  assert.deepStrictEqual(calls[0].args,
                         ["--tests", "tests/a.py::test_one"]);
});

test("selecting a file runs the tests inside it", async () => {
  const cases = [
    testCase("tests/a.py::test_one"),
    testCase("tests/a.py::test_two"),
    testCase("tests/b.py::test_three"),
  ];
  const { explorer, calls, restore } = explorerWith(cases, envelope([]));
  await explorer.discoverAll();
  const file = controllers[0].items.get(ROOT).children.get(id("tests/a.py"));

  await controllers[0].profiles[0].handler(
    { include: [file], exclude: [] }, cancellation().token);
  restore();

  assert.deepStrictEqual(
    calls[0].args,
    ["--tests", "tests/a.py::test_one", "tests/a.py::test_two"]);
});

// ── results → Explorer state ────────────────────────────────────────────────

async function runAll(cases, results, extra) {
  const { explorer, restore } = explorerWith(cases, envelope(results, extra));
  await explorer.discoverAll();
  const file = controllers[0].items.get(ROOT).children.get(id("tests/a.py"));
  const items = [];
  file.children.forEach((i) => items.push(i));
  await controllers[0].profiles[0].handler(
    { include: items, exclude: [] }, cancellation().token);
  restore();
  return controllers[0].runs[0];
}

test("a passing test goes green and a failing one carries its message", async () => {
  const run = await runAll(
    [testCase("tests/a.py::test_one"), testCase("tests/a.py::test_two")],
    [result("tests/a.py::test_one", "passed"),
     result("tests/a.py::test_two", "failed")]);

  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "passed");
  assert.strictEqual(run.states.get(id("tests/a.py::test_two")), "failed");
  assert.match(run.messages.get(id("tests/a.py::test_two")).message,
               /assert 1 == 2/);
});

test("six parametrised results land on the one definition they belong to",
     async () => {
       const results = ["a", "b", "c", "d", "e", "f"].map((p) =>
         result(`tests/a.py::test_one[${p}]`, "passed", "tests/a.py::test_one"));
       const run = await runAll([testCase("tests/a.py::test_one")], results);
       assert.strictEqual(run.states.get(id("tests/a.py::test_one")),
                          "passed");
     });

test("one failing parameter case fails the definition", async () => {
  const results = [
    result("tests/a.py::test_one[a]", "passed", "tests/a.py::test_one"),
    result("tests/a.py::test_one[b]", "failed", "tests/a.py::test_one"),
  ];
  const run = await runAll([testCase("tests/a.py::test_one")], results);
  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "failed");
});

test("an ambiguous result is reported, never attributed", async () => {
  const run = await runAll(
    [testCase("tests/a.py::test_one")],
    [result("tests/a.py::test_one[x]", "passed", "", true)]);

  // Not passed. The result could belong to more than one test, and a green
  // tick on the wrong one is worse than no tick.
  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "skipped");
  assert.match(run.output, /could not attribute/);
});

test("a result attributed to nothing is not applied to anything", async () => {
  const run = await runAll(
    [testCase("tests/a.py::test_one")],
    [result("tests/a.py::test_elsewhere", "failed", "")]);
  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "skipped");
});

test("a requested test with no result is skipped, not left spinning", async () => {
  const run = await runAll(
    [testCase("tests/a.py::test_one"), testCase("tests/a.py::test_two")],
    [result("tests/a.py::test_one", "passed")]);
  assert.strictEqual(run.states.get(id("tests/a.py::test_two")), "skipped");
});

test("an unrecognised outcome fails rather than passing", async () => {
  // A future pytest word must not arrive as a false green.
  const run = await runAll(
    [testCase("tests/a.py::test_one")],
    [result("tests/a.py::test_one", "something_new")]);
  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "failed");
});

test("a busy suite errors with the reason instead of failing the tests",
     async () => {
       const run = await runAll(
         [testCase("tests/a.py::test_one")], [],
         { error: "a test run is already in progress",
           data: { tests: [], run_state: "busy", output: "" } });

       assert.strictEqual(run.states.get(id("tests/a.py::test_one")),
                          "errored");
       assert.match(run.messages.get(id("tests/a.py::test_one")).message,
                    /already in progress/);
     });

test("the run always ends, even when nothing could be run", async () => {
  const run = await runAll([testCase("tests/a.py::test_one")], []);
  assert.ok(run.ended, "a run left open leaves a spinner in the UI forever");
});

// ── cancellation ────────────────────────────────────────────────────────────

test("a cancelled run reports cancelled, not failed", async () => {
  const cases = [testCase("tests/a.py::test_one")];
  const { explorer, restore } = explorerWith(cases, {
    exitCode: 1, envelope: null, transportError: "cancelled", cancelled: true,
  });
  await explorer.discoverAll();
  const file = controllers[0].items.get(ROOT).children.get(id("tests/a.py"));
  const items = [];
  file.children.forEach((i) => items.push(i));
  await controllers[0].profiles[0].handler(
    { include: items, exclude: [] }, cancellation().token);
  restore();

  const run = controllers[0].runs[0];
  // Skipped, not failed: the user stopped it, the suite did not go red.
  assert.strictEqual(run.states.get(id("tests/a.py::test_one")), "skipped");
  assert.match(run.output, /cancelled/i);
});

test("the cancellation token reaches the CLI", async () => {
  const cases = [testCase("tests/a.py::test_one")];
  const { explorer, calls, restore } = explorerWith(cases, envelope([]));
  await explorer.discoverAll();
  const control = cancellation();
  await controllers[0].profiles[0].handler(
    { include: [], exclude: [] }, control.token);
  restore();
  assert.ok(calls[0].token, "no token was passed; cancelling would do nothing");
});

test("a transport failure errors every selected test with the reason",
     async () => {
       const cases = [testCase("tests/a.py::test_one")];
       const { explorer, restore } = explorerWith(cases, {
         exitCode: 3, envelope: null,
         transportError: "could not run python: ENOENT",
       });
       await explorer.discoverAll();
       await controllers[0].profiles[0].handler(
         { include: [], exclude: [] }, cancellation().token);
       restore();

       const run = controllers[0].runs[0];
       assert.strictEqual(run.states.get(id("tests/a.py::test_one")),
                          "errored");
       assert.match(run.messages.get(id("tests/a.py::test_one")).message,
                    /ENOENT/);
     });
