/**
 * test/observations.test.js — capturing the Problems panel for the Manager.
 *
 * Two of these must live on this side of the boundary, because what they
 * assert about is TypeScript: the multiset subtraction that stops the Manager
 * observing itself, and the invariant that a capture mutates nothing. Both are
 * untestable from Python, which never sees a `DiagnosticCollection`.
 *
 * The rest is the inbound coordinate conversion — the twin of the one
 * `diagnostics.test.js` guards in the outbound direction.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { vscodeStub, fakeCollection, folder } = require("./vscode-stub");
const diagnostics = require("../out/diagnostics.js");
const observations = require("../out/observations.js");

const SEVERITY = vscodeStub.DiagnosticSeverity;

function diag(overrides = {}) {
  const o = {
    line: 6, character: 15, endLine: 6, endCharacter: 20,
    severity: SEVERITY.Error, message: "m", source: "Pylance",
    code: "reportOptionalMemberAccess",
    ...overrides,
  };
  const d = new vscodeStub.Diagnostic(
    new vscodeStub.Range(o.line, o.character, o.endLine, o.endCharacter),
    o.message, o.severity);
  d.source = o.source;
  d.code = o.code;
  return d;
}

function fixture() {
  const f = folder(path.join(path.sep + "proj"));
  const collection = fakeCollection();
  const store = new diagnostics.DiagnosticStore(collection);
  return { f, collection, store };
}

function uriIn(f, rel) {
  return vscodeStub.Uri.file(path.join(f.uri.fsPath, rel));
}

// ── the echo loop ───────────────────────────────────────────────────────────

// The fixture below is chosen, not arbitrary, and the first draft was wrong.
//
// Measured against a Set-based implementation of the same function:
//
//   owned 2, editor 2 identical + 1 other   set=1  multiset=1   degenerate
//   owned 1, editor 2 identical             set=0  multiset=1   discriminates
//   owned 1, editor 2 identical + 1 other   set=1  multiset=2   discriminates
//
// The first row is what this test originally used. It is degenerate **with
// respect to this specific mutation**: owning as many copies as exist means
// "remove all matching" and "remove one each" agree, so the test would have
// passed against the very implementation its name rejects. Owning FEWER copies
// than exist is what separates them.
//
// Both wrong implementations were then planted in the compiled output and
// confirmed to go red — and they are caught by DIFFERENT arms, which is why
// all three of these tests are here:
//
//   mutation                              caught by
//   ------------------------------------  ---------------------------------
//   Set difference (removes ALL matching) "multiset subtraction…"  +
//                                         "owning one of two…"
//   budget -> 0 (removes only the FIRST)  "owning as many as exist…"
//
// The second mutation is invisible to the first two tests, because with one
// owned copy it behaves identically to the real thing. A single test here
// would have left half the contract unguarded.
//
// See `gotchas/tests-that-pass-without-testing.md` §3 and §4.

test("multiset subtraction removes one occurrence per owned diagnostic", () => {
  // We own ONE of the two identical rows. A Set-based difference erases both
  // and keeps 1; a multiset keeps the un-owned duplicate as well, so 2.
  const owned = [diag()];
  const editor = [diag(), diag(), diag({ source: "Ruff", code: "F401" })];

  const kept = observations.subtractOwned(editor, owned);

  assert.equal(kept.length, 2);
  assert.deepEqual(kept.map((d) => d.source).sort(), ["Pylance", "Ruff"]);
});

test("owning one of two identical diagnostics leaves the other", () => {
  const kept = observations.subtractOwned([diag(), diag()], [diag()]);
  assert.equal(kept.length, 1);
});

test("owning as many as exist removes all of them", () => {
  // The other half of the contract: multiset subtraction is not "keep one
  // back". When the counts match, nothing survives.
  assert.equal(
    observations.subtractOwned([diag(), diag()], [diag(), diag()]).length, 0);
});

test("a third-party diagnostic sharing our source name survives", () => {
  // Why the subtraction is by VALUE and not by `source`. A genuinely installed
  // pyflakes extension sets `source: "pyflakes"` too, and a name filter would
  // silently drop the independent second opinion this feature exists to get.
  const ours = diag({ source: "pyflakes", code: "", message: "ours", line: 1 });
  const theirs = diag({ source: "pyflakes", code: "", message: "theirs",
                        line: 2 });

  const kept = observations.subtractOwned([ours, theirs], [ours]);

  assert.equal(kept.length, 1);
  assert.equal(kept[0].message, "theirs");
});

test("nothing owned means nothing is subtracted", () => {
  const editor = [diag(), diag({ code: "F401" })];
  assert.equal(observations.subtractOwned(editor, []).length, 2);
});

test("a capture excludes the Manager's own findings", () => {
  const { f, store } = fixture();
  // The Manager reports one finding; the store now owns it.
  store.replace(f, "checks", [{
    file: "src/a.py", line: 7, column: 16, end_line: 7, end_column: 21,
    severity: "error", message: "m", rule: "pyflakes", symbol: "",
  }]);
  const owned = store.ownedFor(path.join(f.uri.fsPath, "src", "a.py"));
  assert.equal(owned.length, 1, "the store should own the finding it was given");

  const entries = [[uriIn(f, path.join("src", "a.py")),
                    [owned[0], diag({ line: 30 })]]];

  const envelope = observations.buildEnvelope(f, entries, store, 1789000000);

  assert.equal(envelope.observations.length, 1,
               "our own finding was captured back as a second source");
  assert.equal(envelope.observations[0].line, 31);
});

// ── the one-way arrow ───────────────────────────────────────────────────────

test("building an envelope mutates neither the collection nor the store", () => {
  const { f, collection, store } = fixture();
  store.replace(f, "checks", [{
    file: "src/a.py", line: 1, column: 1, end_line: 1, end_column: 1,
    severity: "warning", message: "m", rule: "pyflakes", symbol: "",
  }]);
  const before = collection.paths().map(
    (p) => [p, collection.get(p).length]);
  const ownedBefore = store.ownedFor(
    path.join(f.uri.fsPath, "src", "a.py")).length;

  observations.buildEnvelope(
    f, [[uriIn(f, path.join("src", "a.py")), [diag()]]], store, 1);

  assert.deepEqual(
    collection.paths().map((p) => [p, collection.get(p).length]), before);
  assert.equal(
    store.ownedFor(path.join(f.uri.fsPath, "src", "a.py")).length,
    ownedBefore);
});

// ── coordinates: the inbound twin of diagnostics.ts ─────────────────────────

test("0-based editor positions become 1-based envelope coordinates", () => {
  const { f } = fixture();
  const row = observations.toObservation(
    uriIn(f, path.join("src", "a.py")), f.uri.fsPath,
    diag({ line: 6, character: 15, endLine: 6, endCharacter: 20 }));
  assert.equal(row.line, 7);
  assert.equal(row.column, 16);
  assert.equal(row.end_line, 7);
  assert.equal(row.end_column, 21);
});

test("the very start of a file is 1:1, never 0:0", () => {
  // A 0 reaching the envelope is rejected by the Python reader, which is what
  // makes this fail loudly rather than shifting every row one line up.
  const { f } = fixture();
  const row = observations.toObservation(
    uriIn(f, "a.py"), f.uri.fsPath,
    diag({ line: 0, character: 0, endLine: 0, endCharacter: 0 }));
  assert.equal(row.line, 1);
  assert.equal(row.column, 1);
});

test("paths are repo-relative with forward slashes", () => {
  const { f } = fixture();
  const row = observations.toObservation(
    uriIn(f, path.join("src", "sub", "a.py")), f.uri.fsPath, diag());
  assert.equal(row.file, "src/sub/a.py");
});

// ── rule codes ──────────────────────────────────────────────────────────────

test("a rule code is read from all three shapes VS Code allows", () => {
  assert.equal(observations.ruleOf(diag({ code: "F401" })), "F401");
  assert.equal(observations.ruleOf(diag({ code: 7 })), "7");
  assert.equal(
    observations.ruleOf(diag({ code: { value: "MD001", target: {} } })),
    "MD001");
});

test("a diagnostic with no code yields an empty rule, not a crash", () => {
  assert.equal(observations.ruleOf(diag({ code: undefined })), "");
  assert.equal(observations.ruleOf(diag({ code: null })), "");
});

test("the rule stays unqualified so headless and editor can agree", () => {
  // Prefixing the producer here would make pyright's own
  // `reportOptionalMemberAccess` and Pylance's look like different defects,
  // which is the one comparison this feature exists to make.
  const { f } = fixture();
  const row = observations.toObservation(uriIn(f, "a.py"), f.uri.fsPath, diag());
  assert.equal(row.rule, "reportOptionalMemberAccess");
  assert.equal(row.producer, "Pylance");
});

// ── coverage ────────────────────────────────────────────────────────────────

test("analyzed_files is null, because the API cannot answer it", () => {
  const { f, store } = fixture();
  const envelope = observations.buildEnvelope(
    f, [[uriIn(f, "a.py"), [diag()]]], store, 1);
  assert.equal(envelope.coverage.analyzed_files, null);
  assert.equal(envelope.coverage.diagnostic_entries, 1);
  assert.equal(envelope.coverage.files_with_diagnostics, 1);
});

test("a sibling folder's diagnostics are not captured into this folder", () => {
  const { f, store } = fixture();
  const other = folder(path.join(path.sep + "other"));
  const envelope = observations.buildEnvelope(
    f,
    [[uriIn(f, "a.py"), [diag()]],
     [uriIn(other, "b.py"), [diag(), diag({ line: 2 })]]],
    store, 1);
  assert.equal(envelope.observations.length, 1);
  assert.equal(envelope.observations[0].file, "a.py");
});

test("the envelope carries its schema version", () => {
  const { f, store } = fixture();
  const envelope = observations.buildEnvelope(f, [], store, 1);
  assert.equal(envelope.observations_schema_version,
               observations.OBSERVATIONS_SCHEMA_VERSION);
  assert.equal(envelope.captured_at, 1);
});
