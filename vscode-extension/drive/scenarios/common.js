/**
 * What every TokenSave Manager scenario needs first.
 *
 * This extension declares no `activationEvents`, so VS Code activates it only on
 * first use of one of its views or commands. A freshly opened window therefore has
 * no TokenSave status bar item, no save watchers and no CodeLens until something
 * touches it. Scenarios record that, then activate it the way a reader would: by
 * opening its view.
 */

"use strict";

/** Note the state before activation, then open the view container to activate. */
async function activate(editor) {
  const before = await editor.statusBar();
  const present = before.some((item) => /TokenSave/i.test(item));
  editor.note(`before anything touches it, the status bar ${present ? "already has" : "has no"} TokenSave item`);

  // An activity bar entry is an icon: it is named by aria-label, not by text.
  const tab = editor.page.locator('.activitybar [aria-label*="TokenSave" i]').first();
  await tab.waitFor({ timeout: 15_000 });
  await tab.click();
  await editor.waitForText("TokenSave", 60_000);
  return { presentBeforeActivation: present };
}

/**
 * The extension's status bar item, however it is currently worded.
 *
 * Its text changes: "TokenSave: ?" for the first moment, then a git summary such
 * as "master ●4". Only its accessibility label keeps the name, so that is what is
 * matched -- matching the text passed for a second and then never again.
 */
const statusItem = (editor) =>
  editor.page
    .locator('.statusbar-item:has([aria-label*="TokenSave" i]), .statusbar-item[aria-label*="TokenSave" i]')
    .first();

module.exports = { activate, statusItem };
