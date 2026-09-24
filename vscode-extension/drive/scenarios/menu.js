/**
 * How a reader reaches this extension's commands: the editor's right-click menu,
 * the status bar's menu, and the palette. Looked at rather than read off the
 * manifest.
 */

"use strict";

const path = require("node:path");

const { activate, statusItem } = require("./common");

const manifest = require(path.join(__dirname, "..", "..", "package.json"));
const commands = manifest.contributes.commands;
const hiddenFromPalette = new Set(
  (manifest.contributes.menus.commandPalette ?? []).filter((m) => m.when === "false").map((m) => m.command),
);

module.exports = async function menu(editor) {
  await editor.step("open a Python file", async () => {
    await editor.openFile("broken.py");
    await activate(editor);
    // Back to the editor: activating opened the sidebar view, and a click in the
    // text is what the next step needs.
    await editor.shot("file open");
    editor.note(`status bar: ${(await editor.statusBar()).join(" | ")}`);
  });

  await editor.step("right-click in the editor: what does this extension add?", async () => {
    await editor.clickLine("import");
    await editor.clickLine("import", { button: "right" });
    await editor.page.waitForSelector(".monaco-menu", { timeout: 10_000 });
    const items = await editor.menuItems();
    await editor.shot("editor context menu");
    editor.note(`top-level items: ${items.map((i) => `${i.label}${i.submenu ? " ›" : ""}`).join(", ")}`);
    editor.check("Checks This File is offered", items.some((i) => i.label === "Checks This File"));
    editor.check("Test Gaps for This File is offered", items.some((i) => i.label === "Test Gaps for This File"));

    const ours = items.filter((i) => /Checks This File|Test Gaps for This File|TokenSave/.test(i.label));
    editor.note(`this extension adds ${ours.length} top-level item(s) to the context menu, ${ours.filter((i) => i.submenu).length} of them a submenu`);
    await editor.escape();
  });

  await editor.step("the palette: every command is reachable, and how many keys are bound", async () => {
    await editor.keys("ctrl+shift+p");
    await editor.page.waitForSelector(".quick-input-widget:not([style*='display: none'])");
    await editor.page.keyboard.type("TokenSave Manager", { delay: 15 });
    await editor.page.waitForTimeout(700);
    const rows = await editor.quickPickRows();
    await editor.shot("palette filtered to TokenSave Manager");
    const visible = commands.filter((c) => !hiddenFromPalette.has(c.command));
    editor.note(`${visible.length} palette commands declared; first page shows ${rows.length} rows`);
    editor.check("the palette lists this extension's commands", rows.length >= 5, `${rows.length} rows`);
    editor.note(`keybindings declared: ${(manifest.contributes.keybindings ?? []).length}`);
    await editor.escape();
  });

  await editor.step("the status bar item opens a menu", async () => {
    await statusItem(editor).click();
    const rows = await editor.quickPickRows();
    await editor.shot("status bar menu");
    editor.note(`status bar menu: ${rows.map((r) => r.label || r.text).join(" / ")}`);
    editor.check("a menu opened from the status bar", rows.length > 0);
    await editor.escape();
  });

  await editor.step("the Explorer's right-click menu on a Python file", async () => {
    // The sidebar is showing this extension's own view; the file tree is the Explorer's.
    await editor.keys("ctrl+shift+e");
    await editor.page.waitForSelector(".explorer-folders-view .monaco-list-row", { timeout: 10_000 });
    const row = editor.page.locator(".explorer-folders-view .monaco-list-row", { hasText: "broken.py" }).first();
    if (!(await row.count())) {
      await editor.page.locator(".explorer-folders-view .monaco-list-row", { hasText: /^src$/ }).first().click();
    }
    await row.waitFor({ timeout: 10_000 });
    await row.click({ button: "right" });
    await editor.page.waitForSelector(".monaco-menu", { timeout: 10_000 });
    const items = (await editor.menuItems()).map((i) => i.label);
    await editor.shot("explorer context menu");
    editor.check("Checks This File is offered", items.includes("Checks This File"), items.join(", "));
    await editor.escape();
  });
};
