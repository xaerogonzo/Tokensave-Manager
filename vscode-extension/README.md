# TokenSave Manager for VS Code

Run the Manager's checks, sync, doctor and test-gap analysis from inside the
editor, without leaving the project you are working in.

## Setup

The extension needs to know where your TokenSave Manager folder is. Until it
does, the view shows **"Manager CLI unavailable"**.

**The easy way — click the "Manager CLI unavailable" row.** It opens a folder
picker; choose the Manager folder (the one containing `src/`) and it saves the
setting for you. Same thing from the Command Palette: `Ctrl+Shift+P` →
**"TokenSave Manager: Set Manager Path…"**.

**By hand, if you prefer.** Press `Ctrl+,` to open Settings, type `tokensave`
in the search box, and paste the folder into the **Manager Path** field —
it saves as you type. For example:

```
D:\Claude Co worker\Token Save Manager Source
```

**Or in JSON.** `Ctrl+Shift+P` → **"Preferences: Open User Settings (JSON)"**,
then add this line inside the outer braces (mind the comma after the previous
entry):

```jsonc
"tokensaveManager.managerPath": "D:/Claude Co worker/Token Save Manager Source"
```

Either slash style works on Windows, but in JSON a backslash must be
doubled — `D:\\Claude Co worker\\...` — so forward slashes are usually easier.

### Why a folder and not a binary

Pointing at the folder means the extension runs the Manager's **live**
`src/cli.py`:

- **It never goes stale.** A compiled CLI is a snapshot taken at build time, so
  every Manager change needs a rebuild → repackage → reinstall cycle before the
  editor catches up. This has none of that.
- **No `configPath` needed** — the CLI finds `manager-config.json` beside your
  checkout on its own.
- **`checks` works.** That command shells out to `python -m pyflakes`, which a
  frozen build cannot do: under Nuitka onefile `sys.executable` is the
  extracted binary, not an interpreter.

Optional: set `tokensaveManager.pythonPath` if `python` on PATH is not the
interpreter you want.

### If you have no checkout

Set `tokensaveManager.cliPath` to a built `tokensave-manager-cli.exe` (the
Manager's third build target), and `tokensaveManager.configPath` to a
`manager-config.json`, since a relocated binary has none beside it. This path
works, but it is the one with the staleness problem — prefer a checkout.

A CLI can also be bundled into the VSIX at
`bin/windows-x64/tokensave-manager-cli.exe`; the extension falls back to it
when nothing is configured. The published package ships without one, which is
why it is ~14 KB rather than ~15 MB.

Bundling is a **deliberate act**: `.vscodeignore` excludes `bin/**`, so remove
that line before packaging if you actually want the binary in there. It used to
exclude only `bin/**/README.md`, which meant a build artefact left in the tree
would be packaged by accident — and a bundled CLI is a snapshot that goes stale
the moment the Manager changes. The release workflow asserts the binary is
absent, so the accident cannot ship even if the ignore file is edited wrongly.

### Other settings

`tokensaveManager.testGapsBase` — the git ref Test Gaps compares against.
Defaults to `auto`, which asks the repository for its own default branch
via `refs/remotes/origin/HEAD` — right whether it uses `main` or `master`.
Set an explicit ref to override. A ref that does not exist is refused
rather than quietly diffed against nothing, because an empty diff renders
as "0 test gaps" — the most reassuring way to report a question that was
never asked.

`tokensaveManager.statusPollSeconds` — how often the status bar re-reads
`status`, in seconds. Deliberately slow (default 300): file changes already
trigger a refresh through the watcher, so the timer only exists to catch
changes made outside the editor.

`tokensaveManager.statusDebounceMs` — how long the status bar waits after a
file change before refreshing (default 750). A branch switch fires a dozen
watcher events in a second, and without this each one would spawn its own
process to compute the same answer.

## Platform

Source mode runs anywhere Python does. The **bundled** fallback is a Nuitka
Windows build, so with nothing configured on a non-Windows machine the view
says so rather than failing obscurely.

## What it is — and what it deliberately is not

The extension is a **thin shell over the Manager's CLI**. It runs commands,
parses one JSON envelope, and renders the result. It does not reimplement
tokensave project attribution, MCP scope precedence, Doctor's rules, or test
discovery — all of that lives in the Manager, is tested there, and each was the
subject of its own investigation. A second implementation in TypeScript would
just be a second thing to be wrong.

**The Manager remains the full UI.** This is the command-and-status surface.

### Propose-only

The extension may create or update `.tokensave-manager/commit_request.json`.
That is the whole of its write authority over your repository.

It never runs `git commit`, never applies a proposal, and never drives the
Manager's GUI to approve anything. Approval happens in the Manager's Git tab,
in front of a person. There is no command here that can bypass that.

## The view

One root per workspace folder. **Every row carries the folder it belongs to**,
so in a multi-root workspace an action can never be aimed at a sibling project
by accident — the same reason the CLI requires an explicit `--project`.

| Row | What it does |
|---|---|
| **Status** | Branch, dirty state, index counts, MCP binding, pending request — one cheap look |
| **MCP status** | Which tokensave server serves this project |
| **Doctor** | Stale entries, plus the anti-monolith cap audit |
| **Checks** | Syntax + pyflakes |
| **Scout** | Refactor candidates read from the tokensave index. No LLM |
| **Tests** | What exists, what is uncovered, what looks stale |
| **Test gaps** | Tests suggested for what changed against a base ref |
| **Run tests** | Runs the suite once and reports the counts |
| **Commit request** | The request waiting for approval in the Manager |

Rows refresh when a command completes, when the workspace folders change, when
the extension's settings change, and when a watcher sees `.mcp.json` or a
pending commit request move underneath you.

## The Problems panel

**Checks**, **Doctor** and **Scout** put their findings in VS Code's Problems
panel, with a squiggle on the line each one refers to. Click one to jump
straight there.

Everything about a finding — where it is, how bad it is, what to call it — is
decided in the Manager and travels in the CLI's envelope. The extension does
not classify anything; it converts 1-based positions to the editor's 0-based
ones and renders what it was handed.

Three details worth knowing:

- **Results are replaced when a command finishes, never cleared when it
  starts.** A long Scout run leaves the previous findings on screen until it
  has something truer to show, rather than presenting an empty panel that has
  not been earned.
- **Each command owns only its own findings.** Re-running Scout never disturbs
  what Checks reported, and neither one touches a sibling workspace folder —
  even when both folders contain a file at the same relative path.
- **Running the tests produces no diagnostics.** A failing test is not a
  statement about a line of source, and a red suite would bury the findings
  that are. Its output goes to the output channel instead.

Syntax errors are reported twice on purpose, by two different tools that
disagree usefully: `compileall` says the file will not compile and points at
the line, while `pyflakes` knows the exact column. Neither is dropped, and each
names its source.

## Exit codes

The CLI's exit codes are semantic, so failures are distinguishable without
reading text:

| Code | Meaning | What the extension does |
|---|---|---|
| 0 | success | status-bar note |
| 1 | ran, found problems | warning + Show Output |
| 2 | invalid command line | error — always an extension bug, please report |
| 3 | prerequisite missing | warning + Open Settings |
| 4 | ran but unverifiable | shown as "could not verify", never as "clean" |

## Developing

```bash
npm ci                   # the lockfile is committed, so builds are reproducible
npm run compile
npm test                 # stub suite — the decisions, headless, ~0.2s
npm run test:live        # a real editor, extension loaded from out/
npm run test:vsix        # a real editor, extension installed from a packaged .vsix
npm run test:mutations   # break each property, require the suite to notice
npm run package          # vsce package --target win32-x64
```

`npm test` runs against a stubbed `vscode` module (`test/vscode-stub.js`), so
it needs no editor download and no extension host. The stub is behavioural
rather than a mock — ranges keep their numbers and URIs really join — because
the properties worth asserting only mean something if those parts behave.

The stub deliberately does **not** cover the tree or the command wiring; it
says so in its own header. `npm run test:live` does, by booting a real VS Code
and holding the real `vscode` module — a command in `package.json` that nothing
registers fails only when somebody clicks it, and nothing else catches that.

`npm run test:mutations` is what says whether any of it is load-bearing: it
removes each protected property from the compiled output and requires the test
written for it to object. A suite that passes on its first run is what a suite
testing nothing looks like, and booting a real editor makes it *look*
convincing.

**Read `DEVELOPMENT.md` before adding a test** — it covers which suite a new
test belongs in, why the editor keeps stealing your focus and what actually
helps, and the handful of gotchas that cost the most time to rediscover.

CI gates on `npm test` today. The live and packaged suites run in their own
non-blocking jobs; the condition for promoting them is written into
`.github/workflows/ci.yml` as a number rather than "after a few PRs".

The exit-code table and envelope schema are restated in `src/cli.ts` so
TypeScript can branch on them; the Manager's Python suite cross-checks both
against `src/cli.py` and fails if they drift.

Install the resulting `.vsix` with **Extensions → … → Install from VSIX**.
It is not published to the marketplace.
