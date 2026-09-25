/**
 * What every TokenSave Manager scenario needs first.
 *
 * The extension activates once VS Code has finished starting (`onStartupFinished`).
 * It used to declare no activation events at all, which meant a fresh window had no
 * status bar item, save watchers or CodeLens until a view or command was first
 * used; `smoke` now holds it to the new behaviour.
 */

"use strict";

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

/** Wait for the extension to activate on its own, without anyone opening anything. */
async function waitForActivation(editor) {
  await statusItem(editor).waitFor({ timeout: 60_000 });
}

/** Open the extension's view container, as a reader would from the activity bar. */
async function openView(editor) {
  // An activity bar entry is an icon: it is named by aria-label, not by text.
  const tab = editor.page.locator('.activitybar [aria-label*="TokenSave" i]').first();
  await tab.waitFor({ timeout: 15_000 });
  await tab.click();
  await editor.page.waitForFunction(() => /TokenSave/i.test(document.querySelector(".sidebar")?.innerText ?? ""), null, { timeout: 30_000 });
}

module.exports = { statusItem, waitForActivation, openView };
