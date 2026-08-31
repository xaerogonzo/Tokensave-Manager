/**
 * commands.test.js — every command the manifest promises is actually there.
 *
 * A command listed in `package.json` that nothing registers fails only when
 * somebody clicks it: the palette offers the entry, and running it reports
 * "command not found". Nothing in a compile, a lint, or the stub suite catches
 * that, because the manifest and the registration live in different files and
 * neither references the other.
 *
 * A static grep says all 18 resolve today. This is what keeps it true — and
 * it is strictly stronger, because registration can be conditional, can throw
 * during activation, or can be attached to a disposable that was never pushed.
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");
const { describe, it, before } = require("./harness");

const EXTENSION_ID = "tokensave.tokensave-manager";

describe("contributed commands", () => {
  let declared;
  let registered;

  before(async () => {
    const ext = vscode.extensions.getExtension(EXTENSION_ID);
    await ext.activate();
    // Read the manifest from the extension the host actually loaded, not
    // from a path this file composes — they could differ, and the one that
    // matters is the one shipped.
    const manifest = JSON.parse(fs.readFileSync(
      path.join(ext.extensionPath, "package.json"), "utf8"));
    declared = manifest.contributes.commands.map((c) => c.command);
    registered = new Set(await vscode.commands.getCommands(true));
  });

  it("registers every command the manifest declares", () => {
    const missing = declared.filter((id) => !registered.has(id));
    assert.deepStrictEqual(missing, [],
      `declared in package.json but never registered: ${missing.join(", ")}`);
  });

  it("declares a non-trivial number of commands", () => {
    // The population, not just the verdict. `declared` coming back empty --
    // a renamed manifest key, a parse that silently yielded {} -- would make
    // the assertion above vacuously true, and it is the assertion this file
    // exists for.
    assert.ok(declared.length >= 15,
              `only ${declared.length} commands declared; this suite's main ` +
              `assertion is close to vacuous`);
  });

  it("namespaces every command under tokensaveManager", () => {
    const stray = declared.filter((id) => !id.startsWith("tokensaveManager."));
    assert.deepStrictEqual(stray, [],
      `commands outside the extension's namespace: ${stray.join(", ")}`);
  });

  it("does not assert a total count against the editor's own commands", () => {
    // A guard on this file rather than on the extension. VS Code contributes
    // hundreds of commands of its own and adds more each release, so a test
    // written as `registered.size === N` would break on an editor upgrade
    // while saying nothing about this extension.
    assert.ok(registered.size > declared.length * 10,
              "the editor contributes far more commands than this extension; " +
              "if that stops being true, this file's approach needs rethinking");
  });
});
