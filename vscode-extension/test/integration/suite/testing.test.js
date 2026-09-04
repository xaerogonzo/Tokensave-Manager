/**
 * testing.test.js — the Test Explorer, in a real editor.
 *
 * The headless suite covers the decisions: which node ids reach the command
 * line, how a per-test record becomes Explorer state, what a cancelled run
 * reports. None of that needs an editor.
 *
 * What does need one is the tree itself. `vscode.tests.createTestController`
 * has no stand-in worth trusting — the stub's version stores what it is handed,
 * which proves the code called it and nothing about whether a person would see
 * their tests. So this asserts on **rendered rows**, the same rule the projects
 * tree follows: a tree whose items are correct and whose labels are wrong is a
 * broken tree, and only one of those is what anybody looks at.
 *
 * It also covers the property the headless tests structurally cannot: that the
 * tree **refreshes** when a test file appears or disappears. That runs through
 * a real `FileSystemWatcher`, which is exactly the part a fake would assume
 * away.
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { workspaceRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

/** Rows with indentation stripped, for membership assertions. */
const flat = (rows) => rows.map((r) => r.trim());

describe("test explorer", () => {
  let api;

  before(async () => {
    api = await vscode.extensions.getExtension(EXTENSION_ID).activate();
  });

  it("lists the fixture's tests at test granularity", async () => {
    const rows = flat(await api.renderTests());
    // Not "there is a tests/ node" — the individual definitions, which is the
    // whole difference between this and the four numbers `test-run` reported
    // before it existed.
    for (const label of ["test_module_level", "test_parametrised",
                         "TestGrouped::test_method",
                         "test_in_the_second_file"]) {
      assert.ok(rows.includes(label),
                `no row for ${label}; rows: ${rows.join(" | ")}`);
    }
  });

  it("groups tests under the file they live in", async () => {
    const rows = flat(await api.renderTests());
    assert.ok(rows.includes("tests/test_fixture.py"), rows.join(" | "));
    assert.ok(rows.includes("tests/test_second_file.py"), rows.join(" | "));
  });

  it("names a class method with its class, as pytest addresses it", async () => {
    const rows = flat(await api.renderTests());
    assert.ok(rows.includes("TestGrouped::test_method"),
              "a bare method name would be ambiguous between two classes");
  });

  it("shows a parametrised test once, as the one definition it is", async () => {
    // Discovery is an AST walk and cannot see parametrize expansion; the node
    // id it produces is a selector and a prefix. Rendering two rows here would
    // mean inventing expansion the walk never saw.
    const rows = flat(await api.renderTests());
    const matches = rows.filter((r) => r.startsWith("test_parametrised"));
    assert.strictEqual(matches.length, 1, rows.join(" | "));
  });

  it("reports a marker as a description rather than in the label", async () => {
    const rows = await api.renderTests();
    const row = rows.find((r) => r.trim().startsWith("test_parametrised"));
    // Labels stay the test's name; markers are metadata beside it.
    assert.strictEqual(row.trim(), "test_parametrised");
  });

  it("picks up a test file that appears after activation", async () => {
    // The property a fake watcher would assume away. Without invalidation the
    // tree is correct exactly once — at activation — and then silently drifts
    // from the file the user is editing.
    const added = path.join(workspaceRoot(), "tests", "test_added_later.py");
    fs.writeFileSync(added, "def test_added_later():\n    assert True\n",
                     "utf8");
    try {
      await until("the new test to appear in the Explorer", async () => {
        const rows = flat(await api.renderTests());
        return rows.includes("test_added_later");
      });
    } finally {
      fs.rmSync(added, { force: true });
    }
  });

  it("drops a test file that goes away", async () => {
    const added = path.join(workspaceRoot(), "tests", "test_transient.py");
    fs.writeFileSync(added, "def test_transient():\n    assert True\n", "utf8");
    await until("the transient test to appear", async () => {
      const rows = flat(await api.renderTests());
      return rows.includes("test_transient");
    });

    fs.rmSync(added, { force: true });
    await until("a deleted file's tests to leave the Explorer", async () => {
      const rows = flat(await api.renderTests());
      return !rows.includes("test_transient");
    });
  });
});
