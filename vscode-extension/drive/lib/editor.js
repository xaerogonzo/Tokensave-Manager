/**
 * Drive a real VS Code: launch it, look at it, click it, keep a record.
 *
 * The extension-host suites (`test/integration`) run *inside* the editor and can
 * see every API result, but not a single pixel: no menu, no picker row, no
 * webview. This is the other half. It launches the installed VS Code with a
 * remote-debugging port, attaches over the Chrome DevTools Protocol, and works the
 * workbench the way a person does -- keys, right-clicks, reading what is on
 * screen -- while writing a screenshot and a note for every step.
 *
 * Nothing here is specific to one extension. Two extensions carry an identical
 * copy of `drive/lib` (PyScope is the canonical one); a scenario file is what
 * knows about a particular extension.
 *
 * Isolation, because a driver that touches your real editor is worse than none:
 * a throwaway user-data dir and extensions dir per launch, `--disable-extensions`
 * so nobody else's hover ends up in what is read back, and the extension under
 * test loaded from its folder with `--extensionDevelopmentPath`. The launch's own
 * settings are written into that throwaway profile, never into yours.
 */

"use strict";

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const { chromium } = require("playwright-core");

const { Report } = require("./report");

/** Where a normal Windows install puts it. */
const LOCAL_VSCODE = path.join(
  process.env.LOCALAPPDATA ?? "",
  "Programs",
  "Microsoft VS Code",
  "Code.exe",
);

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/** Playwright's names for keys: `ctrl` is `Control`, `cmd` is `Meta`, `esc` is `Escape`. */
const KEY_NAMES = { ctrl: "Control", control: "Control", cmd: "Meta", meta: "Meta", alt: "Alt", shift: "Shift", enter: "Enter", esc: "Escape", escape: "Escape", tab: "Tab", home: "Home", end: "End", up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft", right: "ArrowRight", space: "Space", backspace: "Backspace", delete: "Delete" };
const keyName = (part) => KEY_NAMES[part.toLowerCase()] ?? (part.length === 1 ? part.toUpperCase() : part);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Editor {
  constructor({ page, browser, child, root, report }) {
    this.page = page;
    this.browser = browser;
    this.child = child;
    this.root = root;
    this.report = report;
  }

  // -- launching --------------------------------------------------------

  /**
   * @param {object} options
   * @param {string} options.extensionPath  the extension under test (its package.json folder)
   * @param {string} options.workspace      the folder to open
   * @param {string} options.outDir         where screenshots and the report go
   * @param {string} options.name           scenario name, for the report
   * @param {object} [options.env]          added to the editor's environment (inherited by the extension host)
   * @param {object} [options.settings]     written into the throwaway user settings
   * @param {string[]} [options.args]       extra editor arguments
   */
  static async launch(options) {
    const exe = process.env.VSCODE_EXE || LOCAL_VSCODE;
    if (!fs.existsSync(exe)) {
      throw new Error(
        `VS Code not found at ${exe}. Set VSCODE_EXE to a Code executable; a download is deliberately not attempted.`,
      );
    }

    const root = fs.mkdtempSync(path.join(os.tmpdir(), "vscode-drive-"));
    const userData = path.join(root, "user-data");
    const extensions = path.join(root, "extensions");
    fs.mkdirSync(path.join(userData, "User"), { recursive: true });
    fs.mkdirSync(extensions, { recursive: true });

    fs.writeFileSync(
      path.join(userData, "User", "settings.json"),
      JSON.stringify(
        {
          // A predictable window: nothing that appears on first launch, and no
          // tip, prompt or update check that could sit in front of the thing
          // being looked at.
          "window.titleBarStyle": "custom",
          "window.restoreWindows": "none",
          "window.zoomLevel": 0,
          "workbench.startupEditor": "none",
          "workbench.tips.enabled": false,
          "workbench.enableExperiments": false,
          "workbench.welcomePage.walkthroughs.openOnInstall": false,
          "security.workspace.trust.enabled": false,
          "telemetry.telemetryLevel": "off",
          "update.mode": "none",
          "extensions.autoUpdate": false,
          "editor.minimap.enabled": false,
          "editor.quickSuggestions": { other: "off", comments: "off", strings: "off" },
          "files.autoSave": "off",
          // Nothing that is not the extension under test: no chat panel taking a
          // third of the window, no AI features answering anything.
          "chat.disableAIFeatures": true,
          "git.enabled": false,
          // A native dialog is an OS window, invisible to the DOM and to screenshots.
          "window.dialogStyle": "custom",
          "git.openRepositoryInParentFolders": "never",
          "workbench.secondarySideBar.defaultVisibility": "hidden",
          "workbench.layoutControl.enabled": false,
          ...options.settings,
        },
        null,
        2,
      ),
    );

    const port = await freePort();
    const args = [
      options.workspace,
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${userData}`,
      `--extensions-dir=${extensions}`,
      `--extensionDevelopmentPath=${options.extensionPath}`,
      "--disable-extensions",
      "--disable-workspace-trust",
      "--disable-updates",
      "--skip-release-notes",
      "--skip-welcome",
      "--new-window",
      ...(options.args ?? []),
    ];

    const child = spawn(exe, args, {
      env: { ...process.env, ...options.env },
      stdio: "ignore",
      windowsHide: false,
    });
    child.on("error", (error) => console.error("editor failed to start:", error.message));

    const report = new Report(options.outDir, options.name);
    const browser = await attach(port, child);
    const page = await workbenchPage(browser);
    await page.waitForSelector(".monaco-workbench", { timeout: 60_000 });
    // The window exists before the workbench has finished laying itself out.
    await page.waitForSelector(".statusbar", { timeout: 60_000 });

    const editor = new Editor({ page, browser, child, root, report });
    report.note(`editor ${path.basename(exe)}, debugging port ${port}`);
    // "All installed extensions are temporarily disabled" is the editor's own
    // notice about --disable-extensions; it would sit in front of every shot.
    await editor.clearNotifications();
    return editor;
  }

  // -- recording --------------------------------------------------------

  /** Run one named step; a failure is recorded with a screenshot, then rethrown. */
  async step(title, action) {
    this.report.begin(title);
    try {
      const result = await action();
      this.report.pass();
      return result;
    } catch (error) {
      this.report.fail(error);
      await this.shot(`FAILED - ${title}`).catch(() => {});
      throw error;
    }
  }

  /** A screenshot, filed under the current step. Returns its path. */
  async shot(label, options = {}) {
    const file = this.report.nextShot(label);
    await this.page.screenshot({ path: file, ...options });
    this.report.attach(file, label);
    return file;
  }

  note(text) {
    this.report.note(text);
  }

  /** Something that must be true. Recorded either way, so the report reads as a checklist. */
  check(label, condition, detail = "") {
    this.report.check(label, Boolean(condition), detail);
    return Boolean(condition);
  }

  // -- input ------------------------------------------------------------

  /** `ctrl+k ctrl+e` -- chords are sent as separate presses, as a person types them. */
  async keys(combo) {
    for (const chord of combo.split(" ")) {
      await this.page.keyboard.press(chord.split("+").map(keyName).join("+"));
      await sleep(120);
    }
  }

  /**
   * Press a chord until what it should cause has happened.
   *
   * Right after launch, or after a click, the workbench occasionally drops the
   * first keystroke of a chord (seen as a panel that never opens, about one run in
   * six). A person notices and presses again; so does this. `probe` should wait a
   * few seconds for the effect and throw if it does not come.
   */
  async pressUntil(combo, probe, { tries = 3 } = {}) {
    let last;
    for (let attempt = 1; attempt <= tries; attempt += 1) {
      await this.keys(combo);
      try {
        return await probe();
      } catch (error) {
        last = error;
        if (attempt < tries) {
          this.note(`${combo} had no effect (attempt ${attempt}); pressing again`);
          await this.escape();
        }
      }
    }
    throw last;
  }

  /** Run a command by its palette title, the way most people find one. */
  async palette(title) {
    await this.keys("ctrl+shift+p");
    await this.page.waitForSelector(".quick-input-widget:not([style*='display: none'])");
    await this.page.keyboard.type(title, { delay: 15 });
    await sleep(400);
    await this.keys("enter");
  }

  /** Open a workspace-relative file with Quick Open. */
  async openFile(relative) {
    await this.keys("ctrl+p");
    await this.page.waitForSelector(".quick-input-widget:not([style*='display: none'])");
    await this.page.keyboard.type(relative, { delay: 10 });
    await sleep(500);
    await this.keys("enter");
    await this.page.waitForSelector(".monaco-editor .view-lines", { timeout: 15_000 });
    await sleep(600);
  }

  /** Put the cursor on the first line containing `text`, and return its bounding box. */
  async clickLine(text, options = {}) {
    const line = this.page.locator(".monaco-editor .view-line", { hasText: text }).first();
    await line.waitFor({ timeout: 10_000 });
    await line.click(options);
    return line.boundingBox();
  }

  /** Dismiss every toast, so a screenshot shows the thing being tested. */
  async clearNotifications() {
    await sleep(3500); // toasts arrive after the workbench does
    // Clicked directly rather than through the palette: opening the palette to run
    // "Clear All Notifications" left the workbench in a state where the first
    // keyboard chord afterwards was swallowed (found by a run that never opened
    // the panel). The toolbar is hidden until hover, and a scripted click does not
    // need it visible.
    await this.page.evaluate(() =>
      document
        .querySelectorAll(".notification-toast .codicon-notifications-clear, .notification-toast .codicon-close")
        .forEach((button) => button.click()),
    );
    await sleep(400);
  }

  async escape() {
    await this.page.keyboard.press("Escape");
    await sleep(150);
  }

  // -- reading ----------------------------------------------------------

  /** What the quick pick / palette is offering, top to bottom. */
  async quickPickRows() {
    await this.page.waitForSelector(".quick-input-list .monaco-list-row", { timeout: 10_000 });
    return this.page.$$eval(".quick-input-list .monaco-list-row", (rows) =>
      rows.map((row) => ({
        label: row.querySelector(".label-name")?.textContent?.trim() ?? "",
        description: row.querySelector(".label-description")?.textContent?.trim() ?? "",
        separator: row.classList.contains("quick-input-list-separator-border") ||
          !!row.querySelector(".quick-input-list-separator"),
        text: row.textContent?.trim() ?? "",
      })),
    );
  }

  /** The visible menu (context menu or submenu), as labels and whether each opens a submenu. */
  async menuItems() {
    await this.page.waitForSelector(".monaco-menu", { timeout: 10_000 });
    return this.page.$$eval(".monaco-menu .action-item", (items) =>
      items
        .filter((item) => item.offsetParent !== null)
        .map((item) => ({
          label: item.querySelector(".action-label")?.textContent?.trim() ?? "",
          submenu: item.classList.contains("monaco-submenu-item") || !!item.querySelector(".submenu-indicator"),
          keybinding: item.querySelector(".keybinding")?.textContent?.trim() ?? "",
          disabled: item.classList.contains("disabled"),
        }))
        .filter((item) => item.label),
    );
  }

  /** The status bar, left to right. */
  async statusBar() {
    return this.page.$$eval(".statusbar .statusbar-item", (items) =>
      items.map((item) => item.textContent?.trim() ?? "").filter(Boolean),
    );
  }

  /** Wait for text to appear anywhere in the workbench (not inside a webview). */
  async waitForText(text, timeout = 30_000) {
    await this.page.waitForFunction((needle) => document.body.innerText.includes(needle), text, {
      timeout,
    });
  }

  /**
   * The webview frame whose body text satisfies `predicate`.
   *
   * A VS Code webview is an iframe inside an iframe, and Playwright exposes each
   * as a frame; the one holding an extension's page is the one that has its text.
   */
  async webview(predicate, timeout = 30_000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      for (const frame of this.page.frames()) {
        if (frame === this.page.mainFrame()) {
          continue;
        }
        try {
          const text = await frame.evaluate(() => document.body?.innerText ?? "");
          if (text && predicate(text)) {
            return frame;
          }
        } catch {
          // A frame that navigated or detached while being read: try the next.
        }
      }
      await sleep(300);
    }
    throw new Error("no webview matched within the timeout");
  }

  // -- ending -----------------------------------------------------------

  async close() {
    this.report.write();
    try {
      await this.browser.close();
    } catch {
      // Already gone.
    }
    killTree(this.child);
    // Electron holds its profile open briefly after the window closes.
    await sleep(800);
    fs.rmSync(this.root, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  }
}

async function attach(port, child) {
  const deadline = Date.now() + 60_000;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`the editor exited early (code ${child.exitCode})`);
    }
    try {
      return await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    } catch (error) {
      lastError = error;
      await sleep(500);
    }
  }
  throw new Error(`could not attach to the editor: ${lastError?.message}`);
}

async function workbenchPage(browser) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        if (/workbench/.test(page.url())) {
          return page;
        }
      }
    }
    await sleep(400);
  }
  throw new Error("the workbench window never appeared");
}

/** The tree, not the process: Electron's helpers outlive the launcher. */
function killTree(child) {
  if (!child || child.exitCode !== null || child.pid === undefined) {
    return;
  }
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
  } else {
    child.kill("SIGTERM");
  }
}

module.exports = { Editor, sleep, LOCAL_VSCODE };
