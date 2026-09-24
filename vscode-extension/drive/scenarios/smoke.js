/**
 * Does the driver work here at all: launch, see the workbench, see TokenSave
 * Manager report itself in the status bar. The cheapest scenario, and the first
 * thing to run when something looks wrong.
 */

"use strict";

const { activate, statusItem } = require("./common");

module.exports = async function smoke(editor) {
  await editor.step("the workbench is up", async () => {
    await editor.page.waitForSelector(".statusbar .statusbar-item", { timeout: 30_000 });
    await editor.shot("workbench");
    editor.check("a status bar exists", (await editor.statusBar()).length > 0);
  });

  await editor.step("opening its view activates it, and it reports itself in the status bar", async () => {
    await editor.shot("before activation");
    const { presentBeforeActivation } = await activate(editor);
    editor.check(
      "the status bar item did not exist before activation (documents: no activationEvents)",
      !presentBeforeActivation,
    );
    await editor.shot("status bar");
    const items = await editor.statusBar();
    editor.note(`status bar: ${items.join(" | ")}`);
    editor.check("a TokenSave item is present (found by its accessibility label)", await statusItem(editor).count());
  });
};
