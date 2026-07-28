# tokensave — Architecture Reference

> This document describes the internal architecture of `tokensave.exe` — the third-party
> code-intelligence tool this project depends on. It is based on public documentation at
> https://tokensave.dev and https://github.com/aovestdipaperino/tokensave.
> The source is not available here; this is a reference for understanding how the tool works.

---

## What It Is

tokensave is a **local, zero-network code-graph server** that indexes a project's source files
into a SQLite database and exposes the graph to AI agents via the MCP (Model Context Protocol).
Instead of an AI agent reading raw source files on every message, it queries the pre-built graph —
dramatically cutting token usage while giving the agent richer structural understanding.

**Key properties:**
- 100% local — no data ever leaves the machine
- SQLite-backed — single `.tokensave/tokensave.db` file per project (+ per-branch variants)
- Language-agnostic — 34 languages via tree-sitter parsers
- Agent-agnostic — supports Claude, Cursor, Copilot, Cline, Gemini, and 8 more

---

## Core Pipeline

```
Source files
    │
    ▼
tree-sitter parsers          ← 34 language grammars, run in subprocess workers
    │
    ▼
Symbol extraction             ← functions, classes, methods, types, imports, ...
    │
    ▼
SQLite graph (.tokensave/tokensave.db)
    │   nodes table  — every symbol with kind, location, docstring, complexity
    │   edges table  — call relationships, inheritance, imports, usage
    │   files table  — indexed files with hash + mtime for incremental sync
    ▼
MCP stdio server (tokensave serve)
    │
    ▼
AI agent (Claude, Cursor, etc.)   ← 48 MCP tools available
```

---

## Database Schema

**Location:** `<project-root>/.tokensave/tokensave.db`  
**Format:** SQLite 3, WAL journal mode (enables concurrent reads during writes)  
**Branch variants:** `tokensave.<branch-name>.db` when branch tracking is active

### Core tables

| Table | Contents |
|-------|----------|
| `nodes` | One row per symbol — name, qualified name, kind (function/class/method/…), file, line range, docstring, cyclomatic complexity |
| `edges` | Relationships between nodes — calls, inherits, imports, uses |
| `files` | Indexed files — path, content hash, mtime, last-indexed timestamp |

The graph is a directed multigraph: one source node can have many outbound edges of different
relationship types to many target nodes.

---

## Indexer

### `tokensave init`
Full first-time scan. Walks every non-ignored file, runs the appropriate tree-sitter parser,
extracts all symbols and relationships, writes the complete graph to the DB.
Run once per project. Can take seconds to minutes depending on codebase size.

### `tokensave sync`
Incremental update. Compares file mtimes and content hashes against the `files` table.
Only re-parses files that changed. Fast — typically a few hundred milliseconds.

### `tokensave sync --force`
Forces a full re-scan identical to `init`. Use when the index seems stale or corrupted.

### Subprocess Workers
Tree-sitter grammars can crash on malformed or edge-case source files. To prevent a parser
crash from taking down the MCP server, extraction runs in isolated **subprocess workers**.
If a worker crashes, the file is skipped and the server continues. This is why the binary
compiles with `--no-default-features` (lite) through the full 34-language build — the
worker process boundary is always present.

---

## MCP Server

### Transport
Communicates over **stdio** (stdin/stdout). The AI agent spawns `tokensave serve` as a
subprocess and speaks the MCP JSON protocol over the pipe. No TCP ports, no HTTP.

### Startup
The agent reads the server configuration (e.g. from `claude_desktop_config.json` or
`.claude/mcp.json`) and spawns the server process at session start. The server opens
the SQLite DB and begins serving tool requests.

### The `-p` flag
`tokensave serve -p <absolute-path>` pins the server to a specific project.
Without `-p`, the server attempts to use the process working directory.
**Always use `-p` with an absolute path** — the working directory when spawned by Claude
Desktop is `C:\windows\system32`, not the project folder.

### 48 MCP Tools — Category Summary

| Category | Tools | Purpose |
|----------|-------|---------|
| Discovery | `tokensave_context`, `tokensave_search`, `tokensave_node`, `tokensave_files`, `tokensave_module_api`, `tokensave_similar`, `tokensave_by_qualified_name`, `tokensave_status` | Navigate the codebase without reading files |
| Call graph | `tokensave_callers`, `tokensave_callees`, `tokensave_callers_for`, `tokensave_impact`, `tokensave_affected`, `tokensave_rename_preview`, `tokensave_hotspots` | Trace how code connects and what breaks when you change things |
| Code quality | `tokensave_health`, `tokensave_complexity`, `tokensave_god_class`, `tokensave_dead_code`, `tokensave_unused_imports`, `tokensave_circular`, `tokensave_coupling`, `tokensave_doc_coverage`, `tokensave_gini`, `tokensave_dependency_depth`, `tokensave_dsm`, `tokensave_test_risk`, `tokensave_largest`, `tokensave_distribution`, `tokensave_simplify_scan` | Measure and improve code health |
| Types | `tokensave_type_hierarchy`, `tokensave_inheritance_depth`, `tokensave_recursion`, `tokensave_rank` | Explore type structure and inheritance |
| Editing | `tokensave_str_replace`, `tokensave_multi_str_replace`, `tokensave_insert_at`, `tokensave_ast_grep_rewrite` | Graph-aware code edits |
| Git | `tokensave_changelog`, `tokensave_commit_context`, `tokensave_diff_context`, `tokensave_pr_context`, `tokensave_branch_search`, `tokensave_branch_diff`, `tokensave_branch_list` | Connect the graph to git history |
| Utility | `tokensave_todos`, `tokensave_port_order`, `tokensave_port_status`, `tokensave_test_map` | Miscellaneous lookups |

---

## Background Daemon

`tokensave daemon` runs a file-system watcher that detects source file changes and
automatically triggers an incremental sync. Eliminates the need for manual `tokensave sync`
calls during active development.

Can be installed as a persistent service:
- **Windows:** Task Scheduler entry via `--enable-autostart`
- **macOS:** launchd plist
- **Linux:** systemd unit

---

## Branch Tracking

When multiple git branches are active, each branch maintains an **isolated index**:

```
.tokensave/
├── tokensave.db                   ← default branch
├── tokensave.feature-auth.db      ← feature/auth branch
└── tokensave.fix-null-check.db    ← fix/null-check branch
```

Commands: `tokensave branch add` (track current), `branch list`, `branch gc` (prune deleted).
The daemon automatically syncs the correct DB for the active branch.

---

## Token Savings Mechanism

When an AI agent needs to understand code without tokensave:
- It reads raw source files (hundreds to thousands of lines per query)
- Every session restarts with no structural knowledge

With tokensave:
- Queries hit the pre-built graph — a single `tokensave_context` call returns precise, structured
  results in ~200 tokens instead of the agent reading 5,000 lines of source
- The graph persists across sessions — no re-scanning

Savings scale with project size. On large codebases the reduction is 70–90% of code-context tokens.

---

## Supported Languages (Full Build)

Rust · Python · TypeScript · JavaScript · Go · Java · C · C++ · C# · Ruby · PHP · Swift ·
Kotlin · Scala · Elixir · Erlang · Clojure · Haskell · OCaml · F# · Nim · Zig · Odin ·
Carbon · D · Lua · Julia · R · MATLAB · Groovy · Gradle · Maven · SQL

---

## File Locations (this installation)

| Item | Path |
|------|------|
| Binary | `D:\Claude Co worker\Token Save\tokensave.exe` |
| Project DB | `<project-root>\.tokensave\tokensave.db` (plus `.db-wal` and `.db-shm` siblings for WAL-mode SQLite) |
| Desktop pin file | `%USERPROFILE%\.tokensave\desktop-project.txt` (still single-shot at wrapper startup — live in-session reload deferred) |
| MCP config (Claude Desktop, traditional install) | `%APPDATA%\Claude\claude_desktop_config.json` |
| MCP config (Claude Desktop, UWP / Microsoft Store install) | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` ⚠ |
| MCP config (Claude Code) | `%USERPROFILE%\.claude.json` |
| Tokensave global DB | `%USERPROFILE%\.tokensave\global.db` |
| Tokensave user config | `%USERPROFILE%\.tokensave\config.toml` |

⚠ **UWP gotcha:** Microsoft Store installs of Claude Desktop apply asymmetric file-path redirection — the same path string `%APPDATA%\Claude\claude_desktop_config.json` resolves to a DIFFERENT physical file depending on whether the caller is in UWP-context (Claude Desktop itself and its children) vs. non-UWP-context (the manager, Notepad++, plain cmd.exe). Both files exist; both have the same path string; they have unrelated content. Edit the package-internal file. The manager's `_resolve_desktop_cfg_path()` handles this automatically. See `docs/MCP_INTEGRATION_GOTCHAS.md` for the full diagnosis trail.

## Notes on tokensave 5.1.x ([Unreleased] manager cycle)

- **WAL-mode SQLite.** Incremental syncs only touch `.tokensave/tokensave.db-wal`, not the main `.db` file. The main file's mtime only advances on checkpoint (server shutdown or `sync --force`). The manager's "Last Synced" column reads `max(mtime)` across all three files (`db`, `db-wal`, `db-shm`) so it reflects actual sync activity, not just the last checkpoint.
- **MCP server lifetime.** Claude Desktop respawns the tokensave child every ~60 seconds (its normal MCP heartbeat). Each respawn picks up whatever binary is currently at `tokensave_exe` — meaning `tokensave upgrade` lands in a Claude Desktop session within one heartbeat. The manager's tokensave 5.1.1 → 5.1.2 upgrade in this cycle was confirmed live by observing the post-upgrade MCP children all using the new binary at the next respawn.
- **CLI subcommand names.** `tokensave query` (not `search` — `search` doesn't exist as a subcommand even though MCP exposes `tokensave_search`). `tokensave context` accepts `--format json|markdown` (not `--json`). `tokensave install --agent claude` configures Claude Code integration but writes hook commands without quoting paths-with-spaces — see `docs/upstream-issues/tokensave-hook-quoting.md` for the bug draft.
- **`tokensave doctor` purge prompt is TTY-gated.** Piped stdin won't trigger the y/n prompt for purging stale global-DB entries. The manager's Doctor button detects this case and offers to spawn `cmd.exe /k "tokensave doctor"` in a NEW console window where the user can answer the prompt for real.

## tokensave-wrapper.py (manager-side script)

A thin script the manager registers as Claude Desktop's MCP server in `claude_desktop_config.json`. Spawns `tokensave.exe serve -p <pinned-project>` and proxies its stdio. **Critical:** the Popen call must pass `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` explicitly — default-inheritance under pythonw.exe breaks console-child stdio in a way that times out MCP handshake at 30 s. **Also critical:** keep it single-threaded — `import threading` and daemon threads introduce additional subtle stdio issues. Both of these were learned the hard way and are documented in detail in `docs/MCP_INTEGRATION_GOTCHAS.md`.

The wrapper currently reads the pin file ONCE at startup. Live in-session pin reloading (so `★ Set as Active` swaps the served project without a Claude restart) is deferred — must be implemented out-of-process (sibling watcher daemon that signals via `taskkill /F` to the tokensave PID, or DXT extension migration) rather than inside the wrapper.
