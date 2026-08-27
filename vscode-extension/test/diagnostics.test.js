/**
 * test/diagnostics.test.js — findings rendered as VS Code Diagnostics.
 *
 * This side of the boundary owns almost nothing, and the tests reflect that.
 * Severity *values* are the producer's business and are asserted in the Python
 * suite; all that is checked here is that `envelope severity X` becomes
 * `editor severity X`. The one transformation genuinely owned by TypeScript —
 * 1-based coordinates to 0-based positions — gets the most attention, because
 * doing it twice or not at all moves every squiggle by a line.
 *
 * The rest is isolation: a command replacing only its own results, and a
 * workspace folder never contributing squiggles to its sibling.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { vscodeStub, fakeCollection, folder } = require("./vscode-stub");
const diagnostics = require("../out/diagnostics.js");

const SEVERITY = vscodeStub.DiagnosticSeverity;

function finding(overrides = {}) {
  return {
    file: "src/a.py", line: 1, column: 1, end_line: 1, end_column: 1,
    severity: "warning", message: "m", rule: "pyflakes", symbol: "",
    ...overrides,
  };
}

function storeIn(collection) {
  return new diagnostics.DiagnosticStore(collection);
}

function srcA(f) {
  return path.join(f.uri.fsPath, "src", "a.py");
}

// ── coordinates: the one conversion this side owns ──────────────────────────

test("1-based envelope coordinates become 0-based editor positions", () => {
  const d = diagnostics.toDiagnostic(
    finding({ line: 12, column: 5, end_line: 12, end_column: 14 }));
  assert.equal(d.range.start.line, 11);
  assert.equal(d.range.start.character, 4);
  assert.equal(d.range.end.line, 11);
  assert.equal(d.range.end.character, 13);
});

test("line 1 column 1 lands at the very start of the file", () => {
  const d = diagnostics.toDiagnostic(finding({ line: 1, column: 1 }));
  assert.equal(d.range.start.line, 0);
  assert.equal(d.range.start.character, 0);
});

test("a point finding becomes a zero-width range, not a negative one", () => {
  const d = diagnostics.toDiagnostic(
    finding({ line: 3, column: 7, end_line: 3, end_column: 7 }));
  assert.equal(d.range.start.line, d.range.end.line);
  assert.equal(d.range.start.character, d.range.end.character);
});

test("a one-character range stays exactly one character wide", () => {
  const d = diagnostics.toDiagnostic(
    finding({ line: 3, column: 7, end_line: 3, end_column: 8 }));
  assert.equal(d.range.end.character - d.range.start.character, 1);
});

test("a multi-line range keeps an end column smaller than its start", () => {
  // Normal for a range that wraps: the end is earlier in the line, later in
  // the file. Clamping it against the start would be wrong here.
  const d = diagnostics.toDiagnostic(
    finding({ line: 4, column: 40, end_line: 6, end_column: 3 }));
  assert.equal(d.range.start.line, 3);
  assert.equal(d.range.end.line, 5);
  assert.equal(d.range.end.character, 2);
});

test("a malformed zero line does not produce a negative position", () => {
  const d = diagnostics.toDiagnostic(finding({ line: 0, column: 0 }));
  assert.ok(d.range.start.line >= 0);
  assert.ok(d.range.start.character >= 0);
});

// ── severity: passed through, never inferred ────────────────────────────────

test("severity comes from the envelope and is never inferred from the rule", () => {
  for (const [name, expected] of [
    ["error", SEVERITY.Error],
    ["warning", SEVERITY.Warning],
    ["information", SEVERITY.Information],
    ["hint", SEVERITY.Hint],
  ]) {
    assert.equal(
      diagnostics.toDiagnostic(finding({ severity: name })).severity, expected);
  }
});

test("an unrecognised severity lands on warning rather than crashing", () => {
  assert.equal(
    diagnostics.toDiagnostic(finding({ severity: "banana" })).severity,
    SEVERITY.Warning);
});

test("the producer, not the command, is shown as the source", () => {
  assert.equal(
    diagnostics.toDiagnostic(finding({ rule: "compileall/SyntaxError" })).source,
    "compileall");
  assert.equal(
    diagnostics.toDiagnostic(finding({ rule: "doctor/audit" })).source,
    "doctor");
  assert.equal(
    diagnostics.toDiagnostic(finding({ rule: "pyflakes" })).source, "pyflakes");
});

test("the full rule travels as the diagnostic code", () => {
  assert.equal(
    diagnostics.toDiagnostic(finding({ rule: "scout/dead_code" })).code,
    "scout/dead_code");
});

// ── isolation ───────────────────────────────────────────────────────────────

test("one command's results replace only its own", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));

  store.replace(a, "checks", [finding({ message: "from checks" })]);
  store.replace(a, "scout", [finding({ message: "from scout" })]);
  assert.equal(collection.get(srcA(a)).length, 2);

  // Scout finding nothing this time must not take checks with it.
  store.replace(a, "scout", []);
  const left = collection.get(srcA(a));
  assert.equal(left.length, 1);
  assert.equal(left[0].message, "from checks");
});

test("a folder's results never leak into a sibling with the same paths", () => {
  // Exactly the collision this whole integration exists to avoid: two
  // workspace roots that each contain src/a.py.
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "rootA"));
  const b = folder(path.join(os.tmpdir(), "rootB"));

  store.replace(a, "checks", [finding({ message: "in A" })]);
  store.replace(b, "checks", [finding({ message: "in B" })]);

  assert.equal(collection.get(srcA(a))[0].message, "in A");
  assert.equal(collection.get(srcA(b))[0].message, "in B");
  assert.notEqual(srcA(a), srcA(b));
});

test("re-running a command replaces its findings rather than appending", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));
  store.replace(a, "checks", [finding({ message: "first" })]);
  store.replace(a, "checks", [finding({ message: "second" })]);
  const list = collection.get(srcA(a));
  assert.equal(list.length, 1);
  assert.equal(list[0].message, "second");
});

test("a clean run removes the file from the Problems panel entirely", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));
  store.replace(a, "checks", [finding()]);
  assert.equal(collection.size, 1);
  store.replace(a, "checks", []);
  assert.equal(collection.size, 0);
});

test("a folder leaving the workspace takes its diagnostics with it", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "rootA"));
  const b = folder(path.join(os.tmpdir(), "rootB"));
  store.replace(a, "checks", [finding()]);
  store.replace(b, "checks", [finding()]);
  store.forgetFolder(a);
  assert.deepEqual(collection.paths(), [srcA(b)]);
});

test("findings on several files each land on their own file", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));
  store.replace(a, "checks", [
    finding({ file: "src/a.py" }),
    finding({ file: "src/b.py" }),
    finding({ file: "src/b.py" }),
  ]);
  assert.equal(collection.size, 2);
  assert.equal(collection.get(path.join(a.uri.fsPath, "src", "b.py")).length, 2);
});

test("a finding with no file is skipped rather than crashing", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));
  store.replace(a, "checks", [finding({ file: "" }), finding()]);
  assert.equal(collection.size, 1);
});

test("clear() empties everything", () => {
  const collection = fakeCollection();
  const store = storeIn(collection);
  const a = folder(path.join(os.tmpdir(), "projA"));
  store.replace(a, "checks", [finding()]);
  store.clear();
  assert.equal(collection.size, 0);
});

// ── which commands may produce diagnostics at all ───────────────────────────

test("test-run is deliberately not a diagnostics producer", () => {
  // A failing test is not a diagnostic about a line of source; a red suite
  // would bury the findings that are.
  const tree = fs.readFileSync(
    path.join(__dirname, "..", "src", "tree.ts"), "utf8");
  const declaration = tree.slice(tree.indexOf("DIAGNOSTIC_COMMANDS"));
  const set = declaration.slice(0, declaration.indexOf("]"));
  for (const producer of ["checks", "doctor", "scout"]) {
    assert.ok(set.includes(producer), `${producer} should produce diagnostics`);
  }
  assert.ok(!set.includes("test-run"), "test-run must not produce diagnostics");
});
