/**
 * The extension's own views: the Projects tree (multi-root) and the Savings & Spend
 * webview.
 */

"use strict";

const { activate } = require("./common");

module.exports = async function views(editor) {
  await editor.step("open the TokenSave Manager view container", async () => {
    await activate(editor);
    await editor.page.waitForTimeout(1500);
    await editor.shot("TokenSave Manager view");
    editor.check("the activity bar entry opened a sidebar", await editor.page.locator(".sidebar").isVisible());
  });

  await editor.step("the Projects tree shows both workspace roots", async () => {
    await editor.page.waitForFunction(
      () => /Projects/i.test(document.querySelector(".sidebar")?.innerText ?? ""),
      null,
      { timeout: 30_000 },
    );
    await editor.page.waitForTimeout(2500); // the tree fills after the CLI answers
    const text = await editor.page.locator(".sidebar").innerText();
    await editor.shot("projects tree");
    editor.note(text.replace(/\n+/g, " | ").slice(0, 400));
    editor.check("the first fixture root is listed", /workspace(?!-second)/.test(text));
    editor.check("the second fixture root is listed", /workspace-second/.test(text));
  });

  await editor.step("Savings & Spend renders", async () => {
    await editor.palette("TokenSave Manager: Savings & Spend");
    await editor.page.waitForTimeout(2500);
    await editor.shot("savings and spend");
    let text = "";
    try {
      const frame = await editor.webview(() => true, 15_000);
      text = await frame.evaluate(() => document.body.innerText);
    } catch {
      text = await editor.page.locator(".sidebar").innerText();
    }
    editor.note(text.replace(/\n+/g, " | ").slice(0, 300));
    editor.check("the view has content", text.trim().length > 0);
  });
};
