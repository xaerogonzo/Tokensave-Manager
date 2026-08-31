/**
 * runTestsVsix.js — smoke the artifact users actually install.
 *
 * The live suite loads `out/` through `--extensionDevelopmentPath`. Users
 * install a `.vsix`. Those are built from the same TypeScript, but they are
 * not the same thing: `.vscodeignore` decides what ships, `vscode:prepublish`
 * decides what is compiled first, and a mistake in either produces an
 * extension that works in every test and is broken on install. This runs a
 * small subset against the packaged artifact so that gap is covered.
 *
 * ## The flag that inverts here
 *
 * `runTests.js` passes `--disable-extensions`, which is right there: it
 * suppresses *installed* extensions while the one under test arrives by
 * development path and still activates.
 *
 * Once the extension is genuinely installed, that same flag switches it off —
 * and there is no `--enable-extension` to pair with it. VS Code has
 * `--disable-extension <id>` for suppressing one, but no positive form, so
 * the flag simply cannot be used on this path.
 *
 * The isolation comes from `--extensions-dir` instead. This run builds a
 * fresh profile directory and installs exactly one extension into it, so
 * there is nothing else to suppress: no other extension contributes hovers or
 * diagnostics into the buffers under test because no other extension is
 * present.
 *
 * Getting this wrong is silent in the worst way — the host boots, the suite
 * runs, and every assertion fails on a missing command, which reads as the
 * extension being broken rather than absent.
 */
"use strict";

const cp = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const {
  downloadAndUnzipVSCode,
  resolveCliArgsFromVSCodeExecutablePath,
} = require("@vscode/test-electron");

const shared = require("./shared");

const EXTENSION_ROOT = path.resolve(__dirname, "..", "..");
const TEST_HOME = path.join(EXTENSION_ROOT, ".vscode-test-vsix");
const VSIX = path.join(TEST_HOME, "tokensave-manager.vsix");
const EXTENSION_ID = "tokensave.tokensave-manager";

function run(exe, args, cwd) {
  const r = cp.spawnSync(exe, args, { cwd, encoding: "utf8" });
  if (r.status !== 0) {
    throw new Error(
      `${path.basename(exe)} ${args.join(" ")} failed (${r.status}):\n` +
      `${r.stdout || ""}\n${r.stderr || ""}`);
  }
  return r.stdout || "";
}

/**
 * Run VS Code's CLI, which on Windows is a `.cmd`.
 *
 * Node refuses to spawn a `.cmd` without a shell (EINVAL, post
 * CVE-2024-27980), so this goes through `cmd.exe /c` with every argument
 * quoted. Routing an args array through a shell is normally the thing to
 * avoid — it concatenates without escaping, and this checkout's own path has
 * spaces in it — so the quoting is done here, once, over arguments this file
 * builds entirely. Nothing user-supplied reaches it.
 */
function runVSCodeCli(cli, args) {
  if (process.platform !== "win32") {
    return run(cli, args);
  }
  const quoted = [cli, ...args]
    .map((a) => (/[\s&|<>^]/.test(a) ? '"' + a + '"' : a))
    .join(" ");
  const r = cp.spawnSync(process.env.ComSpec || "cmd.exe",
                         ["/d", "/s", "/c", '"' + quoted + '"'],
                         { encoding: "utf8", windowsVerbatimArguments: true });
  if (r.status !== 0) {
    throw new Error("VS Code CLI failed (" + r.status + "): " + quoted +
                    "\n" + (r.stdout || "") + "\n" + (r.stderr || ""));
  }
  return r.stdout || "";
}


/** npm's own `npx` entry point, beside the running node binary. */
function npxCli() {
  const candidate = path.join(path.dirname(process.execPath),
                              "node_modules", "npm", "bin", "npx-cli.js");
  if (!fs.existsSync(candidate)) {
    throw new Error(
      `could not find npm's npx shim at ${candidate}. It is invoked directly ` +
      "rather than as `npx` because Node refuses to spawn a .cmd without a " +
      "shell; if npm's layout has moved, this is the line to update.");
  }
  return candidate;
}

/** The version vsce stamped on the package, which names the installed dir. */
function version() {
  return JSON.parse(fs.readFileSync(
    path.join(EXTENSION_ROOT, "package.json"), "utf8")).version;
}

function packageVsix() {
  fs.mkdirSync(TEST_HOME, { recursive: true });
  // `npx --yes` rather than a devDependency: vsce is needed by this one path
  // and by the release workflow, and adding it to the lockfile would put a
  // large publishing toolchain into every `npm ci` for the fast job's sake.
  //
  // Invoked through npm's own JS shim rather than `npx` / `npx.cmd`. Node 24
  // refuses to spawn a `.cmd` without a shell (EINVAL, post CVE-2024-27980),
  // and passing an args array through a shell concatenates without escaping —
  // which breaks on any path with a space, and this checkout's has three.
  run(process.execPath, [npxCli(), "--yes", "@vscode/vsce", "package",
                         "--out", VSIX],
      EXTENSION_ROOT);
  const size = fs.statSync(VSIX).size;
  // A bundled CLI binary would push this into the megabytes — .vscodeignore
  // excludes `bin/**` for exactly that reason, and a silent regression there
  // is worth catching here rather than at publish time.
  if (size > 2 * 1024 * 1024) {
    throw new Error(
      `the packaged extension is ${(size / 1024 / 1024).toFixed(1)} MB. ` +
      "Something large was included — check .vscodeignore's bin/** rule.");
  }
  console.log(`packaged: ${(size / 1024).toFixed(1)} KB`);
}

async function main() {
  const workspace = shared.prepareWorkspace(TEST_HOME).file;
  packageVsix();

  const vscodeExe = shared.installedVSCode() || await downloadAndUnzipVSCode();
  const [cli, ...resolved] = resolveCliArgsFromVSCodeExecutablePath(vscodeExe);
  const userDataDir = path.join(TEST_HOME, "user-data");
  const extensionsDir = path.join(TEST_HOME, "extensions");

  // The resolver supplies its own --user-data-dir / --extensions-dir, and
  // they point at the LIVE suite's test home. Passing both pairs leaves VS
   // Code to arbitrate between duplicate flags; if it takes the first, the
  // install lands in the other profile and the run tests nothing. So the
  // resolver's are dropped and this run's isolated pair is the only one.
  const cliArgs = resolved.filter(
    (a) => !/^--(user-data-dir|extensions-dir)=/.test(a));

  runVSCodeCli(cli, [...cliArgs,
                     "--user-data-dir", userDataDir,
                     "--extensions-dir", extensionsDir,
                     "--install-extension", VSIX,
                     "--force"]);
  console.log("installed the packaged extension into an isolated profile");

  // VS Code honours `--extensionTestsPath` only in extension-development
  // mode, and that mode is entered by `--extensionDevelopmentPath`. So this
  // points at the directory the .vsix was UNPACKED INTO — the packaged bytes,
  // read from where the installer put them, rather than the `out/` tree the
  // live suite uses.
  //
  // The previous version of this file passed no dev path at all and appeared
  // to work. It was working by accident: `runTests` pushes
  // `--extensionDevelopmentPath=${options.extensionDevelopmentPath}`
  // unconditionally, so an absent value arrived as the literal string
  // "undefined" — a nonexistent path, which VS Code tolerates while still
  // switching into the mode that runs tests. Rebuilding the argument list
  // correctly removed the bogus flag and the suite stopped running entirely,
  // which is how this was found.
  const installed = path.join(extensionsDir, `${EXTENSION_ID}-${version()}`);
  if (!fs.existsSync(installed)) {
    throw new Error(
      `the installer did not leave an unpacked extension at ${installed}. ` +
      "Its layout is what this suite reads; if the naming changed, this is " +
      "the line to update.");
  }

  const args = shared.buildLaunchArgs({
    extensionDevelopmentPath: installed,
    extensionTestsPath: path.resolve(__dirname, "suite", "vsix-index.js"),
    launchArgs: [
      workspace,
      "--user-data-dir", userDataDir,
      "--extensions-dir", extensionsDir,
      // Deliberately NO --disable-extensions here. See the module header:
      // that flag switches off *installed* extensions, and the subject is now
      // installed. There is no --enable-extension to pair with it — VS Code
      // has --disable-extension <id> (singular) but no positive form — so the
      // flag cannot be used at all on this path. The isolation comes from
      // --extensions-dir: this profile is built fresh and contains exactly
      // one extension, so there is nothing else to suppress.
      "--disable-gpu",
    ],
  });

  const stopMinder = shared.desktopAvailable()
    ? () => {} : shared.keepOutOfTheWay();
  try {
    const code = await shared.launchVSCode({
      exe: vscodeExe,
      args,
      env: {
        TOKENSAVE_TEST_WORKSPACE: shared.workspacePaths(TEST_HOME).first,
        TOKENSAVE_TEST_WORKSPACE_2: shared.workspacePaths(TEST_HOME).second,
        TOKENSAVE_TEST_VSIX: "1",
      },
    });
    if (code !== 0) {
      throw new Error(`the packaged-extension smoke suite failed (exit ${code})`);
    }
  } finally {
    stopMinder();
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
