/**
 * util.js — shared helpers for the live suite.
 */
"use strict";

const path = require("node:path");

/**
 * Do two paths name the same location?
 *
 * Windows-aware, because it has to be: VS Code reports the workspace folder
 * with a **lowercase drive letter** (`d:\...`) regardless of how the path was
 * passed in, so a strict string comparison against a path built from
 * `process.env` fails on the drive letter alone. That is a real difference
 * between what the editor reports and what the launcher passed, and it is not
 * a difference anybody wants a test to be about.
 *
 * Case-insensitive on Windows only. On POSIX `/work/alpha` and `/WORK/ALPHA`
 * are genuinely different directories, and folding them would make this
 * helper wrong in the quieter direction.
 */
function samePath(a, b) {
  if (!a || !b) return false;
  const norm = (p) => {
    const resolved = path.resolve(p);
    return process.platform === "win32" ? resolved.toLowerCase() : resolved;
  };
  return norm(a) === norm(b);
}

/** The disposable fixture workspace this run was launched against. */
function workspaceRoot() {
  const root = process.env.TOKENSAVE_TEST_WORKSPACE;
  if (!root) {
    throw new Error("TOKENSAVE_TEST_WORKSPACE is unset — runTests.js sets it");
  }
  return root;
}

/** The second fixture root, which exists so multi-root behaviour is testable. */
function secondRoot() {
  const root = process.env.TOKENSAVE_TEST_WORKSPACE_2;
  if (!root) {
    throw new Error("TOKENSAVE_TEST_WORKSPACE_2 is unset — runTests.js sets it");
  }
  return root;
}

module.exports = { samePath, workspaceRoot, secondRoot };
