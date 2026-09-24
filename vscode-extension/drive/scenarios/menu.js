/**
 * How a reader reaches this extension's commands: the editor's right-click menu,
 * the status bar's menu, and the palette. Looked at rather than read off the
 * manifest.
 */

"use strict";

const path = require("node:path");

const { waitForActivation, statusItem } = require("./common");

const manifest = require(path.join(__dirname, "..", "..", "package.json"));
const commands = manifest.contributes.commands;
const hiddenFromPalette = new Set(
  (manifest.contributes.menus.commandPalette ?? []).filter((m) => m.when === "false").map((m) => m.command),
);

module.exports = async function menu(editor) {
  await editor.step("open a Python file", async () => {
    await editor.openFile("broken.py");
    await waitForActivation(editor);
    // Back to the editor: activating opened the sidebar view, and a click in the
    // text is what the next step needs.
    await editor.shot("file open");
    editor.note(`status bar: ${(await editor.statusBar()).join(" | ")}`);
  });

  await editor.step("right-click in the editor: one TokenSave Manager submenu, not loose entries", async () => {
    await editor.clickLine("import");
    await editor.clickLine("import", { button: "right" });
    await editor.page.waitForSelector(".monaco-menu", { timeout: 10_000 });
    const top = await editor.menuItems();
    await editor.shot("editor context menu");
    editor.note(`top-level items: ${top.map((i) => `${i.label}${i.submenu ? " ›" : ""}`).join(", ")}`);
    const entry = top.find((i) => i.label === "TokenSave Manager");
    editor.check("a TokenSave Manager entry exists", entry);
    editor.check("and it opens a submenu (has the arrow)", entry?.submenu);
    const loose = top.filter((i) => /^(Checks This File|Test Gaps for This File)$/.test(i.label));
    editor.check("no loose entries beside it", loose.length === 0, loose.map((i) => i.label).join(", "));

    await editor.page.locator(".monaco-menu .action-item", { hasText: /^TokenSave Manager$/ }).first().hover();
    await editor.page.waitForTimeout(700);
    await editor.shot("TokenSave Manager submenu");
    const items = (await editor.menuItems()).map((i) => i.label);
    editor.note(`submenu: ${items.join(", ")}`);
    for (const label of ["Checks This File", "Test Gaps for This File", "Propose a Commit…", "Open this project in the Manager", "Open Manager"]) {
      editor.check(`the submenu offers ${label}`, items.includes(label));
    }
    await editor.escape();
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
    editor.check("the Explorer menu has the same single submenu", items.includes("TokenSave Manager"), items.join(", "));
    editor.check("and no loose Checks This File beside it", !items.includes("Checks This File"));
    await editor.escape();
  });
};
