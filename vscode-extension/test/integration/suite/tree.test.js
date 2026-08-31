/**
 * tree.test.js — the Projects view, as rows rather than as nodes.
 *
 * `test/vscode-stub.js` says in its own header that the tree is what it does
 * not cover. This is that coverage, and it asserts on **rendered strings**:
 * label, description, and indentation, exactly as the sidebar composes them.
 *
 * The reason is not stylistic. `ProjectsProvider.getTreeItem` is where the
 * label, the "not run" description and the pass/warning icon are all decided;
 * a test asserting against the `Node` union behind it would pass with all
 * three wrong.
 */
"use strict";

const assert = require("node:assert");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { workspaceRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

/** Rows with their indentation stripped, for membership assertions. */
const flat = (rows) => rows.map((r) => r.trim());

describe("projects tree", () => {
  let api;

  before(async () => {
    api = await vscode.extensions.getExtension(EXTENSION_ID).activate();
  });

  it("shows the workspace folder as the root row", async () => {
    const rows = await api.renderTree();
    assert.ok(rows.length > 0, "the tree rendered nothing at all");
    // The folder row carries its own path as the description, so a multi-root
    // workspace never leaves the user guessing which checkout a row is about.
    assert.ok(rows[0].includes("workspace"),
              `first row was ${JSON.stringify(rows[0])}`);
  });

  it("offers every action under the folder", async () => {
    const rows = flat(await api.renderTree());
    // The labels a person reads, from tree.ts's ACTIONS table.
    for (const label of ["Status", "MCP status", "Doctor", "Checks", "Scout",
                         "Tests", "Test gaps", "Run tests",
                         "Commit request"]) {
      assert.ok(rows.some((r) => r.startsWith(label)),
                `no row for ${JSON.stringify(label)}; rows: ${rows.join(" | ")}`);
    }
  });

  it("renders un-run actions as 'not run' rather than as a verdict", async () => {
    const rows = await api.renderTree();
    const status = rows.find((r) => r.trim().startsWith("Status"));
    assert.ok(status, "no Status row");
    // The distinction the whole tree rests on: nothing has been run, which is
    // not the same as "ran and was clean". A blank description would read as
    // the latter.
    assert.match(status, /\[not run\]/);
  });

  it("indents actions beneath their folder", async () => {
    const rows = await api.renderTree();
    const folder = rows[0];
    const action = rows.find((r) => r.trim().startsWith("Status"));
    assert.ok(!folder.startsWith("  "), "the folder row should be at depth 0");
    assert.ok(action.startsWith("  "),
              `action rows should be indented: ${JSON.stringify(action)}`);
  });

  it("replaces 'not run' with a summary once an action has run", async () => {
    const folder = vscode.workspace.workspaceFolders[0];
    await vscode.commands.executeCommand(
      "tokensaveManager.runAction", folder, "status");

    const rows = await until("the Status row to report a result", async () => {
      const current = await api.renderTree();
      const row = current.find((r) => r.trim().startsWith("Status"));
      return row && !row.includes("not run") ? row : null;
    });
    // What it summarises to depends on the fixture's git state; that it
    // stopped saying "not run" is the property under test.
    assert.ok(!rows.includes("not run"), rows);
  });
});

describe("projects tree without a usable CLI", () => {
  let api;

  before(async () => {
    api = await vscode.extensions.getExtension(EXTENSION_ID).activate();
  });

  it("offers the fix, wired to the command that provides it", async () => {
    // Point the setting at nothing so `cliUnavailableReason` fires. The
    // config change is workspace-scoped and the workspace is disposable.
    const config = vscode.workspace.getConfiguration("tokensaveManager");
    const previous = config.get("managerPath");
    await config.update("managerPath", "",
                        vscode.ConfigurationTarget.Workspace);
    try {
      await vscode.commands.executeCommand("tokensaveManager.refresh");
      const rows = await until("the unavailable row", async () => {
        const current = await api.renderTree();
        return current.find((r) => r.includes("unavailable")) || null;
      });

      // Rendered: a person sees the row and that it can be clicked.
      assert.match(rows, /click to fix/);
      // Structural: and it is wired to the command that actually helps. A row
      // that says "click to fix" while pointing at the wrong command is a
      // defect no string assertion can see, which is the one place this suite
      // reaches past the rendered text.
      assert.strictEqual(api.treeCommandFor(rows),
                         "tokensaveManager.setManagerPath");
    } finally {
      await config.update("managerPath", previous,
                          vscode.ConfigurationTarget.Workspace);
      await vscode.commands.executeCommand("tokensaveManager.refresh");
    }
  });

  it("recovers once the path is restored", async () => {
    await until("the folder rows to come back", async () => {
      const rows = await api.renderTree();
      return rows.some((r) => r.trim().startsWith("Status")) ? rows : null;
    });
  });
});
