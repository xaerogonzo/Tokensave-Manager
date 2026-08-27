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
`bin/windows-x64/tokensave-manager-cli.exe` before packaging; the extension
falls back to it when nothing is configured. The published package ships
without one, which is why it is ~14 KB rather than ~15 MB.

### Other settings

`tokensaveManager.testGapsBase` — the git ref Test Gaps compares against
(default `origin/master`).

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

Rows refresh when a command completes, when the workspace folders change, when
the extension's settings change, and when a watcher sees `.mcp.json` or a
pending commit request move underneath you.

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
npm install
npm run compile
npm test        # node --test, no test framework dependency
npm run package # vsce package --target win32-x64
```

The exit-code table and envelope schema are restated in `src/cli.ts` so
TypeScript can branch on them; the Manager's Python suite cross-checks both
against `src/cli.py` and fails if they drift.

Install the resulting `.vsix` with **Extensions → … → Install from VSIX**.
It is not published to the marketplace.
