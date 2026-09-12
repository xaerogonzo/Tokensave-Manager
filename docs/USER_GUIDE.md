# TokenSave Manager — User Guide

How to use the application, task by task. `README.md` covers what it is, how to
install it and what each tab holds; this covers the jobs you actually do.

**This file is also the in-app Help.** The `<!-- help:key -->` comments above
the headings are anchors the Help tab reads — they render as nothing here, and
they mean the heading can be reworded freely without breaking anything. See
`src/helpers/help_docs.py`.

---

<!-- help:switching-projects -->
## Switching Projects

> You usually do **not** need to restart Claude Desktop to work on another
> project.

The pin (★ Set as Active) chooses the *default* project — the one tokensave
answers about when nothing says otherwise. Claude Desktop's own chats read it
once, when Desktop starts its tokensave server, so moving that default is the
one thing that really does need a restart.

### Claude Desktop and Claude Code are not the same client

Worth getting straight first, because Claude Code can run inside the Desktop
window and still be a different thing:

- **Desktop's own chats** reach tokensave through the wrapper script, which
  reads the pin when Desktop starts it.
- **Claude Code sessions** register tokensave directly and bind to their own
  working directory. The pin never reaches them, and restarting one changes
  nothing about which project it serves.

So everything below about pinning and restarting is about Desktop's chats. If
you are reading this from a Claude Code session, the pin is not what decides
your project.

### Claude Code: already independent, and how to pin it down

There is one rule underneath all of this. Claude Desktop's own `tokensave`
entry is the only thing that can serve the **wrong** project: Desktop spawns
that server app-level, so its working folder is the app rather than your
session, and every Desktop-hosted session inherits the one server whatever repo
it is in. Retire that entry and no project can be answered from another
codebase.

After that, every session serves its own project — by one of two routes, and
both are fine:

- **It has its own `.mcp.json`** — bound explicitly to itself.
- **It has none** — the user-scoped `tokensave serve` entry is started in *that
  session's* folder, so it resolves to that session's project.

So per-project binding is an upgrade, not a requirement. Settings →
Integrations → 🔌 Manage MCP wiring opens on a page that says which route each
project is on.

Bind a project when the automatic route can guess wrong: a git worktree with no
index of its own, a repo nested inside another one, or sessions you start from
a subdirectory. Binding searches nothing — it names the project:

```
Right-click the project → 🗂 Index → "🔌 Bind to this project…"
(or Settings → Integrations → 🔌 Manage MCP wiring to see them all)
```

A binding only loads in a folder Claude Code has been **trusted** in — the "Do
you trust the files in this folder?" prompt. Until you answer it, the
`.mcp.json` is not read at all and the session quietly falls back to the
automatic route. That is harmless while the fallback exists, and it is why
removing the fallback is a separate, deliberate step.

Two more things to expect: only new sessions pick up a binding, and each
project asks for approval once. `graph_root` is then only needed for reaching
*across* projects, which is what it is for.

### Reading across projects needs no restart at all

Every tokensave tool takes a `graph_root` argument, which opens any indexed
project on demand — including one in a completely unrelated folder tree:

```
tokensave_context(task="…", graph_root="D:\Projects\Other")
```

The Reference tab has this ready to paste — the "🌐 Query another project"
snippet. Fill in the path, copy, paste.

### Two things to watch

- **The selected graph opens read-only** — use it for reading and reviewing,
  not for edits.
- **Claude has to be told** — without `graph_root` it answers from the pinned
  project, and a wrong-project answer looks completely normal.

Turn on `strict_tree` (right-click a project → 🗂 Index → 🛡 Enable
strict_tree…) to make that second case fail loudly instead of quietly answering
from the wrong checkout. The same entry reads *Disable* once it is on, so you
can turn it back off if it ever refuses something legitimate.

> `strict_tree` is hardening, not the mechanism. It guards what happens *inside*
> a server — turning an answer about the wrong checkout into a refusal that
> names both roots. It cannot decide which server a session talks to, and during
> the incident that produced this page it was on in both projects and changed
> nothing. Retiring Claude Desktop's entry is the part that decides.

### When you do want to move the default

This section applies only while Claude Desktop chat is **on**. Turn it off and
nothing reads the pin, so ★ Set as Active is not in the menu at all — there
would be nothing for it to decide. The switch is one button: Settings →
Integrations → 🔌 Manage MCP wiring, top of the page, and it goes both ways.

1. Select the new project in the list
2. Click ★ Set as Active
3. Fully quit Claude Desktop (File → Quit, not just close the window)
4. Relaunch Claude Desktop

> A manager feature that tried to skip step 3 by restarting Desktop's server for
> you was removed in Roadmap-10. Desktop does not start a replacement when its
> MCP server dies — it left you with no tokensave at all. See
> `docs/MCP_INTEGRATION_GOTCHAS.md`.

Tip: to go back to whichever project you last synced automatically, click
Auto-detect instead of pinning a specific project.

---

<!-- help:window-tray -->
## Window & Tray

TokenSave Manager runs in the system tray so it can stay alive between sessions
without cluttering the taskbar.

| control | what it does |
|---|---|
| ╳ Close (X button) | Hides the window to the system tray — the app keeps running |
| _ Minimize | Minimizes normally to the taskbar |
| Tray icon → Show | Restores the window to its last position and size |
| Tray icon → Quit | Fully exits the app |

The window position and size are saved automatically when you hide to tray and
restored the next time you click Show. Quitting with unsaved Settings asks
first.

### Claude CLI model

Settings → Paths & Tools → Claude Code CLI → Model controls which Claude model
the manager uses for its automated background calls: pre-commit AI review, the
Suggest button's Claude CLI strategy, and Draft PR via CLI.

| model | character |
|---|---|
| Haiku 4.5 (default) | Fast (3–5 s), cheap, sufficient for code review and commit messages |
| Sonnet 4.6 | Balanced — slower but catches more nuance in reviews |
| Opus 4.7 | Slow (20–40 s on large diffs) but deepest analysis |
| *(empty)* | Use whatever `~/.claude/settings.json` defaults to |

This setting does **not** affect interactive `claude` sessions you launch from
the terminal or the Reference tab — those still use your global default.

---

<!-- help:adding-a-project -->
## Adding a Project

Two buttons, for two situations. Scaffold starts from nothing; "Add tokensave to
a project" points at a folder you already have.

<!-- help:scaffold -->
### ＋ Scaffold

Pick any folder — empty or existing — and choose what to create:

- **Create BASIC_INSTRUCTIONS.md** — project template for Claude
- **Run tokensave init** — build the code graph (~10–30 s)
- **Add Nuitka build files** — copies `build.ps1` + `build.bat`

While init runs the project appears in the list immediately as `(indexing…)`.
Claude reads `BASIC_INSTRUCTIONS.md` on first session and adapts to whatever
structure already exists.

If the folder already has a tokensave index, *Run tokensave init* is unchecked
by default. If `BASIC_INSTRUCTIONS.md` already exists, the checkbox notes that
it will be overwritten.

<!-- help:retrofit -->
### ⚙ Add tokensave to a project

Add tokensave wiring to a project that already exists — without touching any of
its current files destructively.

- **Add tokensave rules to CLAUDE.md** — prepends a single `@include` line.
  Non-destructive: all existing content is kept.
- **Create BASIC_INSTRUCTIONS.md** — optional project template for Claude.
  Skipped silently if the file already exists.
- **Add Nuitka build files** — copies `build.ps1` + `build.bat`. Skipped
  silently if `build.ps1` already exists.

After applying, a summary popup lists exactly what was created or skipped.

<!-- help:scaffold-column -->
### The Scaffold column

The *Scaffold* column in the project list shows whether
`BASIC_INSTRUCTIONS.md` has been created for each project.

- **✔** — `BASIC_INSTRUCTIONS.md` exists
- **—** — not yet scaffolded; use ＋ Scaffold or ⚙ Add tokensave to a project

The column only checks for `BASIC_INSTRUCTIONS.md`. It does not indicate whether
`CLAUDE.md` has the `@include` line, or whether Nuitka build files are present.

---

<!-- help:nuitka -->
## Nuitka Build Files

Both Scaffold and "Add tokensave to a project" have an *Add Nuitka build files*
checkbox. When ticked, two files are copied from the templates folder into the
target project:

- `build.ps1` — full Nuitka build script (PowerShell)
- `build.bat` — one-line launcher that calls `build.ps1`

After applying, open `build.ps1` and fill in the remaining placeholders:

- `[ENTRY_SCRIPT]` — path to your main `.py` file, relative to `build.ps1`
- `[OUTPUT_NAME]` — the desired `.exe` filename
- `[PROJECT_NAME]` — already filled in from your folder name

Then double-click `build.bat` to compile. Read `NUITKA_GOTCHAS.md` (in the
templates folder) for known pitfalls before your first build.

**Claude Code users:** if you already have the project open in Claude Code you
can skip the button entirely — just say *"Set up a Nuitka build pipeline. Entry
script is src/main.py, output name my-tool.exe."* Claude reads the Nuitka
instructions from `project-baseline.md` via `@include` and will copy and fill in
the templates itself.

---

<!-- help:init-vs-sync -->
## init vs sync

**`tokensave init`** — full first-time index of a project. Run once when setting
up a new project. Builds the complete code graph from scratch. Can take a few
minutes on large codebases.

**`tokensave sync`** — incremental update; only re-indexes files that changed
since the last run. Fast. Run it any time you want the index current after
editing code, or to make Auto-detect pick this project on the next Claude
Desktop restart.

The ↺ Sync entry in the right-click menu runs `sync`. If the project has no
index yet, it asks whether to run `init` instead.

### Force Re-sync vs incremental Sync

Right-click a project → ⟳ Force Re-sync wipes the existing index and rebuilds
from scratch. Use it when:

- Sync finished but a recently-added function cannot be found
- You renamed many files or did a large refactor
- tokensave was upgraded to a new version
- The `.tokensave/` folder was edited by hand

For day-to-day edits the incremental ↺ Sync is sufficient and much faster —
seconds rather than minutes on large projects.

---

<!-- help:auto-detect -->
## How Auto-detect Works

The wrapper script (`tokensave-wrapper.py` / `tokensave-wrapper.exe`) runs at
Claude Desktop startup and decides which project to serve.

**Only while Claude Desktop still defines a tokensave MCP server.** Desktop
starts ONE wrapper for the whole app, not one per session, so every Claude Code
session hosted in Desktop inherited whichever single project this picked —
whatever repository it was working in. Settings → Integrations → 🔌 Manage MCP
wiring → "Retire Desktop tokensave…" turns that off, after which each project
serves itself and the steps below decide nothing.

1. Checks `desktop-project.txt` — uses that path if present and valid
2. Otherwise scans project roots for `.tokensave/tokensave.db` files
3. Picks the one with the most recent modification time
4. Starts `tokensave.exe serve -p <chosen path>`

Running ↺ Sync on a project updates its database timestamp, so the next
Auto-detect restart will naturally pick it up.

*Auto-detect* in the right-click menu clears the pin file, switching back to
automatic selection on the next Claude Desktop restart.

---

<!-- help:categories -->
## Project Categories

Projects are automatically grouped under the label of the search root folder
they belong to. You can override any project's category — and add an optional
sub-category — without moving any files.

### How root labels work

Each entry in Settings → Projects → Search roots has a Label. That label becomes
the category header for all projects found inside that folder. Edit the label in
Settings to rename the whole group at once.

### Overriding a single project

1. Right-click the project row
2. Choose 📁 Assign Category…
3. Pick or type a Category, and an optional Sub-category
4. Click OK — the project moves to the new group immediately

To remove an override and return the project to its root's group, open *Assign
Category…* and click **Clear Override**.

### Sub-categories

Sub-categories appear indented under their parent category, shown as
↳ Sub-category. They work like folders within folders. Right-click any project
at any time to move it between groups.

Category headers and sub-category rows are **not selectable** — right-click and
the action buttons only work on project rows.

---

<!-- help:ai-features -->
## AI Features

TokenSave Manager integrates AI at several points. All AI features are optional
and fail open — if the AI call fails, the operation continues without it.

| feature | LLM used |
|---|---|
| 🤖 Ask tab | Configured provider (Ollama / OpenAI / Anthropic / LM Studio) |
| 🔍 Explain button in Help | Same configured provider |
| 💡 Commit message Suggest | Claude CLI → provider → heuristics |
| 🔍 AI Code Review | Claude CLI → provider |
| 📝 Draft CHANGELOG | Claude CLI |
| 🐙 Draft PR | Claude CLI |
| Pre-commit hook | auto (Claude CLI → provider) or explicit |
| AI Tasks | Claude CLI agent sessions |

### 🤖 Ask tab (bounded agent)

An embedded agent that answers questions about your active project. Type a
question in the Ask tab and press Send. The agent runs up to 8 iterations and
streams its response. Tools available to it:

- `read_file` — read a file at a given path and line range
- `list_directory` — list files in a folder
- `git_log` — recent commit history
- `git_diff` — pending uncommitted diff
- `tokensave_search` — find defined symbols by name
- `tokensave_context` — subgraph for a natural-language query

The tokensave tools only work when the project has a `.tokensave/` index. For
general knowledge questions — git concepts, Python syntax — the agent answers
directly, with no tool calls.

### 🔍 AI Code Review

Right-click a project → 🔍 AI Code Review… streams a severity-coloured review of
your staged or HEAD diff, using the configured LLM or Claude CLI. Results appear
in a live-updating dialog: ✗ critical, ⚠ warning, ℹ info, ✓ pass.

### 💡 Smart commit messages

The 📝 Commit… dialog uses a multi-strategy chain to suggest a message:

1. `CHANGELOG.md` bullets, if you added an entry today
2. AI backend — Claude CLI or the configured provider
3. Diff content — added or changed definitions, file types
4. File-name heuristics (legacy fallback)

Click 💡 Suggest to regenerate. Configure the backend in Settings → AI.

### Claude CLI integration

Settings → Paths & Tools → Claude Code CLI wires in the `claude` binary. It is
used by code review, Draft CHANGELOG, Draft PR, Run checks and the AI tasks.
Configure the model in the same place.

### Ollama model manager

Settings → AI → Manage Models browses, pulls and deletes local Ollama models.
Models pulled here are available as a provider option for every AI feature above
that uses the configured provider.

### When AI features are unavailable

- **No provider configured** — features that need one show *AI disabled* and
  fall through to heuristics. Configure it in Settings → AI.
- **No `.tokensave/` index** — `tokensave_search` and `tokensave_context` return
  "run tokensave init first". The other Ask tools still work.
- **Claude CLI not found** — features that require it show a warning and skip
  that step. Set the path in Settings → Paths & Tools.

### Savings and spend

The **Savings** button beside the output pane shows what tokensave saved and
what the API calls cost. Those are two different ledgers and the dialog keeps
them apart — `gain` is the saving, `cost` is the spend.

---

<!-- help:precommit-hook -->
## The Pre-commit AI Review Hook

Installs a git pre-commit hook that runs an AI review of your staged diff before
every commit. If the review finds critical issues it warns you — the commit
still proceeds, which is the fail-open guarantee. The review runs in the
background using the configured LLM or Claude CLI backend.

### Where the hook lives

```
<project>/.git/hooks/pre-commit
```

It is a plain shell script. You can open and read it at any time. Uninstalling
through the manager removes only that file; nothing else in your project is
touched.

### Installing it

Right-click any project → 🔍 Pre-commit AI Review hook… → Install. The dialog
shows the exact script that will be written. Choose *Remove* to uninstall.

### Backends, auto-selected by priority

1. **auto** (default) — tries Claude CLI first, falls back to the LLM provider,
   then skips if neither is available
2. **claude_cli** — always uses the `claude` binary; fastest when configured
3. **llm** — always uses the configured provider (Anthropic, OpenAI, LM Studio,
   Ollama)

### Bypassing it in an emergency

```
git commit --no-verify -m "your message"
```

That skips **all** hooks for that one commit. Use it sparingly — the hook is
there to catch real issues. The manager never sets `--no-verify` for you.

### If the AI provider is offline

If the AI call fails — timeout, network error, unconfigured provider — the hook
exits 0 and the commit proceeds normally. A warning is printed to the terminal.
Your workflow is never hard-blocked by an AI service failure.

The hook only fires for projects where it is installed. Right-click each project
individually to install it.

---

<!-- help:run-checks -->
## Run Checks

Right-click any project → ✓ Run checks… opens a dialog that runs four quality
checks against the project. All enabled checks run concurrently.

| check | what it runs | cost |
|---|---|---|
| Python syntax | `python -m compileall src/ -q` | free, instant |
| pyflakes | unused imports, undefined names | free, ~1 s |
| Doctor audit | tokensave health check | free |
| Claude Code review | AI review of the PR diff against the base branch | uses tokens, off by default |

Results read: **✓** passed, **✗** failed with `file:line message`, **⏳**
running, **—** skipped because the check was unchecked.

### Large-diff warning

If the Claude review is enabled and your diff exceeds roughly 10,000 characters,
the dialog asks for confirmation before sending it. This prevents accidental
token spend on huge diffs.

### Claude review scope

The Claude review runs against `git diff <base>...HEAD` — triple-dot, PR scope,
the same range GitHub would show in a pull request. The base branch is
auto-detected from your git history.

Your checkbox selections are remembered between sessions. Uncheck the Claude
review once and it stays off until you turn it back on.

---

<!-- help:extension-manager -->
## The VS Code Extension

The Manager ships a VS Code extension, and for a while the copy installed in
your editor was three minor versions behind the one in this repository. Nothing
could say so, because the three versions involved were never shown next to each
other.

### The three versions

- **Source** — `vscode-extension/package.json`
- **Built** — the manifest *inside* the `.vsix` on disk
- **Installed** — what the editor reports

Built is read from inside the archive, never from the filename. A filename is
metadata anybody can rewrite; the manifest is what VS Code installs the thing
as, so a stale build renamed to look current passes every filename check there
is.

### Version parity is not freshness

A fourth signal sits beside those three: whether the artefact is older than the
newest thing the build reads. That is a separate question, and it is the only
one that can catch a project whose version number never moves — PyScope's
extension was stale for weeks with all three of its versions reading `0.1.0`.

So a version mismatch and a stale timestamp are reported as different states,
because they ask you to do different things.

### Building it

```
build-extension.ps1                 clean build + verify
build-extension.ps1 -Install        ... and install it
build-extension.ps1 -SkipTests      ... without the suite
```

One command, the same way every time: `npm ci` against the committed lockfile,
`out\` deleted before `tsc`, the test suite, package, then the artefact
verifier. Deliberately the same sequence the release workflow runs, so a local
build and a released one cannot differ.

Verification failure is a build failure. There is no path where a package is
produced, judged unfit, and still reported as success.

### The Extension Manager

Settings → Paths & Tools → 🧩 Extension Manager shows all four signals and
offers Build, Build & Install and Uninstall. It shells out to
`build-extension.ps1` rather than reimplementing the steps — a GUI with its own
copy of the pipeline would drift from the release path, which is the failure
this feature exists to close.

There is deliberately no bare "install the `.vsix` that is already there"
button. Build & Install always rebuilds first, so a stale artefact cannot reach
your editor by one click.

Install and Uninstall need an editor that answers `--list-extensions`.
`editor_cmd` is configurable and may not be VS Code at all, so the buttons are
disabled with the reason rather than failing obscurely when it is not.

---

<!-- help:file-locations -->
## File Locations

| what | where |
|---|---|
| Active project pin | `%USERPROFILE%\.tokensave\desktop-project.txt` |
| Baseline rules | `{{template_dir}}\project-baseline.md` |
| Project template | `{{template_dir}}\claude-md-template.md` |
| Nuitka templates | `{{template_dir}}\nuitka-build.ps1.template` |
| Wrapper script | `{{wrapper_path}}` |
| Manager log | `{{log_file}}` |
| Manager config | `{{config_path}}` |
| This installation | `{{install_dir}}` |

Those `{{…}}` values are filled in from your own configuration when this page is
shown inside the Manager. On GitHub they stay as written, because there is no
installation to read them from.

---

<!-- help:about -->
## About TokenSave Manager

**Version {{app_version}}** — created by Alexander L Corthell.

A Windows GUI for managing tokensave MCP project integrations: project
discovery and switching, index sync, scaffolding Claude instruction templates,
Nuitka build pipelines, the full git workflow through to GitHub releases, AI
code review and commit messages, the pre-commit hook, the Ask agent, the checks
dialog, CodeGraph and PyScope lifecycle, and integration monitoring.

What it deliberately does not do:

- tokensave branch management (`branch add` / `list` / `gc`) — CLI only
- Cross-platform support; this is Windows only

See `CHANGELOG.md` for the full release history.
