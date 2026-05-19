# TokenSave — Complete Guide

> **What is TokenSave?**  
> A local code-intelligence tool that builds a graph of your codebase (symbols, call relationships, types, files) and exposes it to AI agents via 48 MCP tools. Instead of Claude reading raw source files on every message, it queries the pre-built graph — dramatically cutting token usage on large projects while giving Claude *better* structural understanding than file reads alone.

---

## Table of Contents

1. [Installation](#installation)
2. [First-Time Setup for a Project](#first-time-setup)
3. [Daily Workflow](#daily-workflow)
4. [CLI Reference](#cli-reference)
5. [MCP Tools Reference](#mcp-tools-reference)
6. [Agent Integration](#agent-integration)
7. [Background Daemon](#background-daemon)
8. [Multi-Branch Workflow](#multi-branch-workflow)
9. [Cost & Token Tracking](#cost--token-tracking)
10. [Troubleshooting](#troubleshooting)
11. [Environment Variables](#environment-variables)
12. [Claude Prompt Recipes](#claude-prompt-recipes)

---

## Installation

### Windows (Scoop)
```powershell
scoop bucket add tokensave https://github.com/aovestdipaperino/scoop-bucket
scoop install tokensave
```

### macOS (Homebrew)
```bash
brew install aovestdipaperino/tap/tokensave
```

### From Source (Cargo)
```bash
# Full build — 34 languages
cargo install tokensave

# Medium build — 20 languages
cargo install tokensave --features medium

# Lite build — 11 languages
cargo install tokensave --no-default-features
```

### Supported Languages (Full Build)
Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Elixir, Erlang, Clojure, Haskell, OCaml, F#, Nim, Zig, Odin, Carbon, D, Lua, Julia, R, MATLAB, Groovy, Gradle, Maven, SQL

---

## First-Time Setup

### 1. Index your project
Navigate to your project folder and run:
```powershell
tokensave init
# or from anywhere:
tokensave init "D:\My Projects\MyApp"
```
This builds the code graph. May take a few minutes for large codebases. Creates a `.tokensave/` folder in the project root.

### 2. Wire up your AI agent
```powershell
tokensave install --agent claude    # Claude Code
tokensave install --agent cursor    # Cursor
tokensave install                   # Auto-detect all installed agents
```
This writes the MCP server config so your agent finds tokensave automatically.

### 3. Verify it's working
```powershell
tokensave doctor
tokensave status
```

---

## Daily Workflow

```
Code changes made
      ↓
tokensave sync          ← incremental update (fast — only changed files)
      ↓
Open Claude / restart agent session
      ↓
Claude uses tokensave MCP tools automatically
```

**Key rule:** Run `tokensave sync` after meaningful code changes so Claude sees the current state. You don't need to sync after every save — once before a Claude session is enough.

**Auto-sync alternative:** Run `tokensave daemon` in the background and it watches for file changes and syncs automatically. See [Background Daemon](#background-daemon).

---

## CLI Reference

### Indexing

| Command | Description |
|---------|-------------|
| `tokensave init [path]` | First-time index of a project. Run once per project. |
| `tokensave sync [path]` | Incremental update — only re-indexes changed files. Fast. |
| `tokensave sync --force [path]` | Full rebuild of the code graph from scratch. |
| `tokensave sync --doctor [path]` | Sync and print a list of what changed. |

### Status & Inspection

| Command | Description |
|---------|-------------|
| `tokensave status [path]` | Show indexed file count, symbol count, estimated token savings. |
| `tokensave status --details` | Same, plus breakdown by symbol kind (functions, classes, etc.). |
| `tokensave status --json` | Machine-readable JSON output. |
| `tokensave files [path]` | List all indexed files. |
| `tokensave files --filter <dir>` | Limit to a subdirectory. |
| `tokensave files --pattern <glob>` | Filter by glob pattern. |
| `tokensave files --json` | JSON output. |
| `tokensave query <search> [path]` | Search symbols by name (same as `tokensave_search` MCP tool). |
| `tokensave monitor` | Live TUI dashboard showing every MCP tool call in real time. |

### Health & Maintenance

| Command | Description |
|---------|-------------|
| `tokensave doctor [--agent NAME]` | Health check — verifies installation and agent wiring. |
| `tokensave upgrade` | Self-update to the latest version. |
| `tokensave channel [stable\|beta]` | Show or switch update channel. |

### Agent Setup

| Command | Description |
|---------|-------------|
| `tokensave install` | Auto-detect installed agents and configure them all. |
| `tokensave install --agent claude` | Configure Claude Code only. |
| `tokensave install --agent cursor` | Configure Cursor only. |
| `tokensave reinstall` | Refresh settings for all already-configured agents. |
| `tokensave uninstall [--agent NAME]` | Remove agent integration. |

**Supported agents:** claude, cursor, cline, copilot, gemini, opencode, codex, roo-code, antigravity, zed, kilo, kimi, vibe

### MCP Server (manual)

| Command | Description |
|---------|-------------|
| `tokensave serve` | Start the MCP server (agents do this automatically). |
| `tokensave serve -p <path>` | Serve a specific project by path. |

### Affected Files

| Command | Description |
|---------|-------------|
| `tokensave affected <files...>` | Find test files affected by changes to the given source files. |
| `tokensave affected --stdin` | Read file list from stdin. |
| `tokensave affected --depth N` | Limit recursion depth. |

---

## MCP Tools Reference

These are the 48 tools Claude uses automatically when tokensave is active. You can reference them by name in prompts to guide Claude toward specific analyses.

### Discovery & Navigation

| Tool | What it does |
|------|-------------|
| `tokensave_context` | **Start here.** Natural-language query → relevant symbols, relationships, and code snippets. The main workhorse. |
| `tokensave_search` | Search symbols by name. Returns matching functions, classes, variables. |
| `tokensave_node` | Get full details about a specific symbol (type, location, docstring). |
| `tokensave_files` | List indexed files, optionally filtered. |
| `tokensave_module_api` | Show the public API of a module or file — what it exports. |
| `tokensave_by_qualified_name` | Look up a symbol by its fully-qualified name (e.g. `MyClass.my_method`). |
| `tokensave_similar` | Find code similar to a given pattern or symbol. |
| `tokensave_status` | Retrieve index statistics from within a session. |

### Call Graph & Impact

| Tool | What it does |
|------|-------------|
| `tokensave_callers` | Find everything that calls a given function or method. |
| `tokensave_callees` | Find all functions that a given function calls. |
| `tokensave_callers_for` | Callers for a specific named node. |
| `tokensave_impact` | Full impact analysis — what breaks or changes if you modify X. |
| `tokensave_affected` | Find test files affected by changes to given source files. |
| `tokensave_rename_preview` | Preview every file/line that would change if you rename a symbol. |
| `tokensave_hotspots` | Find the most frequently changed code (git history-based). |

### Code Quality Analysis

| Tool | What it does |
|------|-------------|
| `tokensave_health` | Overall code health metrics — single summary score + breakdown. |
| `tokensave_complexity` | Cyclomatic complexity per function/class. Flags over-complex code. |
| `tokensave_god_class` | Detect god objects — classes with too many responsibilities. |
| `tokensave_dead_code` | Find unreferenced/unreachable symbols. |
| `tokensave_unused_imports` | Find imports that are never used. |
| `tokensave_circular` | Detect circular dependencies between modules. |
| `tokensave_coupling` | Measure how tightly coupled your components are. |
| `tokensave_doc_coverage` | What percentage of public symbols have docstrings/comments. |
| `tokensave_gini` | Code concentration metric — is complexity unevenly distributed? |
| `tokensave_dependency_depth` | Maximum depth of the dependency chain. |
| `tokensave_dsm` | Design Structure Matrix — visualise module dependencies. |
| `tokensave_test_risk` | Which code lacks test coverage and is most at risk. |
| `tokensave_largest` | Find the largest functions and classes by line count. |
| `tokensave_distribution` | Code distribution metrics across files/modules. |
| `tokensave_simplify_scan` | Find opportunities to simplify — overly verbose code patterns. |

### Type & Inheritance

| Tool | What it does |
|------|-------------|
| `tokensave_type_hierarchy` | Full class/type hierarchy for a symbol. |
| `tokensave_inheritance_depth` | How deep is the inheritance chain? |
| `tokensave_recursion` | Find recursive functions. |
| `tokensave_rank` | Rank symbols by a given metric (complexity, size, coupling, etc.). |

### Editing Tools

| Tool | What it does |
|------|-------------|
| `tokensave_str_replace` | Find and replace text across the codebase. |
| `tokensave_multi_str_replace` | Multiple find/replace operations in one call. |
| `tokensave_insert_at` | Insert code at a specific location. |
| `tokensave_ast_grep_rewrite` | Advanced AST-based structural rewriting (handles syntax correctly). |

### Git & Workflow

| Tool | What it does |
|------|-------------|
| `tokensave_changelog` | Generate a changelog from recent git commits. |
| `tokensave_commit_context` | Get context about what a commit changed. |
| `tokensave_diff_context` | Get context for a diff. |
| `tokensave_pr_context` | Get context for a pull request. |
| `tokensave_branch_search` | Search symbols across tracked branches. |
| `tokensave_branch_diff` | Diff the code graph between two branches. |
| `tokensave_branch_list` | List tracked branches and their DB sizes. |

### Utility

| Tool | What it does |
|------|-------------|
| `tokensave_todos` | List all TODO and FIXME comments with file locations. |
| `tokensave_port_order` | Port/endpoint ordering. |
| `tokensave_port_status` | Port/endpoint status. |
| `tokensave_test_map` | Map source files to their test counterparts. |

---

## Agent Integration

### Claude Code (CLAUDE.md)
After running `tokensave install --agent claude`, Claude Code is wired up automatically. To also load the tokensave tool reference into every session, add this line to your project's `CLAUDE.md`:

```
@D:\Claude Co worker\Token Save\project-baseline.md
```

This injects the tool lookup table so Claude always knows which tokensave tool to reach for first.

### Manual MCP Config (Claude Desktop)
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "tokensave": {
      "command": "D:\\Claude Co worker\\Token Save\\tokensave.exe",
      "args": ["serve", "-p", "D:\\Your\\Project\\Path"]
    }
  }
}
```
**Note:** You must restart Claude Desktop after changing this config, and the `-p` flag must point to a folder that has already been initialised with `tokensave init`.

---

## Background Daemon

The daemon watches your project for file changes and syncs automatically — no manual `tokensave sync` needed.

```powershell
# Run in foreground (keep terminal open)
tokensave daemon

# Install as a Windows service (runs on startup)
tokensave daemon --enable-autostart

# Check if daemon is running
tokensave daemon --status

# Remove the service
tokensave daemon --disable-autostart
```

**Operating modes:**

| Mode | Setup | Notes |
|------|-------|-------|
| Manual | Just use `tokensave sync` | Best for occasional Claude sessions |
| Auto-sync (daemon) | `tokensave daemon --enable-autostart` | Always current, zero maintenance |
| Branch-aware | Daemon + `tokensave branch add` | Separate index per git branch |

---

## Multi-Branch Workflow

If you regularly switch git branches, track each branch so Claude doesn't get confused by stale symbols from another branch.

```powershell
# Start tracking the current branch
tokensave branch add

# See all tracked branches and their DB sizes
tokensave branch list

# Stop tracking a branch you no longer need
tokensave branch remove feature/old-thing

# Clean up branches that no longer exist in git
tokensave branch gc

# Remove all except the default branch
tokensave branch removeall
```

When branch tracking is active, `tokensave sync` updates only the index for the current branch.

---

## Cost & Token Tracking

TokenSave tracks how many tokens it saved by answering queries from the graph instead of raw file reads.

```powershell
# 7-day summary (default)
tokensave cost

# Just today
tokensave cost today

# Breakdown by Claude model
tokensave cost --by-model

# Breakdown by task category
tokensave cost --by-task

# Export to JSON or CSV
tokensave cost --export json
tokensave cost --export csv

# Session counter
tokensave current-counter
tokensave reset-counter
```

---

## Troubleshooting

### "Server disconnected" in Claude Desktop
1. Run `tokensave doctor` in the project folder
2. Check that `tokensave init` has been run (`.tokensave/tokensave.db` must exist)
3. Verify the `-p` path in `claude_desktop_config.json` is correct and the DB exists
4. Fully quit and relaunch Claude Desktop (close window is not enough)

### Claude isn't using the tokensave tools
1. Run `tokensave doctor --agent claude`
2. Run `tokensave reinstall` to refresh the agent config
3. Check that your `CLAUDE.md` has the `@include` baseline line
4. Start a new Claude session (tools are registered at session start)

### Index seems stale / Claude gives wrong answers about code structure
```powershell
tokensave sync --force    # Full rebuild
```

### tokensave.exe creates console windows
Use `pythonw.exe` as the MCP server wrapper (see `tokensave-wrapper.py` in this folder). The wrapper auto-selects the most recently synced project and spawns tokensave with `CREATE_NO_WINDOW`.

### Multiple projects in global DB ambiguity
Always pass `-p <absolute-path>` when calling `tokensave serve`. Never rely on the working directory.

---

## Environment Variables

| Variable | Effect |
|----------|--------|
| `DISABLE_TOKENSAVE=true` | Disable tokensave for a specific project (won't serve tools) |
| `TOKENSAVE_WORKER_TOKEN` | Auth token for subprocess workers |
| `TOKENSAVE_DISABLE_SUBPROCESS=1` | Force in-process extraction (skip subprocess workers) |
| `TOKENSAVE_BENCH_REPOS_DIR` | Cache directory for benchmarks |

---

## Claude Prompt Recipes

Copy-paste these into Claude. Replace `[placeholders]` with your specifics.

---

### General exploration

**Get a codebase overview**
```
Use tokensave_context to give me a high-level overview of this project.
What are the main components, how do they relate, and what is the entry point?
```

**Find a specific symbol**
```
Use tokensave_search to find [symbol name].
Then use tokensave_context to explain what it does, what it calls, and what calls it.
```

**Explore a module's public API**
```
Use tokensave_module_api to show me the public API of [module or file name].
What does it export and how is it meant to be used?
```

---

### Call graph & impact

**What calls this function?**
```
Use tokensave_callers to find everything that calls [function name].
Show me the full call chain.
```

**What does this function call?**
```
Use tokensave_callees to show everything [function name] calls, directly and transitively.
```

**What would break if I change X?**
```
Use tokensave_impact to analyse what would be affected if I modify [function or class name].
Show me the full impact chain and flag anything that could break.
```

**Safe rename preview**
```
Use tokensave_rename_preview to show what would change if I rename [old name] to [new name].
List every affected file and line before we proceed.
```

---

### Code quality

**Full health check**
```
Run tokensave_health, tokensave_complexity, and tokensave_god_class.
Give me a health report — flag god classes, high cyclomatic complexity,
circular dependencies, and any dead code.
```

**Find dead code and unused imports**
```
Use tokensave_dead_code and tokensave_unused_imports to find anything
that's no longer used in this project. List them with file locations.
```

**Find circular dependencies**
```
Use tokensave_circular to find any circular dependencies.
For each one, explain how it could be resolved.
```

**Find the most complex files**
```
Use tokensave_largest and tokensave_complexity to find the biggest and
most complex files. Which ones are the best candidates for refactoring?
Summarise the top 5.
```

**Documentation coverage**
```
Use tokensave_doc_coverage to check what percentage of public symbols
have docstrings or comments. List the undocumented ones.
```

---

### Git & changelog

**Generate a changelog entry**
```
Use tokensave_changelog to generate a changelog based on recent commits.
Format it as a CHANGELOG.md entry using Keep a Changelog conventions.
```

**What changed in this commit?**
```
Use tokensave_commit_context for commit [hash] to explain what changed
and why it matters. Focus on the structural/architectural impact.
```

---

### Utilities

**List all TODOs**
```
Use tokensave_todos to list all TODO and FIXME comments in this project.
Group them by file and estimate which ones are highest priority.
```

**Map source to tests**
```
Use tokensave_test_map to show which test files cover [source file or module].
Then use tokensave_test_risk to flag any areas with poor coverage.
```

**Find recursive functions**
```
Use tokensave_recursion to find all recursive functions in this project.
Flag any that could cause stack overflow with large inputs.
```

---

## Quick Reference Card

```
FIRST TIME          tokensave init
DAILY               tokensave sync
FORCE REBUILD       tokensave sync --force
HEALTH CHECK        tokensave doctor
STATS               tokensave status --details
LIVE MONITOR        tokensave monitor
COST REPORT         tokensave cost
AUTO-SYNC           tokensave daemon --enable-autostart
```

---

*Sources: https://tokensave.dev · https://github.com/aovestdipaperino/tokensave*

---

*TokenSave Manager — created by Alexander L Corthell*
