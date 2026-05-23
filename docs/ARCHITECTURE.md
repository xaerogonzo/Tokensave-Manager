# TokenSave Manager — Architecture

## Purpose

A Windows GUI tool for managing tokensave MCP integrations across multiple projects.
It handles project discovery, index synchronisation, Claude Desktop project-switching,
scaffolding new projects with the standard Claude instruction template, and optionally
scaffolding a Nuitka build pipeline for projects that need to ship as standalone executables.

This project wraps **tokensave.exe** (a third-party binary that stays in root).
See `docs/ARCHITECTURE_TOKENSAVE.md` for how tokensave itself works.

---

## Repository Layout

```
Token Save Manager Source/
├── manager-config.json            Machine-specific config (paths, search roots)  — gitignored
├── manager-config.example.json    Clean template with placeholder paths — committed
├── TOKENSAVE_GUIDE.md             Full tokensave CLI + MCP reference
├── BASIC_INSTRUCTIONS.md          Claude session instructions for this project
├── CHANGELOG.md                   Change history
├── build.ps1                      Nuitka compile pipeline — produces dist\ exes
├── build.bat                      Launcher for build.ps1 (bypasses execution policy)
│
├── src/
│   ├── tokensave-manager.py       Main GUI application (~9,500 lines)
│   └── tokensave-wrapper.py       Claude Desktop auto-detection wrapper
│
├── templates/                     Data files used by the manager + shipped in dist\
│   ├── claude-md-template.md      BASIC_INSTRUCTIONS.md template for other projects
│   ├── project-baseline.md        Universal rules file — @included by retrofitted projects
│   ├── nuitka-build.ps1.template  Generic Nuitka build script template (PowerShell)
│   ├── nuitka-build.py.template   Python-based alternative build script
│   ├── nuitka-build.bat.template  One-line bat launcher template for other projects
│   └── NUITKA_GOTCHAS.md          Nuitka pitfalls reference (14 known issues with fixes)
│
├── dist/                          Build output — zip and ship this folder
│   ├── tokensave-manager.exe
│   ├── tokensave-wrapper.exe
│   ├── manager-config.json        Clean config (user fills in on first run)
│   ├── manager-config.example.json
│   ├── TOKENSAVE_GUIDE.md
│   ├── CHANGELOG.md
│   ├── templates\                 All template files (copied by build.ps1)
│   └── docs\                      GITHUB_GUIDE.md, ARCHITECTURE.md, ARCHITECTURE_TOKENSAVE.md
│
├── logs/
│   └── manager.log                Rotating log (500 KB x 5 backups)
│
└── docs/
    ├── ARCHITECTURE.md            This file
    ├── ARCHITECTURE_TOKENSAVE.md  tokensave tool internals reference
    └── GITHUB_GUIDE.md            Beginner GitHub guide (concepts, setup, daily workflow, releases)
```

---

## `src/tokensave-manager.py` — Main Application

Single-file tkinter application (~9,500 lines). Entry point is `if __name__ == "__main__": App().mainloop()`.

### Class hierarchy

```
App (tk.Tk)
├── _style()              — ttk.Style configuration (Catppuccin Mocha theme)
├── _build()              — top-level layout: header + ttk.Notebook + log panel + credit bar
├── _build_projects_tab()
├── _build_reference_tab()  — CLI cheatsheet + snippet listbox with _active_snippets_map
├── _build_help_tab()
├── _build_context_menu() — right-click menu for per-project actions
├── Tray methods          — _setup_tray, _hide_to_tray, _show_from_tray, _quit_app
├── Data methods
│   ├── refresh()           — re-scans projects, rebuilds treeview
│   ├── _auto_refresh()     — 60s timer, skips if a proc is running
│   └── _has_scaffold()     — checks BASIC_INSTRUCTIONS.md exists in a path
├── Command handlers (cmd_*)
│   ├── cmd_set_active / cmd_auto
│   ├── cmd_sync / cmd_sync_all / cmd_force_sync
│   ├── cmd_status / cmd_doctor
│   ├── cmd_git_log          — reads `git log + status` from selected project's own repo
│   ├── cmd_git_init         — right-click: git init + optional initial commit
│   ├── cmd_git_commit       — right-click: open GitCommitDialog
│   ├── cmd_open_folder      — os.startfile(path) → Windows Explorer
│   ├── cmd_open_editor      — shlex.split(editor_cmd) + path → Popen
│   ├── cmd_copy_path        — clipboard
│   ├── cmd_remove / cmd_scaffold / cmd_retrofit / cmd_settings
│   ├── cmd_shadow_links         — right-click: open ShadowLinksDialog for selected project
│   ├── cmd_assign_category      — right-click: open AssignCategoryDialog for selected project
│   ├── cmd_git_push / cmd_git_pull — push/pull with GIT_TERMINAL_PROMPT=0 + auth error handling
│   ├── cmd_git_undo_commit       — git reset --soft HEAD~1 with confirmation
│   ├── cmd_git_set_remote        — open SetRemoteDialog; runs git remote add/set-url
│   ├── cmd_git_new_branch        — open NewBranchDialog; git checkout -b or git branch
│   ├── cmd_git_switch_branch     — open SwitchBranchDialog; git checkout with dirty-tree guard
│   ├── cmd_git_merge             — open branch picker; git merge --no-edit; conflict + dirty-tree handling
│   ├── cmd_git_delete_branch     — safe/force delete; post-delete prompt to push remote deletion if origin/<branch> exists
│   ├── cmd_github_setup          — open GitHubSetupDialog for current/selected project
│   └── _add_snippet / _edit_snippet / _delete_snippet / _on_snippet_saved
└── Worker helpers
    ├── _run()                   — generic tokensave CLI call (threaded, streaming); auto-commits after sync if toggle on; amends previous sync commit if last message was "chore: tokensave sync"
    ├── _run_capture()           — tokensave call returning (output, rc, elapsed); synchronous from thread
    ├── _shell_capture()         — generic shell call returning (output, rc); catches FileNotFoundError
    ├── _scaffold_project()      — write BASIC_INSTRUCTIONS + optional tokensave init + Nuitka + git hook
    ├── _scaffold_nuitka_build() — copy nuitka-build.ps1/bat templates into a project folder
    ├── _do_retrofit()           — prepend @include to CLAUDE.md + optional BASIC_INSTRUCTIONS + Nuitka + shadow links + git hook
    ├── _do_shadow_links()       — generate hardlinks in background thread, optionally run sync after
    ├── _do_assign_category()    — write project_categories override to config, refresh tree
    ├── _do_git_commit()         — git reset → git add -- <selected_files> → git commit -m, in a background thread; commits only the files the user ticked in GitCommitDialog, leaving the rest as working-tree changes
    ├── _do_git_set_remote()     — git remote add/set-url in background thread
    ├── _do_git_new_branch()     — git checkout -b / git branch in background thread
    ├── _do_git_switch_branch()  — git checkout <branch> with rc != 0 error dialog
    ├── (cmd_git_merge inlines its worker)  — git merge --no-edit <source>; on rc != 0, distinguishes "conflict" vs "unmerged/your local changes" and pops the matching dialog
    ├── (cmd_git_delete_branch offer_remote_delete nested fn) — after local delete, scans git branch -r for origin/<branch> and runs git push origin --delete on user confirm
    ├── _git_refresh()           — background fetch of branch/remote/status/log; calls _git_update_ui
    ├── _git_update_ui()         — main-thread update of all Git tab widgets + button states
    ├── _git_show_diff()         — render diff into Text widget with colour tags (capped at 2000 lines)
    ├── _on_git_status_select()  — click file in status listbox → fetch + show diff
    ├── _on_project_select()     — <<TreeviewSelect>> binding: update _git_path, refresh Git tab if visible
    ├── _on_tab_changed()        — <<NotebookTabChanged>> binding: sync project + refresh Git tab
    ├── _git_tab_is_visible()    — returns True when the Git tab is the active notebook tab
    ├── _refresh_snippet_list()  — rebuilds snippet_lb + _active_snippets_map from PROMPT_SNIPPETS + user_snippets
    ├── _show_status_popup() / _format_status_msg()
    ├── _show_git_popup()        — Toplevel with scrollable monospace git output
    └── _log()                   — thread-safe coloured log append

RetrofitDialog (tk.Toplevel)     — modal: 5 checkboxes: tokensave rules / BASIC_INSTRUCTIONS / Nuitka build / shadow links / auto-commit hook
ScaffoldDialog (tk.Toplevel)     — modal: 4 checkboxes: BASIC_INSTRUCTIONS / tokensave init / Nuitka build / auto-commit hook
SettingsDialog (tk.Toplevel)      — modal: edit manager-config.json; search roots as labeled two-column Treeview; auto-commit toggle; "AI commit messages" section with provider/model/api-key/base-URL fields + Anthropic/LM Studio/Ollama preset buttons. Content wrapped in a `Canvas + body Frame` with vertical scrollbar (mousewheel-bound on canvas AND body) so the growing dialog scrolls when its natural height exceeds the window. Save/Cancel buttons packed on `self` (NOT body) so they stay anchored at the bottom outside the scroll area. Resizable, 760×700 default, 640×500 minsize
SnippetEditDialog (tk.Toplevel)  — modal: title + body for adding or editing a user-defined prompt snippet
ShadowLinksDialog (tk.Toplevel)  — modal: editable extension/name map + sync toggle; right-click 🔗 Shadow Links…
AssignCategoryDialog (tk.Toplevel)  — modal: category + sub-category comboboxes with existing values; right-click 📁 Assign Category…
SetRemoteDialog (tk.Toplevel)     — modal: GitHub URL entry with beginner-friendly step-by-step instructions
NewBranchDialog (tk.Toplevel)     — modal: branch name entry + "switch immediately" checkbox
SwitchBranchDialog (tk.Toplevel)  — modal: listbox of local branches; double-click to switch; static pick() helper reused for delete + merge picker flows. Important calling convention: pick()'s signature is `(parent, title, branches, parent_widget=None)` — pass `parent_widget=self` for the centering anchor, NOT `parent=self` (the latter collides with the positional first arg and raises TypeError, which Tk silently swallows on button callbacks → dead button)
GitCommitDialog (tk.Toplevel)     — modal: per-file checklist of working-tree changes with colour-coded status badges (M/A/D/R/?/!) + Select All / None / Modified Only quick-pick buttons + commit message entry with multi-strategy auto-suggest (💡 Suggest button that re-runs the orchestrator scoped to the currently-ticked files); callback signature `(path, message, selected_files: list[str])`. Right-click → 📝 Git Commit… or Git tab → Commit…. Initial selection covers only the FIRST LINE of the suggestion so the AI/CHANGELOG-derived body survives the user typing a new subject
GitHubSetupDialog (tk.Toplevel)  — modal: step-by-step GitHub onboarding wizard. Step 1 saves git identity; Step 2 offers Sign in (primary) + Create account (secondary); Step 3 opens github.com/new; Step 4 sets remote URL; Step 5 first push. Separate Releases section shells out to `gh release create` when `shutil.which("gh")` returns a path. Scrollable canvas wraps the body Frame so it fits small windows. Opened by 🐙 GitHub… button in Git tab header.
ReleaseWizardDialog (tk.Toplevel) — modal: full-featured release wizard. Six sections in a scrollable canvas: (1) version with last-tag detection + Patch/Minor/Major radios biased by commit content + free-text override; (2) auto-filled title; (3) auto-drafted release notes textarea grouped by conventional-commit prefix; (4) build step with `build.ps1` / `build.bat` auto-detect; (5) artefact preview showing dist contents + resolved zip name; (6) CHANGELOG.md sync checkbox. Publish runs a single threaded `_publish_worker` that sequences: build → zip → patch CHANGELOG → stage CHANGELOG only → commit → local `git tag -a` → `git push --follow-tags` → `gh release create --notes-file <tmp>`. Each step short-circuits with a copy-pasteable recovery command. The notes-file temp path is preserved on the final-step failure so the user can retry `gh release create` by hand without retyping notes. Opened by 📦 Release… button on the Git tab. Pre-flight in `cmd_git_release` refuses to open the wizard if the working tree is dirty in anything other than `CHANGELOG.md` (with a one-click handoff to the existing Git Commit dialog).
```

### Release-wizard helpers (module-level, pure)

All ten helpers live in the module-scope section between `_GITIGNORE_TEMPLATES` and `_STOP_HOOK_CMD`. They are unit-testable without instantiating any Tk widget — the wizard dialog is purely a UI shell around these:

| Helper | Purpose |
|---|---|
| `_last_release_tag(path) -> str \| None` | Wraps `git describe --tags --abbrev=0`; None on no tags |
| `_commits_since(path, ref) -> list[dict]` | `git log <ref>..HEAD --pretty=format:%H%x09%s%x09%b%x1f` parsed into `[{hash, subject, body}]`. Records separated by `\x1f`, fields by `\x09` — subject and body are never confused |
| `_classify_commits_for_changelog(commits) -> dict` | Subject-only regex (`_CONVENTIONAL_RE`) + body substring check for `BREAKING CHANGE:`. Skips `auto:` commits. Unknown prefixes fall into `Other`. Returns ordered dict of section → list[str] |
| `_bump_version(tag, kind)` | Semver-aware patch / minor / major bump; date-stamped fallback for non-semver inputs |
| `_suggest_bump_kind(commits)` | Picks bump radio default: any `!` or body BREAKING → major; any `feat` → minor; else patch |
| `_render_release_notes(version, date, sections, summary)` | Pure markdown rendering with the canonical `## [version] — date` header, optional summary paragraph, and `### Section` blocks. Single source of truth used by both the wizard textarea AND `_patch_changelog`'s replacement body |
| `_patch_changelog(path, version, date, notes_md)` | **Idempotent**: replaces existing `## [<version>]` block in place if found, else inserts below `## [Unreleased]`. Atomic write via `.tmp` + `os.replace`. Returns `(ok, status_msg)`; refuses to write if neither anchor nor existing section is present |
| `_zip_dist(dist_path, zip_path)` | Flat zip via `shutil.make_archive(root_dir=dist_path, base_dir=".")` — extracted files land at the archive root, not nested under `dist/`. Strips trailing `.zip` from the passed-in path before calling `make_archive` (which re-appends the format extension), so `foo.zip` stays `foo.zip`, never `foo.zip.zip`. Returns absolute path of created zip |
| `_git_tag(path, tag, message)` | Wraps `git tag -a <tag> -m <message>` — annotated tags so `git describe` works and so `git show <tag>` displays the release title |
| `_git_push_with_tags(path)` | `git push origin HEAD --follow-tags` — commits + the new annotated tag travel together in one network round-trip |

`_CONVENTIONAL_RE` and `_TYPE_TO_SECTION` are the canonical commit-prefix → section mapping. To add a new conventional-commit type (e.g. `revert:`), update both in lockstep — never duplicate the mapping inside the dialog.

### How the dist zip is produced and shipped

When the user clicks 🚀 Publish:

1. After the optional build step finishes, `_publish_worker` computes the zip path:
   ```python
   dist_dir = os.path.join(path, "dist")
   zip_name = f"{self._repo_name}-{tag}-windows.zip"
   zip_path = os.path.join(path, zip_name)
   ```
   For a project at `D:\foo\MyApp` releasing `v1.2.0`, that produces `D:\foo\MyApp\MyApp-v1.2.0-windows.zip` — **next to the repo root, NOT inside `dist/`**. This is intentional: the zip itself is a release artefact, not a build output, so it sits outside the build tree (and the repo's `.gitignore` patterns for `*-windows.zip` keep it out of commits).
2. `_zip_dist(dist_dir, zip_path)` is called. The helper:
   - Bails (returns None) if `dist/` is missing or empty — the wizard then surfaces "Zip step failed" and aborts before any tagging happens.
   - Otherwise calls `shutil.make_archive(base_name=base, format="zip", root_dir=dist_dir, base_dir=".")` where `base` is `zip_path` with any trailing `.zip` stripped.
   - The `root_dir` + `base_dir="."` combo flattens the archive: files inside `dist/` end up at the zip's root. Without these args the archive would contain `dist/tokensave-manager.exe` instead of `tokensave-manager.exe`, forcing users to extract through an extra folder.
3. The returned absolute path is passed as the last positional argument to `gh release create` so it gets uploaded as a release asset.
4. The zip stays on disk after publishing — it isn't deleted. The repo's `.gitignore` should include the matching pattern (`*-windows.zip` or similar) so it doesn't accidentally get committed by the next auto-commit Stop hook.

Contents of the zip are 1:1 whatever `dist/` holds after the build, which is controlled by the project's own `build.ps1` staging block. The wizard doesn't pick and choose — exclude things from the zip by excluding them from the build's output staging.

### UI structure

```
┌─ Header bar (active project badge) ────────────────────────┐
├─ ttk.Notebook ─────────────────────────────────────────────┤
│  ├─ Projects tab                                           │
│  │   ├─ Treeview (tree+headings: Category / Sub-category / Project rows)
│  ├─ Git tab                                                │
│  │   ├─ Header: project name, branch, remote, Set Remote, Refresh
│  │   ├─ Working tree Listbox + Recent commits Text (side by side)
│  │   ├─ Diff Text widget (colour-coded +/- lines, capped at 2000 lines)
│  │   └─ Action bar: Push, Pull, Commit, Undo Last Commit, New Branch, Switch Branch, Merge, Delete Branch, Open PR
│  │   │   Columns: Project (#0, tree col), active ★, path, last synced, scaffold ✔/—
│  │   │   └─ Right-click on project row → context menu (per-project actions)
│  │   │      Right-click on category/sub-category header → no menu
│  │   ├─ Hint: "Right-click any project for actions"
│  │   └─ Toolbar: + Scaffold | ⚙ Retrofit | ↺↺ Sync All | [Settings] [Refresh]
│  ├─ Reference tab                                          │
│  │   ├─ CLI cheatsheet (Text widget)
│  │   └─ Prompt snippets (Listbox + preview + Copy button + Add/Edit/Delete)
│  └─ Help tab                                               │
│      └─ Scrollable formatted text (operational guide)
├─ Separator ────────────────────────────────────────────────┤
└─ OUTPUT log panel (4-line Text, always visible) ───────────┘
```

### Module-level helpers

| Symbol | Purpose |
|--------|---------|
| `GIT_EXE` | Path to `git.exe` used by every git subprocess call. Initialised from `_cfg["git_exe"]` if set, otherwise via `_detect_git()`. Rebuilt in `_on_settings_saved()` when the Settings dialog changes the git path. |
| `_detect_git()` | Returns `shutil.which("git")` if found; falls back to common Windows install paths (`C:\Program Files\Git\cmd\git.exe`, `…\bin\git.exe`, x86 variants); final fallback is the bare string `"git"`. |
| `_GIT_ENV_NO_PROMPT` | `dict(os.environ, GIT_TERMINAL_PROMPT="0")`. Passed as `env=` for network git commands (push/pull) to prevent infinite hangs when GCM hasn't cached credentials yet. |
| `_is_auth_error(text)` | Pattern-match on push/pull output to decide whether to show the GCM-setup message vs a generic error. |
| `_suggest_commit_message(repo_path, status_text)` | **Multi-strategy orchestrator.** Chain order (highest-quality first): LLM (if `commit_message_llm.enabled` in config) → CHANGELOG.md staged bullets (`_extract_changelog_additions` + `_message_from_changelog`) → diff content (`_suggest_from_diff_content` + `_diff_added_python_symbols`) → file-name fallback (`_suggest_from_filenames`, the original v1.0.x function). Every result passes through `_sanitize_commit_message` (72-char subject, imperative mood, blocks filename listings, `chore:` → `refactor:` escalation when source files changed). Used by `GitCommitDialog` for initial fill and the 💡 Suggest button. |
| `_call_llm_for_commit_message(cfg, repo_path)` | Provider abstraction over Anthropic Messages API / OpenAI Chat Completions / OpenAI-compatible local servers (LM Studio, Ollama). Uses `urllib.request` (no extra dependency). MUST silent-fallback on every failure path — any exception, timeout, missing API key, sub-`min_diff_lines` diff, or empty response returns `None` and the orchestrator falls through to heuristics. Diff truncated to `max_diff_chars` (default 8000) for cost control. |
| `_pending_diff(repo_path, *paths)` | `git diff HEAD` (NOT `--cached`). Used by the orchestrator's CHANGELOG and diff-content strategies because the commit dialog generates suggestions BEFORE the user stages files — `--cached` would always return empty. Captures both staged and unstaged changes. |
| `_BASELINE_GITIGNORE` | Multi-line string written by `cmd_git_init` when no `.gitignore` exists. Covers Python cache, Nuitka build output, `.tokensave/`, virtual environments, `.claude/`, and `logs/`. |
| `_STOP_HOOK_CMD` + `_scaffold_git_hook(path)` | Merges a Claude Code Stop hook (`.claude/settings.json`) that auto-commits at the end of every Claude session. Idempotent — detects an existing `git add -A`-prefixed hook before appending. |
| `_Tooltip(widget, text)` | Hover-tooltip helper. 650 ms delay, auto-destroys on Leave/ButtonPress. Used on every Git tab button to give plain-English explanations to beginner users. |
| `_root_path(r)` / `_root_label(r)` | Normalise a `search_roots` entry — supports both bare strings and `{"path":…, "label":…}` dicts. |
| `_parse_git_status_v2(text)` | Pure function. Parses `git status --porcelain=v2 --branch` output into `{"dirty": bool, "ahead": int, "behind": int, "has_remote": bool}`. Used by the async Git column refresh. |
| `_format_git_status_cell(status, has_git)` | Pure function. Returns `(display_text, override_tag)` for the Projects-tab Git column. Encodes the icon vocabulary: ✓ / ● / ↑N / ↓N / ●↑N / — / …. |
| `_kick_off_git_status_refresh()` (App method) | Background-thread walk over `self.projects` that runs `git status --porcelain=v2 --branch` for each git project and updates the Treeview Git column via `_update_git_status_cell(piid, status)`. Mtime-cached per project on `.git/index` so unchanged projects are skipped. Only one refresh in flight at a time. |
| `_offer_commit_after_change(path, summary_label)` (App method) | After destructive manager ops (Ensure .gitignore, Shadow Links, Scaffold, Retrofit), checks `_is_git_repo` + `git status --porcelain` and offers a `messagebox.askyesno` → `_open_commit_dialog(path)` flow. Silent no-op when not a repo or working tree is clean. |
| `_open_commit_dialog(path)` (App method) | Path-explicit version of `cmd_git_commit`. Both `cmd_git_commit` (from Projects tab right-click) and `_offer_commit_after_change` delegate here. |
| `_git_op_in_flight` + `_git_begin_op()` / `_git_end_op()` (App method) | Locking pattern that disables every Git tab button during an in-flight operation. Honoured by `_git_update_ui()` so incidental refreshes don't bypass the lock. All 8 git command methods wrap their workers with begin/end in a `try`/`finally`. |
| `_GITIGNORE_TEMPLATES` | Module-level dict mapping category name → list of patterns. The Baseline category is built from `_BASELINE_GITIGNORE` via `_baseline_patterns()` at module load (single source of truth). Used by `GitignoreDialog`'s template-inject buttons. |
| `_read_gitignore_lines(path)` / `_write_gitignore_lines(path, lines)` | Pure file-IO for the gitignore editor. Read returns `[]` if missing, uses `utf-8-sig` to tolerate PowerShell-written BOMs. Write is atomic (`.tmp` + rename) and always ends with a trailing newline. |
| `CODEGRAPH_EXE` | Path to the codegraph CLI (npm-installed). Resolved from `_cfg["codegraph_exe"] or _detect_codegraph()` at startup; rebuilt in `_on_settings_saved`. Empty string when not installed. |
| `_detect_codegraph()` / `_detect_npm()` | Windows-`.cmd`-first PATH probes. Both check `shutil.which("X.cmd")` before `shutil.which("X")` because npm-installed binaries are `.cmd` shims, not `.exe` files; `subprocess.run` with a bare `.cmd` raises `FileNotFoundError`. |
| `_is_codegraph_project(path)` | True iff `.codegraph/codegraph.db` exists in the project root. Mirrors `_is_git_repo` for codegraph. |
| `_is_local_git_repo(path)` | Strict local "is this folder a git repo root?" check using `os.path.exists(.git)`. Handles both standard repos and git worktrees (where `.git` is a flat pointer file, not a directory). Used by `_offer_commit_after_change` to avoid ghost-prompts when a project is nested inside an unrelated parent git repo. |
| `cmd_codegraph_init/sync/status/remove` (App methods) | Per-project lifecycle commands mirroring the tokensave equivalents. `init` uses `--index` to build the graph in one step. All four gated by `_require_codegraph_installed()`. |
| `_require_codegraph_installed()` (App method) | Parallel to `_require_tokensave`. Returns True iff `CODEGRAPH_EXE` is non-empty AND the file exists. Otherwise shows an install-nudge dialog that opens Settings and focuses the CodeGraph path entry. |
| `GitignoreDialog(tk.Toplevel)` | User-facing `.gitignore` editor. Opened via right-click → 📋 Manage .gitignore…. Canvas-backed scrollable Frame for per-row removal buttons; real-strikethrough font (`tkfont.Font(overstrike=1)`) for marked-removed rows; template inject buttons (push, not stateful); custom-entry field with sanity check; live Pending changes Text widget. Save → `_write_gitignore_lines` → `_offer_commit_after_change`. |

### Configuration (`manager-config.json`)

All machine-specific values live in `manager-config.json` at the project root.
Loaded at startup by both `tokensave-manager.py` and `tokensave-wrapper.py` via `_load_config()`.
Read with `encoding="utf-8-sig"` to silently strip any UTF-8 BOM that PowerShell may have written.
Editable through the Settings dialog without touching JSON directly.

| Key | Purpose |
|-----|---------|
| `tokensave_exe` | Path to tokensave.exe |
| `template_dir` | Path to the `templates/` directory (blank = auto-detect as `<exe-dir>\templates\`) |
| `editor_cmd` | Editor launch command, e.g. `code` or `code --new-window` (parsed via `shlex.split`) |
| `python_exe` | pythonw.exe path (preserved in JSON; no longer shown in Settings UI) |
| `git_exe` | Optional path to `git.exe`. Blank = auto-detect (`shutil.which` → common Windows install paths → bare `"git"`). Settings dialog gives Browse / Auto-detect / Verify (`git --version`) controls. |
| `search_roots` | List of `str \| {"path": str, "label": str}` — scanned for tokensave projects; each root's label is its category header in the Treeview. Bare strings are backward-compatible (label defaults to `basename`). |
| `project_categories` | Dict mapping project path → `{"category": str, "subcategory"?: str}` — per-project override of the root's category label. Edit via right-click → 📁 Assign Category…. |
| `user_snippets` | List of `{"title": str, "text": str}` — user-defined prompt snippets |
| `auto_commit_after_sync` | Boolean. If true, `_run()` runs `git add -A + git commit -m "chore: tokensave sync"` after every successful sync on a git-repo project. If the previous commit was the same message, it's amended (`commit --amend --no-edit`) to avoid history pile-up. |

### Nuitka onefile path resolution

Under `--onefile`, `__file__` resolves into a temp extraction directory, not the exe's location.
Both `.py` source files resolve the base directory via the `NUITKA_ONEFILE_PARENT` env var:

```python
if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
```

`_BASE_DIR` is then used for config, logs, templates, and guide file lookups.

---

## Data Flow

### Project discovery (`find_projects`)
```
for each root in SEARCH_ROOTS (from _cfg):
    rpath  = _root_path(root)   — supports both str and {"path":…,"label":…}
    rlabel = _root_label(root)  — basename fallback if label not set
    os.walk(rpath) depth-limited to MAX_DEPTH, skipping SKIP_DIRS
        if .tokensave/tokensave.db exists:
            append {path, name, db, mtime, root_label=rlabel}
sort by db mtime (most recently synced first)
```
Only folders with an initialised tokensave index appear in the list.

`refresh()` then groups results by `(category, subcategory)` — category comes from
`project_categories[path]` override if set, otherwise from the project's `root_label`.
Category/sub-category header rows are inserted as non-selectable parent nodes in the
Treeview; project rows use `iid="proj:<path>"` so `_selected_path()` can guard against
header-row clicks.

### Active project pin
```
Set as Active  →  write path to DESKTOP_PROJECT_FILE
Auto-detect    →  delete DESKTOP_PROJECT_FILE
Restart Claude Desktop  →  tokensave-wrapper.py reads pin (or auto-selects)
                            →  spawns: tokensave.exe serve -p <path>
```

### tokensave CLI calls (`_run`)
```
Right-click → cmd_*() validates selection
  → _run(["sync"], cwd=path, label=name)
      → threading.Thread(daemon=True)
          → subprocess.Popen([TOKENSAVE] + args, cwd=cwd, stdout=PIPE, ...)
          → stream stdout lines → self.after(0, _log)   [thread-safe]
          → proc.wait()
          → self.after(0, self.refresh)
```
All CLI calls are non-blocking. The GUI stays responsive during indexing.
`CREATE_NO_WINDOW = 0x08000000` suppresses console windows on Windows.

### Scaffold flow
```
+ Scaffold button
  → filedialog.askdirectory()
  → ScaffoldDialog (modal: checkboxes for BASIC_INSTRUCTIONS + init + Nuitka build files)
      → Apply: _scaffold_project(path, create_bi, run_init, scaffold_nuitka)
          → write BASIC_INSTRUCTIONS.md synchronously (if checked)
          → _scaffold_nuitka_build(path) synchronously (if checked)
              → copy nuitka-build.ps1.template → build.ps1 (fills [PROJECT_NAME])
              → copy nuitka-build.bat.template → build.bat
          → if run_init: _insert_pending_row() + worker thread → _run(["init"], ...)
```

### Retrofit flow
```
⚙ Retrofit Existing button  (also available via right-click → ⚙ Retrofit…)
  → filedialog.askdirectory()
  → RetrofitDialog (modal: checkboxes for tokensave rules + BASIC_INSTRUCTIONS + Nuitka + shadow links)
      → Apply: _do_retrofit(path, add_tokensave, add_basic_instructions, add_nuitka, add_shadow_links, shadow_ext_map)
          → worker thread:
              → if add_tokensave: prepend BASELINE_INCLUDE_LINE to CLAUDE.md
              → if add_bi: write BASIC_INSTRUCTIONS.md
              → if add_nuitka: _scaffold_nuitka_build(path)
              → if add_shadow_links: generate_shadow_links(path, shadow_ext_map) + update_gitignore_for_shadows
              → self.after(0, messagebox.showinfo("summary"))
```

### Shadow links flow
```
Right-click → 🔗 Shadow Links…
  → ShadowLinksDialog (editable map text + sync toggle)
      → Apply: _do_shadow_links(path, ext_map, run_sync)
          → worker thread:
              → generate_shadow_links(path, ext_map)   — creates NTFS hardlinks
              → update_gitignore_for_shadows(path, ext_map)
              → if run_sync and created > 0: _run_capture(["sync"], path)
              → self.after(0, self.refresh)

generate_shadow_links key formats:
  ".zsc" → extension match  (Blood.zsc → Blood.zsc.cpp)
  "DECORATE" → exact filename, case-insensitive  (DECORATE → DECORATE.cpp)

update_gitignore_for_shadows pattern format:
  extension key → glob  (*.zsc.cpp)
  name key      → exact (DECORATE.cpp)   — no leading wildcard
```

### Nuitka build template flow
```
_scaffold_nuitka_build(path)
  → reads TEMPLATE_DIR/nuitka-build.ps1.template
  → replaces [PROJECT_NAME] with os.path.basename(path)
  → writes to path/build.ps1  (skips if already exists)
  → shutil.copy2 nuitka-build.bat.template → path/build.bat  (skips if already exists)
  → logs tips: "edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building"
  → returns list of actions taken (for retrofit summary messagebox)
```

---

## Threading Model

tkinter is single-threaded: **all widget updates must happen on the main thread**.

| What runs in a thread | What runs on main thread |
|-----------------------|--------------------------|
| `subprocess.Popen` + stdout streaming | `self.after(0, _log)` — writes to log widget |
| File I/O (scaffold, retrofit) | `self.after(0, self.refresh)` — rebuilds treeview |
| `proc.wait()` | `self.after(0, messagebox.*)` — dialogs |
| `_shell_capture(["git", ...])` | `self.after(0, lambda c=content: self._show_git_popup(name, c))` |

Every `_log(msg, colour)` call schedules `_do()` on the main loop via `self.after(0, ...)`.
Every completion callback (refresh, messagebox, popup creation) is similarly scheduled.
**Never call Toplevel(), widget.configure(), or any other tkinter API directly from a background thread.**

---

## Template System

Five files in `templates/` drive the scaffold/retrofit and build-pipeline features.
All five are copied into `dist\templates\` by `build.ps1` and shipped to end users.

### `templates/claude-md-template.md`
Written as `BASIC_INSTRUCTIONS.md` into scaffolded projects. Contains an `@include`
pointing to `project-baseline.md`, placeholder sections for overview / architecture /
key files / rules, and comment blocks telling Claude to fill them in on first use.

### `templates/project-baseline.md`
A universal rules file `@include`d at the top of every retrofitted project's CLAUDE.md.
Updating this one file instantly propagates changes to all retrofitted projects without
touching individual CLAUDE.md files.

Contents: tokensave tool lookup table, documentation discipline table, code quality rules,
git discipline, and a "Compiling with Nuitka" section pointing Claude at the build templates.

### `templates/nuitka-build.ps1.template`
Generic PowerShell build script for compiling any Python project to a standalone `.exe`.
Contains three user-facing placeholders (`[PROJECT_NAME]`, `[ENTRY_SCRIPT]`, `[OUTPUT_NAME]`).
`[PROJECT_NAME]` is auto-filled by `_scaffold_nuitka_build()` from the folder name;
the other two require manual editing before the first build.

Features: pre-flight checks (Python + Nuitka on PATH), `Clear-NuitkaOrphans` cleanup,
GUI defaults (`--onefile --enable-plugin=tk-inter --include-package=PIL,pystray`),
Anaconda bloat exclusions (`--nofollow-import-to=numpy/scipy/pandas/…`),
commented CLI variant block, conditional uncompressed payload sanity check.
`$ErrorActionPreference = "Continue"` wrapper around `2>&1` capture prevents
`NativeCommandError` from Nuitka's stderr progress lines (NUITKA_GOTCHAS.md #12).

### `templates/nuitka-build.py.template`
Python-based alternative to the PS1 template. Uses `subprocess.run()` (no stderr capture,
no `NativeCommandError` risk). Auto-detects project root via sentinel file walk.
Supports `--onefile`, `--clean`, and all Anaconda exclusions with documentation.
Inherently avoids all PowerShell-specific pitfalls.

### `templates/nuitka-build.bat.template`
One-line `.bat` launcher: `powershell -ExecutionPolicy Bypass -NoProfile -File build.ps1` + `pause`.
Bypasses execution policy so users can double-click to build without configuring PowerShell.

### `templates/NUITKA_GOTCHAS.md`
Reference doc covering 14 known Nuitka pitfalls with cause + fix:
silent crash without `tk-inter` plugin, UTF-8 BOM in JSON config, cp1252 subprocess decode
crash, onefile path resolution via `NUITKA_ONEFILE_PARENT`, PowerShell Unicode parse errors,
`utf8NoBOM` PS5.1 incompatibility, VC++ runtime warning, `__selfdelete__` orphan files,
global Nuitka cache poisoning, exe size sanity thresholds (uncompressed not compressed),
`NativeCommandError` from Nuitka stderr with `$ErrorActionPreference = "Stop"`,
pywebview `--include-package=webview` conflict, and Anaconda base-env bloat.

---

## `src/tokensave-wrapper.py` — Claude Desktop Wrapper

Auto-detection script used as the MCP server command in `claude_desktop_config.json`.
Runs at Claude Desktop startup. Written to avoid console windows (`pythonw.exe`).
Reads `TOKENSAVE` and `SEARCH_ROOTS` from `manager-config.json` (same config as the manager).

```
Logic:
1. Load manager-config.json (relative to src/ → ../manager-config.json)
2. Check DESKTOP_PROJECT_FILE — use that path if it exists and has a valid DB
3. Otherwise: walk SEARCH_ROOTS, collect all .tokensave/tokensave.db paths
4. Pick the one with the most recent mtime (= last synced project)
5. Spawn: tokensave.exe serve -p <chosen-path>
   with CREATE_NO_WINDOW flag and inherited stdio
6. sys.exit(proc.wait()) — becomes the MCP server process
```

**Critical:** the `.tokensave` dir check must happen **before** `dirnames[:] = [d for d in dirnames if not d.startswith(".")]`
prunes it from the walk. This is a known footgun — the check is `has_ts = ".tokensave" in dirnames` before the prune line.

---

## System Tray

Uses `pystray` + `Pillow`. The tray icon is generated at runtime (64×64 dark circle with blue star).
Closing the window (`WM_DELETE_WINDOW`) and minimizing both call `withdraw()` — the process stays alive.
Quit is only available from the tray right-click menu.
Single-instance lock via Windows named mutex (`CreateMutexW`) prevents duplicate manager windows.
