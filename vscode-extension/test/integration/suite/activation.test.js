/**
 * activation.test.js — the extension loads, and hands back its test seam.
 *
 * The cheapest possible assertion, kept because it is the one that tells you
 * whether a failure elsewhere is about the code or about the harness.
 */
"use strict";

const assert = require("node:assert");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { samePath, workspaceRoot, secondRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

describe("activation", () => {
  let api;

  before(async () => {
    const ext = vscode.extensions.getExtension(EXTENSION_ID);
    assert.ok(ext, `extension ${EXTENSION_ID} was not loaded by the host`);
    api = await ext.activate();
  });

  it("returns the test API from activate", () => {
    assert.ok(api, "activate() returned nothing");
    for (const name of ["whenReady", "renderTree", "treeCommandFor",
                        "webviewHtml", "webviewRenderCount", "statusBarText",
                        "pinnedFolderName"]) {
      assert.strictEqual(typeof api[name], "function", `missing ${name}()`);
    }
  });

  it("opened both fixture roots, and nothing else", () => {
    // Multi-root on purpose: it is the arrangement in which "pinned to a
    // folder" and "follows the active editor" give different answers, and
    // the one `tree.ts` opens by talking about.
    const folders = vscode.workspace.workspaceFolders ?? [];
    assert.strictEqual(folders.length, 2,
                       `expected two roots, got ${folders.length}`);
    assert.ok(samePath(folders[0].uri.fsPath, workspaceRoot()),
              `first root is ${folders[0].uri.fsPath}`);
    assert.ok(samePath(folders[1].uri.fsPath, secondRoot()),
              `second root is ${folders[1].uri.fsPath}`);
  });

  it("runs with other extensions disabled", () => {
    // --disable-extensions must not have disabled the one under test. If it
    // had, every other test would fail with a confusing message about a
    // missing command rather than this one.
    const active = vscode.extensions.all.filter(
      (e) => e.isActive && !e.id.startsWith("vscode."));
    const ids = active.map((e) => e.id);
    assert.ok(ids.includes(EXTENSION_ID),
              `extension under test is not active; active: ${ids.join(", ")}`);
  });
});
