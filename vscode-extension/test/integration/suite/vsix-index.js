/**
 * vsix-index.js — the smoke subset run against the PACKAGED extension.
 *
 * Deliberately small. The live suite covers behaviour; this covers the one
 * thing it cannot, which is whether the artifact users install is the artifact
 * that was tested. The failures it exists to catch are packaging failures —
 * a file excluded by `.vscodeignore` that turns out to be needed, a
 * `vscode:prepublish` that did not compile, an activation event that only
 * fires in development — and all of those show up as "the extension is not
 * there" or "its commands are missing", not as a subtle assertion.
 *
 * It reuses `activation.test.js` and `commands.test.js` unchanged rather than
 * keeping a second copy: a smoke suite that has drifted from the suite it
 * mirrors is worse than no smoke suite, and those two files assert exactly
 * the properties packaging can break.
 */
"use strict";

const path = require("node:path");
const harness = require("./harness");

const FILES = [
  "activation.test.js",
  "commands.test.js",
  "tree.test.js",
];

async function run() {
  for (const file of FILES) {
    require(path.join(__dirname, file));
  }
  await harness.run({ grep: process.env.TOKENSAVE_TEST_GREP || "" });
}

module.exports = { run };
