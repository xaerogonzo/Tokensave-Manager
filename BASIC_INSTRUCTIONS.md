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
├── manager-config.json            Machine-specific config (all hardcoded paths live here)
├── Launch TokenSave Manager.bat   Reads python_exe from config, launches src/tokensave-manager.py
├── build.ps1                      Nuitka compile pipeline — produces dist\ exes
├── build.bat                      Double-click launcher for build.ps1
├── BASIC_INSTRUCTIONS.md          This file
├── CHANGELOG.md                   Feature history
├── .gitignore
│
├── src/
│   ├── tokensave-manager.py       Main GUI (~2,000 lines) — App class + dialog classes
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
│   ├── TOKENSAVE_GUIDE.md
│   └── templates\
│
└── docs/
    ├── ARCHITECTURE.md            Manager architecture reference (UI, data flow, threading)
    └── ARCHITECTURE_TOKENSAVE.md  tokensave tool internals reference
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
| `search_roots` | List of `str \| {"path": str, "label": str}` — scanned for tokensave projects. Bare strings are backward-compatible (`label` defaults to `basename`). Each root's label becomes its category header in the Treeview. |
| `project_categories` | Dict `{path: {"category": str, "subcategory"?: str}}` — per-project category override. Set via right-click → 📁 Assign Category…; persisted here automatically. |
| `user_snippets` | List of `{"title": str, "text": str}` dicts — user-defined Claude prompt snippets |
| `auto_commit_after_sync` | Boolean (default `false`) — if `true`, auto-runs `git add -A + git commit` after every successful `tokensave sync` on a git-repo project |

---

## Documentation Files

| File | Purpose |
|------|---------|
| `docs/ARCHITECTURE.md` | Class structure, UI layout, data flow, threading model, config system |
| `docs/ARCHITECTURE_TOKENSAVE.md` | How tokensave.exe works internally |
| `CHANGELOG.md` | Feature history |

---

## Key Files

| File | Role |
|------|------|
| `src/tokensave-manager.py` | Entire GUI — `App(tk.Tk)` + `RetrofitDialog`, `ScaffoldDialog`, `SettingsDialog`, `SnippetEditDialog`, `ShadowLinksDialog`, `AssignCategoryDialog`, `GitCommitDialog` |
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
  tokensave-manager.exe   — main GUI
  tokensave-wrapper.exe   — MCP wrapper for Claude Desktop
  manager-config.json     — clean (user configures on first run)
  TOKENSAVE_GUIDE.md
  templates\              — all 5 template files copied here
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
- **`_scaffold_git_hook(path)`** writes/merges a Claude Code Stop hook into `.claude/settings.json`. It is idempotent — checks for an existing hook whose `command` starts with `"git add -A"` before appending, so calling it multiple times (scaffold + retrofit on the same project) never duplicates the entry.
- **Auto-commit after sync** uses `git diff --cached --quiet` (exits 1 when staged changes exist, 0 when clean) to guard the `git commit` call — this guarantees no empty commits are created when the working tree was already clean.
- **`_BASELINE_GITIGNORE`** is written by `cmd_git_init` when no `.gitignore` exists in the target project. It covers Python cache, Nuitka build output, tokensave index, and virtual environments. Always write it *before* any `git add -A` call to avoid committing machine-specific binary files.

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
