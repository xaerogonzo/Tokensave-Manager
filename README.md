# TokenSave Manager

A Windows GUI for managing [tokensave](https://github.com/aovestdipaperino/tokensave) MCP integrations across multiple projects. Built with Python + tkinter, styled with Catppuccin Mocha.

If you use Claude Code or Claude Desktop across several projects, TokenSave Manager is the control panel: switch active projects, sync indexes, scaffold new projects with Claude instruction templates, manage git history — all without touching the command line.

---

## Table of Contents

- [What is tokensave?](#what-is-tokensave)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
  - [From a compiled release (recommended)](#from-a-compiled-release-recommended)
  - [From source](#from-source)
- [First-Run Setup](#first-run-setup)
- [The Interface](#the-interface)
  - [Projects Tab](#projects-tab)
  - [Git Tab](#git-tab)
  - [Reference Tab](#reference-tab)
  - [Help Tab](#help-tab)
- [Right-Click Menu](#right-click-menu)
- [Git Workflow — Step by Step](#git-workflow--step-by-step)
- [Configuration Reference](#configuration-reference)
- [Building from Source](#building-from-source)
- [Project Structure](#project-structure)
- [Changelog](#changelog)

---

## What is tokensave?

[tokensave](https://github.com/aovestdipaperino/tokensave) is an MCP server and code-graph tool that saves Claude tokens by giving it a structured map of your codebase instead of making it read raw files. It runs as a local daemon, indexes your project into a SQLite database (`.tokensave/tokensave.db`), and exposes tools like `tokensave_context`, `tokensave_search`, and `tokensave_callers` that Claude can call to navigate your code efficiently.

**TokenSave Manager** is the GUI wrapper around that tool — it handles the tedious parts so you don't need to run CLI commands.

---

## Features

### Project Management
- **Automatic project discovery** — scans configured search roots for any folder containing a tokensave index (`.tokensave/tokensave.db`)
- **One-click project switching** — set any project as the active one for Claude Desktop; a pin file tells the wrapper which project to serve
- **Sync / Force Re-sync** — run `tokensave sync` or `tokensave sync --force` on any project from the GUI
- **Sync All** — syncs every indexed project sequentially with `[1/n]` progress logging and a final summary
- **Status + Doctor** — run `tokensave status` and `tokensave doctor` without leaving the app
- **Auto-refresh** — project list silently re-scans every 60 seconds; skips if a sync is running
- **System tray** — close or minimise sends the manager to the tray; right-click to Show or Quit
- **Single-instance lock** — launching a second copy focuses the existing window

### Project Organisation
- **Category grouping** — each search root has a configurable label that becomes a category header in the project list. Projects under `D:\Doom Mods` appear under a "Doom Mods" header, etc.
- **Sub-categories** — right-click any project → **📁 Assign Category…** to put it in a sub-group (e.g. "Doom Mods → GZDoom")
- **Per-project category overrides** — saved in `manager-config.json` so they survive restarts

### Scaffolding & Retrofit
- **Scaffold new project** — picks a folder and optionally writes a `BASIC_INSTRUCTIONS.md` (Claude session instructions), runs `tokensave init`, adds a Nuitka build pipeline, and/or adds an auto-commit Stop hook for Claude Code sessions
- **Retrofit existing project** — adds tokensave MCP rules to an existing `CLAUDE.md` via `@include`, optionally with all the same extras as Scaffold
- **Shadow Links** — generates NTFS hardlinks of source files with a secondary extension (e.g. `.zsc` → `.zsc.cpp`) so editors with limited language support can still parse them. Right-click → **🔗 Shadow Links…**
- **Ensure .gitignore** — right-click → **📋 Ensure .gitignore** non-destructively merges baseline entries (Python cache, Nuitka output, `.tokensave/`, `.claude/`, virtual environments, etc.) into any project's `.gitignore` without overwriting anything custom

### Git Integration (no command line needed)
- **Git tab** — live view of any project's git state: current branch, remote URL, working tree changes, recent commits, colour-coded diff viewer
- **Push / Pull** — one-click with `git push -u origin HEAD` / `git pull`; graceful auth-error message if GitHub credentials aren't cached yet
- **Commit with per-file staging** — every changed file is shown as a checkbox with a colour-coded status badge (M modified / A added / D deleted / R renamed / ? untracked). Tick exactly the files you want, write a message (or use **💡 Suggest** for an auto-generated conventional-commit message), commit only those files
- **Undo Last Commit** — `git reset --soft HEAD~1`; keeps all changes, removes only the commit marker
- **Branch management** — New Branch, Switch Branch, Delete Branch dialogs; no typing required
- **Open PR on GitHub** — on a feature branch, opens the GitHub compare page directly in your browser. On master/main, walks you through the branch workflow step by step
- **Set Remote** — step-by-step dialog for connecting a project to a GitHub repository
- **GitHub Setup wizard** — full onboarding: set git identity, sign in / create account, create repo on GitHub, set remote, first push, create a release
- **Auto-commit after sync** — optional toggle (Settings) that runs `git add -A + git commit` automatically after every successful tokensave sync; amends the previous commit if it was also a sync commit, to avoid history pile-up
- **Auto-commit Stop hook** — optional per-project Claude Code hook that commits whatever Claude changed at the end of each session

### Settings & Tools
- **Settings dialog** — configure all paths (tokensave.exe, template dir, editor command, git.exe) and search roots through a GUI; changes apply immediately
- **GitHub CLI installer** — Settings dialog includes a **"Install via winget"** button that installs the GitHub CLI (`gh`) in the background; shows a green checkmark when found on PATH
- **Git auto-detection** — finds `git.exe` via PATH or common Windows install locations automatically
- **Reference tab** — CLI cheatsheet + 12 built-in Claude prompt snippets (codebase overview, symbol search, impact analysis, health check, etc.) with copy-to-clipboard; add your own custom snippets
- **Help tab** — full operational guide covering every feature, the git workflow, GitHub setup, project categories, and more
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
pythonw src/tokensave-manager.py
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
| **GitHub CLI** | Click **Install via winget** if not already installed |

After saving, the manager scans your search roots and shows any folder that has been initialised with `tokensave init` (i.e. has a `.tokensave/tokensave.db`).

If a folder doesn't appear, right-click it → **+ Scaffold** → tick "Run tokensave init", or use **⚙ Retrofit Existing** from the toolbar.

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
- **Project** — project folder name; `★` marks the active project
- **Path** — full folder path
- **Last Synced** — age of the tokensave index (how long ago the last sync ran)
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
│  [⬆ Push]  [⬇ Pull]  [📝 Commit…]  [↩ Undo Last Commit]          │
│  [🌿 New Branch]  [🔀 Switch Branch…]  [🗑 Delete Branch…]         │
│  [🔗 Open PR]                                                      │
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
| 📝 Commit… | Per-file staging dialog (see below) |
| ↩ Undo Last Commit | `git reset --soft HEAD~1` — remove last commit, keep changes |
| 🌿 New Branch | Create a branch with optional immediate switch |
| 🔀 Switch Branch… | Switch to a different local branch |
| 🗑 Delete Branch… | Delete a non-current branch (safe by default; offers force if unmerged) |
| 🔗 Open PR | Open GitHub's compare page for the current branch; or explains branch workflow if on main |

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
| 📂 Open Folder | Open in Windows Explorer |
| ✏ Open in Editor | Launch in configured editor |
| ⎘ Copy Path | Copy full project path to clipboard |
| ⚙ Retrofit… | Add tokensave rules / BASIC_INSTRUCTIONS / Nuitka / git hook |
| 🔗 Shadow Links… | Generate NTFS hardlinks with a secondary extension |
| 📁 Assign Category… | Move project to a different category or sub-category |
| 🗑 Remove Index… | Delete `.tokensave/` and remove project from the list (project files untouched) |
| Auto-detect | Switch from manual pin back to automatic project detection |

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
4. **⬆ Push** — send your branch to GitHub
5. **🔗 Open PR** — GitHub opens with a "Compare & pull request" button
6. Review your changes on GitHub, add a description, click **Create pull request**
7. When ready, **Merge** on GitHub (or just push directly to master for solo projects)
8. Back in the manager: **🔀 Switch Branch…** → `master`, then **⬇ Pull** to bring the merged changes down

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
├── src/
│   ├── tokensave-manager.py       Main GUI (~4,700 lines)
│   └── tokensave-wrapper.py       Claude Desktop MCP wrapper
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
    ├── ARCHITECTURE.md            Class structure, UI layout, threading model
    ├── ARCHITECTURE_TOKENSAVE.md  tokensave internals reference
    └── GITHUB_GUIDE.md            Beginner GitHub guide
```

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full history.

**Recent highlights (Unreleased):**
- Git tab with full push/pull/commit/branch/diff UI
- Per-file staging in the commit dialog with colour-coded badges
- Conventional-commit message auto-suggestion
- GitHub Setup wizard (step-by-step onboarding)
- Open PR button with beginner branch-workflow guide
- GitHub CLI installer in Settings
- Project categories + sub-categories in the project tree
- Ensure .gitignore — one-click baseline gitignore for any project
- Auto-commit after sync + Claude session Stop hook

---

## Credits

Created by **Alexander L Corthell**

Built with Python, tkinter, [Catppuccin Mocha](https://github.com/catppuccin/catppuccin), and [tokensave](https://github.com/aovestdipaperino/tokensave).
