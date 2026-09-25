/**
 * TokenSave Manager's VS Code driver.
 *
 *     npm run drive -- smoke
 *     npm run drive -- menu views --visible
 *
 * `lib/` is shared with PyScope's extension and identical there; what is specific
 * to this extension is here and in `scenarios/`. See `README.md`.
 *
 * The workspace is the live suite's own fixture, built by `test/integration/
 * shared.js`: two git roots with different numbers of uncommitted files, one of
 * them with a file the checks flag. Reusing it means the driver looks at the same
 * states the suite asserts about, and inherits the fixture's own proof that it can
 * produce them.
 */

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { main } = require("./lib/cli");

const EXTENSION = path.resolve(__dirname, "..");
const shared = require("../test/integration/shared");

// The suite's launcher knows where an installed editor is (and honours
// TOKENSAVE_TEST_VSCODE); the driver library reads VSCODE_EXE.
if (!process.env.VSCODE_EXE) {
  const found = shared.installedVSCode();
  if (found) {
    process.env.VSCODE_EXE = found;
  }
}

main({
  script: __filename,
  launcher: path.join(EXTENSION, "test", "integration", "run-on-desktop.ps1"),
  extensionPath: EXTENSION,
  // Replaced per launch below: the fixture is built when a scenario starts, not
  // when this file loads, because this file loads twice (once to hand the run to a
  // private desktop, once inside it).
  workspace: "",
  outDir: path.join(EXTENSION, "drive-out"),
  env: {},
  settings: {},
  launchOptions: () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "tsm-drive-fixture-"));
    return { workspace: shared.prepareWorkspace(home).file };
  },
  scenarios: {
    smoke: require("./scenarios/smoke"),
    menu: require("./scenarios/menu"),
    views: require("./scenarios/views"),
  },
});
