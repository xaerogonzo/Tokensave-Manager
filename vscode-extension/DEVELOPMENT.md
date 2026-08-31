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

## The editor never appears on your screen

A mutation run launches an editor per live arm — fourteen on the current set.
On Windows every one of them runs on a **private desktop**, so none of them
appears on yours, takes the foreground, or flashes.

`test/integration/run-on-desktop.ps1` creates a desktop with `CreateDesktop`,
launches VS Code onto it via `STARTUPINFO.lpDesktop`, waits, and returns the
exit code. A window on a desktop that is not the active one cannot raise
itself and is not composited onto your screen — so there is nothing to react
to, rather than something reacted to quickly.

Measured before it was built, because it was not obvious a GPU-accelerated
Electron app would tolerate it: VS Code was alive after 25 s, had drawn five
windows on the private desktop, and had **zero** on the active one.

Two switches:

* `TOKENSAVE_TEST_FOCUS=1` — run on your own desktop and do nothing about the
  windows, for when you want to watch a run.
* `TOKENSAVE_TEST_DESKTOP=0` — run on your own desktop but fall back to the
  window minder below. The escape hatch if a future Electron stops tolerating
  a private desktop.

### The fallback, and why it is still here

`keep-out-of-the-way.ps1` minimises windows titled `Extension Development
Host` as they appear. It is no longer on the default path, but it is what
`TOKENSAVE_TEST_DESKTOP=0` uses, and it is worth keeping for the same reason
any fallback is.

It reacts rather than prevents, so it leaves a flash as long as its detection
latency. Two things kept that short: it enumerates windows with `EnumWindows`
rather than `Get-Process` (**0.20 ms per sweep against 78 ms** here, which is
what makes a 15 ms poll affordable), and it re-minimises for 25 s because VS
Code activates itself again once the extension host has loaded — a one-shot
minimise gets undone a second later, which looked from outside like a watcher
reporting success while the window sat in the foreground.

### What did *not* fix it

Raising `HKCU:\Control Panel\Desktop\ForegroundLockTimeout` is the usual
advice and **does not apply**. Checked rather than repeated: this machine
already has it at `150000` and the windows still took focus. The setting
governs whether a *background* process may steal the foreground, and these
launches are not background — VS Code is spawned by the terminal running the
tests, and Windows grants foreground rights to a child of the foreground
process, so the activation was legitimate under exactly the rule that setting
enforces. That is why the fix had to change where the window lives rather than
who is allowed to raise it.

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

**`.vscodeignore` has to know about new scratch directories.** The live
suites write profiles and downloaded editors under `.vscode-test*/`, and
nothing excluded them at first — the packaged extension went from 43 KB to
**5.3 MB across 392 files**, most of it a VS Code user profile. The size
assertion in `runTestsVsix.js` caught it; without that it would have been
found at publish time, or not at all.

**The webview's page script lives inside a TypeScript template literal.** A
backtick anywhere in `savings.ts`'s `html()` — including in a comment — closes
the string. `tsc` catches it, but the error points at the next line and reads
as nonsense until you know why.

**The fixture's `test/workspace/src/broken.py` is defective on purpose.** The
launcher refuses to start if it stops producing findings, because a fixture
that cannot produce the interesting case makes every test about it vacuous
*and green*. None of the repository's six `test_no_*` guards see it — they all
walk `src/` only — so no exception has to be named for it today.
