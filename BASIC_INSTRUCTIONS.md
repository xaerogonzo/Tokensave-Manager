# TokenSave Manager — Basic Instructions

@D:\Claude Co worker\Token Save Manager Source\templates\project-baseline.md

---

## Project Overview

**Name:** TokenSave Manager  
**Stack:** Python 3, tkinter/ttk (GUI), subprocess (tokensave CLI), threading, pystray (tray icon), Pillow  
**Entry point:** `Launch TokenSave Manager.bat` → reads `python_exe` from `manager-config.json` → `src/tokensave-manager.py`  
**Purpose:** Windows GUI for managing tokensave MCP project integrations — switching active projects for Claude Desktop, syncing indexes, scaffolding new projects with Claude instruction templates and/or Nuitka build pipelines, and managing search roots via a Settings dialog.

**Current source location:** `D:\Claude Co worker\Token Save Manager Source\`  
**Runtime dependency:** `tokensave.exe` — path stored in `manager-config.json` (not bundled with source)

---

## Project Structure

```
Token Save Manager Source/
├── manager-config.json            Machine-specific config (all hardcoded paths live here)  — gitignored
├── manager-config.example.json    Clean template with placeholder paths — committed for new users
├── Launch TokenSave Manager.bat   Reads python_exe from config, launches src/tokensave-manager.py
├── build.ps1                      Nuitka compile pipeline — produces dist\ exes
├── build.bat                      Double-click launcher for build.ps1
├── BASIC_INSTRUCTIONS.md          This file
├── CHANGELOG.md                   Feature history
├── TOKENSAVE_GUIDE.md             Full tokensave CLI + MCP reference
├── .gitignore                     Excludes manager-config.json, .claude/, .tokensave/, dist/, etc.
│
├── src/
│   ├── tokensave-manager.py       Main GUI (~4,700 lines) — App class + dialog classes
│   └── tokensave-wrapper.py       Claude Desktop auto-detection wrapper (MCP server launcher)
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
│   ├── manager-config.json        Clean config for new installs
│   ├── manager-config.example.json
│   ├── TOKENSAVE_GUIDE.md
│   ├── CHANGELOG.md
│   ├── templates\
│   └── docs\                      GITHUB_GUIDE.md, ARCHITECTURE.md, ARCHITECTURE_TOKENSAVE.md
│
└── docs/
    ├── ARCHITECTURE.md            Manager architecture reference (UI, data flow, threading)
    ├── ARCHITECTURE_TOKENSAVE.md  tokensave tool internals reference
    └── GITHUB_GUIDE.md            Beginner GitHub guide — concepts, setup, daily workflow, releases
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
| `git_exe` | Optional absolute path to `git.exe`. Blank = auto-detect via `shutil.which("git")` + common Windows install paths. Used by every git subprocess call as `GIT_EXE`. Configurable via Settings → Git exe row (Browse / Auto-detect / Verify). |
| `search_roots` | List of `str \| {"path": str, "label": str}` — scanned for tokensave projects. Bare strings are backward-compatible (`label` defaults to `basename`). Each root's label becomes its category header in the Treeview. |
| `project_categories` | Dict `{path: {"category": str, "subcategory"?: str}}` — per-project category override. Set via right-click → 📁 Assign Category…; persisted here automatically. |
| `user_snippets` | List of `{"title": str, "text": str}` dicts — user-defined Claude prompt snippets |
| `auto_commit_after_sync` | Boolean (default `false`) — if `true`, auto-runs `git add -A + git commit` after every successful `tokensave sync` on a git-repo project. If the previous commit was already `"chore: tokensave sync"`, the new auto-commit is amended onto it instead of stacking. |

---

## Documentation Files

| File | Purpose |
|------|---------|
| `docs/ARCHITECTURE.md` | Class structure, UI layout, data flow, threading model, config system |
| `docs/ARCHITECTURE_TOKENSAVE.md` | How tokensave.exe works internally |
| `docs/GITHUB_GUIDE.md` | Beginner GitHub guide — git/GitHub concepts, first-time setup, daily workflow, branches, releases, common problems, glossary. Shipped in `dist\docs\` |
| `CHANGELOG.md` | Feature history |

---

## Key Files

| File | Role |
|------|------|
| `src/tokensave-manager.py` | Entire GUI — `App(tk.Tk)` + `RetrofitDialog`, `ScaffoldDialog`, `SettingsDialog`, `SnippetEditDialog`, `ShadowLinksDialog`, `AssignCategoryDialog`, `SetRemoteDialog`, `NewBranchDialog`, `SwitchBranchDialog`, `GitCommitDialog`, `GitHubSetupDialog` |
| `src/tokensave-wrapper.py` | MCP server wrapper for Claude Desktop — reads same `manager-config.json` |
| `manager-config.json` | Single source of truth for all machine-specific paths |
| `templates/project-baseline.md` | @included by every retrofitted project's CLAUDE.md — edit here to update all |
| `templates/claude-md-template.md` | Written as `BASIC_INSTRUCTIONS.md` when scaffolding a new project |
| `templates/nuitka-build.ps1.template` | Generic Nuitka build script — copied by Scaffold/Retrofit Nuitka option |
| `templates/nuitka-build.bat.template` | Bat launcher — copied alongside `nuitka-build.ps1.template` |
| `templates/NUITKA_GOTCHAS.md` | Pre-build reading for anyone using the Nuitka templates |
| `build.ps1` | Nuitka build pipeline — compiles manager + wrapper to standalone `.exe` files in `dist\` |
| `build.bat` | Double-click to compile — calls `build.ps1` via `powershell -ExecutionPolicy Bypass` |
| `Launch TokenSave Manager.bat` | Reads `python_exe` from `manager-config.json` via PowerShell, launches manager |

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
  templates\                    — all 5 template files copied here
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

`build.ps1` also runs `Clear-NuitkaOrphans` before and after each compile to remove any
leftover `*.onefile-build` / `*.build` dirs that AV file locks may have prevented cleanup of.

Prerequisites: `pip install nuitka ordered-set zstandard pillow pystray`

See `templates/NUITKA_GOTCHAS.md` for known pitfalls (BOM in config, cp1252 subprocess crash, etc.).

---

## Project-Specific Rules

- **`manager-config.json` is the only file with absolute paths.** All other path logic derives from `__file__` or reads from config. Never hardcode paths back into `tokensave-manager.py` or `tokensave-wrapper.py`.
- **`template_dir` is the source of truth** for template paths. `BASIC_INSTRUCTIONS_TEMPLATE` and `BASELINE_INCLUDE_LINE` are derived from it automatically.
- **`project-baseline.md` is shared across all retrofitted projects.** Changes propagate immediately to every project that has been retrofitted — edit carefully.
- **All tkinter widget updates must go through `self.after(0, ...)`** when called from a background thread. Direct widget calls from threads will crash.
- **`CREATE_NO_WINDOW`** must be passed to every `subprocess.Popen` call to suppress Windows console popups.
- **Settings dialog opens automatically on startup** if `tokensave_exe` or `template_dir` are missing or invalid — this is the intended first-run experience on a new machine.
- **`_active_snippets_map`** is the single source of truth for listbox index → snippet data. Never do raw index arithmetic against `PROMPT_SNIPPETS` length — always consult the map. The separator entry has `type == "separator"` and must be skipped in all action handlers.
- **Git commands use `git -C <project_path>`** — they operate on the selected project's own repo. No git history is stored in or associated with the manager.
- **`_is_git_repo(path)`** is the module-level guard used by both `cmd_git_init` (to avoid re-initialising) and `GitCommitDialog` (to disable the Commit button if no repo exists). Always use it rather than re-running `git rev-parse` inline.
- **`messagebox.askyesno` must run on the main thread** — in `cmd_git_init`, the "Create initial commit?" prompt is scheduled via `self.after(0, ask_initial_commit)` from the background thread, not called directly.
- **`DEFAULT_SHADOW_EXT_MAP` supports two key formats.** Dot-prefixed keys (`.zsc`) match by file extension; non-dot keys (`DECORATE`) match by exact filename, case-insensitive. Both use the same shadow filename logic: `original_path + suffix`. `update_gitignore_for_shadows` emits a glob for extension keys (`*.zsc.cpp`) and a literal name for name keys (`DECORATE.cpp`). `remove_shadow_links` checks both types when identifying shadows to delete.
- **Shadow links are NTFS hardlinks** — zero extra disk space, update instantly with the source file, and can be deleted without affecting the original. They only work on NTFS volumes (standard on Windows). Do not add shadow link generation to any path that may be on FAT32/exFAT.
- **`search_roots` dual format** — `_root_path(r)` and `_root_label(r)` are the only places that should read a root entry. Never access `r["path"]` or `r` directly elsewhere; bare strings and dicts must both be handled transparently.
- **`project_categories`** is the sole source of truth for per-project category overrides. `refresh()` reads it directly from `_cfg` — no in-memory cache. `_do_assign_category()` always calls `_save_config(_cfg)` after mutating it.
- **Category/sub-category Treeview rows** use `iid="cat:<name>"` / `iid="sub:<cat>:<sub>"`. Project rows use `iid="proj:<path>"`. `_selected_path()` and `_on_right_click()` both guard by checking `iid.startswith("proj:")` — never assume the selected row is a project row.
- **Git tab state** — `self._git_path` tracks the project currently shown in the Git tab. `_on_project_select()` (bound to `<<TreeviewSelect>>`) and `_on_tab_changed()` (bound to `<<NotebookTabChanged>>`) keep it in sync. Never read `self._git_path` without a `if not self._git_path: return` guard.
- **`_GIT_ENV_NO_PROMPT`** must be passed as `env=` to `_shell_capture()` for all network git commands (push, pull). It sets `GIT_TERMINAL_PROMPT=0`, preventing an infinite hang when credentials aren't cached. Compatible with Git Credential Manager (GCM authenticates via browser, not stdin).
- **`_is_auth_error(text)`** is the single check for GitHub authentication failures. Use it after every push/pull rc != 0 to decide whether to show the GCM setup message vs. a generic error.
- **`_git_show_diff()`** caps rendering at 2000 lines to prevent main-thread stutter on large generated files.
- **`SwitchBranchDialog.pick()`** is a static synchronous picker used by `cmd_git_delete_branch`. It blocks via `parent.wait_window()` and returns the selected branch name or `""`.
- **`_scaffold_git_hook(path)`** writes/merges a Claude Code Stop hook into `.claude/settings.json`. It is idempotent — checks for an existing hook whose `command` starts with `"git add -A"` before appending, so calling it multiple times (scaffold + retrofit on the same project) never duplicates the entry.
- **Auto-commit after sync** uses `git diff --cached --quiet` (exits 1 when staged changes exist, 0 when clean) to guard the `git commit` call — this guarantees no empty commits are created when the working tree was already clean. If the previous commit was also `"chore: tokensave sync"`, the commit is **amended** instead of creating a new one, preventing history pile-up.
- **`_BASELINE_GITIGNORE`** is written by `cmd_git_init` when no `.gitignore` exists in the target project. It covers Python cache, Nuitka build output, tokensave index, and virtual environments. Always write it *before* any `git add -A` call to avoid committing machine-specific binary files.
- **`_ensure_gitignore(path)`** is a module-level helper that non-destructively merges `_BASELINE_GITIGNORE` entries into an existing `.gitignore`. It reads the current file, builds a set of non-comment lines that are already present, and appends only the lines that are missing. Returns a `list[str]` of human-readable result lines (suitable for `self._log()`). Idempotent — safe to run repeatedly. Still called by `cmd_git_init` to write the baseline when initialising a brand-new repo. **NOT** exposed as a right-click action anymore — the user-facing entry is now `GitignoreDialog`.
- **`GitignoreDialog`** is the user-facing `.gitignore` editor. Opened via right-click → **📋 Manage .gitignore…** (`cmd_manage_gitignore`). Supports add (template injection + custom entry) and remove (with × → ↺ toggle and confirm-on-comment). Uses a canvas-backed scrollable Frame for the current-entries list (a `tk.Listbox` cannot embed per-row buttons). Strikethrough via `tkfont.Font(overstrike=1)`. Template buttons are push buttons (not checkboxes) so reopening the dialog doesn't lie about which categories were "applied". Blank lines in `_original_lines` are tracked by index but not rendered — preserved verbatim on save. After Save, calls `_offer_commit_after_change(path, ".gitignore")` so dirty state never sits silently. The Baseline category is the first template button and matches what `_ensure_gitignore` writes.
- **`_GITIGNORE_TEMPLATES`** is the single source of truth for category → pattern mappings. The Baseline category is derived from `_BASELINE_GITIGNORE` via `_baseline_patterns()` at module load — never hand-maintain two lists. To add a new category, append a new dict entry; the dialog's template button row picks it up automatically.
- **`_read_gitignore_lines(path)` / `_write_gitignore_lines(path, lines)`** are the pure file-IO helpers used by `GitignoreDialog`. Read uses `utf-8-sig` (BOM-tolerant). Write is atomic via `.tmp` + rename and always ends with a trailing newline. Use these instead of inline `open()` calls for any new gitignore work.
- **`CODEGRAPH_EXE` parallels `GIT_EXE`** — the single source for the codegraph CLI path. Resolved at startup from `_cfg["codegraph_exe"] or _detect_codegraph()`, rebuilt in `_on_settings_saved` via `global CODEGRAPH_EXE`. **Empty string when not installed** (not the bare command name) so `if CODEGRAPH_EXE:` cleanly tests installation. All codegraph subprocess calls must pass `[CODEGRAPH_EXE, ...]` — never `["codegraph", ...]`.
- **Windows `.cmd` resolution priority** — `_detect_npm()` and `_detect_codegraph()` probe `.cmd` BEFORE the bare name (`shutil.which("npm.cmd")` first, then `shutil.which("npm")`). This is critical because npm-installed CLIs on Windows are `.cmd` shims, not `.exe` files; `subprocess.run([list, ...])` with a bare `.cmd` raises `FileNotFoundError` unless the full filename including extension is passed.
- **`cmd_codegraph_init` uses `--index`** — matches the experience of `tokensave init`, which also builds the initial graph in one step. Without `--index`, codegraph just creates `.codegraph/config.json` and a user would have to manually run `codegraph index` after. The single-step flow makes the right-click menu predictable.
- **CodeGraph auto-syncs only while its MCP server is running** — the file watcher is tied to the MCP server lifecycle, which Claude Code spawns on-demand. Edits made with Claude Code closed are not seen until the next session catches up. The right-click → 🧠 CodeGraph Sync command stays useful for forcing an incremental update manually. Never remove it on the assumption that auto-sync covers everything.
- **The manager does NOT call `codegraph install`** — that's codegraph's own one-time MCP-config wizard (writes to `~/.claude.json`, optional Cursor/Codex/opencode targets). The user runs `npx @colbymchenry/codegraph` once globally. The TokenSave Manager only handles per-project lifecycle (init/sync/status/remove). Re-implementing the install wizard would duplicate maintenance and risk drifting from upstream.
- **`_is_local_git_repo(path)` is the correct local check** — uses `os.path.exists(.git)` (supports both directories AND worktree pointer files). The existing `_is_git_repo` uses `git rev-parse --git-dir` which walks UPWARD and would falsely identify nested project folders inside an unrelated parent repo. Use `_is_local_git_repo` whenever the intent is "should we treat this folder as its own version-controlled project?". `_offer_commit_after_change` uses the local check.
- **Template injection is action-oriented, not stateful.** When a user clicks `[+ Python]` in `GitignoreDialog`, those patterns get appended to `_additions` (or have their pending-removal un-done — smart conflict resolution). The button is a `ttk.Button`, not a `tk.Checkbutton`. Never reintroduce checkbox-style templates: reopening the dialog reads raw lines from the file, not category state, so checkboxes would mislead the user about what's "active".
- **`_suggest_commit_message(status_text)`** is a module-level helper that generates a conventional-commit-style message from `git status --short` output. Called by `GitCommitDialog._fill_suggestion()` — which feeds it only the *currently-selected* file lines, so suggestions reflect what's actually being committed. Pure function, never mutates state.
- **`GitCommitDialog` does per-file staging.** Each working-tree entry is rendered as a checkbox row with a colour-coded status badge (M/A/D/R/?/!) and plain-English description. The callback signature is `(path, message, selected_files: list[str])`. `_do_git_commit()` runs `git reset` → `git add -- <selected_files>` → `git commit -m <message>` to guarantee only ticked files are committed. Files left unchecked remain as un-committed working-tree changes for a later commit. Select All / Select None / Modified Only quick-pick buttons assist common cases.
- **`GitHubSetupDialog`** is the GitHub onboarding wizard. Step 2 offers both **Sign in to GitHub** (primary, `github.com/login`) and **Create free account** (secondary, `github.com/signup`). It checks git identity (`git config --global`), remote URL, and `gh` CLI availability on open, then updates step indicators (✅/⚠️/ℹ️/⬜) live. `_sh()` calls `_app._shell_capture()` — these are fast config queries, safe to run on the main thread. The Create Release flow shells out to `gh release create` and is only shown when `shutil.which("gh")` returns a path. `_build()` is wrapped in a diagnostic try/except so any widget-creation failure surfaces as a messagebox rather than being swallowed by `pythonw`.
- **Catppuccin palette key is `subtext`, NOT `subtext0`.** The only subtext shade defined in `C` is `subtext` (#bac2de). Using `C["subtext0"]` raises `KeyError` and silently aborts the current widget build — verify any new dialog code against the `C = {…}` dict at the top of the file.
- **Git tab layout order** — action buttons are packed **before** the diff viewer in `_build_git_tab()`. The diff viewer has `expand=True` and fills remaining space. This ordering is intentional: it ensures buttons are always visible even when the window is small.
- **`cmd_git_log` does not open a popup.** It switches the notebook to the Git tab and calls `_git_refresh()` so all git information lives in one place. Never re-introduce a separate log dialog.
- **`GIT_EXE` is the single source for the git executable path.** All git subprocess calls use `[GIT_EXE, ...]` — never bare `"git"`. `_detect_git()` resolves it from `_cfg["git_exe"]`, then `shutil.which("git")`, then common Windows install paths, then falls back to `"git"`. `_on_settings_saved()` updates the module global via `global GIT_EXE`.
- **`_Tooltip(widget, text)`** is the hover-tooltip helper. 650 ms delay before showing, auto-destroyed on `<Leave>` or `<ButtonPress>`. Used to give plain-English explanations of every Git tab button. Add tooltips to any new button targeting beginner users.
- **`.claude/` MUST be in `.gitignore`.** It holds Claude Code's local per-machine session settings (Stop hooks, transcripts) which should never be committed. If a user accidentally commits it, the fix is `git rm -r --cached .claude` + new commit + push. `_BASELINE_GITIGNORE` (written by `cmd_git_init`) already includes it.
- **`_offer_commit_after_change(path, summary_label)`** is the manager's rule for any operation that writes to project files. Any new method that modifies a project's files (creates a `.gitignore`, writes templates, etc.) MUST call this helper at the end. It silently no-ops when the project isn't a git repo or the working tree is still clean; otherwise it shows a yes/no prompt and opens `GitCommitDialog` for the project. Never leave a destructive manager action with silent dirty state.
- **`_open_commit_dialog(path)`** is the path-explicit version of `cmd_git_commit`. Use it from any flow that already knows the project path (e.g. the offer-commit-after-change flow). `cmd_git_commit` itself reads the Projects-tab selection and delegates to `_open_commit_dialog`.
- **`_git_op_in_flight` + `_git_begin_op()` / `_git_end_op()`** is the locking pattern for Git tab operations. Every new git command method MUST follow this shape: call `_git_begin_op()` synchronously before spawning the worker; the worker's `finally` calls `self.after(0, self._git_end_op)`. This prevents double-click races. The flag is honoured by `_git_update_ui()` so the lock survives incidental refreshes during an op.
- **`_parse_git_status_v2(text)`** and **`_format_git_status_cell(status, has_git)`** are pure functions. They are the only place that knows about the porcelain v2 format or the Git column display strings — never duplicate this parsing logic elsewhere. The status dict shape is `{"dirty": bool, "ahead": int, "behind": int, "has_remote": bool}`.
- **Async Git status column** — `_kick_off_git_status_refresh()` runs status checks in a background thread with `time.sleep(0.05)` yield between projects. It caches the result on the project dict via `_git_idx_mtime` keyed against `.git/index` mtime; unchanged projects skip the subprocess. Only one refresh runs at a time (re-entrancy is cancelled via `_git_status_refresh_cancel`). Never run synchronous status checks in `refresh()` itself — that would block UI on a 60-second auto-refresh with 10+ projects.
- **Git tag system: baseline + status override.** Project rows are tagged with one baseline tag (`active`/`git_only`/`scaffold`/`normal`) plus one git-status override tag (`git_clean`/`git_dirty`/`git_ahead`/`git_behind`/`git_mixed`/`git_pending`/`git_none`). Tkinter resolves foreground from the later tag in the tuple, so the status override wins visually. `_GIT_STATUS_TAGS` is the canonical set of override tags — never strip tags by `startswith("git_")` because `git_only` is a baseline tag with a completely different meaning.
- **`_classify_commits_for_changelog(commits)` is the canonical commit-prefix → changelog-section mapping.** Used by `ReleaseWizardDialog` to auto-draft release notes. If you ever add a new conventional-commit prefix (e.g. `revert:`) or change which section a prefix maps to, edit `_TYPE_TO_SECTION` and `_CONVENTIONAL_RE` in lockstep at module scope — never duplicate this logic in the dialog. The regex is subject-only by design; body text gets a separate `"BREAKING CHANGE:"` substring check to avoid false positives from prose that mentions `feat:` in passing.
- **Release Wizard sequencing — local tag BEFORE push.** `gh release create` only tags remotely. Without an explicit local `git tag` step, the user's local tree has no record the release happened until the next `git fetch --tags`. The wizard pipeline is fixed at: build → zip → patch CHANGELOG → stage CHANGELOG only → commit → local `git tag -a` → `git push --follow-tags` → `gh release create`. Never reorder these — the recovery messages baked into `_publish_worker` assume this exact sequence so users know exactly what to clean up at each failure point.
- **Release Wizard pre-flight refuses dirty trees.** `cmd_git_release` blocks if `git status --porcelain` shows anything other than `CHANGELOG.md` (which the wizard owns). This guarantees release-prep commits are laser-focused. Do not relax this check — letting unrelated WIP into a release-prep commit is a foot-gun that doesn't fix itself.
- **`.bat` build scripts MUST be invoked via `cmd.exe /c <name>`** — passing the bare `.bat` to `subprocess.Popen` raises `WinError 193: %1 is not a valid Win32 application` on Windows because `.bat` files aren't executables. `.ps1` files use `powershell -ExecutionPolicy Bypass -File <name>`. The Release Wizard's build dispatch branch in `_publish_worker` is the only place this distinction matters today — if you add another script-invocation site, mirror the same dispatch.
- **`_zip_dist` MUST strip trailing `.zip` before calling `shutil.make_archive`.** `make_archive(base_name, format, …)` re-appends the format extension automatically — passing `"foo.zip"` produces `foo.zip.zip` on disk. The helper also uses `root_dir=dist_path, base_dir="."` so the resulting zip is flat (no `dist/` prefix inside the archive). Never call `make_archive` directly elsewhere — always go through `_zip_dist` so both gotchas stay handled in one place.
- **`_patch_changelog` is idempotent by design.** First scans for an existing `## [<version>]` header; if found, the section block is REPLACED (not duplicated). If absent, inserts below the `## [Unreleased]` anchor. If neither exists, returns `(False, "missing anchor")` and writes nothing rather than producing a malformed file. Atomic write via `.tmp` + `os.replace`. Callers should treat the second tuple element as a status string (`"inserted"` / `"replaced"` / error) — never log the boolean alone.

---

## Known Gaps / Roadmap

Items the manager currently lacks. Document them here so Claude doesn't try to "fill the gap" implicitly and so future sessions can pick them up intentionally.

| Gap | Notes |
|-----|-------|
| **tokensave branch support** | No UI for `tokensave branch add/list/gc` — must run from CLI |
| **Daemon management** | No start/stop/status UI for `tokensave daemon` — must run from CLI |
| **Cost tracking** | No UI for `tokensave cost` — must run from CLI |
| **Cross-platform support** | Windows-only (`os.startfile`, `CREATE_NO_WINDOW`, PowerShell build scripts) |
| **Git diff / patch view** | Git Log shows `--oneline` + `status --short` only; no inline diff or commit details |
| **Multi-repo awareness** | Projects with submodules or monorepos appear as a single entry |
