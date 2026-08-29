/**
 * test/surfaces.test.js — the decision logic behind the new surfaces.
 *
 * The status bar, the commit composer and the savings view are mostly
 * rendering, and rendering is not what breaks. What breaks is the handful of
 * judgements underneath: whether an unreadable project reads as clean, whether
 * a burst of watcher events becomes a burst of subprocesses, whether an
 * overtaken response overwrites a newer one, and whether a per-file run wipes
 * findings it never looked at.
 *
 * Each of those is a pure function or a small class here, tested without an
 * extension host — the same trade the existing tests make, and for the same
 * reason.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { folder, fakeCollection } = require("./vscode-stub");

const status = require("../out/status");
const commit = require("../out/commit");
const savings = require("../out/savings");
const { DiagnosticStore } = require("../out/diagnostics");
const { COMMANDS, commandByAction } = require("../out/commands");

/** An envelope shaped like the CLI's, with `data` filled in. */
function envelope(data, extra = {}) {
  return {
    exitCode: 0,
    transportError: null,
    envelope: {
      schema_version: 1, cli_version: "test", command: "status", ok: true,
      data, findings: [], warnings: [], error: null, ...extra,
    },
  };
}

// ── status: unknown is never clean ────────────────────────────────────────

test("an unreadable status is summarised as unknown, not clean", () => {
  const result = { exitCode: 3, envelope: null,
                   transportError: "CLI unavailable" };
  assert.strictEqual(status.summarise(result), null);
  assert.match(status.renderText(null), /\?/);
});

test("a null dirty flag renders as unknown rather than clean", () => {
  // The CLI sends null when git could not answer. Rendering that as "clean"
  // would report a healthy tree it never managed to read.
  const summary = status.summarise(envelope({
    git: { branch: "main", dirty: null, changed_files: null },
    mcp: {}, commit_request: {},
  }));
  assert.strictEqual(summary.dirty, null);
  assert.match(status.renderText(summary), /\?/);
});

test("a dirty tree shows how many files changed", () => {
  const summary = status.summarise(envelope({
    git: { branch: "main", dirty: true,
           changed_files: [{ path: "a.py" }, { path: "b.py" }] },
    mcp: {}, commit_request: {},
  }));
  assert.strictEqual(summary.changedFiles, 2);
  assert.match(status.renderText(summary), /●2/);
});

test("a pending request is shown in the bar", () => {
  const summary = status.summarise(envelope({
    git: { branch: "main", dirty: false },
    mcp: {}, commit_request: { pending: true },
  }));
  assert.ok(summary.pendingRequest);
  assert.match(status.renderText(summary), /inbox/);
});

test("the tooltip says MCP config was not probed", () => {
  // `configured` is a fact about a file on disk. Rendering it as a plain tick
  // would claim an effective-scope verdict this command deliberately does not
  // compute, because probing CREATES a ~/.claude.json entry.
  const summary = status.summarise(envelope({
    git: { branch: "main", dirty: false },
    mcp: { configured: true, probed: false }, commit_request: {},
  }));
  const tooltip = status.renderTooltip(summary, "proj", true);
  assert.match(tooltip, /config present/);
  assert.match(tooltip, /not probed/);
  assert.doesNotMatch(tooltip, /MCP: ✓/);
});

test("the tooltip distinguishes an unknown Manager from a stopped one", () => {
  const summary = status.summarise(envelope({
    git: {}, mcp: {}, commit_request: {},
  }));
  assert.match(status.renderTooltip(summary, "p", null), /Manager: unknown/);
  assert.match(status.renderTooltip(summary, "p", false),
               /Manager: not running/);
});

// ── status: bounded refresh ───────────────────────────────────────────────

test("a burst of events becomes exactly one refresh", async () => {
  let runs = 0;
  const debouncer = new status.Debouncer(10, () => { runs += 1; });
  for (let i = 0; i < 12; i += 1) {
    debouncer.schedule();
  }
  await new Promise((r) => setTimeout(r, 40));
  assert.strictEqual(runs, 1, "twelve watcher events must not mean twelve runs");
  debouncer.dispose();
});

test("a disposed debouncer never fires", async () => {
  let runs = 0;
  const debouncer = new status.Debouncer(10, () => { runs += 1; });
  debouncer.schedule();
  debouncer.dispose();
  await new Promise((r) => setTimeout(r, 40));
  assert.strictEqual(runs, 0);
});

test("an overtaken response is dropped", () => {
  // Two status calls in flight; the older finishes last. Without this the
  // stale answer replaces the fresh one and nothing looks wrong.
  const sequence = new status.Sequence();
  const first = sequence.next();
  const second = sequence.next();
  assert.ok(sequence.accept(second));
  assert.ok(!sequence.accept(first), "the older reply must not be applied");
});

test("responses arriving in order are all accepted", () => {
  const sequence = new status.Sequence();
  const a = sequence.next();
  assert.ok(sequence.accept(a));
  const b = sequence.next();
  assert.ok(sequence.accept(b));
});

// ── commit composer ───────────────────────────────────────────────────────

test("an unreadable repository yields null, not an empty file list", () => {
  // Empty means "nothing changed"; null means "we could not find out". A
  // composer that conflated them would offer a clean tree for a broken repo.
  assert.strictEqual(
    commit.changedFiles(envelope({ git: { changed_files: null } })), null);
  assert.deepStrictEqual(
    commit.changedFiles(envelope({ git: { changed_files: [] } })), []);
});

test("changed files are read with their status", () => {
  const files = commit.changedFiles(envelope({
    git: { changed_files: [
      { path: "src/a.py", status: "modified" },
      { path: "b.py", status: "renamed", old_path: "a.py" },
    ] },
  }));
  assert.strictEqual(files.length, 2);
  assert.strictEqual(files[1].old_path, "a.py");
});

test("a rename shows where the file came from", () => {
  const item = commit.toPickItem(
    { path: "b.py", status: "renamed", old_path: "a.py" });
  assert.strictEqual(item.label, "b.py");
  assert.match(item.detail, /renamed from a\.py/);
});

test("a pending-conflict failure is recognised as a choice, not an error", () => {
  const conflict = {
    exitCode: 1, transportError: null,
    envelope: { error: "a different commit request is already pending; "
                       + "pass --replace to overwrite it" },
  };
  assert.ok(commit.isPendingConflict(conflict));
});

test("an ordinary failure is not mistaken for a pending conflict", () => {
  const other = {
    exitCode: 1, transportError: null,
    envelope: { error: "something else went wrong" },
  };
  assert.ok(!commit.isPendingConflict(other));
});

test("a filed request is read back with its correlation id", () => {
  const filed = commit.readFiled(envelope({
    id: "abc123def456", duplicate: false, manager_running: true,
  }));
  assert.strictEqual(filed.id, "abc123def456");
  assert.strictEqual(filed.managerRunning, true);
});

test("an unreadable reply yields null rather than a fake id", () => {
  assert.strictEqual(commit.readFiled(envelope({})), null);
});

// ── savings view ──────────────────────────────────────────────────────────

test("text and attribute escaping are separate", () => {
  // One generic escaper used in both places is how one of the two contexts
  // ends up subtly wrong. Every string in this view is untrusted content.
  assert.strictEqual(savings.escapeText('<img src=x>'),
                     "&lt;img src=x&gt;");
  assert.match(savings.escapeAttr('a"b'), /&quot;/);
  assert.doesNotMatch(savings.escapeText('a"b'), /&quot;/,
                      "text nodes do not need quote escaping");
});

test("the CSP forbids inline script and everything by default", () => {
  const csp = savings.contentSecurityPolicy("vscode-resource:", "NONCE123");
  assert.match(csp, /default-src 'none'/);
  assert.match(csp, /script-src 'nonce-NONCE123'/);
  assert.doesNotMatch(csp, /script-src[^;]*unsafe-inline/,
                      "a blanket inline allowance would make an escaping slip "
                      + "exploitable");
});

test("each nonce is different", () => {
  assert.notStrictEqual(savings.makeNonce(), savings.makeNonce());
});

test("only the documented ranges are accepted", () => {
  assert.ok(savings.isRange("30d"));
  assert.ok(!savings.isRange("fortnight"));
  assert.ok(!savings.isRange(""));
});

test("an unreadable cost envelope becomes an error model, not zeros", () => {
  const model = savings.toModel(
    { exitCode: 1, envelope: null, transportError: "boom" }, "30d", null);
  assert.match(model.error, /boom/);
  assert.strictEqual(model.savings, null);
  assert.strictEqual(model.spend, null);
});

test("a good envelope keeps the sections apart", () => {
  const model = savings.toModel(envelope({
    savings: { ok: true, saved_tokens: 47776 },
    spend: { ok: true, total_cost_usd: 4132.75 },
    opportunity: { ok: true, replaceable_turns: 239 },
    savings_history: { ok: true, days: [] },
  }), "30d", 12345);

  assert.strictEqual(model.savings.saved_tokens, 47776);
  assert.strictEqual(model.spend.total_cost_usd, 4132.75);
  // The two dollar figures are different quantities and never share a field.
  assert.ok(!("usd" in model.spend));
  assert.strictEqual(model.spendFetchedAt, 12345);
});

test("a section that failed keeps its reason", () => {
  const model = savings.toModel(envelope({
    savings: { ok: true },
    spend: { ok: false, reason: "ledger locked" },
  }), "7d", null);
  assert.strictEqual(model.spend.ok, false);
  assert.strictEqual(model.spend.reason, "ledger locked");
});

// ── scoped diagnostics ────────────────────────────────────────────────────

function finding(file, message) {
  return { file, line: 1, column: 1, end_line: 1, end_column: 1,
           severity: "warning", message, rule: "pyflakes", symbol: "" };
}

test("a file-scoped run replaces only that file's findings", () => {
  // The invariant the editor context actions depend on: "Checks this file"
  // looked at one file, so it may not clear findings in another.
  const collection = fakeCollection();
  const store = new DiagnosticStore(collection);
  const root = folder(path.join("C:", "proj"));

  store.replace(root, "checks",
                [finding("a.py", "in a"), finding("b.py", "in b")]);
  assert.strictEqual(collection.size, 2);

  store.replace(root, "checks", [finding("a.py", "still in a")], ["a.py"]);
  assert.strictEqual(collection.size, 2, "b.py must survive a run about a.py");
  assert.strictEqual(
    collection.get(path.join("C:", "proj", "b.py"))[0].message, "in b");
  assert.strictEqual(
    collection.get(path.join("C:", "proj", "a.py"))[0].message, "still in a");
});

test("a file-scoped run with no findings clears only that file", () => {
  const collection = fakeCollection();
  const store = new DiagnosticStore(collection);
  const root = folder(path.join("C:", "proj"));

  store.replace(root, "checks",
                [finding("a.py", "in a"), finding("b.py", "in b")]);
  store.replace(root, "checks", [], ["a.py"]);

  assert.strictEqual(collection.get(path.join("C:", "proj", "a.py")), undefined);
  assert.ok(collection.get(path.join("C:", "proj", "b.py")),
            "fixing a.py must not clear b.py");
});

test("a whole-project run still replaces everything it owns", () => {
  const collection = fakeCollection();
  const store = new DiagnosticStore(collection);
  const root = folder(path.join("C:", "proj"));

  store.replace(root, "checks",
                [finding("a.py", "in a"), finding("b.py", "in b")]);
  store.replace(root, "checks", [finding("a.py", "only a now")]);

  assert.strictEqual(collection.size, 1);
  assert.strictEqual(collection.get(path.join("C:", "proj", "a.py"))[0].message,
                     "only a now");
});

test("one producer's refresh leaves the others alone", () => {
  // The exact invariant: checks/A, checks/B and scout/A present; refreshing
  // scout/A leaves the first two untouched.
  const collection = fakeCollection();
  const store = new DiagnosticStore(collection);
  const root = folder(path.join("C:", "proj"));

  store.replace(root, "checks",
                [finding("a.py", "checks a"), finding("b.py", "checks b")]);
  store.replace(root, "scout", [finding("a.py", "scout a")]);
  store.replace(root, "scout", [finding("a.py", "scout a again")]);

  const onA = collection.get(path.join("C:", "proj", "a.py"))
    .map((d) => d.message);
  assert.ok(onA.includes("checks a"), "checks findings must survive");
  assert.ok(onA.includes("scout a again"));
  assert.ok(!onA.includes("scout a"), "the stale scout finding must be gone");
  assert.ok(collection.get(path.join("C:", "proj", "b.py")));
});

test("forgetting a folder leaves a sibling folder intact", () => {
  const collection = fakeCollection();
  const store = new DiagnosticStore(collection);
  const one = folder(path.join("C:", "one"));
  const two = folder(path.join("C:", "two"));

  store.replace(one, "checks", [finding("a.py", "one")]);
  store.replace(two, "checks", [finding("a.py", "two")]);
  store.forgetFolder(one);

  assert.strictEqual(collection.get(path.join("C:", "one", "a.py")), undefined);
  assert.ok(collection.get(path.join("C:", "two", "a.py")));
});

// ── the generated table ───────────────────────────────────────────────────

test("the generated command table is populated and namespaced", () => {
  assert.ok(COMMANDS.length > 5);
  for (const command of COMMANDS) {
    assert.ok(command.action, "every command needs a stable identifier");
    assert.ok(["pure_read", "observe_refresh", "mutating"]
      .includes(command.sideEffect), `${command.action}: ${command.sideEffect}`);
  }
});

test("only the commands that support --paths claim to", () => {
  // The per-file editor actions consult this before scoping, so it must match
  // what the CLI accepts.
  const scoped = COMMANDS.filter((c) => c.acceptsPaths).map((c) => c.action);
  assert.deepStrictEqual(scoped.sort(), ["checks", "test-gaps"]);
});

test("cost and doctor are not advertised as pure reads", () => {
  // Both refresh tokensave's own bookkeeping — measured, not assumed.
  assert.strictEqual(commandByAction("cost").sideEffect, "observe_refresh");
  assert.strictEqual(commandByAction("doctor").sideEffect, "observe_refresh");
});

test("an unknown action lookup returns undefined rather than throwing", () => {
  assert.strictEqual(commandByAction("nope"), undefined);
});
