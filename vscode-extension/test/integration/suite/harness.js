/**
 * harness.js — the ~100 lines that replace Mocha.
 *
 * `extensionTestsPath` requires a module exporting `run()` returning a promise
 * that rejects on failure. Node's own `node:test` runs in-process but only
 * signals completion through `process.exitCode` at exit, which is too late for
 * that contract, so the choice was Mocha or this.
 *
 * Mocha would have been one line, and brought three advisories with it whose
 * npm-suggested fix is a downgrade flagged as breaking. For a runner this
 * repository can write in a hundred lines — and which already hand-rolls its
 * `vscode-stub` and its geometry oracle rather than take dependencies — that
 * trade did not look worth making. `@vscode/test-electron` is the one new
 * dependency, and `npm audit` reports zero vulnerabilities.
 *
 * ## The failure this runner must not have
 *
 * A bespoke runner that silently registers nothing reports a green run having
 * executed no tests, which is indistinguishable from success and worse than a
 * red one. Two things guard it: `run()` refuses a run with zero tests, and the
 * mutation runner compares each arm's test count against a control, so an arm
 * that dies early is reported rather than read as "no failures".
 *
 * Deliberately missing: nested `describe`, `.only`, retries, parallelism,
 * fixtures. Every one is a thing to get subtly wrong, and none is needed by a
 * suite that runs an editor once and asks it questions.
 */
"use strict";

const suites = [];
let current = null;

/** Group tests. One level; nesting is not supported and would be silently
 *  wrong rather than an error in a naive implementation, so it throws. */
function describe(name, fn) {
  if (current) {
    throw new Error(
      `nested describe(${JSON.stringify(name)}) inside ` +
      `${JSON.stringify(current.name)} — this harness is one level deep`);
  }
  current = { name, tests: [], before: [], beforeEach: [], afterEach: [] };
  try {
    fn();
  } finally {
    suites.push(current);
    current = null;
  }
}

function _requireSuite(what) {
  if (!current) {
    throw new Error(`${what}() must be called inside describe()`);
  }
  return current;
}

function it(name, fn) { _requireSuite("it").tests.push({ name, fn }); }
function before(fn) { _requireSuite("before").before.push(fn); }
function beforeEach(fn) { _requireSuite("beforeEach").beforeEach.push(fn); }
function afterEach(fn) { _requireSuite("afterEach").afterEach.push(fn); }

/**
 * Wait for *predicate* to return something truthy.
 *
 * `setTimeout(2000)` in a test is a bet on the machine. The failure carries
 * the last value observed, because the difference between "timed out" and
 * "timed out, and here is what it kept saying" is most of the debugging.
 */
async function until(describeIt, predicate, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  let error;
  while (Date.now() < deadline) {
    try {
      last = await predicate();
      if (last) return last;
      error = undefined;
    } catch (err) {
      error = err;          // a predicate that throws while warming up
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(
    `timed out after ${timeoutMs}ms waiting for ${describeIt}; ` +
    (error ? `last threw: ${error.message}`
           : `last value: ${JSON.stringify(last)}`));
}

async function _runAll(fns, label) {
  for (const fn of fns) {
    try {
      await fn();
    } catch (err) {
      err.message = `${label}: ${err.message}`;
      throw err;
    }
  }
}

/**
 * Run everything registered. Resolves with counts; rejects if anything failed.
 *
 * `timeoutMs` is per test and generous: activation spawns a Python CLI, and a
 * default of a second or two fails on the machine rather than on the code.
 */
async function run({ timeoutMs = 90000, grep = "" } = {}) {
  const pattern = grep ? new RegExp(grep, "i") : null;
  const failures = [];
  let passed = 0;
  let total = 0;

  for (const suite of suites) {
    const selected = suite.tests.filter(
      (t) => !pattern || pattern.test(`${suite.name} ${t.name}`));
    if (!selected.length) continue;

    console.log(`\n  ${suite.name}`);
    try {
      await _runAll(suite.before, "before hook");
    } catch (err) {
      // A failed `before` invalidates every test under it. Reporting them as
      // "not run" rather than silently skipping keeps the count honest.
      for (const t of selected) {
        total++;
        failures.push({ name: `${suite.name} > ${t.name}`, err });
        console.log(`    ✖ ${t.name} (before hook failed)`);
      }
      continue;
    }

    for (const t of selected) {
      total++;
      const started = Date.now();
      try {
        await _runAll(suite.beforeEach, "beforeEach hook");
        await Promise.race([
          Promise.resolve().then(t.fn),
          new Promise((_, rej) => setTimeout(
            () => rej(new Error(`timed out after ${timeoutMs}ms`)), timeoutMs)),
        ]);
        await _runAll(suite.afterEach, "afterEach hook");
        passed++;
        console.log(`    ✔ ${t.name} (${Date.now() - started}ms)`);
      } catch (err) {
        failures.push({ name: `${suite.name} > ${t.name}`, err });
        console.log(`    ✖ ${t.name} — ${err.message}`);
        // afterEach still runs, so one failure cannot leak state into the
        // next test and turn one red into a cascade.
        try { await _runAll(suite.afterEach, "afterEach hook"); } catch { }
      }
    }
  }

  console.log(`\n  ${passed} passing, ${failures.length} failing ` +
              `(${total} total)`);
  // Machine-readable, and the line the mutation runner parses to compare an
  // arm's population against the control.
  console.log(`TOKENSAVE_TEST_COUNTS total=${total} passed=${passed} ` +
              `failed=${failures.length}`);

  if (total === 0) {
    throw new Error(
      "no tests ran. A green run that executed nothing is the failure this " +
      "runner most needs to avoid, so this is an error rather than a pass" +
      (pattern ? ` (grep ${JSON.stringify(grep)} matched nothing)` : ""));
  }
  if (failures.length) {
    for (const f of failures) {
      console.log(`\n  ${f.name}\n${f.err && f.err.stack ? f.err.stack : f.err}`);
    }
    throw new Error(`${failures.length} test(s) failed`);
  }
  return { total, passed };
}

module.exports = { describe, it, before, beforeEach, afterEach, until, run };
