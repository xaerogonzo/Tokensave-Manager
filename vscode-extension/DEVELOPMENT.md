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

### The Test Explorer, specifically

Its *decisions* — which node ids reach the command line, how a per-test record
becomes Explorer state, what a cancelled run reports — go in the stub suite;
none of that needs an editor.

Its *tree* goes in the live suite, for the same reason the projects tree does:
the stub's `TestController` stores what it is handed, which proves the code
called it and nothing about whether a person would see their tests.

And **invalidation belongs in the live suite only**. "A test file appears and
the tree picks it up" runs through a real `FileSystemWatcher`, which is exactly
the part a fake would assume away.

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

**`node --test`'s output format depends on the Node version and on whether
stdout is a TTY.** The mutation runner reads test names out of that output to
decide whether the *right* test objected, so a format change turns caught arms
into "CAUGHT BY THE WRONG TEST". It has happened twice: once locally against
TAP, and once on CI, where Node 20 emitted TAP into a parser that had been
fixed by measuring Node 24. `runSuite` now passes `--test-reporter=spec`
explicitly. Do not remove it to tidy up.

**The suite needs `pyflakes`, and its absence looks like a broken fixture.**
Every finding the diagnostics tests assert about comes from
`python -m pyflakes`. Without it, `cli.py checks` returns `ok:false` with an
**empty** findings list -- which is indistinguishable, at the findings level,
from a fixture that has no defects, and `ok:false` does not separate them
either, because a check that found real problems reports that too.

This is the whole reason `assertFixtureProducesFindings` runs before the editor
launches: it caught this on the first Linux CI run, where the message initially
accused the fixture. It now prints every failing check's output alongside, so a
missing analyser names itself. The CI jobs install pyflakes explicitly.

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

**The stub was more permissive than the editor, and 22 tests passed against an
id VS Code rejects.** Test item ids joined the folder and the node id with a
NUL. The stub stored whatever it was handed; the live suite came back with
*"Test IDs may not include the ... symbol"* and the whole tree had failed to
build. Ids are percent-encoded now, and **the stub refuses control characters
too**, so the same mistake fails in the one-second suite. Reverting the id
format makes 17 headless tests fail, which is the check that this actually
closed. When you add a stub method, make it refuse what the real one refuses.

Underneath that is a smaller one: the NUL had been *typed literally* into the
source, where it is invisible in every editor and every diff — so the first
symptom was a test failing with "cannot read properties of undefined" and an
investigation in the wrong module. Write the escape.

**`until(description, predicate)` takes the description FIRST.** Passing them
the other way round does not fail fast; it times out after 30 seconds with
"last threw: predicate is not a function", once per test.

**Piping a suite through `tail` throws away its exit code.** `npm run
test:live | tail -30` reports the exit status of `tail`, which is always 0. A
run with 7 failures looked green until the "7 test(s) failed" line was read out
of the middle of the output. Redirect to a file and check `$?`.

**Patch at the boundary the test is about, and check the code reaches it.**
Two `setup.ts` tests patched `runProjectlessCli` and passed for the wrong
reason: `verifySetup` checks `resolveRunner` first, and against a fictional
root it returned "broken" without ever calling the CLI.

**A counter attached to a function you later replace records nothing.** The
stale-response test swapped `runCli` mid-test, then asserted on a counter
belonging to the swapped-out version. Instrument once and vary the behaviour
inside.

**Bound a poll loop by attempts, not by wall-clock time.** The Manager bridge's
loop originally ran to a `Date.now()` deadline, so a test supplying a no-op
`sleep` spun as fast as the machine allowed for six real seconds. The backoff
list is the budget now, and a test and a user get the same number of polls.

**The live fixture has a `tests/` directory and the Explorer tests write into
it.** They clean up in a `finally`, but an interrupted run can leave
`test_added_later.py` or `test_transient.py` behind. Both are in the disposable
copy under `.vscode-test/`, not in `test/workspace/`, so the next run starts
clean.
