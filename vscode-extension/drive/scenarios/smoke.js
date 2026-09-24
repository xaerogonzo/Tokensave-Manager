/**
 * Does the driver work here at all, and does the extension come up on its own:
 * launch, see the workbench, see TokenSave Manager report itself in the status bar
 * without anyone opening a view or running a command.
 */

"use strict";

const { statusItem, waitForActivation } = require("./common");

module.exports = async function smoke(editor) {
  await editor.step("the workbench is up", async () => {
    await editor.page.waitForSelector(".statusbar .statusbar-item", { timeout: 30_000 });
    await editor.shot("workbench");
    editor.check("a status bar exists", (await editor.statusBar()).length > 0);
  });

  await editor.step("the extension activates on its own and reports itself in the status bar", async () => {
    // Nothing has been opened or run. It used to declare no activation events, so
    // this item did not exist until a view or command was first used.
    await waitForActivation(editor);
    await editor.shot("status bar, nothing opened");
    editor.note(`status bar: ${(await editor.statusBar()).join(" | ")}`);
    editor.check("a TokenSave item is present with nothing opened (found by its accessibility label)", await statusItem(editor).count());
  });
};
