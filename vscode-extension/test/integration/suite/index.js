/**
 * suite/index.js — what VS Code calls once the extension host is up.
 *
 * Loads every `*.test.js` beside this file and runs them. The list is explicit
 * rather than globbed: `glob` was a dependency this suite did not otherwise
 * need, and a hand-written list fails loudly when a file is renamed instead of
 * quietly running one fewer test.
 */
"use strict";

const path = require("node:path");
const harness = require("./harness");

/** Test files, in the order they run. Add new files here. */
const FILES = [
  "activation.test.js",
  "commands.test.js",
  "tree.test.js",
  "diagnostics.test.js",
  "webview.test.js",
  "statusbar.test.js",
  "testing.test.js",
];

async function run() {
  for (const file of FILES) {
    require(path.join(__dirname, file));
  }
  await harness.run({ grep: process.env.TOKENSAVE_TEST_GREP || "" });
}

module.exports = { run };
