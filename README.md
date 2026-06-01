# TokenSave Manager

A Windows GUI for managing your Claude Code / Claude Desktop projects: code-graph indexing, git operations, project scaffolding, and `.gitignore` editing — all from one place. Built with Python + tkinter, styled with Catppuccin Mocha.

The manager supports **two code-graph backends as equal citizens**: [tokensave](https://github.com/aovestdipaperino/tokensave) (bundled) and [CodeGraph](https://github.com/colbymchenry/codegraph) (optional, installed via npm). A single project can use either or both — they don't conflict.

If you use Claude across several projects, this is the control panel: switch active projects, sync indexes, scaffold new projects with Claude instruction templates, manage git history and `.gitignore`, create GitHub releases — all without touching the command line.

---

## Table of Contents

- [Code Intelligence — tokensave + CodeGraph](#code-intelligence--tokensave--codegraph)
  - [What is a code-graph tool?](#what-is-a-code-graph-tool)
  - [tokensave](#tokensave)
  - [CodeGraph](#codegraph)
  - [Side-by-side](#side-by-side)
  - [When to use which (or both)](#when-to-use-which-or-both)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
  - [From a compiled release (recommended)](#from-a-compiled-release-recommended)
  - [From source](#from-source)
- [First-Run Setup](#first-run-setup)
- [The Interface](#the-interface)
  - [Projects Tab](#projects-tab)
  - [Git Tab](#git-tab)
  - [🤖 Ask Tab](#-ask-tab)
  - [Reference Tab](#reference-tab)
  - [Help Tab](#help-tab)
- [Right-Click Menu](#right-click-menu)
- [Git Workflow — Step by Step](#git-workflow--step-by-step)
- [Configuration Reference](#configuration-reference)
- [Building from Source](#building-from-source)
- [Project Structure](#project-structure)
- [Roadmap](#roadmap)
- [Changelog](#changelog)

---

## Code Intelligence — tokensave + CodeGraph

The manager treats two different code-graph tools as equal citizens. You can use either, both, or neither on any given project. This section explains what they do, how they differ, and how to pick.

### What is a code-graph tool?

When Claude Code or Claude Desktop explores a codebase to answer your questions, it normally spawns Explore agents that scan files with `grep`, `glob`, and `Read` — burning tokens on every tool call.

A **code-graph tool** pre-indexes your project into a local SQLite database (symbols, function calls, imports, class hierarchies, etc.) and exposes that knowledge to Claude via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Instead of grep-and-read loops, Claude queries the graph directly with tools like `<tool>_search`, `<tool>_callers`, `<tool>_context`.

Real numbers from the field: a tested benchmark on a 25k-file codebase answered a cross-cutting question in **6 tool calls and 35 seconds with a code-graph** vs. **37 tool calls and 2 m 8 s without**. That's ~84% fewer calls and 73% faster — and the savings compound across a session.

Both tools below produce these results. The differences are in distribution, sync model, and ecosystem fit.

### tokensave

- **Source**: [github.com/aovestdipaperino/tokensave](https://github.com/aovestdipaperino/tokensave)
- **Distributed as**: standalone Windows `.exe` (bundle whatever the project ships)
- **DB location**: `.tokensave/tokensave.db` inside each indexed project
- **MCP tools**: `tokensave_search`, `tokensave_context`, `tokensave_callers`, `tokensave_callees`, `tokensave_impact`, `tokensave_node`, `tokensave_files`, `tokensave_status`, `tokensave_changelog`, `tokensave_health` (with `details=true` for per-dimension sub-scores), `tokensave_redundancy` (AST-level functional duplicate detector), `tokensave_call_chain`, `tokensave_find_exact_symbol`, `tokensave_file_dependents`, plus ~40 others (TODOs, dead code, doc coverage, complexity, etc.). Requires **tokensave v6.0.0+**
- **Sync model**: explicit. You run `tokensave sync` (or use the manager's ↺ Sync button). The manager can also auto-commit after sync, and Sync All processes every indexed project sequentially.
- **Manager integration**: deep. The dedicated `tokensave-wrapper.exe` auto-selects which project to serve to Claude Desktop based on a pin file. The manager scaffolds and retrofits projects with full tokensave templates. Sync age is shown in the **Last Synced** column.
- **Why pick it**: comes bundled, no extra install, mature toolset, fine-grained sync control, rich auxiliary tools (TODOs, dead-code finder, complexity reports). The pin-based project switching is unique — it tells Claude Desktop "use THIS project" without restarting your config.

### CodeGraph

- **Source**: [github.com/colbymchenry/codegraph](https://github.com/colbymchenry/codegraph)
- **Distributed as**: npm package `@colbymchenry/codegraph` (TypeScript, requires Node.js 18+)
- **DB location**: `.codegraph/codegraph.db` inside each indexed project
- **MCP tools**: `codegraph_search`, `codegraph_context`, `codegraph_callers`, `codegraph_callees`, `codegraph_impact`, `codegraph_node`, `codegraph_files`, `codegraph_status`
- **Sync model**: automatic. A native OS file watcher (FSEvents / inotify / ReadDirectoryChangesW) re-indexes changed files in real time **while CodeGraph's MCP server is running inside an active Claude Code session**. Manual `🧠 CodeGraph Sync` from the manager is still useful for catching up after editing with Claude Code closed.
- **Manager integration**: per-project lifecycle. The manager handles **Init / Sync / Status / Remove Index** through the right-click menu. The CG column shows ✓ for initialised projects. The Settings dialog can install codegraph globally via `npm install -g`.
- **Why pick it**: zero-touch auto-sync, very fast on huge codebases (the maintainer's benchmark indexed the Swift compiler — 25,874 files, 272,898 nodes — in under 4 minutes), framework-aware routing for 13 web frameworks (Django, Flask, FastAPI, Express, Laravel, Rails, Spring, etc.), broader install reach (also configures Cursor, Codex CLI, opencode if you use them).

### Side-by-side

|  | **tokensave** | **CodeGraph** |
|---|---|---|
| **Distribution** | Standalone Windows `.exe` (bundled) | npm package, requires Node.js 18+ |
| **DB location** | `.tokensave/tokensave.db` | `.codegraph/codegraph.db` |
| **Languages** | Multi-language via custom extractors | 19+ via tree-sitter (TS, JS, Py, Go, Rust, Java, C#, PHP, Ruby, C, C++, Swift, Kotlin, Scala, Dart, Svelte, Vue, Liquid, Pascal/Delphi) |
| **Sync model** | Explicit (`sync`, `sync --force`) | Auto-watch + manual fallback |
| **Sync surface in manager** | "Last Synced" column shows age; ↺ Sync / ⟳ Force / 📊 Status / 🔍 Doctor right-click actions | "CG" column shows ✓ / —; 🧠 Init / Sync / Status / Remove right-click actions |
| **Claude Desktop pin** | ✅ via `tokensave-wrapper.exe` | ❌ (relies on per-project Claude Code config instead) |
| **Framework routes** | ❌ | ✅ Django / Flask / FastAPI / Express / Laravel / Rails / Spring / Gin / Axum / ASP.NET / Vapor / React Router / SvelteKit |
| **Auxiliary analysis** | Rich: TODOs, dead code, doc coverage, complexity, hotspots, god classes | Core graph queries only |
| **Manager handles install** | N/A (point at the `.exe`) | "Install via npm" button in Settings |
| **MCP-config wizard** | Manager has its own (Retrofit dialog) | CodeGraph ships its own (`npx @colbymchenry/codegraph`) |

### When to use which (or both)

**Use tokensave alone** if you want the bundled experience with no Node.js dependency, you value the pin-based project switching for Claude Desktop, or you regularly use the auxiliary analysis tools (dead-code, TODOs, etc.).

**Use CodeGraph alone** if you live primarily in Claude Code (not Desktop), you want zero-touch auto-sync, your projects are huge (10k+ files), or you work with the web frameworks CodeGraph natively understands.

**Use both** if you're undecided, want to A/B compare their answers on the same project, or want to expose both toolsets to Claude simultaneously — they namespace their MCP tools differently (`tokensave_*` vs `codegraph_*`) so they never collide. The manager treats them as fully independent: a project's row shows tokensave's sync age in **Last Synced** AND ✓ in **CG** at the same time.

The manager's design philosophy: **never force a choice you don't want to make**. Adding CodeGraph never breaks tokensave, removing it never breaks anything else, and a project can move between the two states freely.

---

## Features

### Project Management
- **Automatic project discovery** — scans configured search roots for any folder containing a tokensave index (`.tokensave/`), a CodeGraph index (`.codegraph/`), or a git repository (`.git/`). All three types appear in the list and are visually distinguished
- **One-click project switching** — set any tokensave project as the active one for Claude Desktop; a pin file tells `tokensave-wrapper.exe` which project to serve
- **Project status at a glance** — three indicator columns:
  - **Last Synced** — age of the tokensave index (`2h ago`, `3d ago`)
  - **CG** — ✓ if CodeGraph has indexed this project, — otherwise
  - **Git** — ✓ clean, ● uncommitted changes, ↑N commits ahead, ↓N behind, ●↑N mixed
- **Auto-refresh** — project list silently re-scans every 60 seconds; skips if a sync is running. Git statuses are computed asynchronously with `.git/index` mtime caching so unchanged projects skip the subprocess call
- **System tray** — close or minimise sends the manager to the tray; right-click to Show or Quit
- **Single-instance lock** — launching a second copy focuses the existing window

### Code Intelligence — tokensave AND CodeGraph
- **Two backends, one UI** — right-click a project to run tokensave's `↺ Sync / 📊 Status / ⟳ Force Re-sync / 🔍 Doctor` and CodeGraph's `🧠 CodeGraph Init / Sync / Status / Remove Index` from the same menu. The manager handles both lifecycles independently
- **Sync All** — syncs every tokensave-indexed project sequentially with `[i/n]` progress logging; git-only and CodeGraph-only projects are skipped with a note
- **Friendly install nudges** — clicking a CodeGraph action on a project without the tool installed opens a dialog explaining how to install (Settings → CodeGraph → Install via npm)
- **CodeGraph install button** — Settings → CodeGraph has an **"Install via npm"** button that runs `npm install -g @colbymchenry/codegraph` in the background. Windows `EPERM`/`EACCES` failures (the common system-wide-Node trap) surface a specific hint about reinstalling Node per-user or running as admin
- **Auto-detected paths** — both `tokensave_exe` and `codegraph_exe` auto-detect on save; `.cmd`-first Windows shim resolution handles npm-installed binaries correctly

### Project Organisation
- **Category grouping** — each search root has a configurable label that becomes a category header in the project list. Projects under `D:\Doom Mods` appear under a "Doom Mods" header, etc.
- **Sub-categories** — right-click any project → **📁 Assign Category…** to put it in a sub-group (e.g. "Doom Mods → GZDoom")
- **Per-project category overrides** — saved in `manager-config.json` so they survive restarts

### Scaffolding & Retrofit
- **Scaffold new project** — picks a folder and optionally writes a `BASIC_INSTRUCTIONS.md` (Claude session instructions), runs `tokensave init`, adds a Nuitka build pipeline, and/or adds an auto-commit Stop hook for Claude Code sessions
- **Retrofit existing project** — adds tokensave MCP rules to an existing `CLAUDE.md` via `@include`, optionally with all the same extras as Scaffold
- **Shadow Links** — generates NTFS hardlinks of source files with a secondary extension (e.g. `.zsc` → `.zsc.cpp`) so editors with limited language support can still parse them. Right-click → **🔗 Shadow Links…**
- **Manage .gitignore dialog** — right-click → **📋 Manage .gitignore…** opens a full editor: scrollable list of current entries with per-row `×` remove (real strikethrough font on marked rows), one-click template injection for 11 categories (Baseline, Python, Node.js, Rust, Java/JVM, .NET, VS Code, JetBrains, macOS, Windows, Nuitka), custom-entry field with dedup + sanity check, live `+`/`−` diff preview before saving. Atomic file write; the existing commit-after-change flow then offers to commit the result. The baseline category includes `.tokensave/`, `.codegraph/`, `.claude/`, Python cache, Nuitka output, virtual environments, and OS noise — all auto-protected so binary index DBs never get committed
  - **🤖 AI Suggest button** — one click scans the project and asks the configured AI (Ollama / Claude CLI / Anthropic / any OpenAI-compatible provider) for gitignore patterns tailored to the actual project's tech stack. Context is gathered without scanning deep trees: if CodeGraph has indexed the project (`.codegraph/codegraph.db` exists), the full file-extension and top-level directory list is read from CodeGraph's SQLite database for zero extra I/O cost — otherwise falls back to `os.listdir`. The AI also sees untracked files (`git ls-files --others`) and the current ignore state so it never repeats patterns already present. Suggestions appear as pending additions in the diff panel (with a `# AI suggested patterns` attribution header) — nothing is written until you click Save. Reuses the `ask_tab_llm → commit_message_llm` config fallback chain so no extra settings are needed.
  - **📂 Browse… button** lets you pick a file or folder via the OS file picker; the path is auto-relativized against the project root and `/` is appended for directories. Cross-drive picks (Windows) and parent-of-project picks both surface a warning instead of producing invalid `..\` patterns.
  - **UNTRACKED FILES panel** (git repos only) lists output from `git ls-files --others --exclude-standard` with a click-to-add `[+]` button per file — populated in a background thread so opening the dialog stays instant even on large repos. Clicking `[+]` strikes through the row visually so you can see what's pending.
  - **🔒 Privacy semantics banner + ⚙ Advanced — Scrub from History…** — files added here stop being PUSHED from now on, but older commits on GitHub still contain them. For full-history erase, the Advanced disclosure opens a dedicated dialog with a 9-layer safety net: one-click `pip install git-filter-repo` if absent, auto untrack-and-commit preamble (filter-repo refuses non-clean trees), scrollable file picker pre-filled from the tracked-but-ignored list, affected-commits preview, auto backup branch (`backup/before-scrub-<ts>`), confirmation-phrase typing (must type the file's basename), `git filter-repo --invert-paths --force`, post-scrub force-push guidance with re-clone warning, and a destructive-action red banner so a novice can't fire the action accidentally. The manager snapshots the `origin` remote URL before the scrub and automatically re-adds it afterward — `git filter-repo` unconditionally removes all remotes during history rewrite, which would otherwise leave the force-push step with no remote to push to. The manager never auto-pushes — the force-push command is shown for the user to run manually after they've verified the result.

### Git Integration (no command line needed)
- **Git tab** — live view of any project's git state: current branch, remote URL, working tree changes, recent commits, colour-coded diff viewer
- **Push / Pull / Fetch** — one-click with `git push -u origin HEAD` / `git pull` / `git fetch --prune`; graceful auth-error message if GitHub credentials aren't cached yet. **📡 Fetch** updates the remote-branch list without merging — use before Switch Branch… to see branches a collaborator pushed or that you created on another machine
- **Commit with per-file staging** — every changed file is shown as a checkbox with a colour-coded status badge (M modified / A added / D deleted / R renamed / ? untracked). Tick exactly the files you want, write a message (or use **💡 Suggest** for a multi-strategy auto-generated conventional-commit message), commit only those files
- **Smart commit-message suggestions** — the **💡 Suggest** button runs a strategy chain: optional AI call (Anthropic / OpenAI / LM Studio / Ollama) → staged CHANGELOG.md bullets parsed into `feat(scope): subject + body` → diff content (added Python `def`/`class` names, file-kind heuristics) → file-name fallback. Output is sanitised: subjects ≤ 72 chars, imperative mood, generic `chore:` escalated to `refactor:` when source files changed, filename-listing anti-patterns blocked
- **Undo Last Commit** — `git reset --soft HEAD~1`; keeps all changes, removes only the commit marker
- **Branch management** — New Branch, Switch Branch, Merge, Delete Branch dialogs; no typing required. **Switch Branch and Merge dialogs show remote-tracking branches** below a `── remote ──` separator (each marked with `↓`); selecting one runs `git checkout <name>` and git's DWIM auto-creates a local tracking branch. Remote branches already checked out locally are deduplicated.
- **Merge a branch INTO the current one** — `⇄ Merge…` picks a source branch (the destination is wherever you are right now). Uses `git merge --no-edit` so no editor pops up. Conflicts surface as a dialog with the resolve-and-commit or `git merge --abort` instructions; dirty-tree errors get their own dialog
- **Remote-aware Delete Branch** — after a successful local delete, the manager checks `git branch -r` for `origin/<branch>`. If it exists, you get a prompt to also `git push origin --delete <branch>` so GitHub stays in sync. Works for both safe and force deletes
- **Open PR on GitHub** — on a feature branch, opens the GitHub compare page directly in your browser. On master/main, walks you through the branch workflow step by step
- **Draft PR description** — `Draft PR…` button asks an AI to draft a structured PR description (Summary / Technical Implementation / Threat Model / Verification Steps / Testing checklist). Primary click uses Claude Code CLI if configured (opens a new terminal — your app stays unblocked); falls back to Ollama / API path otherwise. Right-click or Shift+click shows an explicit-choice menu including **"Set PR base branch…"** (persists a per-project override so PRs can target `Roadmap-7.5` instead of `master`). The **Ollama / API** dialog is a **standalone, resizable window** (native min/max + its own taskbar entry) that **streams the draft live** as the model writes — a grounding badge ("✓ Grounded: tokensave + codegraph") and a status line (grounding → generating) show progress instead of a blank wait. It has a PR title field and **Copy / Create PR on GitHub / Open in Browser** buttons (pinned and always visible), plus a **🧪 test-gap panel** listing which changed files have no tests with one-click stub or AI-generate actions. Re-triggering Draft PR while a draft is open or edited asks before discarding it. The generated body includes a **Testing checklist** with a `### Coverage gaps` subsection (changed files lacking tests) on both the Ollama and Claude CLI paths. Diff strategy: `git diff <merge-base>` — includes committed + staged + unstaged work so the AI sees your full branch context even before the final commit; for local models the diff budget scales with the model's context window (`num_ctx`) so large branches aren't truncated to a sliver
- **Set Remote** — step-by-step dialog for connecting a project to a GitHub repository
- **GitHub Setup wizard** — full onboarding: set git identity, sign in / create account, create repo on GitHub, set remote, first push, create a release
- **Commit prompts after manager actions** — when Ensure .gitignore / Shadow Links / Scaffold / Retrofit / 🧠 CodeGraph Init modifies files in a git project, a "Commit this change now?" dialog appears so the working tree never sits silently dirty. Uses a strict local-repo check (`os.path.exists(.git)` — supports git worktrees) so projects nested inside an unrelated parent repo don't get ghost prompts
- **Button locking during git operations** — every Git tab button greys out while a push / pull / commit / branch operation is in flight, then re-enables when the worker finishes. Prevents the classic double-push race
- **Auto-commit after sync** — optional toggle (Settings) that runs `git add -A + git commit` automatically after every successful tokensave sync; amends the previous commit if it was also a sync commit, to avoid history pile-up. With the AI integration enabled, can produce a fresh AI-generated message per sync instead (no amend-stacking) — opt-in via the **"Also use AI for sync auto-commit messages"** sub-toggle
- **Auto-commit Stop hook** — optional per-project Claude Code hook that commits whatever Claude changed at the end of each session

### AI features (Stages 0–4 shipped; Roadmap-7 cascade rounds completed)
- **Stage 0 — Smart commit-message generation** (described under "Git Integration" above)
- **Stage 1 — 🔍 AI Code Review** — right-click any project → "🔍 AI Code Review…" opens a split-pane dialog: top pane shows `git diff HEAD` with green/red colour-coding, bottom pane streams an AI-generated structured review (⚠ High / ⚡ Medium / 💡 Low / ℹ Observations sections). Async with token-by-token streaming via byte-aligned SSE parsing — visible progress instead of a long spinner. Stop / Regenerate / Copy buttons. Pure read-only: no tools, no file writes
- **Stage 2 — 🤖 Ask tab** — notebook tab next to Git. Chat interface where a local LLM uses six READ-ONLY tools (`read_file`, `list_directory`, `git_log`, `git_diff`, `tokensave_search`, `tokensave_context`) to answer questions about the selected project. Bounded loop (8 iterations default), cumulative context budget (~40 000 chars across tool outputs), per-tool error wrapping, path-containment validation, stop-via-event-flag cancellation. Tool calls + results appear inline in the chat log (peach for call, dim grey for result, blue for user, default for assistant). See `docs/AGENT_ARCHITECTURE.md` for the locked architectural rules
- **Stage 3 — 📝 Doc Updates dialog** — right-click any project → "📝 Doc Updates…" opens a registry-driven dialog with one tab per doc type (CHANGELOG, README, ARCHITECTURE, ROADMAP, MEMORY, TOKENSAVE_GUIDE, generic docs). Drafts targeted updates from a commit-range against the existing file content; multi-section + hallucination protection refuses to invent section titles not present in the existing doc. Apply routes through ProposalBridge for old-vs-new diff review. Per-tab `🔍 Tokensave tools` checkbox enables Ollama-only mid-drafting tool calls. Apply-time validation surfaces a warning banner above the text widget when rejected (e.g. hallucinated section titles); the banner auto-clears the moment the user edits the draft content. The whole pipeline auto-resolves a commit range from the doc-anchor markers and shows an elapsed-time tick (`"Drafting on Ollama (12s)…"`) so long generations never look like a hang
- **Tokensave + codegraph grounding** — every AI surface (commit messages, PR draft, code review, Ask tab non-agentic, Doc Updates) injects a slim grounding block built from `tokensave tool context/search` PLUS `codegraph context/affected` (when codegraph is indexed for the project). Both sources are dedup-merged with per-source caps so the prompt stays bounded. Master toggle in Settings → AI backend selection → "Code-graph grounding"; default ON. Grounding is silent-fail — projects without either tool indexed get a clean grounding-less prompt
- **Codegraph freshness UX** — selecting a project in the Projects tab kicks a debounced background `codegraph sync` if the index is stale (200 s tolerance). The CG column shows a health glyph: `✓ indexed` / `⏳ stale` / `⚠ under-indexed` / `—`. If the index is genuinely broken (tokensave sees ≫ more files than codegraph — common after a project refactor), a one-time-per-session dialog offers a full reindex. Every grounded LLM call passes through `ensure_fresh` first, so a stale index won't silently feed bad signal to the model
- **🦙 Ollama Model Manager** — Settings → "🦙 Manage Ollama Models…" launches a dedicated dialog that uses Ollama's native REST API to browse installed models, pull new ones with live progress, see per-model context windows, and delete unwanted ones
- **🦙 Ollama Model Manager — detailed view** — the dedicated dialog uses Ollama's REST API (`GET /api/tags`, `POST /api/show`, streaming `POST /api/pull`, `DELETE /api/delete`). Cancel during a pull explicitly closes the `HTTPResponse` to unblock the worker (setting a `threading.Event` alone doesn't break out of `read()`)
- **🔄 Upgrade tokensave from the manager** — Settings has an always-visible "🔄 Upgrade tokensave" button that runs `tokensave upgrade`. Three signals keep the button label fresh: a local `tokensave --version` probe at startup, the sync-output parser catching `Update available: vA → vB` lines, and an hourly GitHub releases poller (`api.github.com/.../releases/latest`). When a newer version is available the button promotes to a green Primary "🔄 Upgrade tokensave to vX.Y.Z". A "🔍 Check integration" button sits beside it — runs `scripts/check_tokensave_integration.py` and shows the report in a scrollable dialog. Right-click any project → **🔄 Integration check** for the same report. See [`docs/UPGRADE_INTEGRATION.md`](docs/UPGRADE_INTEGRATION.md) for the full workflow
- **Claude Code CLI integration** — Settings → Git tools has a "Claude Code CLI" row with Browse + Auto-detect. When configured, the manager spawns `claude` (`npm install -g @anthropic-ai/claude-code`) in its own detached terminal window for tasks like Draft PR — your app stays fully unblocked while Claude works. Auto-detect probes the `.cmd` shim first (npm's Windows convention) and falls back to `%APPDATA%\npm\claude.cmd`. If auto-detect comes up empty (npm bin not on PATH in this launch context), paste the full path manually. A **Model** combobox below the path selects which Claude model the manager uses for its automated `claude --print` calls (pre-commit review, commit-message Suggest, Draft PR). Defaults to `claude-haiku-4-5-20251001` (fast, 3–5 s). Empty = defer to `~/.claude/settings.json`. Does not affect interactive `claude` sessions
- **✓ Run checks…** — right-click any project → "✓ Run checks…" opens a dialog that runs four pre-merge quality checks concurrently: Python syntax (`compileall`), pyflakes, Doctor audit (calls `_audit_project_tree` directly — no subprocess), and an optional Claude Code review of the PR-scope diff (`git diff <base>...HEAD`). All four are toggleable checkboxes persisted to `cfg.raw["checks_enabled"]`; Claude review is off by default (token cost). A large-diff warning fires on the main thread if the diff exceeds 10k chars before the Claude call is sent. Results update live with ✓ / ✗ / ⏳ / — icons as each future resolves. Dialog close cancels queued futures cleanly. Footer bar adds two one-click actions: **📋 Generate GitHub Actions** (writes `quality-checks.yml` from enabled checks) and **🔗 Install/Remove pre-push hook** (blocks bad pushes at the git level)
- **GitHub Actions CI** — `.github/workflows/ci.yml` runs free deterministic checks (syntax + pyflakes) on every push and pull request. No secrets, no paid runners. The ✓ or ✗ badge appears on every PR automatically. The **Run Checks dialog** can also generate a manager-curated `quality-checks.yml` covering your currently-enabled checks
- **Pre-push hook** — one click in the Run Checks dialog installs `.git/hooks/pre-push`, which blocks every `git push` if syntax, pyflakes, or the doctor audit fail. Claude check is always skipped (too slow for automatic gating). Fail-open on infrastructure errors; remove via the same dialog
- **📊 Cost viewer** — `📊 Cost` button next to View Log opens a metric dashboard showing tokens saved, dollar value recouped, and total input/output token counts (parsed from `tokensave cost`). Subprocess runs in a background thread so the dialog opens instantly with `Loading…` placeholders, then updates when the data arrives

### Settings & Tools
- **Settings dialog** — configure all paths (`tokensave_exe`, `template_dir`, `editor_cmd`, `git_exe`, `codegraph_exe`, `claude_cli_exe`, `claude_cli_model`) and search roots through a GUI; changes apply immediately. Validates and auto-detects on save. Scrollable and resizable (760×700 default, 640×500 minimum) so growing sections never push controls off-screen
- **🔌 MCP Integration configurator** — Settings → "🔌 Manage MCP wiring…" opens a dialog that classifies the `tokensave` MCP entries in BOTH Claude Desktop's `claude_desktop_config.json` AND Claude Code's `~/.claude.json`. UWP-aware: detects Microsoft Store / packaged Claude installs and targets the per-package config under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\` (where the legacy `%APPDATA%\Claude\` path resolves to a DIFFERENT file from inside the package's process tree — see `docs/MCP_INTEGRATION_GOTCHAS.md` for the full story). Each config row shows ✓ correct / ⚠ bypasses wrapper / ✗ missing. Recognises `tokensave install --agent claude`'s canonical direct-serve shape (`{"command": "tokensave.exe", "args": ["serve"]}`) as valid for Claude Code so the banner doesn't fight an upstream tool. Apply writes via `shutil.copy2` backup-first; refuses to write while Claude is running (the config would be silently clobbered by Desktop's preferences-save). Skip list persists so dismissed warnings don't return
- **AI commit messages** — Settings dialog has a dedicated **"AI commit messages"** section with a provider dropdown (Anthropic / OpenAI / OpenAI-compatible / Ollama), model field, API-key env-var name, and base URL for local OpenAI-compatible servers. Quick-preset buttons for **Anthropic** (Claude Haiku/Sonnet/Opus), **LM Studio** (`http://localhost:1234`), and **Ollama** (`http://localhost:11434`). Min-diff-lines threshold so trivial commits skip the LLM. All LLM failures silent-fallback to the heuristic chain — never blocks the commit dialog
- **GitHub CLI installer** — Settings dialog includes a **"Install via winget"** button that installs the GitHub CLI (`gh`) in the background; shows a green checkmark when found on PATH
- **CodeGraph installer** — Settings dialog includes a **"Install binary (npm)"** button that installs `@colbymchenry/codegraph` globally. Runs on a background thread so the GUI never freezes; surfaces Windows EPERM/EACCES errors with actionable hints
- **CodeGraph MCP configuration** — three Step-2 buttons in Settings → CodeGraph: **🔌 Configure MCP (auto)** runs `codegraph install --yes` to wire codegraph into Claude Code's MCP servers (`~/.claude.json`) so the `mcp__codegraph__*` tools appear in agent sessions; **⚙ Configure MCP — pick agents…** opens a picker for choosing among the 4 supported agents (claude / cursor / codex / opencode) with destination-path detection so un-installed agents render disabled; **🧹 Uninstall MCP** reverses everything. Status row reports per-agent wiring state by parsing `~/.claude.json` directly
- **💾 Tool Manager dialog** — single discovery surface (Settings → tokensave or CodeGraph section → 🛠️ Open Tool Manager…; OR Help tab → 💾 Tool Manager… in the left nav) for the full **install / update / uninstall** lifecycle of both code-graph tools. Tokensave install downloads the latest Windows zip from GitHub releases and extracts to `%LOCALAPPDATA%\TokenSaveManager\bin\` (no admin needed). Codegraph update runs `npm install -g @colbymchenry/codegraph@latest`. Cascading uninstall strips MCP wiring FIRST then removes the binary, with graceful fallback if MCP cleanup fails so users never get trapped with a broken-but-undeletable tool. Hardened against Windows npm-shim crashes, Zip Slip, GitHub rate-limit (403), and double-click races
- **Per-feature grounding toggles** — Settings → AI backend selection nests two checkboxes under the master "Code-graph grounding" toggle: opt-in for commit-message Suggest (default OFF — live testing showed grounding hurts on big multi-file commits for small models) and opt-out for Draft PR (default ON — PRs genuinely benefit from test-impact + symbol context). The Git Commit dialog's strategy badge appends 🔗 grounded or ✕ ungrounded after each Suggest so you can see at a glance which mode produced any given message
- **Git auto-detection** — finds `git.exe` via PATH or common Windows install locations automatically
- **CodeGraph auto-detection** — finds `codegraph.cmd` in `%APPDATA%\npm\` or wherever npm placed it; probes `.cmd` before bare names since Windows `subprocess.run` requires the extension on shim files
- **Reference tab** — CLI cheatsheet + 12 built-in Claude prompt snippets (codebase overview, symbol search, impact analysis, health check, etc.) with copy-to-clipboard; add your own custom snippets
- **Help tab** — full operational guide covering every feature, including a dedicated CodeGraph section, the git workflow, GitHub setup, project categories, and more
- **Output log** — always-visible coloured log panel at the bottom of the window; all subprocess output appears here in real time

---

## Requirements

- **Windows 10 / 11** (NTFS required for shadow links; everything else works on any NTFS volume)
- **tokensave.exe** — the tokensave binary (not bundled; [get it here](https://github.com/aovestdipaperino/tokensave))
- **Git for Windows** — [git-scm.com/download/win](https://git-scm.com/download/win) — required for all Git tab features
- **Node.js 18+** (optional) — only needed if you want to use [CodeGraph](https://github.com/colbymchenry/codegraph) as an alternative or complementary code-graph tool. The manager has an "Install via npm" button in Settings → CodeGraph that runs `npm install -g @colbymchenry/codegraph` for you

**For running from source only:**
- Python 3.10 or later
- `pip install pillow pystray`

**Optional:**
- **GitHub CLI (`gh`)** — needed for releases from the GitHub Setup wizard. Install it from Settings → GitHub CLI → "Install via winget", or manually: `winget install --id GitHub.cli`

---

## Installation

### From a compiled release (recommended)

1. Download the latest release zip from the [Releases](https://github.com/xaerogonzo/Tokensave-Manager/releases) page
2. Extract anywhere — `C:\Tools\TokenSave Manager\` is a good spot
3. Run `tokensave-manager.exe`
4. The Settings dialog opens automatically on first run — fill in:
   - **tokensave.exe path** — wherever you put `tokensave.exe`
   - **Search roots** — one or more folders to scan for tokensave projects (e.g. `D:\My Projects`)
5. Click Save — the manager scans and populates the project list

That's it. No Python install required.

### From source

```powershell
# Clone the repo
git clone https://github.com/xaerogonzo/Tokensave-Manager.git
cd "Tokensave-Manager"

# Install Python dependencies
pip install pillow pystray

# Copy the example config and fill it in
copy manager-config.example.json manager-config.json
# Edit manager-config.json — set tokensave_exe and search_roots at minimum

# Launch
"Launch TokenSave Manager.bat"
```

Or run directly:
```powershell
pythonw src/app.py
```

---

## First-Run Setup

When you launch for the first time (or when config paths are invalid), the **Settings dialog** opens automatically with a red banner describing what's missing.

**Minimum required fields:**

| Field | What to set |
|-------|-------------|
| **tokensave.exe** | Full path to `tokensave.exe` — e.g. `C:\Tools\tokensave.exe` |
| **Search roots** | Click **+ Add**, browse to a folder that contains your projects (e.g. `D:\My Projects`), give it a label like "My Projects" |

**Optional but recommended:**

| Field | What to set |
|-------|-------------|
| **Editor command** | `code` for VS Code, `code --new-window`, `notepad`, etc. |
| **Git exe** | Leave blank to auto-detect, or click **Auto-detect** |
| **GitHub CLI** | Click **Install via winget** if not already installed (enables 🔗 Open PR + the Releases section in GitHub Setup) |
| **CodeGraph** | Click **Install via npm** if you want the alternative code-graph backend (requires Node.js 18+) |

After saving, the manager scans your search roots and shows any folder that has been initialised with `tokensave init` (`.tokensave/`), `codegraph init` (`.codegraph/`), OR contains a `.git/` repository.

If a folder doesn't appear, it probably has none of those markers. Either:
- Right-click it → **+ Scaffold** → tick "Run tokensave init" to bootstrap a tokensave project
- Right-click → **🧠 CodeGraph Init** to bootstrap a CodeGraph project
- Right-click → **🔧 Git Init** to make it a git repo (then it'll appear with git features available)

You can also use **⚙ Retrofit Existing** from the toolbar to add tokensave rules + templates to an existing project.

---

## The Interface

### Projects Tab

The main tab. Shows all discovered tokensave projects grouped by category.

```
┌─ Category header (from search root label) ─────────────────────────┐
│  ↳ Sub-category (optional per-project override)                     │
│     ★  Project Name        D:\path\to\project    2h ago    ✔       │
│        Another Project     D:\path\to\other       1d ago    —       │
└────────────────────────────────────────────────────────────────────┘
```

**Columns:**
- **Project** — project folder name; `★` marks the active (pinned) project for Claude Desktop
- **Path** — full folder path
- **Last Synced** — age of the tokensave index (how long ago the last sync ran); `—` for projects without tokensave
- **CG** — `✓` if CodeGraph has indexed this project (`.codegraph/codegraph.db` exists); `—` otherwise. CodeGraph auto-syncs while its MCP server is running, so unlike tokensave there's no meaningful "last synced age" to show
- **Git** — at-a-glance git status: `✓` clean (all pushed, no changes), `●` uncommitted changes (yellow row), `↑N` N commits ahead of remote (sky row), `↓N` N commits behind (red row), `●↑N` mixed (peach row), `—` not a git repo. Computed asynchronously after `refresh()` with `.git/index` mtime caching so unchanged projects skip the subprocess call
- **Scaffold** — `✔` means `BASIC_INSTRUCTIONS.md` exists; `—` means Claude has no instructions for this project yet

**Toolbar buttons:**
- **+ Scaffold** — initialise a new project with Claude instructions and/or tokensave index
- **⚙ Retrofit Existing** — add tokensave + Claude instructions to a project that already exists
- **↺↺ Sync All** — sync every project in the list one by one
- **⟳ Refresh** — re-scan search roots now (also happens automatically every 60s)
- **⚙ Settings** — open the Settings dialog

The **active project badge** at the top of the window shows which project is currently pinned for Claude Desktop, and whether it was manually pinned or auto-detected.

### Git Tab

A full git control panel for whichever project is selected in the Projects tab.

```
┌─ Git tab ─────────────────────────────────────────────────────────┐
│  ProjectName    Branch: main    Remote: github.com/you/repo       │
│  [Set Remote]  [🐙 GitHub…]  [⟳ Refresh]                         │
├───────────────────────────────────────────────────────────────────┤
│  WORKING TREE                    RECENT COMMITS                   │
│  M  src/main.py                  a1b2c3  feat: add thing  2h ago  │
│  ?  new_file.txt                 d4e5f6  fix: crash       1d ago  │
│  (click a file → diff below)                                      │
├───────────────────────────────────────────────────────────────────┤
│  DIFF                                                             │
│  @@ -100,4 +100,6 @@                                             │
│  -old line                                                        │
│  +new line                                                        │
├───────────────────────────────────────────────────────────────────┤
│  [⬆ Push]  [⬇ Pull]  [📡 Fetch]  [📝 Commit…]  [↩ Undo Last Commit]│
│  [🌿 New Branch]  [🔀 Switch Branch…]  [⇄ Merge…]                  │
│  [🗑 Delete Branch…]  [🔗 Open PR]                                 │
└────────────────────────────────────────────────────────────────────┘
```

**Header:**
- Shows current branch name and remote URL (shortened for readability)
- **Set Remote** — connects the project to a GitHub repository (step-by-step dialog)
- **🐙 GitHub…** — opens the GitHub Setup wizard
- **⟳ Refresh** — re-reads branch, remote, status, and log

**Working Tree panel:**
- Lists every modified, added, deleted, or untracked file
- Click any file to see its diff in the viewer below (colour-coded: green for additions, red for deletions)

**Recent Commits panel:**
- Shows the last 15 commits in `--oneline` format

**Diff viewer:**
- Colour-coded with syntax highlighting for `+` / `-` / `@@` / `---` / `+++` lines
- Capped at 2000 lines to prevent UI stutter on large generated files

**Action buttons** (disabled automatically when no git repo or no remote):

| Button | What it does |
|--------|-------------|
| ⬆ Push | `git push -u origin HEAD` — send commits to GitHub |
| ⬇ Pull | `git pull` — download commits from GitHub |
| 📡 Fetch | `git fetch --prune` — refresh remote-branch info without merging. Use before Switch Branch… to see branches created elsewhere |
| 📝 Commit… | Per-file staging dialog (see below) |
| ↩ Undo Last Commit | `git reset --soft HEAD~1` — remove last commit, keep changes |
| 🌿 New Branch | Create a branch with optional immediate switch |
| 🔀 Switch Branch… | Switch to a local or remote-tracking branch. Remote-only branches appear below a `── remote ──` separator; picking one auto-creates a local tracking copy |
| ⇄ Merge… | Merge another branch INTO the current one (use after switching to master to pull a finished feature back in). Lists both local and remote-tracking branches (remote picks merge via `origin/<name>`). Handles conflict + dirty-tree errors with inline instructions |
| 🗑 Delete Branch… | Delete a non-current branch (safe by default; offers force if unmerged). After local delete, prompts to also delete `origin/<branch>` if it exists on GitHub |
| 🔗 Open PR | Open GitHub's compare page for the current branch; or explains branch workflow if on main |
| 🐙 Merge PR… | Lists open PRs via `gh pr list --json`. Pick one + a merge strategy (Merge / Squash / Rebase) + optional delete-source-branch. Single confirmation modal shows title, source/base branches, diff stats. On confirm: `gh pr merge <N> --merge\|--squash\|--rebase`, then auto-switches local to the default branch and pulls so you end up sitting on the freshly-merged state |
| 📦 Release… | One-button GitHub release (see Release Wizard above) |
| Draft PR… | Ask AI to draft a structured PR description. Right-click or Shift+click to choose Claude CLI (external terminal, uses your subscription) or **Ollama / API** (inline dialog, free with local models). Right-click also exposes **"Set PR base branch…"** to target any branch instead of auto-detected master/main |
| 🧪 Test Gaps… | Diff this branch against its base and show which changed `src/*.py` files have no `tests/test_*.py`. One-click **template stubs** or **AI-written tests** (Auto / Claude CLI / Ollama) for the flagged files. AI tests are generated → run → repaired, then **re-verified against the full `pytest -m "not tk"` suite and rolled back if they break it** (only gate-safe tests are kept; passing-but-partial files show `✓ N/M`). Or **📋 Copy a Claude Code prompt** to hand the gaps to an agentic session and **↻ Re-scan** to pick up what it writes. Works without drafting a PR first |

Every button has a hover tooltip with a plain-English explanation.

**Commit dialog detail:**

When you click 📝 Commit…, each changed file appears as a row:

```
[✓]  M  modified    src/main.py
[✓]  A  added       new_file.txt
[ ]  ?  untracked   scratch.txt
     [Select All]  [Select None]  [Modified Only]

Commit message:
[ feat: add new_file.txt, update main.py      ]  [💡 Suggest]

[Commit]  [Cancel]
```

- Tick exactly the files you want in this commit; unticked files stay as working-tree changes for a later commit
- **💡 Suggest** generates a conventional-commit message based on *only the ticked files* (`feat:`, `fix:`, `docs:`, `chore:`, etc.)
- Select All / Select None / Modified Only are quick-pick shortcuts

### 🤖 Ask Tab

Stage 2 of the agentic-AI roadmap (see `docs/AGENT_ARCHITECTURE.md` and `docs/ROADMAP.md`). A chat interface where a local LLM uses read-only tools to answer questions about the selected project.

```
🤖 Ask — MyProject               [provider: ollama / qwen2.5-coder:14b]
─────────────────────────────────────────────────────────────────────
│  🤖  Ready. Ask anything about the selected project…              │
│                                                                   │
│  👤  Where is the commit-message generator?                       │
│  🔧  tokensave_search({"query": "commit message generator"})      │
│      _suggest_commit_message (function) - helpers/commit...       │
│  🔧  read_file({"path": "src/helpers/commit_messages.py",         │
│                  "start_line": 1, "end_line": 200})               │
│      <body of _suggest_commit_message with line numbers>          │
│  🤖  The commit-message generator lives in                        │
│      _suggest_commit_message at src/helpers/commit_messages.py.   │
│      It runs a strategy chain: LLM → CHANGELOG bullets → diff…    │
├───────────────────────────────────────────────────────────────────┤
│ [type your question here…                          ] [Send] [Stop]│
│ [Clear history]                                                   │
```

**Tools the agent has access to** (all read-only):

| Tool | Purpose |
|------|---------|
| `read_file` | Read a file's contents. Supports `start_line` / `end_line` for files larger than 50 KB. When a path isn't found, the error suggests likely candidates by basename. |
| `list_directory` | List entries with `/` suffix on subdirs. Skips noise (`__pycache__`, `node_modules`, hidden dirs). |
| `git_log` | `git log --oneline -n N` (default 20). |
| `git_diff` | `git diff HEAD [-- path]`. Capped at 24 KB. |
| `tokensave_search` | Runs `tokensave query <q>` — finds DEFINED SYMBOLS by name (functions, classes, methods, constants). NOT a full-text grep. |
| `tokensave_context` | Runs `tokensave context --format json --max-nodes 10 <task>`. Result is slimmed (per-node metadata trimmed to name/kind/qualified_name/file_path/start_line/end_line/signature/parent_id) so the model can reason about it without burning tokens. |

**Provider support**:
- **Ollama / OpenAI / OpenAI-compatible** (LM Studio, vLLM, llama.cpp) — full tool-calling. The agent passes `options.num_ctx=32768` to Ollama specifically (Ollama's default is 2048, easily blown after a few tool iterations).
- **Anthropic** — falls back to a one-shot completion without tools and surfaces a hint. Adding native Anthropic tool-use is a follow-up.

**Tool-call rescue**: local models (qwen2.5-coder especially) sometimes emit tool calls as JSON text in the assistant content field instead of using the structured `tool_calls` array. The agent detects this pattern (four shapes: `{"name": ..., "arguments": ...}`, `{"tool": ...}`, `{"name": ..., "parameters": ...}`, `{"function": {...}}`, with `\`\`\`json` fence stripping and balanced-brace substring scanning) and synthesises a proper tool call from the embedded JSON.

**Error reporting**: any LLM HTTP failure surfaces the actual response body (HTTPError bodies, network reasons, JSON decode failures) — no more generic "LLM request failed" messages.

Recommended starter prompts:
- "How is this project structured? What are the major components?"
- "Where is the commit-message generator? How does it decide what message to suggest?"
- "What did I change in the last 10 commits?"
- "Why might `_call_llm` fail silently? What are all the failure modes?"

### Reference Tab

- **CLI cheatsheet** — every common `tokensave` command with flags, formatted for copy-paste
- **Prompt snippets** — 12 built-in Claude Code prompts: codebase overview, symbol search, impact analysis, dead-code scan, TODO list, changelog generation, health check, etc.
- Copy any snippet to the clipboard with one click, or **Add** your own custom snippets that persist in `manager-config.json`

### Help Tab

A scrollable guide covering:
- How to switch active projects
- Button reference for every toolbar and context menu item
- Project categories and how to set them up
- The full git workflow — what branches are, when to use them, step-by-step PR creation
- GitHub Setup walkthrough
- File locations

---

## Right-Click Menu

Right-click any project row in the Projects tab to get the full per-project action menu:

| Menu Item | What it does |
|-----------|-------------|
| ★ Set as Active | Pin this project for Claude Desktop |
| ↺ Sync | Run `tokensave sync` |
| 📊 Status | Show `tokensave status` output |
| ⟳ Force Re-sync | Run `tokensave sync --force` |
| 🔍 Doctor | Run `tokensave doctor` |
| 📜 Git Log | Switch to Git tab and refresh |
| 📝 Git Commit… | Open commit dialog for this project |
| 🔧 Git Init | Initialise a git repo + write baseline `.gitignore` + optional initial commit |
| 🧠 CodeGraph Init | Initialise CodeGraph + build the initial graph (`codegraph init --index`) |
| 🧠 CodeGraph Sync | Incremental update of CodeGraph's index for the project |
| 🧠 CodeGraph Status | Show CodeGraph stats (file count, node count, backend) |
| 🧠 Remove CodeGraph Index | Delete `.codegraph/` — source untouched, re-initialise via Init |
| 📋 Manage .gitignore… | Open the gitignore editor — view current entries, inject template patterns (Python / Node / Rust / IDE / OS / Nuitka / etc.), add custom entries, or remove existing ones |
| 🧹 Untrack Ignored Files… | Find files that are tracked by git but also match a `.gitignore` rule (the "stale tracking" problem), select which to untrack via `git rm --cached`. Local files are preserved — only git's index is updated |
| 📂 Open Folder | Open in Windows Explorer |
| ✏ Open in Editor | Launch in configured editor |
| ⎘ Copy Path | Copy full project path to clipboard |
| ⚙ Retrofit… | Add tokensave rules / BASIC_INSTRUCTIONS / Nuitka / git hook |
| 🔗 Shadow Links… | Generate NTFS hardlinks with a secondary extension |
| 📁 Assign Category… | Move project to a different category or sub-category |
| 🗑 Remove Index… | Delete `.tokensave/` and remove project from the list (project files untouched) |
| Auto-detect | Switch from manual pin back to automatic project detection |
| 📝 Draft CHANGELOG entry… | Ask the LLM to draft [Unreleased] bullets from commits since the last release tag — reviews in a proposal dialog before any file write |
| 🔬 Refactor scout… | Surface code-health findings (complexity, god classes, dead code) via deterministic SQL against the tokensave DB — LLM only enters if you click Investigate on a card |
| ✓ Run checks… | Run four pre-merge quality checks concurrently (Python syntax, pyflakes, Doctor audit, optional Claude Code review). All four are individually toggleable; Claude review is off by default (uses API tokens). Results appear live as each check finishes |

---

## Git Workflow — Step by Step

If you're new to git, here's the pattern the manager is designed around:

### Setting up a project for GitHub (once per project)

1. Right-click the project → **🔧 Git Init** — creates the git repo and a proper `.gitignore`
2. Click **Yes** when asked about the initial commit — stages and commits everything
3. Go to [github.com/new](https://github.com/new) and create an empty repository (no README, no .gitignore)
4. In the Git tab, click **🐙 GitHub…** → follow the wizard, or click **Set Remote** and paste the URL
5. Click **⬆ Push** — your project is now on GitHub

### Daily workflow (making changes)

1. **🌿 New Branch** — create a branch for your changes (e.g. `feature/new-button`)
   - Changes on this branch don't affect `master` until you're ready
2. Make your edits in your editor
3. **📝 Commit…** — tick the files you changed, write a message, commit
   - Repeat steps 2–3 as many times as needed
4. **⬆ Push** — send your branch to GitHub (optional — only if you want a backup or to share)
5. **🔀 Switch Branch…** → `master`
6. **⬇ Pull** — pick up any new master commits before merging
7. **⇄ Merge…** → pick your feature branch — confirmation reads "Merge X INTO master?"
8. **⬆ Push** — master with the merged commits goes to GitHub
9. **🗑 Delete Branch…** → pick your feature branch
   - Yes to local delete → Yes to "Also delete from GitHub?"

Steps 5–9 are the full **finish-a-branch** flow. If you prefer GitHub's PR review UI for collaborators, do steps 1–4 then use **🔗 Open PR** at step 5 instead, and merge from GitHub's web interface — then come back and run the delete step.

### Quick solo workflow (no PR needed)

For a project only you work on, you can skip branches entirely:
1. Make changes
2. **📝 Commit…** — commit what you want
3. **⬆ Push** — done

---

## Configuration Reference

All settings live in `manager-config.json` at the project root (not committed to git). Edit via the **Settings** dialog or directly in a text editor.

```json
{
  "tokensave_exe":          "C:/Tools/tokensave.exe",
  "template_dir":           "",
  "python_exe":             "C:/Python312/pythonw.exe",
  "editor_cmd":             "code",
  "git_exe":                "",
  "codegraph_exe":          "",
  "search_roots": [
    {"path": "D:/My Projects",  "label": "My Projects"},
    {"path": "D:/Work",         "label": "Work"},
    "D:/Old Projects"
  ],
  "project_categories": {
    "D:/My Projects/SomeApp": {"category": "Apps", "subcategory": "Tools"}
  },
  "user_snippets": [
    {"title": "My snippet", "text": "Ask Claude to..."}
  ],
  "auto_commit_after_sync": false
}
```

| Key | Required | Description |
|-----|----------|-------------|
| `tokensave_exe` | Yes | Absolute path to `tokensave.exe` |
| `template_dir` | No | Path to the `templates/` folder. Leave blank to auto-detect as `<exe-dir>\templates\` |
| `python_exe` | Source only | Path to `pythonw.exe`, used by the `.bat` launcher |
| `editor_cmd` | No | Editor launch command. Supports flags: `code`, `code --new-window`, `notepad` |
| `git_exe` | No | Path to `git.exe`. Leave blank to auto-detect via PATH |
| `codegraph_exe` | No | Path to the CodeGraph CLI (usually `codegraph.cmd` in `%APPDATA%\npm\`). Leave blank to auto-detect. Empty when CodeGraph isn't installed — all CodeGraph features show a friendly "install via npm" nudge in that case |
| `search_roots` | Yes | Folders to scan. Bare strings or `{"path": "...", "label": "..."}` dicts. Each label becomes a category header |
| `project_categories` | No | Per-project category overrides — managed via the right-click menu |
| `user_snippets` | No | Custom prompt snippets shown in the Reference tab |
| `auto_commit_after_sync` | No | If `true`, auto-commits after every successful sync (default: `false`) |

---

## Building from Source

Prerequisites:
```powershell
pip install nuitka ordered-set zstandard pillow pystray
```

Build:
```powershell
.\build.bat
```

Or directly:
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

**Output** (`dist\`):
```
dist\
  tokensave-manager.exe       — main GUI (standalone, no Python needed)
  tokensave-wrapper.exe       — MCP wrapper for Claude Desktop
  manager-config.json         — clean config for new users
  manager-config.example.json — annotated template
  TOKENSAVE_GUIDE.md
  CHANGELOG.md
  templates\                  — all template files
  docs\                       — GITHUB_GUIDE.md, ARCHITECTURE.md, ARCHITECTURE_TOKENSAVE.md
```

Place an `icon.ico` (256×256) next to `build.ps1` to bake a custom icon into the exe.

**Notes:**
- `build.ps1` uses `--nofollow-import-to=numpy,scipy,pandas,...` to skip Anaconda packages — drops the uncompressed payload from ~500 MB to ~55 MB
- The payload size check in `build.ps1` reads the uncompressed size from Nuitka's output (not the compressed exe size) to avoid false-positive warnings
- See `templates/NUITKA_GOTCHAS.md` for known Nuitka pitfalls

---

## Project Structure

```
Token Save Manager Source/
├── manager-config.json            Machine-specific config (gitignored)
├── manager-config.example.json    Template config with placeholder paths
├── Launch TokenSave Manager.bat   Dev launcher (reads python_exe from config)
├── build.ps1                      Nuitka compile pipeline
├── build.bat                      Double-click build launcher
├── BASIC_INSTRUCTIONS.md          Claude session instructions for this project
├── CHANGELOG.md                   Feature history
├── TOKENSAVE_GUIDE.md             Full tokensave CLI + MCP reference
│
├── src/                           Post-Round-4 layout: App + main() in app.py,
│   │                              everything else in subpackages.
│   ├── app.py                     Entry point — App(tk.Tk) + main()
│   ├── state.py                   ManagerConfig dataclass (runtime-mutable settings)
│   ├── constants.py               Immutable constants (palette, regex, paths)
│   ├── theme.py                   _Tooltip widget
│   ├── tokensave-wrapper.py       Claude Desktop MCP wrapper (~120 lines —
│   │                              MUST stay single-threaded and pass
│   │                              sys.stdin/stdout/stderr explicitly to
│   │                              Popen; see MCP_INTEGRATION_GOTCHAS.md)
│   ├── agent.py                   LocalAgent loop for the 🤖 Ask tab
│   │                              (~600 lines, Stage 2)
│   ├── agent_tools.py             ToolSpec registry: read_file,
│   │                              list_directory, git_log, git_diff,
│   │                              tokensave_search, tokensave_context
│   │                              (~500 lines, Stage 2)
│   ├── helpers/                   12 modules of pure / IO helpers (no UI)
│   ├── dialogs/                   18 tk.Toplevel dialog classes (one per file)
│   └── controllers/               4 tab controllers (Projects/Git/Ask/Snippets)
│
├── templates/
│   ├── claude-md-template.md      BASIC_INSTRUCTIONS template for new projects
│   ├── project-baseline.md        Universal rules @included by retrofitted projects
│   ├── nuitka-build.ps1.template  Nuitka build script template
│   ├── nuitka-build.py.template   Python-based alternative build script
│   ├── nuitka-build.bat.template  Bat launcher template
│   └── NUITKA_GOTCHAS.md          Nuitka pitfalls reference (14 known issues)
│
└── docs/
    ├── ARCHITECTURE.md              Class structure, UI layout, threading model
    ├── ARCHITECTURE_TOKENSAVE.md    tokensave internals reference
    ├── AGENT_ARCHITECTURE.md        LocalAgent loop + tool registry + propose-only rules
    ├── ROADMAP.md                   Staged plan for local AI features (Stages 0–8)
    ├── MCP_INTEGRATION_GOTCHAS.md   Field manual: UWP path redirection, stdio bugs,
    │                                Connectors UI vs legacy config, live-reload paths,
    │                                tokensave install hook-quoting upstream bug
    ├── GITHUB_GUIDE.md              Beginner GitHub guide
    └── upstream-issues/             Drafts / resolved bugs against upstream tools
        ├── tokensave-hook-quoting.md        [FIXED v6.0.0 #81]
        ├── tokensave-health-details.md      [FIXED v6.0.0 #82]
        ├── tokensave-redundancy-tool.md     [SHIPPED v6.0.0 #83]
        ├── tokensave-daemon-stop-windows.md [MOOT v6.0.0]
        ├── tokensave-daemon-child-no-window.md [MOOT v6.0.0]
        └── tokensave-daemon-status-autostart.md [MOOT v6.0.0]
```

---

## Roadmap

The manager is growing into a **propose-only local AI assistant** for project maintenance — code review, CHANGELOG drafting, dead-code scouting, whole-project Q&A. Every AI suggestion waits for explicit user approval; nothing auto-applies.

**Status:** Stages 0–2 shipped. Stage 0 = smart commit messages. Stage 1 = AI Code Review with streaming. Stage 2 = 🤖 Ask tab with tool-calling agent against your own code.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full staged plan (Stages 0–8), model recommendations (Ollama with `qwen2.5-coder:14b`, `qwen2.5:14b`, or Anthropic Claude Haiku), and the locked architectural rules. [docs/AGENT_ARCHITECTURE.md](docs/AGENT_ARCHITECTURE.md) has the technical detail on the Stage 2 agent loop, tool registry, and the propose-only design. [docs/MCP_INTEGRATION_GOTCHAS.md](docs/MCP_INTEGRATION_GOTCHAS.md) is the field manual for everything we learned debugging the wrapper + MCP integration the hard way.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full history.

**Recent highlights (Unreleased)** — see CHANGELOG.md for the detailed bullets on each:



**Refactoring**
- Helper extraction across doc-drafter, agent, gitignore editor, and projects tab to reduce method complexity

(Add this as a new sub-section in the "Recent highlights (Unreleased)" block. The existing sections can remain unchanged.)

**Doc-drafter refinements**
- 📝 Doc Updates… — refined bullet filtering with noop-removal and quality-based truncation, post-apply state preview for honest diffs, literal template/placeholder detection with mirror-contract safety validation, per-session backend override for flexible model selection, improved README deduplication and draft sanitization
- ⚙ Backend override dropdown (`_backend_override_var` in `dialogs/doc_drafter.py`)
- 🛠 Narrowed doc pathspec to prevent masking code commits (`doc_drafter.py`)
**Roadmap-6 — Ask tab + gitignore AI + doc-drafter**
- 🤖 Ask tab — separate `ask_tab_llm` config (independent of commit-message model), Claude CLI provider option, SSE streaming for final-turn tokens, session log persistence (`logs/ask_sessions.md`)
- 🤖 Gitignore AI Suggest — one-click AI-powered pattern recommendations in the .gitignore editor; CodeGraph SQLite used as a zero-cost project file listing when available; path-scoped basename dedup suppresses redundant `src/__pycache__/` style suggestions when the broader pattern is already ignored
- 📝 Doc Updates… right-click dialog — drafts CHANGELOG `[Unreleased]` bullets AND README "Recent highlights" sub-section content from a commit range via the configured local AI. Per-tab thread isolation, ProposalBridge-gated Apply, mixed-commit boundary handling, sparse-commit safety net. Both CHANGELOG and README use append-only insertion (`insert_unreleased_bullets` / `insert_readme_highlights_subsection`) — drafter generates only new content; patcher splices into the right sub-section while preserving everything else verbatim. Robust against small-model truncation that wiped detail on the original full-block-regeneration design. Architecture + Memory tabs deferred to Roadmap-7
- 📝 Documentation snippet category in Reference tab — 7 curated copy-paste prompts for README / CHANGELOG / architecture / memory / consistency-check / migration-note / PR description
- Help tab — comprehensive static reference content with per-section follow-up ask prompts
- tokensave CLI subcommand fix (`tokensave tool search` / `tokensave tool context` — old `query` / `context` subcommands removed upstream)

**Major: local-AI integration (Stages 0–2 of the roadmap)**
- 🤖 Ask tab — Stage 2 chat interface with bounded tool-calling agent (`src/agent.py` + `src/agent_tools.py`)
- 🔍 AI Code Review with token-by-token streaming
- 🦙 Ollama Model Manager dialog with native REST API integration
- 🔄 Upgrade tokensave from the manager, with hourly GitHub releases polling
- Tool-call rescue for local models that emit calls as JSON-in-content
- read_file with `start_line`/`end_line` for large files + basename-match suggestions on not-found errors

**MCP / wrapper hardening**
- 🔌 MCP Integration configurator (UWP-aware, label-aware classification — recognises `tokensave install` canonical shape for Claude Code)
- Wrapper stdio fix: explicit `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` in Popen (the root cause of the 30-second MCP attach timeout)
- 🔍 Doctor button with stale-entry purge offer + cmd.exe spawn fallback for TTY-gated prompts
- Last Synced column now reads max mtime across `.db`/`.db-wal`/`.db-shm` (SQLite WAL-mode aware)

**Git workflow**
- 🐙 Merge PR button — full GitHub PR merge from the manager (lists PRs via `gh pr list`, three strategies, auto-syncs local after)
- ⇄ Merge button — merge a branch INTO the current one
- Remote-aware Delete Branch — prompts to also delete `origin/<branch>`
- `[project-name]` prefix on all git log lines

**Docs**
- `docs/AGENT_ARCHITECTURE.md` — agent loop design
- `docs/ROADMAP.md` — staged plan with status badges
- `docs/MCP_INTEGRATION_GOTCHAS.md` — postmortem field manual
- `docs/upstream-issues/tokensave-hook-quoting.md` — draft of an upstream bug we discovered

**Earlier highlights** (these landed before the current cycle):
- Git tab with full push/pull/commit/branch/diff UI, per-file staging, conventional-commit auto-suggest
- GitHub Setup wizard, Open PR button, Release Wizard with `gh release create` pipeline
- Project categories + sub-categories, gitignore editor with template inject + diff preview
- Ensure .gitignore, Auto-commit after sync, Claude session Stop hook

**Doc-drafter refinements**

**Roadmap-6 — Ask tab + gitignore AI + doc-drafter**

**Major: local-AI integration (Stages 0–2 of the roadmap)**

**MCP / wrapper hardening**

**Git workflow**

**Docs**

**Earlier highlights** (these landed before the current cycle):
**Doc-drafter refinements**
- 📝 Doc Updates… — refined bullet filtering with noop-removal and quality-based truncation, post-apply state preview for honest diffs, literal template/placeholder detection with mirror-contract safety validation, per-session backend override for flexible model selection, improved README deduplication and draft sanitization

**Roadmap-6 — Ask tab + gitignore AI + doc-drafter**
- 🤖 Ask tab — separate `ask_tab_llm` config (independent of commit-message model), Claude CLI provider option, SSE streaming for final-turn tokens, session log persistence (`logs/ask_sessions.md`)
- 🤖 Gitignore AI Suggest — one-click AI-powered pattern recommendations in the .gitignore editor; CodeGraph SQLite used as a zero-cost project file listing when available; path-scoped basename dedup suppresses redundant `src/__pycache__/` style suggestions when the broader pattern is already ignored
- 📝 Doc Updates… right-click dialog — drafts CHANGELOG `[Unreleased]` bullets AND README "Recent highlights" sub-section content from a commit range via the configured local AI. Per-tab thread isolation, ProposalBridge-gated Apply, mixed-commit boundary handling, sparse-commit safety net. Both CHANGELOG and README use append-only insertion (`insert_unreleased_bullets` / `insert_readme_highlights_subsection`) — drafter generates only new content; patcher splices into the right sub-section while preserving everything else verbatim. Robust against small-model truncation that wiped detail on the original full-block-regeneration design. Architecture + Memory tabs deferred to Roadmap-7
- 📝 Documentation snippet category in Reference tab — 7 curated copy-paste prompts for README / CHANGELOG / architecture / memory / consistency-check / migration-note / PR description
- Help tab — comprehensive static reference content with per-section follow-up ask prompts
- tokensave CLI subcommand fix (`tokensave tool search` / `tokensave tool context` — old `query` / `context` subcommands removed upstream)

**Major: local-AI integration (Stages 0–2 of the roadmap)**
- 🤖 Ask tab — Stage 2 chat interface with bounded tool-calling agent (`src/agent.py` + `src/agent_tools.py`)
- 🔍 AI Code Review with token-by-token streaming
- 🦙 Ollama Model Manager dialog with native REST API integration
- 🔄 Upgrade tokensave from the manager, with hourly GitHub releases polling
- Tool-call rescue for local models that emit calls as JSON-in-content
- read_file with `start_line`/`end_line` for large files + basename-match suggestions on not-found errors

**MCP / wrapper hardening**
- 🔌 MCP Integration configurator (UWP-aware, label-aware classification — recognises `tokensave install` canonical shape for Claude Code)
- Wrapper stdio fix: explicit `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` in Popen (the root cause of the 30-second MCP attach timeout)
- 🔍 Doctor button with stale-entry purge offer + cmd.exe spawn fallback for TTY-gated prompts
- Last Synced column now reads max mtime across `.db`/`.db-wal`/`.db-shm` (SQLite WAL-mode aware)

**Git workflow**
- 🐙 Merge PR button — full GitHub PR merge from the manager (lists PRs via `gh pr list`, three strategies, auto-syncs local after)
- ⇄ Merge button — merge a branch INTO the current one
- Remote-aware Delete Branch — prompts to also delete `origin/<branch>`
- `[project-name]` prefix on all git log lines

**Docs**
- `docs/AGENT_ARCHITECTURE.md` — agent loop design
- `docs/ROADMAP.md` — staged plan with status badges
- `docs/MCP_INTEGRATION_GOTCHAS.md` — postmortem field manual
- `docs/upstream-issues/tokensave-hook-quoting.md` — draft of an upstream bug we discovered

**Earlier highlights** (these landed before the current cycle):
- Git tab with full push/pull/commit/branch/diff UI, per-file staging, conventional-commit auto-suggest
- GitHub Setup wizard, Open PR button, Release Wizard with `gh release create` pipeline
- Project categories + sub-categories, gitignore editor with template inject + diff preview
- Ensure .gitignore, Auto-commit after sync, Claude session Stop hook

**Doc-drafter refinements**
- 📝 Doc Updates… — refined bullet filtering with noop-removal and quality-based truncation, post-apply state preview for honest diffs, literal template/placeholder detection with mirror-contract safety validation, per-session backend override for flexible model selection, improved README deduplication and draft sanitization

**Roadmap-6 — Ask tab + gitignore AI + doc-drafter**
- 🤖 Ask tab — separate `ask_tab_llm` config (independent of commit-message model), Claude CLI provider option, SSE streaming for final-turn tokens, session log persistence (`logs/ask_sessions.md`)
- 🤖 Gitignore AI Suggest — one-click AI-powered pattern recommendations in the .gitignore editor; CodeGraph SQLite used as a zero-cost project file listing when available; path-scoped basename dedup suppresses redundant `src/__pycache__/` style suggestions when the broader pattern is already ignored
- 📝 Doc Updates… right-click dialog — drafts CHANGELOG `[Unreleased]` bullets AND README "Recent highlights" sub-section content from a commit range via the configured local AI. Per-tab thread isolation, ProposalBridge-gated Apply, mixed-commit boundary handling, sparse-commit safety net. Both CHANGELOG and README use append-only insertion (`insert_unreleased_bullets` / `insert_readme_highlights_subsection`) — drafter generates only new content; patcher splices into the right sub-section while preserving everything else verbatim. Robust against small-model truncation that wiped detail on the original full-block-regeneration design. Architecture + Memory tabs deferred to Roadmap-7
- 📝 Documentation snippet category in Reference tab — 7 curated copy-paste prompts for README / CHANGELOG / architecture / memory / consistency-check / migration-note / PR description
- Help tab — comprehensive static reference content with per-section follow-up ask prompts
- tokensave CLI subcommand fix (`tokensave tool search` / `tokensave tool context` — old `query` / `context` subcommands removed upstream)

**Major: local-AI integration (Stages 0–2 of the roadmap)**
- 🤖 Ask tab — Stage 2 chat interface with bounded tool-calling agent (`src/agent.py` + `src/agent_tools.py`)
- 🔍 AI Code Review with token-by-token streaming
- 🦙 Ollama Model Manager dialog with native REST API integration
- 🔄 Upgrade tokensave from the manager, with hourly GitHub releases polling
- Tool-call rescue for local models that emit calls as JSON-in-content
- read_file with `start_line`/`end_line` for large files + basename-match suggestions on not-found errors

**MCP / wrapper hardening**
- 🔌 MCP Integration configurator (UWP-aware, label-aware classification — recognises `tokensave install` canonical shape for Claude Code)
- Wrapper stdio fix: explicit `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` in Popen (the root cause of the 30-second MCP attach timeout)
- 🔍 Doctor button with stale-entry purge offer + cmd.exe spawn fallback for TTY-gated prompts
- Last Synced column now reads max mtime across `.db`/`.db-wal`/`.db-shm` (SQLite WAL-mode aware)

**Git workflow**
- 🐙 Merge PR button — full GitHub PR merge from the manager (lists PRs via `gh pr list`, three strategies, auto-syncs local after)
- ⇄ Merge button — merge a branch INTO the current one
- Remote-aware Delete Branch — prompts to also delete `origin/<branch>`
- `[project-name]` prefix on all git log lines

**Docs**
- `docs/AGENT_ARCHITECTURE.md` — agent loop design
- `docs/ROADMAP.md` — staged plan with status badges
- `docs/MCP_INTEGRATION_GOTCHAS.md` — postmortem field manual
- `docs/upstream-issues/tokensave-hook-quoting.md` — draft of an upstream bug we discovered

**Earlier highlights** (these landed before the current cycle):
- Git tab with full push/pull/commit/branch/diff UI, per-file staging, conventional-commit auto-suggest
- GitHub Setup wizard, Open PR button, Release Wizard with `gh release create` pipeline
- Project categories + sub-categories, gitignore editor with template inject + diff preview
- Ensure .gitignore, Auto-commit after sync, Claude session Stop hook

---

## Testing

The manager ships with a pytest test suite covering pure-logic helpers, the AI
test-generation / PR-draft pipeline, and the Tk dialogs + controllers. Tests are
zero-runtime-dependency for end users — only the dev workflow installs pytest.

```bash
pip install -r requirements-dev.txt
python -m pytest                    # everything
python -m pytest -m "not tk"        # pure-logic only — no display needed (~10 s)
python -m pytest -m tk              # dialog/controller tests only (needs a display)
```

Tk-marked tests need a display: on Windows they run as-is; on Linux CI they run
under `xvfb-run`. The CI gate is `pytest -m "not tk"` (the same gate the Test Gaps
AI generator re-verifies generated tests against before keeping them).

On Linux CI the dialog tests run under `xvfb-run -a` with `python3-tk`
installed. The GitHub Actions workflow (`.github/workflows/ci.yml`) splits
the test runs into three gating tiers:

- `test-warn`: pushes to `Roadmap-*` branches → warn-only
- `test-gate`: PRs to `main`/`master` → HARD gate (failing tests block the merge)
- `test-postmerge`: pushes to `main`/`master` → HARD gate (catches bad merges)

### Test Manager dialog (v4.13)

The Help tab's **🧪 Test Manager…** button opens a 4-tab dialog that
covers the full test lifecycle for novice users:

- **Run + View**: per-file last-run status, ▶ Run All / Run Selected /
  🛑 Stop buttons, and 🔁 Sync PR Checklist (writes back to the open
  PR via `gh api ... --input -`).
- **Coverage Gaps**: lists `src/` files without matching tests; one
  click takes you to the Scaffold tab to generate a starter test file.
- **Stale Tests**: AST-based scanner flags tests that import deleted
  modules or non-existent symbols; "Mark as still valid" silences
  false positives.
- **Scaffold**: pick a source file + template kind, preview the
  generated test, click Generate. Generated files pass pytest
  immediately so you get green feedback before customising.

See `memory/tests_pattern.md` for the fixture catalogue + Tk gotchas
(G-A through G-K) when writing new tests, and `memory/test_manager.md`
for the v4.13 decision tree ("when should I add tests?").

---

## Credits

Created by **Alexander L Corthell**

Built with Python, tkinter, [Catppuccin Mocha](https://github.com/catppuccin/catppuccin), and [tokensave](https://github.com/aovestdipaperino/tokensave).
