# Developing the extension

Four commands, in increasing cost and decreasing frequency.

| Command | What it runs | Needs |
|---|---|---|
| `npm test` | Stub suite — the decisions, headless | node |
| `npm run test:live` | Real VS Code, extension loaded from `out/` | an editor, python |
| `npm run test:vsix` | Real VS Code, extension **installed from a packaged .vsix** | as above |
| `npm run test:mutations` | Breaks each property and requires a failure | as above |

`npm test` is what CI gates on today. The live suite runs non-blocking in the
`extension-live` job and is promoted by deleting one `continue-on-error` line,
after ten consecutive green pull-request runs.

## Which suite should a new test go in?

Put it in the **stub suite** unless it cannot live there. That suite runs in
under a second and needs nothing installed, so it is where a test gets read and
re-run. `test/vscode-stub.js` is behavioural rather than a mock — `Range` keeps
its numbers and the collection really stores what it is handed — so most
mapping and decision logic is testable there.

Use the **live suite** when the property is only true inside an editor:
a command actually being registered, what a tree row renders as, what reaches
the Problems panel, what the webview document contains, what the status bar
says. The rule of thumb is whether a real `vscode` module is load-bearing.

Use the **vsix suite** only for things packaging can break. It deliberately
reuses the live suite's activation, command and tree files rather than keeping
a second copy, because a smoke suite that has drifted from what it mirrors is
worse than none.

## The window that keeps taking your focus

A mutation run launches an editor per live arm — fourteen on the current set —
and each one activates itself.

`test/integration/keep-out-of-the-way.ps1` watches for windows whose title
contains `Extension Development Host` and minimises them. That marker appears
only on windows opened for extension testing, so an editor you are working in
is never touched; the filter is the safety property and is preferred to
matching on process id, since the test host is a `Code.exe` like any other.

Two things make it effective enough to be worth having:

* It enumerates windows through `EnumWindows` rather than `Get-Process`.
  Measured on this machine: **0.20 ms per sweep against 78 ms**. The old
  version's effective latency was its 120 ms poll *plus* a 78 ms sweep; it is
  now about 15 ms, and the flash is exactly as long as the detection latency.
* It re-minimises a window for 25 seconds after first seeing it, because VS
  Code shows its window, loads the extension host, and then activates itself
  again. A one-shot minimise gets undone a second later — which is what it
  looked like from the outside: a watcher reporting success while the window
  sat in the foreground.

The mutation runner starts **one** minder for the whole run. Starting one per
arm was worse than none for the first seconds of each, because PowerShell
compiles the interop at startup.

`TOKENSAVE_TEST_FOCUS=1` disables it when you want to watch a run.

### What does *not* fix the rest

A brief flash remains, and the usual advice — raising
`HKCU:\Control Panel\Desktop\ForegroundLockTimeout` — **does not apply**. That
was checked rather than repeated: this machine already has it at `150000`, and
the windows still take focus.

The setting governs whether a *background* process may steal the foreground.
These launches are not background. VS Code is spawned by the terminal running
the tests, and Windows grants foreground rights to a child of the foreground
process, so the activation is legitimate under exactly the rule that setting
enforces.

The complete fix is launching onto a separate desktop (`CreateDesktop` plus
`STARTUPINFO.lpDesktop`), whose windows cannot take focus from the active
desktop at all. That has not been done: it is a real change with real risk for
a GPU-accelerated Electron app, and the remaining flash is now milliseconds.

## Gotchas worth knowing before you touch this

**Do not run two live suites at once.** `npm run test:live` and
`npm run test:mutations` share `.vscode-test/`, and a concurrent run deletes
the workspace out from under the other. The vsix runner uses its own
`.vscode-test-vsix/` and is safe alongside either.

**An interrupted run leaks an editor.** The test host outlives its launcher, so
ctrl-c leaves a VS Code holding the workspace open and the next run fails
`EPERM` on cleanup. The launcher retries and then names the process to kill:

```powershell
Get-Process Code | Where-Object { $_.MainWindowTitle -like '*Extension Development Host*' } | Stop-Process -Force
```

**`--disable-extensions` means opposite things in the two live suites.** With
`--extensionDevelopmentPath` it suppresses other extensions while the subject
still activates, which is what you want. Once the extension is genuinely
installed it switches the subject off — and there is no `--enable-extension` to
pair with it. The vsix runner therefore omits the flag entirely and gets its
isolation from a fresh `--extensions-dir` containing exactly one extension.

**The webview's page script lives inside a TypeScript template literal.** A
backtick anywhere in `savings.ts`'s `html()` — including in a comment — closes
the string. `tsc` catches it, but the error points at the next line and reads
as nonsense until you know why.

**The fixture's `test/workspace/src/broken.py` is defective on purpose.** The
launcher refuses to start if it stops producing findings, because a fixture
that cannot produce the interesting case makes every test about it vacuous
*and green*. None of the repository's six `test_no_*` guards see it — they all
walk `src/` only — so no exception has to be named for it today.
