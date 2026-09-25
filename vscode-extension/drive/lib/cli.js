/**
 * The command line shared by every extension's driver.
 *
 *     node drive/run.js <scenario> [--visible] [--keep]
 *
 * On Windows the whole run re-executes on a private desktop first, so the editor
 * it launches is never drawn on yours and never takes the foreground. That is
 * possible only because screenshots are taken over the DevTools protocol, from
 * inside the renderer, rather than by photographing the screen. `--visible` runs
 * on the desktop you are looking at instead, for watching it happen.
 */

"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { Editor } = require("./editor");

function desktopWanted(argv) {
  return (
    process.platform === "win32" &&
    process.env.CI !== "true" &&
    !argv.includes("--visible") &&
    process.env.DRIVE_ON_DESKTOP !== "1"
  );
}

function runOnPrivateDesktop({ launcher, script, argv }) {
  const spec = path.join(os.tmpdir(), `vscode-drive-desktop-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(
    spec,
    JSON.stringify({ exe: process.execPath, args: [script, ...argv], timeoutSeconds: 1200 }),
    "utf8",
  );
  const result = spawnSync(
    "powershell.exe",
    ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", launcher, "-SpecPath", spec],
    {
      stdio: "inherit",
      windowsHide: true,
      // The desktop launcher's own marker keeps the extension's test runner from
      // re-launching itself, and this one stops a second re-exec here.
      env: { ...process.env, DRIVE_ON_DESKTOP: "1", PYSCOPE_ON_DESKTOP: "1", TSM_ON_DESKTOP: "1" },
    },
  );
  fs.rmSync(spec, { force: true });
  return result.status ?? 1;
}

/**
 * @param {object} config
 * @param {string} config.script       absolute path of the run.js being executed
 * @param {string} config.launcher     absolute path of run-on-desktop.ps1
 * @param {string} config.extensionPath
 * @param {string} config.workspace
 * @param {string} config.outDir
 * @param {object} config.scenarios    name -> async (editor, context) => void
 * @param {(name:string)=>object} [config.launchOptions]  per-scenario env/settings
 */
async function main(config) {
  const argv = process.argv.slice(2);
  const names = argv.filter((a) => !a.startsWith("--"));
  if (names.length === 0 || names.some((n) => !config.scenarios[n])) {
    console.error(`usage: node ${path.basename(config.script)} <scenario...> [--visible]`);
    console.error(`scenarios: ${Object.keys(config.scenarios).join(", ")}`);
    process.exit(2);
  }

  if (desktopWanted(argv)) {
    process.exit(runOnPrivateDesktop({ launcher: config.launcher, script: config.script, argv }));
  }

  let failures = 0;
  for (const name of names) {
    const extra = config.launchOptions?.(name) ?? {};
    const editor = await Editor.launch({
      extensionPath: config.extensionPath,
      workspace: config.workspace,
      outDir: config.outDir,
      name,
      ...extra,
      env: { ...config.env, ...extra.env },
      settings: { ...config.settings, ...extra.settings },
    });
    try {
      await config.scenarios[name](editor, config);
    } catch (error) {
      failures += 1;
      console.error(`\nscenario ${name} failed: ${error?.message ?? error}`);
    } finally {
      if (editor.report.failed) {
        failures += 1;
      }
      await editor.close();
    }
  }
  process.exit(failures ? 1 : 0);
}

module.exports = { main };
