# Project Baseline Rules

<!-- This file is auto-included by every project's CLAUDE.md via @include.
     Edit it here to update all projects simultaneously. -->

---

## Tokensave: use it before Read, Grep or Glob

**The rule, and its trigger.** Before any tool call whose purpose is *finding
out where something is, or what it does*, use a tokensave tool. `Read` is for a
body you are about to edit or must verify line by line. It is not for locating
one.

Measured 2026-09-10 across 30 days on this machine: **912 of 49,285 turns could
have been served by a tokensave query** — 858 `Read` and 54 `Grep`. That is
what this rule is for, and it is why the wording is "before" rather than
"prefer".

**Do not hand code research to a general-purpose or search subagent.** An agent
that reads files re-derives what the graph already holds, and pays a second
context to do it. If a skill or a system prompt recommends one for exploring a
codebase, this rule wins.

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

**Check freshness before trusting an answer.** `tokensave_status` says when the
index last synced. A stale graph is not a broken tool, but it is a wrong answer,
and the honest move is to say so rather than quietly work from it.

Fall back to `Read` when you need the exact implementation body.
`tokensave_context` with `include_code: true` pulls those snippets without the
whole file.

---

## Gotchas: read the relevant file BEFORE you start

Hard-won failure modes, kept alongside this file in `templates/gotchas/`.
They are **not** @included -- collectively they are ~91 KB, and paying that on
every message in every project to be occasionally useful is the wrong trade.
This index is the cheap part; the files are the expensive part.

| If you are about to... | Read first |
|---|---|
| compile to a standalone `.exe` | `gotchas/nuitka-build-setup.md`, then `NUITKA_GOTCHAS.md` |
| rename/move a directory, or chase a file lock | `gotchas/windows-filesystem.md` |
| touch a CustomTkinter/Tk view, or screenshot one | `gotchas/customtkinter.md` |
| build or change a plain Tk/ttk dialog | `gotchas/tkinter-patterns.md` |
| extract shared code into a package two projects use | `gotchas/shared-python-packages.md` |
| write files from a script, or run a bulk rename | `gotchas/agent-scripting.md` |
| write or trust a test, a guard or a mutation run | `gotchas/tests-that-pass-without-testing.md` |
| read a CI result, or write a workflow | `gotchas/ci-green-for-the-wrong-reason.md` |
| check a window, dialog or layout by hand | `gotchas/verifying-a-gui.md` |
| write or debug PowerShell, or read a `.ps1` build script | `gotchas/powershell-silent-failures.md` |
| shell out to another program on Windows | `gotchas/windows-subprocess.md` |
| parse another tool’s output, or wire in a linter | `gotchas/wiring-in-an-external-analyzer.md` |
| design what a result, report or cursor returns | `gotchas/empty-is-not-unknown.md` |
| need admin rights, or split off a privileged helper | `gotchas/elevation-and-privilege.md` |

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
