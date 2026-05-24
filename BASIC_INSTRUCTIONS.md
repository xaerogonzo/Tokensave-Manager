# TokenSave Manager — Basic Instructions

@D:\Claude Co worker\Token Save Manager Source\templates\project-baseline.md

---

## Working Rules for Claude

This section is **load-bearing** and is read first when this file is `@`-included
into a session. The rules below are operational, not aspirational — every one
traces back to a real recurring failure mode in past Claude Code sessions on
this codebase.

### A. Anti-monolith caps (soft limits with exception protocol)

| Cap | Threshold | Notes |
|---|---|---|
| File length | **aim for ≤800 lines; Doctor warns at 1500** | Files between 800–1500 are an aspirational target, not an audit failure. Above 1500 → propose a split. UI/layout files exempt (see carve-out) |
| Method length | **100 lines** | Logic methods only; declarative layout exempt if complexity ≤ 3 |
| Class method count | **40 direct methods** | AST count of direct `FunctionDef` / `AsyncFunctionDef` children of `ClassDef`; nested defs, decorators, lambdas don't count |
| Cyclomatic complexity | **10** | `tokensave_complexity` is canonical (see semantics below) |

**Complexity semantics** (frozen here so we don't drift if upstream `tokensave_complexity` changes its algorithm):
- Each `if` / `elif` / `for` / `while` / `except` adds 1
- Each `and` / `or` short-circuit adds 1
- Each `match` arm adds 1
- Comprehensions with conditions add 1 per `if` clause
- Nested function definitions do NOT recursively contribute to the outer function's score (they have their own)

**Layout-method carve-out (hybrid: naming + complexity, not name alone)** — a method qualifies for the 100-line exemption when **both** hold:
- Name pattern: `_build_ui`, `_build_*`, `_populate_*`, `_render_*`, `_layout_*` — broadly any method whose primary purpose is widget construction
- Cyclomatic complexity ≤ 3

Naming alone never grants immunity. A 200-line `_build_database_section` with 8 branches still violates the cap. Split UI by **semantic grouping** (wizard step, panel, section) — never by arbitrary geometry chunks (`_build_top_left_row`, `_build_footer_padding_frame` are anti-patterns).

**Exception protocol** — three escape hatches:
1. **Temporary exceedance** during an in-progress extraction is fine; the final committed state should trend downward.
2. **Grandfathered files** stay on the list in `cfg.raw["doctor_skip_monolith_paths"]` (e.g. `src/tokensave-wrapper.py` — intentionally single-file).
3. **Explicit override**: a top-of-file comment with a concrete rationale: `# anti-monolith: exempt — generated parser table; splitting harms readability`. Doctor's audit requires a non-empty rationale string after the `—`.

**When you would violate a cap**, state it explicitly to the user before writing code: *"This change would push `git_tab.py` past 1,500 lines; recommend extracting the branch-management cluster first."* Do not silently violate. Do not auto-fan-out into multi-file refactors without approval (see Rule F).

### B. Documentation discipline on big changes

"Big change" = any of:
- adds ≥1 new file under `src/`
- renames a class or public function
- adds a new tab/dialog/controller
- touches the import graph (new helper, new dialog, new controller)
- changes the `manager-config.json` schema (new key, renamed key, changed default — these are de-facto public contract)
- changes `cfg.raw` keys read by multiple modules (silent architecture drift risk)
- changes the threading model (new background thread, new `after(...)` bridge, new event/lock)
- changes a persistence format (`.gitignore` layout, `CHANGELOG.md` anchors, `.claude/settings.json` hook shape)

For any big change, in the SAME commit you must update:
- `CHANGELOG.md` — entry under `[Unreleased]` with one-line summary
- `docs/ARCHITECTURE.md` — if file count, controller list, or helper list changed
- `BASIC_INSTRUCTIONS.md` — if a new project-rule emerged
- `docs/ROADMAP.md` — mark items shipped, add follow-ups

Verification gate: *"What docs did I touch?"* — if the answer is "none" and the change was big, the change is incomplete.

### C. Tokensave-first exploration

- Reading code to answer a question? Use `tokensave_context` first. Don't use Read/Glob/Grep for code research when tokensave is available (see global `~/.claude/CLAUDE.md`).
- Before deleting "unused" code: grep-verify. Tk-callback false positives are documented in `~/.claude/projects/D--Claude-Co-worker-Token-Save-Manager-Source/memory/tokensave_python_false_positives.md`.

### D. Project guard-rails (the high-impact subset)

These are the rules whose violation causes silent breakage. Every controller and dialog in this project obeys them:

- **`ManagerConfig` contract**: never snapshot derived values in `__init__`. Read `self._cfg.X` at execution time. Settings saves rebind via `cfg.refresh_derived()` and stale snapshots would silently return wrong values.
- **All Windows `subprocess.Popen` calls** must pass `creationflags=CREATE_NO_WINDOW`. Without it, a console window flickers briefly every invocation.
- **All Tk widget mutations from background threads** go through `self.after(0, ...)` with a `winfo_exists()` guard. Direct widget calls from threads will crash.
- **Controllers import dialogs directly**; cross-dialog deps (e.g. `SettingsDialog` opening `MCPConfigDialog`) use **lazy in-handler imports** to avoid module-load cycles.
- **`helpers/` never imports from `controllers/` or `dialogs/`** (no upward imports). Acyclic import graph is a verified invariant.

### E. When in doubt, surface, don't decide

- Adding a new top-level dependency? Ask first — the project deliberately avoids LangChain/LlamaIndex per `docs/ROADMAP.md` rule 2.
- About to add a feature flag, backwards-compat shim, or `try/except` swallow? Ask — the user prefers direct changes (see global CLAUDE.md "Don't add error handling, fallbacks, or validation for scenarios that can't happen").
- **If implementing the request cleanly would violate project rules or require disproportionate complexity, explain the tension before coding.** Don't silently bend the rules; don't silently fan-out into a sweeping refactor. Architectural tension is output-worthy.

### F. Refactor budget (prevent thrash)

**Operational form** (easier for the model to obey than %-of-lines): do not modify files outside the direct scope of the request unless (a) needed for compilation/runtime correctness, OR (b) explicitly approved by the user. "I'm changing function X, so I'll also clean up function Y in a different file" → list it at the end, don't bundle it.

- Soft guideline for in-scope cleanup: ~15–20% of touched lines is a reasonable cap on collateral edits within files you're already changing.
- A bug fix or small feature should NOT trigger an 11-file cleanup. Spotted cleanup opportunities go at the end of the response (or use the spawn-task chip) — never bundled into the same diff.
- Cap extractions to ONE per feature commit unless the user asks for a sweep. The Round 4 / Round 5 audits were explicitly user-initiated multi-file campaigns; ordinary day-to-day work is not.

### G. Metric hierarchy (when rules conflict)

Priority order — higher wins:
1. **Correctness** — never break behaviour to satisfy a metric.
2. **Readability** — a coherent 220-line `_build_ui` beats four fragmented 60-line `_build_*_chunk` helpers split on geometry.
3. **Architectural clarity** — semantic grouping, callback injection, no upward imports.
4. **Metric compliance** — line caps, method counts, complexity scores.

If a refactor would improve metric (4) at the cost of (2) or (3), don't do it. Metrics exist to **surface candidates for human judgment**, not to be the goal themselves.

### H. Governance hygiene (meta-rule)

- **New rules require evidence.** Don't add governance rules speculatively. Every rule above traces back to a real recurring failure mode. If a future Claude session goes wrong in a new way, then — and only then — propose a new rule.
- **The caps and rules in this file are the canonical source.** If a downstream tool (Doctor audit, tokensave, an external linter) disagrees: `tokensave_complexity` wins for complexity scores; otherwise this file wins. Do not silently invent additional thresholds.

---

## Project Overview

**Name:** TokenSave Manager
**Stack:** Python 3, tkinter/ttk (GUI), subprocess (tokensave CLI), threading, pystray (tray icon), Pillow
**Entry point:** `Launch TokenSave Manager.bat` → reads `python_exe` from `manager-config.json` → `src/app.py`
**Purpose:** Windows GUI for managing tokensave MCP project integrations — switching active projects for Claude Desktop, syncing indexes, scaffolding new projects with Claude instruction templates and/or Nuitka build pipelines, and managing search roots via a Settings dialog.

**Current source location:** `D:\Claude Co worker\Token Save Manager Source\`
**Runtime dependency:** `tokensave.exe` — path stored in `manager-config.json` (not bundled with source)

---

## Project Structure

```
Token Save Manager Source/
├── manager-config.json            Machine-specific config (all hardcoded paths live here)  — gitignored
├── manager-config.example.json    Clean template with placeholder paths — committed for new users
├── Launch TokenSave Manager.bat   Reads python_exe from config, launches src/app.py
├── build.ps1                      Nuitka compile pipeline — produces dist\ exes
├── build.bat                      Double-click launcher for build.ps1
├── BASIC_INSTRUCTIONS.md          This file
├── CHANGELOG.md                   Feature history
├── TOKENSAVE_GUIDE.md             Full tokensave CLI + MCP reference
├── .gitignore                     Excludes manager-config.json, .claude/, .tokensave/, dist/, etc.
│
├── src/                           App + main() in app.py, everything else in
│   │                              subpackages (helpers/, dialogs/, controllers/).
│   ├── app.py                     Entry point — App(tk.Tk) class + main(). Owns the
│   │                              single ManagerConfig instance and passes it down
│   │                              to every controller/dialog.
│   ├── state.py                   ManagerConfig dataclass (runtime-mutable settings;
│   │                              read-only @property getters; mutated via
│   │                              raw.update() + save() + refresh_derived()).
│   ├── constants.py               Immutable constants: C palette, regex tables,
│   │                              CREATE_NO_WINDOW, _ANSI, _GIT_ENV_NO_PROMPT,
│   │                              paths (_BASE_DIR, _CONFIG_PATH, LOG_FILE).
│   ├── theme.py                   _Tooltip widget (Tk-coupled UI primitive).
│   ├── tokensave-wrapper.py       Claude Desktop auto-detection wrapper (MUST stay single-threaded
│   │                              AND pass sys.stdin/stdout/stderr to Popen explicitly — see
│   │                              docs/MCP_INTEGRATION_GOTCHAS.md before touching).
│   ├── prompts.py                 Built-in Claude prompt snippets (ROM defaults; overrides in cfg).
│   ├── agent.py                   LocalAgent loop for the 🤖 Ask tab — Stage 2 read-only
│   │                              tool calling + Stage 3 write-tool dispatch via the
│   │                              injected on_write_proposal bridge.
│   ├── agent_tools.py             ToolSpec registry: 6 read-only tools (read_file, list_directory,
│   │                              git_log, git_diff, tokensave_search, tokensave_context) +
│   │                              opt-in write_file via build_tools(with_write=True).
│   ├── precommit_review.py        Entry point for the git pre-commit AI review hook
│   │                              (Roadmap-2 P5b). Standalone — invoked by
│   │                              .git/hooks/pre-commit; bootstraps sys.path to src/.
│   │
│   ├── helpers/                   Pure / IO helpers — no UI dependencies. Each module
│   │   │                          takes only the parameters it needs.
│   │   ├── config.py              _load_config, _save_config, _migrate_config
│   │   ├── detection.py           _detect_git, _detect_gh, _detect_npm, _detect_codegraph,
│   │   │                          _detect_claude_cli, _is_codegraph_project, _root_path,
│   │   │                          _root_label, _version_lt
│   │   ├── runtime.py             log, _setup_logger, _acquire_instance_lock,
│   │   │                          _bring_existing_to_front, _make_tray_icon
│   │   ├── project_discovery.py   find_projects(roots), get_pinned, set_pinned,
│   │   │                          clear_pinned, fmt_age, load_basic_instructions_template
│   │   ├── git.py                 _is_git_repo, _is_local_git_repo, _parse_git_status_v2,
│   │   │                          _format_git_status_cell, _find_tracked_but_ignored,
│   │   │                          _fetch_tags, _git_tag, _git_push_with_tags
│   │   ├── gitignore.py           _ensure_gitignore, _baseline_patterns, _read_gitignore_lines,
│   │   │                          _write_gitignore_lines, _BASELINE_GITIGNORE, _GITIGNORE_TEMPLATES
│   │   ├── shadow_links.py        generate_shadow_links, remove_shadow_links,
│   │   │                          update_gitignore_for_shadows, DEFAULT_SHADOW_EXT_MAP
│   │   ├── scaffold.py            _scaffold_git_hook + _AUTO_COMMIT_HELPER script body
│   │   ├── mcp.py                 _resolve_desktop_cfg_path, _wrapper_path, _canonical_mcp_entry,
│   │   │                          _classify_mcp_entry, _apply_mcp_fix, _is_claude_running,
│   │   │                          _MCP_CONFIGS, _MCP_CMD_CHECKERS
│   │   ├── llm.py                 _call_llm, _call_anthropic, _call_openai_compat,
│   │   │                          _iter_sse_events, _iter_json_lines, _is_auth_error
│   │   ├── commit_messages.py     _suggest_commit_message (orchestrator) + 4 _strat_*
│   │   │                          strategy fns + sanitiser cluster + _pending_diff,
│   │   │                          _call_llm_for_commit_message
│   │   ├── release.py             _last_release_tag, _commits_since, _classify_commits_for_changelog,
│   │   │                          _bump_version, _suggest_bump_kind, _render_release_notes,
│   │   │                          _zip_dist, _release_basename, _fmt_size
│   │   │                          (Roadmap-2 P2: _patch_changelog removed — use
│   │   │                          helpers/changelog_patch.py)
│   │   ├── changelog_patch.py     insert_changelog_release (idempotent atomic patcher —
│   │   │                          replaces existing ## [version] block bounded by next
│   │   │                          ^## \[ line; falls back to insertion under
│   │   │                          ## [Unreleased]. Wired into ReleaseWizard.)
│   │   ├── pr_draft.py            generate_pr_draft (LLM-based PR description drafting)
│   │   ├── daemon_cost.py         get_daemon_status, toggle_daemon, toggle_autostart,
│   │   │                          parse_tokensave_cost
│   │   ├── claude_cli.py          spawn_claude_cli (detached terminal via CREATE_NEW_CONSOLE)
│   │   └── precommit_hook.py      install/remove/detect git pre-commit hook + review
│   │                              runner (P5b). 3-value backend dispatch
│   │                              (auto/claude_cli/llm); severity parser; sentinel marker
│   │                              for install/remove symmetry. Fail-open invariant.
│   │
│   ├── dialogs/                   tk.Toplevel dialog classes. Each takes cfg: ManagerConfig
│   │   │                          via __init__ when it needs to read settings; bare-data
│   │   │                          dialogs (NewBranch, SwitchBranch, AssignCategory, etc.) skip it.
│   │   ├── settings.py            SettingsDialog (+_probe_loaded_model helper)
│   │   ├── release_wizard.py      ReleaseWizardDialog + _ReleaseCtx (paired)
│   │   ├── mcp_config.py          MCPConfigDialog — mutates cfg.raw["mcp_skip_warnings"]
│   │   ├── ai_code_review.py      AICodeReviewDialog — takes both llm_cfg dict + cfg
│   │   ├── git_commit.py          GitCommitDialog
│   │   ├── ollama_model_mgr.py    OllamaModelManagerDialog
│   │   ├── gitignore.py           GitignoreDialog (lazy-imports UntrackIgnoredDialog)
│   │   ├── github_setup.py        GitHubSetupDialog
│   │   ├── retrofit.py            RetrofitDialog
│   │   ├── scaffold.py            ScaffoldDialog
│   │   ├── snippet_edit.py        SnippetEditDialog
│   │   ├── shadow_links.py        ShadowLinksDialog
│   │   ├── set_remote.py          SetRemoteDialog
│   │   ├── merge_pr.py            MergePRDialog
│   │   ├── new_branch.py          NewBranchDialog
│   │   ├── switch_branch.py       SwitchBranchDialog (+ static pick() helper)
│   │   ├── assign_category.py     AssignCategoryDialog
│   │   ├── untrack_ignored.py     UntrackIgnoredDialog
│   │   ├── cost_viewer.py         CostViewerDialog (📊 button in app footer)
│   │   └── proposal.py            WriteProposal dataclass + ProposalDialog +
│   │                              ProposalBridge (P1 — race-safe agent worker ↔ Tk main
│   │                              coordinator; 5-min event.wait; first-resolution-wins;
│   │                              automated test harness in __main__)
│   │
│   └── controllers/               Tab + sub-controllers.
│       ├── projects_tab.py        ProjectsTabController — Projects tree + thin command wrappers
│       ├── git_tab.py             GitTabController — Git tab + push/pull/release/Draft PR/
│       │                          Open PR on GitHub. ~38 methods after P4 extracted
│       │                          BranchManagementController.
│       ├── branch_mgmt_ctrl.py    BranchManagementController (P4 — new/switch/merge/
│       │                          delete-branch cluster; callback injection from GitTab)
│       ├── ask_tab.py             AskTabController — 🤖 Ask tab + agent thread plumbing +
│       │                          ProposalBridge registration for write-tool gating
│       ├── snippets.py            SnippetsController — 📚 Reference tab
│       ├── help_tab.py            HelpTabController — ❓ Help tab (16 sections)
│       ├── update_poller.py       UpdatePollerController — tokensave version probe + GH update check
│       ├── doctor_ctrl.py         Doctor command (tokensave doctor + purge flow +
│       │                          P0 monolith audit: file/method/class/complexity caps via
│       │                          AST walk + non-Python line-count check)
│       ├── scaffold_ctrl.py       Scaffold + Retrofit commands
│       ├── sync_ctrl.py           Sync / Status / Set-active / Force-sync
│       ├── fileops_ctrl.py        File ops (open folder/editor, copy path, remove index)
│       ├── shadowlinks_ctrl.py    Shadow links dialog + background generation
│       ├── codegraph_ctrl.py      CodeGraph init / sync / status / remove
│       └── git_ops_ctrl.py        Git ops from Projects tab (init, log, commit, AI review,
│                                  gitignore, untrack, P5b pre-commit hook install/remove)
│
├── templates/                     Data files used by the manager (all shipped in dist\templates\)
│   ├── claude-md-template.md      BASIC_INSTRUCTIONS template written into scaffolded projects
│   ├── project-baseline.md        Universal rules file @included by all retrofitted projects
│   ├── nuitka-build.ps1.template  Generic Nuitka build script for other projects (PowerShell)
│   ├── nuitka-build.py.template   Python-based alternative build script (no PS gotchas)
│   ├── nuitka-build.bat.template  Bat launcher template for other projects
│   └── NUITKA_GOTCHAS.md          Nuitka pitfalls reference (14 known issues)
│
├── dist/                          Build output — zip and ship this folder
│   ├── tokensave-manager.exe
│   ├── tokensave-wrapper.exe
│   ├── manager-config.json
│   ├── manager-config.example.json
│   ├── TOKENSAVE_GUIDE.md
│   ├── CHANGELOG.md
│   ├── templates\
│   └── docs\
│
└── docs/
    ├── ARCHITECTURE.md             Manager architecture reference
    ├── ARCHITECTURE_TOKENSAVE.md   tokensave tool internals reference
    ├── AGENT_ARCHITECTURE.md       LocalAgent + tool registry + locked propose-only rules
    ├── ROADMAP.md                  Staged plan for local AI features
    ├── MCP_INTEGRATION_GOTCHAS.md  Postmortem field manual — READ before changing the wrapper
    ├── GITHUB_GUIDE.md             Beginner GitHub guide
    └── upstream-issues/            Drafts of bugs to file against upstream tools
```

---

## manager-config.json

All machine-specific values. Edit via the Settings dialog in the GUI, or directly in JSON.
Must be updated when the project moves to a new location or machine.

| Key | Purpose |
|-----|---------|
| `tokensave_exe` | Absolute path to `tokensave.exe` (third-party binary, not in this repo) |
| `template_dir` | Absolute path to the `templates/` directory (blank = auto-detect as `<exe-dir>\templates\`) |
| `editor_cmd` | Command (+ optional flags) for Open in Editor, e.g. `code` or `code --new-window` |
| `python_exe` | Path to `pythonw.exe` — used by the `.bat` launcher only; not shown in Settings UI |
| `git_exe` | Optional absolute path to `git.exe`. Blank = auto-detect via `shutil.which("git")` + common Windows install paths. Used by every git subprocess call. Configurable via Settings → Git exe row. |
| `search_roots` | List of `str \| {"path": str, "label": str}` — scanned for tokensave projects. Bare strings are backward-compatible (`label` defaults to `basename`). Each root's label becomes its category header in the Treeview. |
| `project_categories` | Dict `{path: {"category": str, "subcategory"?: str}}` — per-project category override. Set via right-click → 📁 Assign Category…; persisted here automatically. |
| `user_snippets` | List of `{"title": str, "text": str}` dicts — user-defined Claude prompt snippets |
| `auto_commit_after_sync` | Boolean (default `false`) — if `true`, auto-runs `git add -A + git commit` after every successful `tokensave sync`. If the previous commit was `"chore: tokensave sync"`, the new auto-commit is amended onto it. |
| `commit_message_llm` | Dict. AI-commit + general LLM settings. Keys include `enabled`, `provider`, `model`, `api_key_env`, `base_url`, `min_diff_lines`, `max_diff_chars`, `timeout_seconds`, `use_for_sync_autocommit`, `num_ctx` (Ollama context window, default 32768). Read by both `_call_llm` and `LocalAgent`. |
| `mcp_skip_warnings` | List of absolute paths. MCP config files the user has dismissed warnings about. |
| `tokensave_update_poll_hours` | Float, default 1.0 (min 0.25). GitHub releases poller cadence for tokensave update detection. |
| `builtin_snippet_overrides` | Dict `{title: text}`. Per-prompt overrides for the built-in Claude prompt snippets defined in `src/prompts.py`. Defaults are ROM; this dict is the RAM overlay. Default `{}`. |
| `codegraph_exe` | Optional absolute path to the codegraph CLI. Blank = auto-detect (`.cmd`-first via `shutil.which`, then `%APPDATA%\npm\codegraph.cmd`). Configurable via Settings → CodeGraph section. Empty string when not installed — never the bare command name. |
| `claude_cli_exe` | Optional absolute path to the Claude Code CLI (`claude.cmd` from `npm install -g @anthropic-ai/claude-code`). Blank = auto-detect (`.cmd`-first via `shutil.which`, then `%APPDATA%\npm\claude.cmd`). Used by the Git tab's Draft PR button CLI execution path — spawns a detached terminal (`CREATE_NEW_CONSOLE + cmd.exe /k`) rather than capturing stdout. |
| `doctor_skip_monolith_paths` | Optional list of project-relative paths Doctor's monolith audit should skip (e.g. `["src/tokensave-wrapper.py"]` for intentionally single-file modules). Default `[]`. |

---

## Documentation & Key Files

| File | Role |
|------|------|
| `src/app.py` | Entry point — `App(tk.Tk)` + `main()`. Constructs the single `ManagerConfig` instance and the tab controllers. |
| `src/state.py` | `ManagerConfig` dataclass — runtime-mutable settings with read-only `@property` getters. Mutation: `cfg.raw.update(...) + cfg.save() + cfg.refresh_derived()`. |
| `src/controllers/` | Tab + sub-controllers — see Project Structure tree. Each takes only the callbacks it needs (callback injection, never parent reference). |
| `src/dialogs/` | One `tk.Toplevel` per file. See Project Structure tree. |
| `src/helpers/` | Pure / IO helpers (git, llm, mcp, commit_messages, release, etc.). No UI dependencies — safe to import from anywhere. |
| `src/tokensave-wrapper.py` | MCP server wrapper for Claude Desktop — reads same `manager-config.json` |
| `manager-config.json` | Single source of truth for all machine-specific paths |
| `templates/project-baseline.md` | @included by every retrofitted project's CLAUDE.md — edit here to update all |
| `templates/claude-md-template.md` | Written as `BASIC_INSTRUCTIONS.md` when scaffolding a new project |
| `build.ps1` / `build.bat` | Nuitka build pipeline — compiles to standalone `.exe` files in `dist\` |
| `Launch TokenSave Manager.bat` | Reads `python_exe` from `manager-config.json` via PowerShell, launches manager |
| `docs/ARCHITECTURE.md` | Class structure, UI layout, data flow, threading model, config system |
| `docs/ARCHITECTURE_TOKENSAVE.md` | How tokensave.exe works internally |
| `docs/GITHUB_GUIDE.md` | Beginner GitHub guide — shipped in `dist\docs\` |
| `docs/ROADMAP.md` | Staged plan for local AI features |
| `CHANGELOG.md` | Feature history |

---

## Build Pipeline (build.ps1 / build.bat)

Produces two standalone `.exe` files in `dist\` with no Python install required.
Run by double-clicking `build.bat`.

```
dist\
  tokensave-manager.exe         — main GUI
  tokensave-wrapper.exe         — MCP wrapper for Claude Desktop
  manager-config.json           — clean (user configures on first run)
  manager-config.example.json   — annotated template
  TOKENSAVE_GUIDE.md
  CHANGELOG.md
  templates\                    — all template files copied here
  docs\                         — GITHUB_GUIDE.md, ARCHITECTURE.md, ARCHITECTURE_TOKENSAVE.md
```

Key Nuitka flags:
- `--onefile` — single portable executable per script
- `--windows-console-mode=disable` — no console window for GUI builds
- `--enable-plugin=tk-inter` — bundles TCL/TK DLLs (critical; silent crash without it)
- `--include-package=PIL,pystray` — bundle GUI/tray dependencies recursively
- `--remove-output` — cleans temp `.build`/`.dist` dirs after packing
- `--assume-yes-for-downloads` — auto-accepts MinGW download if MSVC absent
- `--windows-icon-from-ico` — optional; place `icon.ico` in project root

`build.ps1` runs `Clear-NuitkaOrphans` before and after each compile to remove leftover `*.onefile-build` / `*.build` dirs that AV file locks may have prevented cleanup of.

Prerequisites: `pip install nuitka ordered-set zstandard pillow pystray`

See `templates/NUITKA_GOTCHAS.md` for known pitfalls (BOM in config, cp1252 subprocess crash, etc.).

---

## Project-Specific Rules

Rules are grouped by subsystem. The top **Working Rules** section governs *how* to make changes; this section governs the *invariants* the code already obeys.

### Config & State

- **`manager-config.json` is the only file with absolute paths.** All other path logic derives from `__file__` or reads from config. Never hardcode paths back into `src/app.py`, `src/state.py`, or `src/tokensave-wrapper.py`.
- **`template_dir` is the source of truth** for template paths. `basic_instructions_template` and `baseline_include_line` are derived from it automatically via `cfg.refresh_derived()`.
- **`project-baseline.md` is shared across all retrofitted projects.** Changes propagate immediately to every project that has been retrofitted — edit carefully.
- **`search_roots` dual format** — `_root_path(r)` and `_root_label(r)` are the only places that should read a root entry. Never access `r["path"]` or `r` directly elsewhere; bare strings and dicts must both be handled transparently.
- **`project_categories` is the sole source of truth** for per-project category overrides. `refresh()` reads it directly from `cfg` — no in-memory cache. `_do_assign_category()` always calls `cfg.save()` after mutating it.
- **Settings dialog opens automatically on startup** if `tokensave_exe` or `template_dir` are missing or invalid — this is the intended first-run experience on a new machine.
- **`_active_snippets_map`** is the single source of truth for listbox index → snippet data. Never do raw index arithmetic against `PROMPT_SNIPPETS` length — always consult the map. The separator entry has `type == "separator"` and must be skipped in all action handlers.

### Subprocess

- **`CREATE_NO_WINDOW`** must be passed to every `subprocess.Popen` call to suppress Windows console popups.
- **`.bat` build scripts MUST be invoked via `cmd.exe /c <name>`** — passing the bare `.bat` to `subprocess.Popen` raises `WinError 193: %1 is not a valid Win32 application` on Windows because `.bat` files aren't executables. `.ps1` files use `powershell -ExecutionPolicy Bypass -File <name>`.
- **`_GIT_ENV_NO_PROMPT`** must be passed as `env=` to `_shell_capture()` for all network git commands (push, pull). It sets `GIT_TERMINAL_PROMPT=0`, preventing an infinite hang when credentials aren't cached. Compatible with Git Credential Manager (GCM authenticates via browser, not stdin).
- **Windows `.cmd` resolution priority** — `_detect_npm()`, `_detect_codegraph()`, `_detect_claude_cli()` all probe `.cmd` BEFORE the bare name (`shutil.which("name.cmd")` first, then `shutil.which("name")`). This is critical because npm-installed CLIs on Windows are `.cmd` shims, not `.exe` files; `subprocess.run([list, ...])` with a bare `.cmd` raises `FileNotFoundError` unless the full filename including extension is passed.
- **`cmd.exe` multi-quote parsing**: when both an exe path and an argument contain spaces, `cmd.exe` strips the outermost quotes. The fix used in `helpers/claude_cli.py` is the `""outer""` double-double-quote wrapper passed as a raw string with `CREATE_NEW_CONSOLE`, never as a list. See the helper's docstring before adding similar patterns.

### GUI / Tkinter

- **All tkinter widget updates from background threads must go through `self.after(0, ...)`** with a `winfo_exists()` guard. Direct widget calls from threads will crash. Background-built widgets must be constructed on the main thread via `after(0, ...)` — never inside the worker.
- **`messagebox.askyesno` must run on the main thread** — in `cmd_git_init`, the "Create initial commit?" prompt is scheduled via `self.after(0, ask_initial_commit)` from the background thread.
- **Catppuccin palette key is `subtext`, NOT `subtext0`.** The only subtext shade defined in `C` is `subtext` (#bac2de). Using `C["subtext0"]` raises `KeyError` and silently aborts the current widget build.
- **`SettingsDialog` content packs onto `body`, NOT `self`.** The dialog is wrapped in a `Canvas + body Frame` with a vertical scrollbar. Every section / row / widget added inside the dialog MUST use `body` as parent. Only two exceptions: (1) the outer `_scroll_wrap` Frame is packed on `self`, and (2) the Save/Cancel `btn_row` is packed on `self` so it stays anchored at the bottom. Mousewheel is bound on both `_canvas` and `body`; if you add a new scrollable sub-widget inside the dialog, intercept the wheel event so it doesn't double-scroll.
- **`_Tooltip(widget, text)`** is the hover-tooltip helper. 650 ms delay before showing, auto-destroyed on `<Leave>` or `<ButtonPress>`. Add tooltips to any new button targeting beginner users.
- **Treeview iids**: Category/sub-category rows use `iid="cat:<name>"` / `iid="sub:<cat>:<sub>"`. Project rows use `iid="proj:<path>"`. `_selected_path()` and `_on_right_click()` both guard by checking `iid.startswith("proj:")` — never assume the selected row is a project row.
- **`except as e` deferred-lambda NameError**: Python clears the exception variable at the end of the `except` block, so `except Exception as e: ... after(0, lambda: messagebox.showerror("...", str(e)))` will NameError when the lambda fires. Fix: `err_msg = str(e)` then `lambda m=err_msg:`. Already fixed in `dialogs/settings.py`, `controllers/shadowlinks_ctrl.py` — match this pattern.
- **Late-binding lambda in loops**: when generating rows in a for loop, capture loop variables as default kwargs: `lambda f=filepath, b=btn: handler(f, b)`. Without `f=filepath`, every callback captures the last loop value.
- **`ProposalBridge` threading invariants** (Roadmap-2 P1 — applies to ANY future agent write-tool work that wires through `dialogs/proposal.py:ProposalBridge`): (1) `event.wait()` only ever runs on the agent worker thread — NEVER the Tk main thread. (2) Dialog construction and destruction always happen on Tk main via `root.after(0, ...)`. (3) `_resolve()` is idempotent under a `threading.Lock` — first resolution wins; late clicks after timeout, double-callbacks, and external cancellation all reduce to no-ops. (4) Disk I/O happens on the agent worker thread (in `post_accept`), not Tk main, so AV / OneDrive / Defender stalls can't hitch the GUI. (5) Each agent run gets its OWN bridge instance — never share, or an abandoned proposal can starve concurrent runs. (6) `AskTabController` registers active bridges in a Lock-guarded set; `App._quit_app` calls `cancel_all_proposals()` before destroy so worker threads never deadlock on a destroyed Tk event loop.

### Git Operations

- **Git commands use `git -C <project_path>`** — they operate on the selected project's own repo. No git history is stored in or associated with the manager.
- **`_is_local_git_repo(path)` vs `_is_git_repo(path)`** — `_is_local_git_repo` uses `os.path.exists(.git)` (supports both directories AND worktree pointer files). `_is_git_repo` uses `git rev-parse --git-dir` which walks UPWARD and would falsely identify nested project folders inside an unrelated parent repo. Use `_is_local_git_repo` whenever the intent is "should we treat this folder as its own version-controlled project?". `_offer_commit_after_change` uses the local check.
- **`GIT_EXE` / `cfg.git_exe` is the single source for the git executable path.** All git subprocess calls use `[self._cfg.git_exe, ...]` — never bare `"git"`.
- **`_is_auth_error(text)`** is the single check for GitHub authentication failures. Use it after every push/pull rc != 0 to decide whether to show the GCM setup message vs. a generic error.
- **`_git_show_diff()`** caps rendering at 2000 lines to prevent main-thread stutter on large generated files.
- **`SwitchBranchDialog.pick()`** is a static synchronous picker used by `cmd_git_delete_branch`. It blocks via `parent.wait_window()` and returns the selected branch name or `""`.
- **Git tab state** — `self._git_path` tracks the project currently shown in the Git tab. `_on_project_select()` and `_on_tab_changed()` keep it in sync. Never read `self._git_path` without a `if not self._git_path: return` guard.
- **Git tab layout order** — action buttons are packed **before** the diff viewer in `_build_git_tab()`. The diff viewer has `expand=True` and fills remaining space. Buttons stay visible even when the window is small.
- **`cmd_git_log` does not open a popup.** It switches the notebook to the Git tab and calls `_git_refresh()` so all git information lives in one place.
- **`_git_op_in_flight` + `_git_begin_op()` / `_git_end_op()`** is the locking pattern for Git tab operations. Every new git command method MUST follow this shape: call `_git_begin_op()` synchronously before spawning the worker; the worker's `finally` calls `self.after(0, self._git_end_op)`. Prevents double-click races.
- **`_parse_git_status_v2(text)`** and **`_format_git_status_cell(status, has_git)`** are pure functions. They are the only place that knows about the porcelain v2 format. Status dict shape: `{"dirty": bool, "ahead": int, "behind": int, "has_remote": bool}`.
- **Async Git status column** — `_kick_off_git_status_refresh()` runs status checks in a background thread with `time.sleep(0.05)` yield between projects. Caches the result on the project dict via `_git_idx_mtime` keyed against `.git/index` mtime; unchanged projects skip the subprocess. Only one refresh runs at a time. Never run synchronous status checks in `refresh()` itself.
- **Git tag system: baseline + status override.** Project rows are tagged with one baseline tag (`active`/`git_only`/`scaffold`/`normal`) plus one git-status override tag (`git_clean`/`git_dirty`/`git_ahead`/`git_behind`/`git_mixed`/`git_pending`/`git_none`). Tkinter resolves foreground from the later tag in the tuple. `_GIT_STATUS_TAGS` is the canonical set of override tags — never strip tags by `startswith("git_")` because `git_only` is a baseline tag with a completely different meaning.

### Gitignore

- **`.claude/` MUST be in `.gitignore`.** It holds Claude Code's local per-machine session settings (Stop hooks, transcripts) which should never be committed. `_BASELINE_GITIGNORE` (written by `cmd_git_init`) already includes it.
- **`_BASELINE_GITIGNORE`** is written by `cmd_git_init` when no `.gitignore` exists. Covers Python cache, Nuitka build output, tokensave index, and virtual environments. Always write it *before* any `git add -A` call to avoid committing machine-specific binary files.
- **`_ensure_gitignore(path)`** non-destructively merges `_BASELINE_GITIGNORE` entries into an existing `.gitignore`. Idempotent. NOT exposed as a right-click action anymore — the user-facing entry is `GitignoreDialog`.
- **`GitignoreDialog`** is the user-facing `.gitignore` editor. Opened via right-click → **📋 Manage .gitignore…**. Template buttons are push buttons (not checkboxes) so reopening the dialog doesn't lie about which categories were "applied". Blank lines in `_original_lines` are tracked by index but not rendered — preserved verbatim on save. After Save, calls `_offer_commit_after_change(path, ".gitignore")`. Browse + untracked-files panel (Roadmap-1) add files via `_add_custom`; both go through the cross-drive / parent-traversal guards.
- **`_GITIGNORE_TEMPLATES`** is the single source of truth for category → pattern mappings. The Baseline category is derived from `_BASELINE_GITIGNORE` via `_baseline_patterns()` at module load — never hand-maintain two lists.
- **`_read_gitignore_lines(path)` / `_write_gitignore_lines(path, lines)`** are the pure file-IO helpers. Read uses `utf-8-sig` (BOM-tolerant). Write is atomic via `.tmp` + rename and always ends with a trailing newline. Use these instead of inline `open()` calls.
- **Template injection is action-oriented, not stateful.** Clicking `[+ Python]` appends to `_additions`. Never reintroduce checkbox-style templates: reopening the dialog reads raw lines from the file, not category state, so checkboxes would mislead the user.

### Commit / Release

- **`_suggest_commit_message(repo_path, status_text)`** is the multi-strategy orchestrator for the Git Commit dialog suggestion. Chain order (highest-quality first): LLM (if enabled) → CHANGELOG.md staged bullets → diff content → file-name patterns. Every non-empty result passes through `_sanitize_commit_message` which enforces 72-char subjects, imperative mood, blocks filename listings, and escalates generic `chore:` to `refactor:` when source files changed. NEVER duplicate any of these helpers.
- **`_pending_diff(repo_path, *paths)` uses `git diff HEAD`, NOT `git diff --cached`.** The Git Commit dialog generates suggestions BEFORE the user stages files — `--cached` would always return empty. Any new helper needing the pre-commit diff MUST use this function.
- **`_call_llm_for_commit_message(cfg, repo_path)` MUST silent-fallback on every failure path.** Supported providers: `anthropic`, `openai`, `openai_compatible`. Any exception, timeout, missing key, empty response, or sub-`min_diff_lines` diff returns `None`. NEVER let an LLM error surface in the commit dialog. Result is always passed through `_sanitize_commit_message`.
- **Auto-commit-after-sync has two modes.** Default: `chore: tokensave sync` with amend-stacking (each new sync amends the previous sync commit). Opt-in: `commit_message_llm.use_for_sync_autocommit = true` — each sync gets a unique AI-generated message and amend-stacking is DISABLED. Mode is decided in the `_run` worker around the `auto_commit_after_sync` block — never branch this logic anywhere else.
- **GitCommitDialog does per-file staging.** Each working-tree entry is rendered as a checkbox row with a colour-coded status badge. The callback signature is `(path, message, selected_files: list[str])`. `_do_git_commit()` runs `git reset` → `git add -- <selected_files>` → `git commit -m <message>` to guarantee only ticked files are committed.
- **Auto-commit after sync** uses `git diff --cached --quiet` (exits 1 when staged changes exist) to guard the `git commit` call — guarantees no empty commits when the working tree was already clean. If the previous commit was `"chore: tokensave sync"`, the commit is **amended** instead of stacking.
- **`_bump_version` accepts four kinds: patch / minor / major / hotfix.** Hotfix is the intentional non-semver one — bumps a 4th segment (`v1.0.4` → `v1.0.4.1`). Patch/minor/major always normalise BACK to 3-part. `_suggest_bump_kind` never returns "hotfix" — it stays a manual user choice.
- **`_classify_commits_for_changelog(commits)` is the canonical commit-prefix → changelog-section mapping.** Used by `ReleaseWizardDialog` to auto-draft release notes. Edit `_TYPE_TO_SECTION` and `_CONVENTIONAL_RE` in lockstep at module scope. The regex is subject-only by design; body text gets a separate `"BREAKING CHANGE:"` substring check.
- **Release Wizard sequencing — local tag BEFORE push.** Pipeline: build → zip → patch CHANGELOG → stage CHANGELOG only → commit → local `git tag -a` → `git push --follow-tags` → `gh release create`. Never reorder — the recovery messages baked into `_publish_worker` assume this exact sequence.
- **Release Wizard pre-flight refuses dirty trees.** `cmd_git_release` blocks if `git status --porcelain` shows anything other than `CHANGELOG.md`. Do not relax this check.
- **`_zip_dist` MUST strip trailing `.zip` before calling `shutil.make_archive`.** `make_archive` re-appends the format extension automatically. The helper also uses `root_dir=dist_path, base_dir="."` so the resulting zip is flat. Never call `make_archive` directly elsewhere.
- **`_patch_changelog` is idempotent by design.** First scans for an existing `## [<version>]` header; if found, replaces the block. If absent, inserts below `## [Unreleased]`. If neither exists, returns `(False, "missing anchor")` and writes nothing. Atomic write via `.tmp` + `os.replace`. Replacement boundary: only up to (but not including) the next `^## \[` line — manual notes between sections are preserved.
- **`_offer_commit_after_change(path, summary_label)`** is the manager's rule for any operation that writes to project files. Any new method that modifies a project's files MUST call this helper at the end. Silently no-ops when the project isn't a git repo or the working tree is still clean; otherwise prompts and opens `GitCommitDialog`.
- **`_open_commit_dialog(path)`** is the path-explicit version of `cmd_git_commit`. Use it from any flow that already knows the project path.

### Scaffolding / Shadow Links / CodeGraph

- **`_scaffold_git_hook(path)`** writes/merges a Claude Code Stop hook into `.claude/settings.json`. Idempotent — checks for an existing hook whose `command` starts with `"git add -A"` before appending, so scaffold + retrofit on the same project never duplicates the entry.
- **`DEFAULT_SHADOW_EXT_MAP` supports two key formats.** Dot-prefixed keys (`.zsc`) match by file extension; non-dot keys (`DECORATE`) match by exact filename, case-insensitive. Both use the same shadow filename logic: `original_path + suffix`. `update_gitignore_for_shadows` emits a glob for extension keys (`*.zsc.cpp`) and a literal name for name keys (`DECORATE.cpp`).
- **Shadow links are NTFS hardlinks** — zero extra disk space, update instantly with the source file, can be deleted without affecting the original. They only work on NTFS volumes. Do not add shadow link generation to any path that may be on FAT32/exFAT.
- **`CODEGRAPH_EXE` / `cfg.codegraph_exe` parallels `git_exe`** — the single source for the codegraph CLI path. **Empty string when not installed** (not the bare command name) so `if self._cfg.codegraph_exe:` cleanly tests installation. All codegraph subprocess calls must pass `[self._cfg.codegraph_exe, ...]` — never `["codegraph", ...]`.
- **`cmd_codegraph_init` uses `--index`** — matches `tokensave init` (single-step build).
- **CodeGraph auto-syncs only while its MCP server is running** — the file watcher is tied to the MCP server lifecycle. Edits made with Claude Code closed are not seen until the next session catches up. The right-click → 🧠 CodeGraph Sync command stays useful for forcing an incremental update.
- **The manager does NOT call `codegraph install`** — that's codegraph's own one-time MCP-config wizard. The user runs `npx @colbymchenry/codegraph` once globally. TokenSave Manager only handles per-project lifecycle (init/sync/status/remove).

### GitHub Setup

- **`GitHubSetupDialog`** is the GitHub onboarding wizard. Step 2 offers **Sign in to GitHub** (primary, `github.com/login`) and **Create free account** (secondary, `github.com/signup`). Checks git identity, remote URL, and `gh` CLI availability on open, then updates step indicators (✅/⚠️/ℹ️/⬜) live. The Create Release flow shells out to `gh release create` and is only shown when `shutil.which("gh")` returns a path. `_build()` is wrapped in a diagnostic try/except so any widget-creation failure surfaces as a messagebox rather than being swallowed by `pythonw`.

---

## Known Gaps / Roadmap

Items the manager currently lacks. Document them here so Claude doesn't try to "fill the gap" implicitly and so future sessions can pick them up intentionally.

| Gap | Notes |
|-----|-------|
| **tokensave branch support** | No UI for `tokensave branch add/list/gc` — must run from CLI |
| **Cross-platform support** | Windows-only (`os.startfile`, `CREATE_NO_WINDOW`, PowerShell build scripts) |
| **Git diff / patch view** | Git Log shows `--oneline` + `status --short` only; no inline diff or commit details |
| **Multi-repo awareness** | Projects with submodules or monorepos appear as a single entry |
