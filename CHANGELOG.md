# Changelog — TokenSave Manager
<!-- Created by Alexander L Corthell -->

## [Unreleased]

## [0.9.0] — 2026-05-18

### Added
- **Nuitka build template scaffolding** — both Scaffold and Retrofit Existing dialogs now have an "Add Nuitka build files" checkbox; when ticked, `build.ps1` + `build.bat` are copied from the templates folder into the target project with `[PROJECT_NAME]` pre-filled
- `_scaffold_nuitka_build()` helper method — handles template copying, skips files that already exist, logs tips for the remaining placeholders (`[ENTRY_SCRIPT]`, `[OUTPUT_NAME]`)
- **Three new template files** shipped in `dist\templates\` (and usable by any project via Claude Code):
  - `templates/nuitka-build.ps1.template` — full build script with pre-flight checks, `Clear-NuitkaOrphans`, GUI/CLI variant blocks, conditional payload size check
  - `templates/nuitka-build.bat.template` — one-line execution policy bypass launcher
  - `templates/NUITKA_GOTCHAS.md` — 11-section Nuitka pitfalls reference (silent crash, BOM, cp1252, onefile paths, orphan files, cache poisoning, etc.)
- Help tab "Nuitka Build Files" section — explains placeholders, references NUITKA_GOTCHAS.md, and includes a Claude Code "ask Claude" tip for users who prefer typing over clicking
- Credit bar at bottom of main window and "Created by Alexander L Corthell" in Help tab About section

### Changed
- `build.ps1` now uses separate arg sets for manager (GUI: tk-inter + PIL + pystray) and wrapper (CLI: headless, no GUI plugins) — wrapper build is smaller and correctly skips the GUI size check
- `build.ps1` size sanity check now parses the **uncompressed payload size** from Nuitka's build log (e.g. 55 MB) instead of the compressed exe size (~14 MB), eliminating the false-positive WARN that fired every build
- `build.ps1` includes `Clear-NuitkaOrphans` before and after each compile to remove leftover `*.onefile-build` / `*.build` dirs that AV file locks may have stranded
- `templates/project-baseline.md` updated with "Compiling with Nuitka" section — instructs Claude Code to use the templates when asked to set up a build pipeline
- `docs/ARCHITECTURE.md` updated: repository layout, class hierarchy, scaffold/retrofit flow diagrams, new Template System section covering all 5 templates, Nuitka onefile path resolution pattern
- `BASIC_INSTRUCTIONS.md` updated: project structure, key files table, build pipeline section, config key descriptions

### Fixed
- `subprocess.Popen` calls in manager and sync-all worker now use `encoding="utf-8", errors="replace"` — prevents `'charmap' codec can't decode` crash when syncing projects with UTF-8 tool output on Windows
- Toolbar and hint bar now packed `side=tk.BOTTOM` before the body frame — prevents them being hidden when the window is resized below ~400 px height
- Settings dialog no longer shows the `python_exe` field (preserved in JSON but irrelevant to compiled exe users)
- `_open_guide()` now resolves `TOKENSAVE_GUIDE.md` from `_BASE_DIR` instead of `TEMPLATE_DIR`

## [0.8.0] — 2026-05-17

### Added
- `manager-config.json` — single source of truth for all machine-specific paths (`tokensave_exe`, `template_dir`, `python_exe`, `search_roots`); replaces all hardcoded constants in source files
- Settings dialog — edit all paths and search roots through the GUI with Browse buttons; changes apply immediately without restart
- Startup config validation — if `tokensave_exe` or `template_dir` are missing/invalid on launch, Settings dialog opens automatically with a red banner describing what needs fixing
- `build.ps1` — two-stage pipeline: Stage 1 copies source to a staging folder; Stage 2 (stubbed) contains ready-to-enable Nuitka compile flags

### Changed
- `tokensave-manager.py` and `tokensave-wrapper.py` both load from `manager-config.json` — no more duplicated hardcoded constants across files
- `Launch TokenSave Manager.bat` reads `python_exe` from config via PowerShell and uses `%~dp0` for script path — fully portable, no machine-specific paths in the file
- Settings button added to toolbar (right side)

### Removed
- `src/tokensave-wrapper.ps1` — legacy PowerShell wrapper deleted; Python wrapper is authoritative
- `__pycache__/` from project root — leftover from before `src/` restructure

### Fixed
- `.gitignore` now covers `__pycache__/`, `*.pyc`, `logs/`, `dist/`, `build/`
- `.tokensave/config.json` excludes updated to include `dist/**`, `logs/**`, `__pycache__/**`

## [0.7.0] — 2026-05-17

### Added
- Right-click context menu on project rows — all per-project actions (Set Active, Sync, Status, Force Re-sync, Doctor, Remove, Auto-detect) accessible without toolbar buttons
- Remove Index — deletes `.tokensave/` from a project, removing it from the list (project files untouched)
- Sync All — syncs every indexed project sequentially with `[i/n]` progress logging and a final summary
- Auto-refresh — project list silently re-scans every 60 s; skips if a sync is running
- System tray icon — close or minimize hides to tray; right-click tray → Show / Quit
- Single-instance lock — launching a second manager focuses the existing window instead of opening a duplicate
- Scaffold dialog — replaces the bare folder-picker + messagebox; shows checkboxes for BASIC_INSTRUCTIONS.md and tokensave init with state pre-detected from the folder
- Pending row — new project appears in the list as "(indexing…)" immediately when init starts, replaced by real data on completion

### Changed
- Toolbar reduced to 4 global buttons (Scaffold, Retrofit Existing, Sync All, Refresh); per-project actions moved to context menu
- OUTPUT log panel now always visible at any window size (fixed pack order)
- Help tab updated to describe context menu layout and new features
- File Locations in Help tab corrected to `src/` and `templates/` paths

## [0.4.0] — 2026-05-16

### Added
- Reference tab with CLI cheatsheet and 12 copy-paste Claude prompt snippets
- 📖 Open Full Guide button linking to TOKENSAVE_GUIDE.md
- 📊 Status, ⟳ Force Re-sync, 🔍 Doctor quick-action buttons on Projects tab
- TOKENSAVE_GUIDE.md — complete tokensave CLI + MCP reference (34 commands, 48 tools, prompt recipes)

### Changed
- Project restructured: source files → `src/`, templates → `templates/`, docs → `docs/`

## [0.3.0] — 2026-05-16

### Added
- Scaffold and Retrofit now show completion messagebox (visible confirmation of what happened)
- Error handling in scaffold/retrofit worker threads — failures surface as error dialogs
- Scaffold: offers to run `tokensave init` after creating BASIC_INSTRUCTIONS.md
- Retrofit: "Also create BASIC_INSTRUCTIONS.md" now defaults to checked

### Fixed
- Scaffold hint label was still showing old "docs/, README, CHANGELOG" description after simplification
- Help tab scaffold description listed removed features (docs/, README.md, CHANGELOG.md)

## [0.2.0] — 2026-05-16

### Added
- Help tab with operational guide (switching projects, button reference, file locations)
- Scaffold column in project treeview showing ✔ / — for BASIC_INSTRUCTIONS.md presence
- Retrofit dialog with two checkboxes (tokensave @include, BASIC_INSTRUCTIONS.md)
- project-baseline.md — universal rules file @included by all retrofitted projects
- claude-md-template.md — adaptive BASIC_INSTRUCTIONS template (checks existing structure)
- Scaffold button creates BASIC_INSTRUCTIONS.md only; Claude handles docs structure adaptively

### Changed
- Simplified scaffold: removed hardcoded docs/, README.md, CHANGELOG.md creation
- Template updated to instruct Claude to inspect existing project structure before filling in

## [0.1.0] — 2026-05-11

### Added
- Initial GUI: Projects tab with indexed project treeview
- Set as Active, Sync, Auto-detect, Refresh buttons
- find_projects() scanning SEARCH_ROOTS for .tokensave/tokensave.db
- Pin file system: %USERPROFILE%\.tokensave\desktop-project.txt
- tokensave-wrapper.py: auto-detection wrapper for Claude Desktop
- Background threading for all tokensave CLI calls
- Log output panel
- Catppuccin Mocha dark theme
