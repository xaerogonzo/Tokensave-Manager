# Project Baseline Rules

<!-- This file is auto-included by every project's CLAUDE.md via @include.
     Edit it here to update all projects simultaneously. -->

---

## Tokensave: Use It First

Tokensave is active for this project. Before reaching for `Read`, `Grep`, or `Glob`, try a tokensave tool — it's cheaper and faster than reading raw files.

| Task | First tool | Fallback |
|---|---|---|
| Find where a symbol is defined | `tokensave_search` | `Grep` |
| Understand what a function calls | `tokensave_callees` | `Read` |
| Find callers of a function | `tokensave_callers` | `Grep` |
| Get context for a task or bug | `tokensave_context` | Read the specific file |
| Understand what a file/module exports | `tokensave_module_api` | `Read` |
| Explore file structure | `tokensave_files` | `Glob` |
| Find TODOs / FIXMEs | `tokensave_todos` | `Grep` |
| Find biggest or most-connected classes | `tokensave_hotspots`, `tokensave_god_class` | — |
| Check code health before/after a change | `tokensave_health`, `tokensave_session_start` / `tokensave_session_end` | — |

Only fall back to `Read` when you need the exact implementation body to edit it or verify precise logic. Use `tokensave_context` with `include_code: true` to pull snippets without reading whole files.

---

## Gotchas: read the relevant file BEFORE you start

Hard-won failure modes, kept alongside this file in `templates/gotchas/`.
They are **not** @included -- collectively they are ~40 KB, and paying that on
every message in every project to be occasionally useful is the wrong trade.
This index is the cheap part; the files are the expensive part.

| If you are about to... | Read first |
|---|---|
| compile to a standalone `.exe` | `NUITKA_GOTCHAS.md` |
| rename/move a directory, or chase a file lock | `gotchas/windows-filesystem.md` |
| touch a CustomTkinter/Tk view, or screenshot one | `gotchas/customtkinter.md` |
| extract shared code into a package two projects use | `gotchas/shared-python-packages.md` |
| write files from a script, or run a bulk rename | `gotchas/agent-scripting.md` |

**These describe failures that do not raise.** A wrong appearance mode renders a
plausible-looking screenshot; a re-export shim passes its tests for the wrong
reason; an unpinned git dependency ships a different product on each build.
Reading the file costs a minute. Rediscovering its contents has repeatedly cost
an afternoon.

**When you hit a new one, add it.** A gotcha earns a place here when it (a) cost
more than ~15 minutes, (b) failed *silently* or misleadingly, and (c) is not
specific to one project. Symptom -> Cause -> Fix, and include the measurement if
there was one.

---

## Documentation Discipline

After any code change, update the minimum set of docs necessary — **proportional to the significance of the change**. Never rewrite a whole file when a one-sentence addition covers it.

| What changed | Update | Scope |
|---|---|---|
| Pure internal bug fix | `CHANGELOG.md` only | One-liner entry |
| New symbol, function, or file | `CLAUDE.md` → Key Files / File Map section | One-liner; skip others unless architecture changed |
| Architecture change (new module, layer, data flow) | `docs/ARCHITECTURE.md` targeted section | + one-liner in `CHANGELOG.md` |
| User-visible feature or behaviour change | `README.md` targeted section | + `CHANGELOG.md` entry |
| Breaking change | `README.md` + `CHANGELOG.md` | Clearly marked |

**Rules:**
- Edit only the section that changed — not the whole document
- Skip a doc entirely if nothing in it is affected
- Create any of these files if they don't exist yet (stub is fine)
- When in doubt: add a one-liner to the relevant section rather than leaving it stale

---

## Code Quality

- Before writing new code, check with `tokensave_search` or `tokensave_context` whether a suitable utility already exists
- Keep functions small and single-purpose
- Docstring/comment public functions and non-obvious logic
- Prefer editing existing code over adding new abstractions unless the existing code is fundamentally unsuitable

---

## Git Discipline

- Commit messages explain **why**, not just what (bad: "fix bug"; good: "fix null check in scanner — crashed on empty file list")
- Commit logical units of work; avoid giant all-at-once dumps
- Don't commit generated files, compiled outputs, or secrets

> **TokenSave Manager** (if installed): right-click any project in the manager → **📜 Git Log** to see the last 20 commits and working-tree status without leaving the tool. Use this to orient yourself on what changed recently before diving in.
>
> **Prefer the manager's Git Commit dialog over committing via Claude Code CLI.** Right-click the project → **📝 Git Commit…** uses a locally-configured LLM (Ollama, LM Studio, etc.) to draft the message at near-zero cost. Committing via a bash tool call in Claude Code burns Anthropic API tokens for something a local model handles well. This is a preference, not a hard rule — use direct `git commit` when the manager isn't running or the situation clearly calls for it.

---

## Compiling with Nuitka

If this project needs to ship as a standalone `.exe`, don't roll a build pipeline from scratch — copy the templates already proven to work, kept alongside this file:

- `nuitka-build.ps1.template` → rename to `build.ps1`, edit the three placeholders (`[PROJECT_NAME]`, `[ENTRY_SCRIPT]`, `[OUTPUT_NAME]`)
- `nuitka-build.bat.template` → rename to `build.bat` (launcher; bypasses execution policy)
- `NUITKA_GOTCHAS.md` → read first if anything goes wrong

The template defaults assume a tkinter GUI app. For CLI tools, swap in the clearly-marked CLI block inside `Build-Exe`. Both variants include:
- Pre-flight checks (Python on PATH, Nuitka installed)
- Orphan cleanup of stuck `*.onefile-build` / `*.build` / `*.dist` directories
- Conditional size-sanity check (warns on undersized GUI builds; skipped for CLI)
- PowerShell 5.1-compatible JSON writing (no BOM)

Claude: when the user asks you to set up a Nuitka build pipeline, use these templates as the starting point rather than writing one freehand. They encode a long list of non-obvious gotchas that aren't worth rediscovering.

---

## Python GUI (Tkinter) Patterns

If this project uses Tkinter, follow the conventions established in TokenSave Manager as a reference implementation:

**Thread safety**: All widget updates from background threads go through `self.after(0, callback)` with a `winfo_exists()` guard. Never call widget methods directly from a worker thread.

**Colour palette**: Use named colour keys (e.g. `C["text"]`, `C["overlay0"]`) from a central palette dict — never hardcode hex values. Define the palette in `constants.py` or equivalent.

**Async task pattern**: background thread + `self.after(0, ...)` widget updates + Stop button + daemon=True. Long tasks go in a dedicated controller class, not in the app or dialog directly.

**Scrollable dialogs**: Wrap dialog content in `Canvas + inner Frame` with a vertical scrollbar. All child widgets pack onto the inner `body` frame, not the canvas or the dialog itself. Bind `<MouseWheel>` on both the canvas and the body frame to prevent double-scroll.

**Window geometry**: Before `withdraw()` to hide to tray, save `self.geometry()` and persist it to config. Restore it with `self.geometry(saved)` before `deiconify()`. On startup, validate the saved geometry is on-screen before applying (guard against disconnected monitors).

**Minimize vs tray**: `_` (minimize) → minimize to taskbar normally. `X` (close) → hide to tray via `WM_DELETE_WINDOW` protocol. Never intercept `<Unmap>` to force minimize into a tray-hide — it confuses users who expect normal taskbar behavior.

**Right-click menu `…` discipline**: Append `…` to `add_command(label=...)` only when the action opens a dialog requiring further input. Commands that execute immediately get no `…`. Example: "Git Commit…" opens a dialog; "Sync" runs immediately.

**Section builders**: One `_build_X_section(body)` method per semantic section (not per visual region). Each builder packs its own separator and header label. No geometry-based chunking.
