/**
 * diagnostics.test.js — the real Problems panel, not a fake collection.
 *
 * `test/diagnostics.test.js` already drives `DiagnosticStore` against a
 * stand-in that records what it was handed. That covers the mapping; it cannot
 * cover whether the mapping reaches the editor, whether the URIs resolve to
 * files VS Code agrees exist, or whether the coordinates land where a user
 * would click. Those need `vscode.languages.getDiagnostics`.
 *
 * The fixture's `src/broken.py` is defective on purpose and the launcher
 * refuses to start if it has stopped being so — otherwise every assertion here
 * would be vacuous and green.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { workspaceRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

const uriFor = (rel) =>
  vscode.Uri.file(path.join(workspaceRoot(), ...rel.split("/")));

describe("problems panel", () => {
  let broken;
  let clean;

  before(async () => {
    await vscode.extensions.getExtension(EXTENSION_ID).activate();
    broken = uriFor("src/broken.py");
    clean = uriFor("src/clean.py");

    const folder = vscode.workspace.workspaceFolders[0];
    await vscode.commands.executeCommand(
      "tokensaveManager.runAction", folder, "checks");
    await until("checks to publish diagnostics", () =>
      vscode.languages.getDiagnostics(broken).length > 0 || null);
  });

  it("reports the defective file to the editor", () => {
    const found = vscode.languages.getDiagnostics(broken);
    assert.ok(found.length >= 2,
              `expected >=2 diagnostics, got ${found.length}`);
  });

  it("reports nothing for the clean file", () => {
    // Without this, "reports findings" is indistinguishable from "reports
    // findings about everything".
    assert.deepStrictEqual(vscode.languages.getDiagnostics(clean), []);
  });

  it("converts the Manager's 1-based lines to the editor's 0-based ones", () => {
    const found = vscode.languages.getDiagnostics(broken);
    // The CLI reports the unused `os` import on line 8. Off by one here puts
    // every squiggle on the wrong line, which is the sort of thing that looks
    // fine in a screenshot and is wrong for every user.
    const unusedOs = found.find((d) => /\bos\b/.test(d.message));
    assert.ok(unusedOs, `no finding mentioned 'os': ` +
              found.map((d) => d.message).join(" | "));
    assert.strictEqual(unusedOs.range.start.line, 7,
                       "line 8 (1-based) must render at index 7 (0-based)");
  });

  it("attaches findings to real Range objects the editor accepts", () => {
    const [first] = vscode.languages.getDiagnostics(broken);
    assert.ok(first.range instanceof vscode.Range,
              "diagnostics must carry a real Range, not a shape like one");
    assert.ok(first.range.start.line >= 0);
    assert.ok(Object.values(vscode.DiagnosticSeverity)
                .includes(first.severity));
  });

  it("names the tool that produced the finding, not the extension", () => {
    // `diagnostics.ts` sets source from `producerOf(finding.rule)`, so the
    // Problems panel shows "(pyflakes)" rather than "(tokensave)". That is
    // the more useful of the two — a reader wants to know which tool to argue
    // with — and this asserts the deliberate choice rather than the obvious
    // one. An earlier version of this test expected "tokensave" and failed
    // against correct code.
    const found = vscode.languages.getDiagnostics(broken);
    const sources = new Set(found.map((d) => d.source));
    assert.ok(!sources.has(undefined) && !sources.has(""),
              `some findings carry no source: ${[...sources].join(", ")}`);
    assert.ok(sources.has("pyflakes"),
              `expected pyflakes among the sources, got ${[...sources].join(", ")}`);
  });

  it("a per-file run does not clear findings it never looked at", async () => {
    const before = vscode.languages.getDiagnostics(broken).length;
    assert.ok(before > 0, "nothing to preserve — the setup did not take");

    // Scope a run to the clean file. The findings in broken.py were not part
    // of that run, so clearing them would report a file as fixed on the
    // strength of never having examined it.
    await vscode.commands.executeCommand("vscode.open", clean);
    await vscode.commands.executeCommand("tokensaveManager.checksFile");

    await new Promise((r) => setTimeout(r, 1500));
    assert.strictEqual(vscode.languages.getDiagnostics(broken).length, before,
                       "a run scoped to clean.py cleared broken.py's findings");
  });
});
