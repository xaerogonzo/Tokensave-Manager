/**
 * shared.js — the launcher pieces both entry points need.
 *
 * `runTests.js` boots the extension from `out/` through
 * `--extensionDevelopmentPath`; `runTestsVsix.js` installs the packaged
 * artifact and boots that. Everything before the launch is identical — build
 * the disposable multi-root fixture, prove it produces the states the tests
 * assert about, find an editor, keep its window out of the way — so it lives
 * here rather than in two copies that drift.
 *
 * The one thing that is NOT shared is the launch arguments, because that is
 * exactly where the two differ: see `runTestsVsix.js` on why
 * `--disable-extensions` means opposite things in the two cases.
 */
"use strict";

const cp = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const EXTENSION_ROOT = path.resolve(__dirname, "..", "..");
const REPO_ROOT = path.resolve(EXTENSION_ROOT, "..");
const TEMPLATE = path.join(EXTENSION_ROOT, "test", "workspace");
const TEMPLATE_2 = path.join(EXTENSION_ROOT, "test", "workspace-second");

/** Where a given test home keeps its copies of the fixture. */
function workspacePaths(testHome) {
  return {
    first: path.join(testHome, "workspace"),
    second: path.join(testHome, "workspace-second"),
    file: path.join(testHome, "fixture.code-workspace"),
  };
}

/** Local VS Code, when there is one. */
function installedVSCode() {
  const candidates = [
    process.env.TOKENSAVE_TEST_VSCODE,
    path.join(os.homedir(), "AppData", "Local", "Programs",
              "Microsoft VS Code", "Code.exe"),
    "C:\\Program Files\\Microsoft VS Code\\Code.exe",
    "/usr/share/code/code",
    "/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
  ];
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c;
  }
  return undefined;   // let test-electron download one (the CI path)
}

/**
 * Remove the previous run's workspace, tolerating a Windows file lock.
 *
 * A test host survives its launcher: kill a run mid-flight (ctrl-c, or a
 * mutation arm that times out) and the VS Code it spawned keeps running with
 * the workspace open, so the next run's `rmSync` fails EPERM. Retrying covers
 * the common case where the process is on its way out; when it is not, the
 * message names the process to kill rather than leaving a bare errno, because
 * the window is identifiable by exactly the marker the focus watcher uses.
 */
function clearWorkspace(testHome) {
  const { first: WORKSPACE, second: WORKSPACE_2 } = workspacePaths(testHome);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      for (const dir of [WORKSPACE, WORKSPACE_2]) {
        fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3 });
      }
      return;
    } catch (err) {
      if (attempt === 4) {
        throw new Error(
          "could not remove " + WORKSPACE + ": " + err.message + "\n\n" +
          "A leaked Extension Development Host is probably holding it open " +
          "— an interrupted run leaves one behind. Close it, or run:\n" +
          "  powershell -Command \"Get-Process Code | Where-Object " +
          "{ $_.MainWindowTitle -like '*Extension Development Host*' } | " +
          "Stop-Process -Force\"");
      }
      cp.spawnSync(process.execPath, ["-e", "setTimeout(()=>{},600)"]);
    }
  }
}

function git(args, cwd) {
  const r = cp.spawnSync("git", args, { cwd, encoding: "utf8" });
  if (r.status !== 0) {
    throw new Error(`git ${args.join(" ")} failed: ${r.stderr || r.stdout}`);
  }
  return (r.stdout || "").trim();
}

/**
 * Build the disposable workspace, and prove it produces the states the tests
 * assert about.
 *
 * The verification is not ceremony. A fixture that cannot produce the
 * interesting case makes every test about that case vacuous *and green*, so
 * this refuses to launch an editor rather than hand the suite a workspace it
 * cannot fail against.
 */
function prepareWorkspace(testHome) {
  const { first: WORKSPACE, second: WORKSPACE_2, file: WORKSPACE_FILE } =
    workspacePaths(testHome);
  clearWorkspace(testHome);
  for (const [template, dest] of [[TEMPLATE, WORKSPACE],
                                  [TEMPLATE_2, WORKSPACE_2]]) {
    fs.mkdirSync(dest, { recursive: true });
    fs.cpSync(template, dest, { recursive: true });
    git(["init", "-q", "."], dest);
    git(["config", "user.email", "fixture@example.invalid"], dest);
    git(["config", "user.name", "fixture"], dest);
    // A committed .mcp.json the status-bar test can touch to fire the file
    // watcher. Writing a *new* file there would change the root's change
    // count, which is the very thing that test reads.
    fs.writeFileSync(path.join(dest, ".mcp.json"), "{}", "utf8");
    git(["add", "-A"], dest);
    git(["commit", "-qm", "fixture"], dest);
  }

  // The two roots are left with DIFFERENT numbers of uncommitted files, and
  // that is load-bearing rather than sloppy.
  //
  // The status bar renders a change count, so two roots differing only in
  // being dirty are still not distinguishable if both render the same marker.
  // Distinct counts make the bar's subject readable from its text: pinned to
  // the first root it reads ●2, and a bar that followed the active editor
  // into the second would read ●1. The mutation runner reported the pin test
  // as decorative twice before this — once for a single-root workspace, and
  // once for roots whose states looked alike.
  //
  // `.mcp.json` is committed in the second root so a test can touch it to
  // fire the file watcher without changing either count.
  for (const name of ["uncommitted-a.txt", "uncommitted-b.txt"]) {
    fs.writeFileSync(path.join(WORKSPACE, name), "dirty on purpose", "utf8");
  }
  fs.writeFileSync(path.join(WORKSPACE_2, "uncommitted.txt"),
                   "dirty on purpose", "utf8");

  // Multi-root, and deliberately so. With a single folder open, "pinned to a
  // folder" and "follows the active editor" are the same answer, so the test
  // for the pin cannot fail — which is what the mutation runner reported
  // before this existed. Two roots make the difference observable, and
  // multi-root is the case `tree.ts` opens by talking about.
  fs.writeFileSync(WORKSPACE_FILE, JSON.stringify({
    folders: [{ path: WORKSPACE }, { path: WORKSPACE_2 }],
    settings: {
      "tokensaveManager.managerPath": REPO_ROOT,
      "tokensaveManager.pythonPath":
        process.env.TOKENSAVE_TEST_PYTHON || "python",
      "tokensaveManager.statusPollSeconds": 3600,
      "tokensaveManager.statusDebounceMs": 100,
    },
  }, null, 2) + "\n", "utf8");

  // No per-folder .vscode/settings.json. The .code-workspace above carries
  // the same settings, and writing a second copy into the first root after
  // its commit left that root with one untracked file — which showed up as a
  // change marker in the status bar and failed the pin test for a reason
  // that had nothing to do with the pin. The pinned root must be clean for
  // that test to mean anything.

  assertFixtureProducesFindings(WORKSPACE);
  return workspacePaths(testHome);
}

/**
 * Explain a zero-finding `checks` run, when the explanation is in the envelope.
 *
 * A check that COULD NOT RUN and a check that found nothing are the same thing
 * at the `findings` level -- both are an empty list -- and `ok:false` does not
 * separate them either, because a check that found real problems is also
 * `ok:false`. The distinguishing evidence sits in `data.<check>.output`, and
 * it is worth surfacing: the first time this guard fired on CI it said the
 * fixture was not defective, when the truth was that the Linux runner had no
 * pyflakes and the fixture was fine.
 *
 * Reports every failing check rather than pattern-matching "No module named":
 * an analyser can fail to start for reasons that phrase does not cover, and a
 * guard that recognises one of them reads as a guard that has ruled out the
 * rest.
 */
function describeSilentChecks(envelope) {
  const data = envelope.data || {};
  const failed = Object.keys(data).filter((k) => data[k] && data[k].ok === false);
  if (failed.length === 0) { return ""; }
  const lines = failed.map(
    (k) => "  " + k + ": " + (data[k].output || "").slice(0, 300));
  return (
    "\nEvery check that reported failure, in case one of them could not " +
    "run at all -- a missing analyser produces exactly this state, " +
    "ok:false with no findings, and is not a fixture problem:\n" +
    lines.join("\n") + "\n\n");
}

/** Run the real CLI and require the defective file to actually be defective. */
function assertFixtureProducesFindings(WORKSPACE) {
  const python = process.env.TOKENSAVE_TEST_PYTHON || "python";
  const r = cp.spawnSync(
    python,
    [path.join(REPO_ROOT, "src", "cli.py"), "checks",
     "--project", WORKSPACE, "--json"],
    { encoding: "utf8" });

  let envelope;
  try {
    envelope = JSON.parse(r.stdout);
  } catch {
    throw new Error(
      "the fixture could not be verified: `cli.py checks` produced no JSON.\n" +
      `stdout: ${(r.stdout || "").slice(0, 400)}\n` +
      `stderr: ${(r.stderr || "").slice(0, 400)}`);
  }
  const findings = envelope.findings || [];
  const broken = findings.filter((f) => (f.file || "").includes("broken"));
  if (broken.length < 2) {
    throw new Error(
      "the fixture does not produce the state the diagnostics tests assert " +
      `about: expected >=2 findings in broken.py, got ${broken.length}. ` +
      "Fix the fixture rather than the assertions — a workspace that cannot " +
      "produce findings makes every diagnostics test vacuously green.\n" +
      describeSilentChecks(envelope) +
      JSON.stringify(findings, null, 2));
  }
  const clean = findings.filter((f) => (f.file || "").includes("clean"));
  if (clean.length !== 0) {
    throw new Error(
      "clean.py reported findings, so the suite cannot tell 'reports real " +
      "findings' from 'reports findings about everything': " +
      JSON.stringify(clean, null, 2));
  }
  console.log(`fixture verified: ${broken.length} findings in broken.py, ` +
              `0 in clean.py`);
}

/**
 * Start the window minder, and return a function that stops it.
 *
 * See keep-out-of-the-way.ps1 for why this exists and why it is safe. Windows
 * only; elsewhere the editor does not steal focus the same way and there is
 * nothing to do.
 */
function keepOutOfTheWay() {
  if (process.platform !== "win32" || process.env.TOKENSAVE_TEST_FOCUS === "1") {
    return () => {};
  }
  // The mutation runner starts one minder for the whole run and sets this.
  // Starting a fresh PowerShell per arm was measurably worse than none for
  // the first second or two of each: `Add-Type` compiles C# at startup, so
  // every arm had a cold window in which the editor came up unopposed.
  if (process.env.TOKENSAVE_TEST_MINDER === "external") {
    return () => {};
  }
  const script = path.join(__dirname, "keep-out-of-the-way.ps1");
  let child;
  try {
    child = cp.spawn("powershell.exe", [
      "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
      "-File", script,
    ], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  } catch (err) {
    // Never fatal. Failing a whole test run because a convenience could not
    // start would be a worse outcome than a window taking the foreground.
    console.warn(`window minder did not start: ${err.message}`);
    return () => {};
  }
  child.stdout.on("data", (b) => process.stdout.write(`  [focus] ${b}`));
  child.on("error", (e) => console.warn(`window minder: ${e.message}`));
  return () => { try { child.kill(); } catch { /* already gone */ } };
}

// ── launching, on a private desktop where possible ───────────────────────────

/**
 * The arguments `runTests` would have built, so the spawn can be routed.
 *
 * Copied deliberately from `@vscode/test-electron`'s `runTest.js` rather than
 * imported, because it does not export them — it builds the list inside the
 * function that also does the spawning, and the spawning is the part that has
 * to change. Re-read that file if a version bump changes the flags; the
 * failure mode of drifting is a test host launched slightly differently from
 * the one CI runs.
 *
 * `--user-data-dir` / `--extensions-dir` are NOT added here: test-electron
 * only supplies its own defaults when the caller passed neither, and both
 * callers pass both.
 */
function buildLaunchArgs({ extensionTestsPath, extensionDevelopmentPath,
                           launchArgs = [] }) {
  const args = [
    "--no-sandbox",
    "--disable-gpu-sandbox",
    "--disable-updates",
    "--skip-welcome",
    "--skip-release-notes",
    "--no-cached-data",
    "--disable-workspace-trust",
    `--extensionTestsPath=${extensionTestsPath}`,
  ];
  if (extensionDevelopmentPath) {
    args.push(`--extensionDevelopmentPath=${extensionDevelopmentPath}`);
  }
  return launchArgs.concat(args);
}

/** Is the private-desktop launcher usable, and wanted? */
function desktopAvailable() {
  if (process.platform !== "win32") return false;
  // The same switch that turns off the window minder: someone who asked to
  // watch a run must not have it moved somewhere they cannot see it.
  if (process.env.TOKENSAVE_TEST_FOCUS === "1") return false;
  // Escape hatch, in case a future Electron stops tolerating it.
  if (process.env.TOKENSAVE_TEST_DESKTOP === "0") return false;
  return true;
}

/**
 * Run VS Code and resolve with its exit code.
 *
 * On Windows this puts the editor on a **private desktop**, where its window
 * cannot take the foreground because it is not on the desktop you are looking
 * at. That is a complete fix rather than a mitigation: the window-minder
 * reacts to a window that has already appeared and leaves a flash as long as
 * its detection latency, and this leaves nothing to react to.
 *
 * Everywhere else — and when the desktop is declined — it is a plain spawn,
 * which is what CI does under xvfb, where nothing has a foreground to steal.
 */
function launchVSCode({ exe, args, env = {}, timeoutSeconds = 900 }) {
  const fullEnv = { ...process.env, ...env };

  if (!desktopAvailable()) {
    return new Promise((resolve, reject) => {
      const child = cp.spawn(exe, args, { env: fullEnv });
      child.stdout.on("data", (d) => process.stdout.write(d));
      child.stderr.on("data", (d) => process.stderr.write(d));
      child.on("error", reject);
      child.on("close", (code) => resolve(code ?? 1));
    });
  }

  // The spec goes through a file rather than the command line: the argument
  // list contains a dozen Windows paths, and handing them to PowerShell as a
  // single string means quoting them twice and getting it right both times.
  const spec = path.join(os.tmpdir(),
                         `tokensave-desktop-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(spec, JSON.stringify({ exe, args, timeoutSeconds }), "utf8");

  return new Promise((resolve, reject) => {
    const child = cp.spawn("powershell.exe", [
      "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
      "-File", path.join(__dirname, "run-on-desktop.ps1"),
      "-SpecPath", spec,
    ], { env: fullEnv, windowsHide: true });
    child.stdout.on("data", (d) => process.stdout.write(d));
    child.stderr.on("data", (d) => process.stderr.write(d));
    child.on("error", reject);
    child.on("close", (code) => {
      try { fs.unlinkSync(spec); } catch { /* best effort */ }
      resolve(code ?? 1);
    });
  });
}

module.exports = {
  EXTENSION_ROOT,
  REPO_ROOT,
  workspacePaths,
  installedVSCode,
  prepareWorkspace,
  keepOutOfTheWay,
  buildLaunchArgs,
  desktopAvailable,
  launchVSCode,
};
