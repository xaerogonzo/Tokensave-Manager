/**
 * runTests.js — boot a real VS Code with this extension loaded, and run the
 * suite inside it.
 *
 * The extension's user-visible surfaces — tree rows, the webview document,
 * the status bar item, Problems entries, whether a contributed command is
 * actually registered — are unreachable from outside the editor. The stub
 * suite in `test/*.test.js` says so in its own header: it covers the
 * decisions, not the wiring. This covers the wiring, by holding the real
 * `vscode` module in a real window.
 *
 * ## The fixture is copied, never used in place
 *
 * `test/workspace/` is a template. It is copied to `.vscode-test/workspace/`
 * and `git init`-ed there, for three reasons: the repository cannot contain a
 * nested git repository without submodule ceremony; the mutation runner has to
 * be free to destroy anything it touches; and a `.tokensave/` index generated
 * beside the template would sit in the source tree pointing at itself.
 *
 * ## managerPath is resolved here, not committed
 *
 * The extension shells out to the Manager's `src/cli.py`, so the fixture's
 * states come from a real CLI run. The path to that checkout is written into
 * the copied workspace's `.vscode/settings.json` at setup, from this file's
 * own location — a developer's absolute path committed into a fixture is
 * wrong on CI and rots locally.
 */
"use strict";

const path = require("node:path");
const { downloadAndUnzipVSCode } = require("@vscode/test-electron");

const shared = require("./shared");

const EXTENSION_ROOT = shared.EXTENSION_ROOT;
const TEST_HOME = path.join(EXTENSION_ROOT, ".vscode-test");

async function main() {
  const workspace = shared.prepareWorkspace(TEST_HOME).file;
  const exe = shared.installedVSCode() || await downloadAndUnzipVSCode();

  const args = shared.buildLaunchArgs({
    extensionDevelopmentPath: EXTENSION_ROOT,
    extensionTestsPath: path.resolve(__dirname, "suite", "index.js"),
    launchArgs: [
      workspace,   // the .code-workspace file, so both roots open
      // Suppresses *installed* extensions. The one under test arrives via
      // extensionDevelopmentPath and still activates — which is the point:
      // another extension's hovers and diagnostics land in the same buffers
      // these tests read back, and a test that passes because a language
      // server agreed is worth nothing.
      "--disable-extensions",
      // A profile of its own, so nothing here touches real settings and
      // nothing in real settings decides whether tests pass.
      "--user-data-dir", path.join(TEST_HOME, "user-data"),
      "--extensions-dir", path.join(TEST_HOME, "extensions"),
      // Otherwise the first run blocks on a modal nobody is there to click.
      // Trust is not under test; this is bypassing a real safety mechanism
      // for a reason, rather than by oversight.
      "--disable-workspace-trust",
      "--disable-gpu",
    ],
  });

  const env = {
    TOKENSAVE_TEST_WORKSPACE: shared.workspacePaths(TEST_HOME).first,
    TOKENSAVE_TEST_WORKSPACE_2: shared.workspacePaths(TEST_HOME).second,
    TOKENSAVE_TEST_REPO: shared.REPO_ROOT,
    TOKENSAVE_TEST_GREP: process.env.TOKENSAVE_TEST_GREP || "",
  };

  // The window minder is only needed when the editor is on the desktop you
  // are looking at. On a private desktop there is no window to put down, so
  // starting one would be a PowerShell process doing nothing.
  const stopMinder = shared.desktopAvailable()
    ? () => {} : shared.keepOutOfTheWay();
  try {
    const code = await shared.launchVSCode({ exe, args, env });
    if (code !== 0) {
      throw new Error(`the live suite failed (exit ${code})`);
    }
  } finally {
    stopMinder();
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
