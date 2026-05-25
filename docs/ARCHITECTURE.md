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
├── src/                           Post-Round-4 layout: App + main() in app.py, every
│   │                              other concern in a subpackage (no monolith).
│   ├── app.py                     Entry point — App(tk.Tk) + main(). Constructs the
│   │                              single ManagerConfig instance and passes it down to
│   │                              every controller/dialog. ~1,800 lines including
│   │                              PROMPT_SNIPPETS + Help-tab section bodies.
│   ├── state.py                   ManagerConfig dataclass (runtime-mutable settings;
│   │                              read-only @property getters; mutated via
│   │                              raw.update() + save() + refresh_derived()).
│   ├── constants.py               Immutable constants: C palette, regex tables,
│   │                              CREATE_NO_WINDOW, _ANSI, _GIT_ENV_NO_PROMPT,
│   │                              paths (_BASE_DIR, _CONFIG_PATH, LOG_FILE).
│   ├── theme.py                   _Tooltip widget (Tk-coupled UI primitive).
│   ├── tokensave-wrapper.py       Claude Desktop auto-detection wrapper (~120 lines)
│   ├── agent.py                   LocalAgent loop for the 🤖 Ask tab — Stage 2
│   │                              read-only tool calling + Stage 3 write-tool dispatch
│   │                              via injected on_write_proposal bridge
│   ├── agent_tools.py             ToolSpec registry — 6 read-only tools + opt-in
│   │                              `write_file` via build_tools(with_write=True);
│   │                              write tools route through ProposalBridge per Stage 3
│   ├── precommit_review.py        Standalone entry point for the git pre-commit AI
│   │                              review hook — reads `git diff --cached`, dispatches
│   │                              to backend, parses severity, exits 0 (warn) or 1
│   │                              (block per threshold). Fail-open on every error.
│   │
│   ├── helpers/                   19 modules of pure / IO helpers — no UI deps.
│   │   ├── config.py              _load_config, _save_config, _migrate_config
│   │   ├── detection.py           _detect_git/_gh/_npm/_codegraph/_claude_cli,
│   │   │                          _root_path/_label, _version_lt
│   │   ├── runtime.py             log, _setup_logger, _acquire_instance_lock, _make_tray_icon
│   │   ├── project_discovery.py   find_projects(roots), get/set/clear_pinned, fmt_age
│   │   ├── git.py                 _is_git_repo, _parse_git_status_v2, _format_git_status_cell,
│   │   │                          _find_tracked_but_ignored, _fetch_tags, _git_tag, _git_push_with_tags
│   │   ├── gitignore.py           _ensure_gitignore, _read/_write_gitignore_lines,
│   │   │                          _BASELINE_GITIGNORE, _GITIGNORE_TEMPLATES
│   │   ├── shadow_links.py        generate/remove_shadow_links, update_gitignore_for_shadows,
│   │   │                          DEFAULT_SHADOW_EXT_MAP
│   │   ├── scaffold.py            _scaffold_git_hook + _AUTO_COMMIT_HELPER script body
│   │   ├── mcp.py                 _classify_mcp_entry, _apply_mcp_fix, _resolve_desktop_cfg_path,
│   │   │                          _wrapper_path, _canonical_mcp_entry, _is_claude_running,
│   │   │                          _MCP_CONFIGS, _MCP_CMD_CHECKERS
│   │   ├── llm.py                 _call_llm, _call_anthropic, _call_openai_compat,
│   │   │                          _iter_sse_events, _iter_json_lines, _is_auth_error
│   │   ├── commit_messages.py     _suggest_commit_message + 4 _strat_* strategies + sanitiser
│   │   │                          cluster + _pending_diff + _call_llm_for_commit_message
│   │   ├── release.py             _last_release_tag, _commits_since, _classify_commits_for_changelog,
│   │   │                          _bump_version, _suggest_bump_kind, _render_release_notes,
│   │   │                          _zip_dist, _release_basename, _fmt_size
│   │   │                          (Roadmap-2 P2: _patch_changelog removed; canonical
│   │   │                          implementation now lives in helpers/changelog_patch.py)
│   │   ├── daemon_cost.py         get_daemon_status, toggle_daemon, toggle_autostart,
│   │   │                          parse_tokensave_cost
│   │   │                          (parses `tokensave daemon --status` + `tokensave cost`;
│   │   │                          P2 fixed `--start` bug and added autostart toggle)
│   │   ├── pr_draft.py            generate_pr_draft — LLM-backed structured PR description
│   │   │                          (Summary / Technical / Threat Model / Verification)
│   │   ├── changelog_patch.py     insert_changelog_release (versioned release patcher);
│   │   │                          update_unreleased (replaces [Unreleased] body, EOF-safe);
│   │   │                          read_unreleased (returns current [Unreleased] body for
│   │   │                          LLM integration). All atomic via .tmp + os.replace.
│   │   │                          (original insert_changelog_release: replaces ## [version]
│   │   │                          block bounded by next ^## \[ line; falls back to insertion under
│   │   │                          ## [Unreleased]). Wired into ReleaseWizard P2.
│   │   ├── claude_cli.py          spawn_claude_cli — detached cmd.exe via CREATE_NEW_CONSOLE
│   │   │                          with ""outer"" multi-quote fix + \r\n strip (TTY safety)
│   │   └── precommit_hook.py      install/remove/detect git pre-commit AI review hook
│   │                              + the review runner (run_review, parse_severity_summary,
│   │                              severity_blocks_commit, backend dispatch for
│   │                              "auto"/"claude_cli"/"llm"). Roadmap-2 P5b.
│   │
│   ├── dialogs/                   20 tk.Toplevel dialog classes — one per file.
│   │   ├── settings.py            SettingsDialog (+ _probe_loaded_model helper)
│   │   ├── release_wizard.py      ReleaseWizardDialog + _ReleaseCtx (paired)
│   │   ├── mcp_config.py          MCPConfigDialog
│   │   ├── ai_code_review.py      AICodeReviewDialog
│   │   ├── git_commit.py          GitCommitDialog
│   │   ├── ollama_model_mgr.py    OllamaModelManagerDialog
│   │   ├── gitignore.py           GitignoreDialog
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
│   │   ├── cost_viewer.py         CostViewerDialog (2x2 metric grid; bg-threaded fetch)
│   │   └── proposal.py            WriteProposal dataclass + ProposalDialog +
│   │                              ProposalBridge (P1 — race-safe _resolve under Lock,
│   │                              5-min event.wait timeout, WM_DELETE_WINDOW = reject,
│   │                              post-timeout expired-state UX, automated test harness
│   │                              in __main__ covering 4 race-safety paths)
│   │
│   └── controllers/               Tab controllers + Round-5 sub-controllers extracted
│       │                          from the original god classes. Each takes cfg via
│       │                          callback injection (never a parent reference).
│       ├── projects_tab.py        ProjectsTabController (thin orchestrator after Round 5
│       │                          extracted 9 sub-controllers — ~45 direct methods after
│       │                          Roadmap-2 P5b added one wrapper)
│       ├── git_tab.py             GitTabController (push/pull/release/Draft PR/Open PR
│       │                          on GitHub — ~38 direct methods after Roadmap-2 P4
│       │                          extracted BranchManagementController)
│       ├── branch_mgmt_ctrl.py    BranchManagementController (P4 — new/switch/merge/
│       │                          delete-branch cluster extracted from GitTabController;
│       │                          merge orchestration decomposed into _prepare_merge_sources
│       │                          + _confirm_merge + _merge_worker + _explain_merge_failure)
│       ├── ask_tab.py             AskTabController (🤖 Ask tab — Stage 2 chat)
│       ├── snippets.py            SnippetsController (📚 Reference tab)
│       ├── help_tab.py            HelpTabController (❓ Help tab; extracted from App)
│       ├── update_poller.py       UpdatePollerController (tokensave version probe +
│       │                          GitHub release polling; extracted from App)
│       ├── codegraph_ctrl.py      CodeGraphController (init/sync/status/remove)
│       ├── doctor_ctrl.py         DoctorController (tokensave doctor + purge + P0
│       │                          monolith audit: file/method/class/complexity caps via
│       │                          AST walk + non-Python line-count check; exposes
│       │                          count_methods_in_class for verification scripts)
│       ├── scaffold_ctrl.py       ScaffoldRetrofitController
│       ├── sync_ctrl.py           SyncStatusController (sync, sync_all, force_sync)
│       ├── fileops_ctrl.py        FileOpsController (open folder/editor, copy path)
│       ├── shadowlinks_ctrl.py    ShadowLinksController
│       ├── git_ops_ctrl.py        GitOpsController (git log/commit/AI-review/init/
│       │                          manage-gitignore/untrack-ignored from Projects tab)
│       └── ai_tasks_ctrl.py       AITasksController — long-running AI write tasks
│                                  (Stage 3 CHANGELOG drafter; future: refactor scout,
│                                  release narrative). Per-task per-project lock.
│                                  Orchestrates only; all shared infra in helpers/.
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
    ├── ARCHITECTURE.md             This file
    ├── ARCHITECTURE_TOKENSAVE.md   tokensave tool internals reference
    ├── AGENT_ARCHITECTURE.md       LocalAgent loop + tool registry + propose-only rules
    ├── ROADMAP.md                  Staged plan for local AI features (Stages 0–8)
    ├── MCP_INTEGRATION_GOTCHAS.md  Field manual: UWP path redirection, wrapper stdio bug,
    │                               Connectors UI vs legacy config, live-reload paths
    ├── GITHUB_GUIDE.md             Beginner GitHub guide (concepts, setup, daily workflow)
    └── upstream-issues/            Drafts of bugs to file against upstream tools
        └── tokensave-hook-quoting.md   tokensave install --agent claude path-quoting bug
```

---

## Application architecture (post-Round 4)

### Module-load graph

```
app.py
  ├── state.py           — ManagerConfig (loaded once in App.__init__)
  ├── constants.py       — palette, regex, paths
  ├── theme.py           — _Tooltip
  ├── helpers/*          — pure / IO helpers (no UI)
  ├── dialogs/*          — 18 tk.Toplevel classes
  └── controllers/*      — 4 tab controllers
        └── controllers/* each import the dialogs they instantiate
            (lazy in-handler imports for any cross-dialog cycle risk —
             see Rule 6 in CHANGELOG Round 4 Phase C decisions).
```

`state.py` sits at the bottom of the import graph. It uses lazy imports inside
`refresh_derived()` for `helpers.detection._detect_git` etc. so importing
`ManagerConfig` from a controller or dialog under `TYPE_CHECKING` is safe and
free of circular-import risk.

### The ManagerConfig contract

`App.__init__` constructs `self._cfg = ManagerConfig.load()` exactly once. Every
controller and dialog that needs settings access takes `cfg: ManagerConfig` via
`__init__` and stores it as `self._cfg`. Reads happen at execution time
(`self._cfg.git_exe`, `self._cfg.raw.get("editor_cmd")`) — **never** snapshot in
`__init__` (this is "Rule 3" in the CHANGELOG's Round 4 plan).

The seven derived fields are read-only `@property` getters that raise
`AttributeError` on direct assignment. The single supported mutation path is:

```python
self._cfg.raw.update(new_values_from_settings_dialog)
self._cfg.save()              # persist to manager-config.json
self._cfg.refresh_derived()   # recompute cached git_exe / codegraph_exe
```

That sequence is performed by `App._on_settings_saved`. Every other holder of
`self._cfg` automatically sees the new values because they're reading through
the same instance.

### Entry point

```python
# src/app.py — bottom of file
def main() -> None:
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
```

`Launch TokenSave Manager.bat` invokes `python src/app.py`. `build.ps1` passes
`src/app.py` as the Nuitka entry point; the compiled binary is still named
`tokensave-manager.exe` for backward-compat with the bundled-wrapper detection
in `helpers/mcp.py`.

### Legacy section (pre-Round 4 monolith)

The sections that follow were written when the GUI lived in a single
`src/tokensave-manager.py` file with module-level globals (`TOKENSAVE`,
`GIT_EXE`, `CODEGRAPH_EXE`, `BASIC_INSTRUCTIONS_TEMPLATE`,
`BASELINE_INCLUDE_LINE`, `_cfg`). Those globals are **gone**. The semantics
described below are still correct — substitute the global names mentally as
you read:

| Legacy reference (in this doc) | Post-Round-4 equivalent |
|---|---|
| `GIT_EXE` | `self._cfg.git_exe` |
| `TOKENSAVE` | `self._cfg.tokensave_exe` |
| `TEMPLATE_DIR` | `self._cfg.template_dir` |
| `CODEGRAPH_EXE` | `self._cfg.codegraph_exe` |
| `SEARCH_ROOTS` | `self._cfg.search_roots` |
| `BASIC_INSTRUCTIONS_TEMPLATE` | `self._cfg.basic_instructions_template` |
| `BASELINE_INCLUDE_LINE` | `self._cfg.baseline_include_line` |
| `_cfg[k]` / `_cfg.get(k)` | `self._cfg.raw[k]` / `self._cfg.raw.get(k)` |
| `_save_config(_cfg)` | `self._cfg.save()` |
| `global GIT_EXE; GIT_EXE = …` in `_on_settings_saved` | `self._cfg.refresh_derived()` (single call) |
| Module-level helpers (e.g. `_is_git_repo`) | Same name, now in `helpers/<module>.py` (e.g. `helpers/git.py`) |
| Dialog / controller classes | Same names, now one per file under `dialogs/` or `controllers/` |

Round 4 was a structural-only refactor — no behaviour changes. Every rule
about thread boundaries, `self.after(0, ...)`, `_git_begin_op` /
`_git_end_op`, `_offer_commit_after_change`, etc. is unchanged; only the
file location changed.

---

## `src/app.py` — Main Application (legacy text)

Single-file tkinter application (~12,500 lines as of [Unreleased]). Entry point is `if __name__ == "__main__": App().mainloop()`. Size has grown substantially this cycle from the addition of Stages 1–2 AI features, the MCP configurator, the Ollama Model Manager, and various dialog classes — see the "Dialog classes added [Unreleased]" subsection below for the new ones.

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
│   ├── ► Git tab commands (cmd_git_push/pull/undo/set-remote/new-branch/switch/merge/delete/release/open-pr/merge-pr + cmd_github_setup) → GitTabController (see below)
│   ├── ► Snippet commands (_add_snippet / _edit_snippet / _delete_snippet / _on_snippet_saved) → SnippetsController (see below)
│   └── ► Ask tab UI → AskTabController (see below)
└── Worker helpers (App-owned)
    ├── _run()                   — generic tokensave CLI call (threaded, streaming); delegates _auto_commit_after_sync() for the post-sync commit; amends previous commit if last message was "chore: tokensave sync"
    ├── _run_capture()           — tokensave call returning (output, rc, elapsed); synchronous from thread
    ├── _shell_capture()         — generic shell call returning (output, rc); catches FileNotFoundError
    ├── _scaffold_project()      — write BASIC_INSTRUCTIONS + optional tokensave init + Nuitka + git hook
    ├── _scaffold_nuitka_build() — copy nuitka-build.ps1/bat templates into a project folder
    ├── _do_retrofit()           — prepend @include to CLAUDE.md + optional BASIC_INSTRUCTIONS + Nuitka + shadow links + git hook
    ├── _do_shadow_links()       — generate hardlinks in background thread, optionally run sync after
    ├── _do_assign_category()    — write project_categories override to config, refresh tree
    ├── _do_git_commit()         — git reset → git add -- <selected_files> → git commit -m, background thread; commits only ticked files from GitCommitDialog
    ├── _on_project_select()     — <<TreeviewSelect>>: calls self._git.set_active_path() + self._git.refresh() if Git tab visible
    ├── _on_tab_changed()        — <<NotebookTabChanged>>: routes to self._ask_ctrl.on_tab_selected() or self._git.refresh()
    ├── _show_status_popup() / _format_status_msg()
    ├── _show_git_popup()        — Toplevel with scrollable monospace git output
    └── _log()                   — thread-safe coloured log append

GitTabController (standalone class, App._git)
    ├── __init__(notebook, cfg, get_path, on_log, on_shell, on_commit) — 4 explicit callbacks; no App back-ref; queue.Queue for worker→main-thread log drain
    ├── Public API: is_visible() / refresh() / set_active_path(path) / has_path()
    ├── _poll_log_queue()        — drains entire (msg, color) queue every 100 ms via self._tab.after(); inner try/except prevents loop death on bad log lines
    ├── _build_git_tab() → _build_git_header() / _build_git_status_pane(mid) / _build_git_action_bar() / _build_git_diff_pane()
    ├── cmd_git_push / cmd_git_pull — push/pull with GIT_TERMINAL_PROMPT=0 + auth error handling
    ├── cmd_git_undo_commit       — git reset --soft HEAD~1 with confirmation
    ├── cmd_git_set_remote / _do_git_set_remote — SetRemoteDialog; git remote add/set-url
    ├── cmd_git_new_branch / _do_git_new_branch — NewBranchDialog; git checkout -b or git branch
    ├── cmd_git_switch_branch / _do_git_switch_branch — SwitchBranchDialog; git checkout with dirty-tree guard
    ├── cmd_git_merge             — branch picker; git merge --no-edit; conflict + dirty-tree handling
    ├── cmd_git_delete_branch / _confirm_branch_delete / _do_delete_branch — safe/force delete with extracted confirm + remote-delete prompt helpers
    ├── cmd_github_setup          — GitHubSetupDialog for current/selected project
    ├── cmd_git_open_pr / cmd_git_merge_pr / _show_merge_pr_dialog / _do_merge_pr / _post_merge_pr_sync
    ├── cmd_git_release           — pre-flight dirty-tree check then opens ReleaseWizardDialog
    ├── _git_refresh()           — background fetch of branch/remote/status/log; calls _git_update_ui
    ├── _git_update_ui()         — main-thread update of all Git tab widgets + button states
    ├── _git_show_diff()         — render diff into Text widget with colour tags (capped at 2000 lines)
    ├── _on_git_status_select()  — click file in status listbox → fetch + show diff
    └── _git_begin_op() / _git_end_op() — disable/enable all Git tab buttons during in-flight operations

SnippetsController (standalone class, App._snippets_ctrl)
    ├── Reference tab snippet list: _refresh_snippet_list() + _add_snippet / _edit_snippet / _delete_snippet / _reset_snippet / _on_snippet_saved
    ├── Built-in catalogue lives in src/prompts.py (immutable ROM). User edits
    │   layer on top via cfg.raw["builtin_snippet_overrides"] (RAM overlay) —
    │   Reset just pops the override key, defaults are never destructively
    │   modified. Saving an empty override body is treated as an implicit
    │   reset (pops the key rather than persisting an empty string).
    ├── Placeholder substitution: snippets may contain [[double-bracket]]
    │   tokens (single [brackets] are reserved for markdown links and
    │   footnote refs). Selecting a snippet renders Label+Entry pairs in
    │   a 2-column grid below the preview; Copy substitutes filled values.
    │   Empty fields leave [[token]] as literal text in the clipboard.
    │   Pure helpers _extract_placeholders / _substitute_placeholders sit
    │   at module scope so they're unit-testable without a Tk root.
    └── Layout-jitter prevention: action buttons are packed side=tk.BOTTOM
        FIRST, then placeholder frame, then preview last with expand=True.
        Result: adding/removing placeholder fields shrinks/grows the preview
        but never moves the Copy/Edit/Reset/Delete row Y-coordinate.

AskTabController (standalone class, App._ask_ctrl)
    ├── 🤖 Ask tab: chat log Text, Send/Stop/Clear controls, _ask_messages conversation history
    └── on_tab_selected() — called by App._on_tab_changed when Ask tab gains focus

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
AICodeReviewDialog (tk.Toplevel) — modal: Stage 1 AI Code Review. Split-pane with the project's `git diff HEAD` on top (green/red colour-coded) and a streaming AI review below. One `_call_llm` invocation with locked `_SYSTEM_PROMPT` class-constant. Tokens stream into the bottom pane via an `on_token` callback wrapped in worker-side batching (~50ms / 8 tokens) to avoid Tk event-loop saturation. Section-header colour tags (⚠/⚡/💡/ℹ) applied in a final pass when streaming ends. Stop button via `_review_token` mismatch (cancellation never orphans the worker — stale results just get discarded). Opened by right-click → 🔍 AI Code Review….
OllamaModelManagerDialog (tk.Toplevel) — modal: browse / pull / delete Ollama models without leaving the manager. Uses Ollama's NATIVE REST API (not the OpenAI-compatible /v1 surface): `GET /api/version` health check, `GET /api/tags` for installed models, `POST /api/show` per-model context-length, streaming `POST /api/pull` with live progress bar, `DELETE /api/delete`. Pull progress streams newline-delimited JSON via `_iter_json_lines`. Critical pattern: Cancel during pull explicitly closes the `HTTPResponse` object (not just `threading.Event.set()` — the worker is syscall-blocked inside `read()` and only socket close unblocks it). "Use for AI features" button pre-fills the parent SettingsDialog's provider/model/base-URL fields. Opened by Settings → "🦙 Manage Ollama Models…".
MCPConfigDialog (tk.Toplevel) — modal: classify + edit `tokensave` MCP entries in BOTH Claude Desktop's config AND Claude Code's `~/.claude.json`. Header banner warns if Claude is currently running (via `_is_claude_running`) — Desktop rewrites its config file every 1–2 minutes with cached in-memory state, silently clobbering any edit applied while it's alive. Per-row diff display, per-row Apply button (separate confirmations, one per config), Re-detect, Open file, Skip-and-don't-warn-again (writes path to `manager-config.json` → `mcp_skip_warnings` list which `_check_config` honours). Apply uses backup-first via `shutil.copy2`. The Apply-refused path uses `messagebox.showerror` (red icon, reads as rejection — earlier showwarning was easy to dismiss without realising no write happened), logs a red `MCP Apply REFUSED:` line to the main OUTPUT pane, AND calls `self._render()` to refresh the row state. UWP-aware: `_resolve_desktop_cfg_path` globs `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\` and targets that when found. Opened by Settings → "🔌 Manage MCP wiring…" OR auto-launched 800ms after manager startup if `_check_config` finds drift.
MergePRDialog (tk.Toplevel) — modal: pick an open GitHub PR + a merge strategy, then confirm. Treeview lists every open PR via `gh pr list --json number,title,headRefName,baseRefName,additions,deletions,author,url --limit 50`. Three strategy buttons (Merge commit / Squash and merge / Rebase and merge) map 1:1 to `gh pr merge`'s flags. Delete-source-branch checkbox (default on, matches GitHub web UI default). Single confirmation modal per merge attempt. Callback signature: `(path, pr_number, strategy, delete_branch, title)`. Opened by Git tab → "🐙 Merge PR…". Strategy buttons are gated until a row is selected. Post-merge: App's `_post_merge_pr_sync` auto-discovers the default branch via `git symbolic-ref refs/remotes/origin/HEAD --short`, switches local to it if needed, runs `git pull --ff-only`.
ReleaseWizardDialog (tk.Toplevel) — modal: full-featured release wizard. Six sections in a scrollable canvas: (1) version with last-tag detection + Patch/Minor/Major radios biased by commit content + free-text override; (2) auto-filled title; (3) auto-drafted release notes textarea grouped by conventional-commit prefix; (4) build step with `build.ps1` / `build.bat` auto-detect; (5) artefact preview showing dist contents + resolved zip name; (6) CHANGELOG.md sync checkbox. Publish runs `_publish_worker` (background thread) which passes a `_ReleaseCtx` dataclass (`tag`, `title`, `notes`, `zip_path`, `notes_file`, `staged_files`) through 8 `_pub_*` step methods in sequence: `_pub_build` → `_pub_zip` → `_pub_write_notes` → `_pub_patch_changelog` → `_pub_stage_commit` → `_pub_tag` → `_pub_push` → `_pub_gh_release`; cleanup and done are inlined at the end. Each step returns `bool` and calls `self._fail(msg)` on error with a copy-pasteable recovery command; `staged_files` uses `dataclasses.field(default_factory=list)` to prevent mutable-default cross-run contamination. The notes temp file is preserved on final-step failure so the user can retry `gh release create` without retyping notes. Opened by 📦 Release… on the Git tab. Pre-flight in `cmd_git_release` refuses if working tree is dirty beyond `CHANGELOG.md` (hands off to Git Commit dialog).
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
| `_suggest_commit_message(repo_path, status_text)` | **Multi-strategy orchestrator.** Chain order (highest-quality first): LLM → CHANGELOG.md staged bullets → diff content → file-name fallback. Implemented as a lambda table over four `_strat_*` module-level functions (`_strat_llm`, `_strat_changelog`, `_strat_diff`, `_strat_filenames`); the loop calls each in order, skips `None` returns, and passes the first hit to `_sanitize_commit_message`. Sanitize pipeline: `_strip_md` (promoted from inner fn) strips markdown formatting → `_escalate_commit_type` rewrites `chore:`/`docs:` subjects to `refactor:` when source files changed → subject truncated to 72 chars → `_normalize_commit_body` wraps paragraphs + bullet lists via `textwrap.fill`. Used by `GitCommitDialog` for initial fill and the 💡 Suggest button. |
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
| `_git_op_in_flight` + `_git_begin_op()` / `_git_end_op()` (GitTabController method) | Locking pattern that disables every Git tab button during an in-flight operation. Honoured by `_git_update_ui()` so incidental refreshes don't bypass the lock. All git command methods wrap their workers with begin/end in a `try`/`finally`. |
| `_GITIGNORE_TEMPLATES` | Module-level dict mapping category name → list of patterns. The Baseline category is built from `_BASELINE_GITIGNORE` via `_baseline_patterns()` at module load (single source of truth). Used by `GitignoreDialog`'s template-inject buttons. |
| `_read_gitignore_lines(path)` / `_write_gitignore_lines(path, lines)` | Pure file-IO for the gitignore editor. Read returns `[]` if missing, uses `utf-8-sig` to tolerate PowerShell-written BOMs. Write is atomic (`.tmp` + rename) and always ends with a trailing newline. |
| `CODEGRAPH_EXE` | Path to the codegraph CLI (npm-installed). Resolved from `_cfg["codegraph_exe"] or _detect_codegraph()` at startup; rebuilt in `_on_settings_saved`. Empty string when not installed. |
| `_detect_codegraph()` / `_detect_npm()` | Windows-`.cmd`-first PATH probes. Both check `shutil.which("X.cmd")` before `shutil.which("X")` because npm-installed binaries are `.cmd` shims, not `.exe` files; `subprocess.run` with a bare `.cmd` raises `FileNotFoundError`. |
| `_is_codegraph_project(path)` | True iff `.codegraph/codegraph.db` exists in the project root. Mirrors `_is_git_repo` for codegraph. |
| `_is_local_git_repo(path)` | Strict local "is this folder a git repo root?" check using `os.path.exists(.git)`. Handles both standard repos and git worktrees (where `.git` is a flat pointer file, not a directory). Used by `_offer_commit_after_change` to avoid ghost-prompts when a project is nested inside an unrelated parent git repo. |
| `cmd_codegraph_init/sync/status/remove` (App methods) | Per-project lifecycle commands mirroring the tokensave equivalents. `init` uses `--index` to build the graph in one step. All four gated by `_require_codegraph_installed()`. |
| `_require_codegraph_installed()` (App method) | Parallel to `_require_tokensave`. Returns True iff `CODEGRAPH_EXE` is non-empty AND the file exists. Otherwise shows an install-nudge dialog that opens Settings and focuses the CodeGraph path entry. |
| `GitignoreDialog(tk.Toplevel)` | User-facing `.gitignore` editor. Opened via right-click → 📋 Manage .gitignore…. Canvas-backed scrollable Frame for per-row removal buttons; real-strikethrough font (`tkfont.Font(overstrike=1)`) for marked-removed rows; template inject buttons (push, not stateful); custom-entry field with sanity check; live Pending changes Text widget. Save → `_write_gitignore_lines` → `_offer_commit_after_change`. |

### Helpers added in this cycle ([Unreleased])

| Symbol | Purpose |
|--------|---------|
| `_call_llm(cfg, system_prompt, user_prompt, max_tokens=1500, timeout=None, on_token=None)` | Generalised from the commit-message-specific helper. Returns `str \| None`. New `on_token` parameter enables streaming: when provided, sends `"stream": true` to the provider and calls `on_token(delta)` for each text chunk. Anthropic + OpenAI-compatible streaming both supported via byte-aligned `_iter_sse_events` SSE parser. The streaming path still returns the accumulated full text at end-of-stream for callers that use the return value. Used by `AICodeReviewDialog._start_review` (streaming) and `_call_llm_for_commit_message` (non-streaming). |
| `_iter_sse_events(response)` | Module-level generator. Accumulates raw bytes from an `HTTPResponse` in a `bytearray`, splits on `\n` (CRLF tolerant), yields each `data: ...` payload. Handles mid-line network fragmentation correctly — `readline()` doesn't work reliably here because the SSE stream isn't always newline-terminated at network-buffer boundaries. |
| `_iter_json_lines(response)` | Same idea as `_iter_sse_events` but for Ollama's `/api/pull` newline-delimited JSON output (no `data:` prefix). |
| `_TOKENSAVE_UPDATE_RE` | Regex matching `Update available: vX.Y.Z → vA.B.C` lines in tokensave sync output. Used by `_run`'s line-by-line parser to detect when a newer tokensave release is available and populate `App._tokensave_available_version`. Accepts Unicode `→` and ASCII `->`/`=>` arrows. |
| `_version_lt(a, b) -> bool` | Module-level numeric-tuple version comparator. Splits on `.`, pads to equal length, compares tuple-wise. Falls back to string compare on parse failure. Used by `App._check_tokensave_updates` to decide if a GitHub-reported latest tag is newer than the installed version. |
| `_MCP_DESKTOP_CFG_PATH` / `_MCP_CODE_CFG_PATH` | Module-level constants for the two Claude MCP config files. Desktop path is computed via `_resolve_desktop_cfg_path()`. |
| `_resolve_desktop_cfg_path()` | UWP-aware: globs `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json` first and returns the most-recently-touched candidate. Falls back to the traditional `%APPDATA%\Claude\claude_desktop_config.json` if no UWP install detected. Critical for Microsoft Store / packaged Claude installs where the two paths resolve to DIFFERENT physical files depending on caller context — see `docs/MCP_INTEGRATION_GOTCHAS.md`. |
| `_canonical_mcp_entry()` | Returns the dict shape the manager wants every Claude config to have — `pythonw.exe + tokensave-wrapper.py` in source mode, `tokensave-wrapper.exe` in bundled mode. Sources `python_exe` from `manager-config.json`. |
| `_classify_mcp_entry(cfg_path)` | Pure inspector that returns `{state, label, issue, current, proposed, cfg_path}`. Label-aware: for Claude Code (`.claude.json` paths), treats `tokensave.exe serve` (no hardcoded `-p`) as `state="ok"` because that's the canonical shape `tokensave install --agent claude` writes and `tokensave doctor` blesses. For Claude Desktop, requires wrapper-routed (Desktop's long-lived MCP server can't re-auto-detect per spawn). Hardcoded `-p` flagged in both. |
| `_apply_mcp_fix(cfg_path, entry)` | Writes a new `tokensave` entry into the given JSON config, backup-first via `shutil.copy2(..., f"{path}.backup.{int(time.time()*1000)}")`. Preserves all other `mcpServers` entries verbatim. Returns `(ok, msg)`. Creates parent dirs if needed; tolerates the case where the file doesn't exist yet (creates a fresh one with just the tokensave entry). |
| `_is_claude_running()` | Uses `tasklist /FO CSV /NH` to detect running `claude.exe` and `claude-code.exe` processes. Returns `{"desktop": bool, "code": bool, "pids": [...]}`. Best-effort (silent failure on tasklist error). Used by `MCPConfigDialog._apply` to refuse writing over running Claude (whose preferences-save loop would silently clobber the edit). |
| `App._probe_tokensave_version()` | At App startup: runs `tokensave --version` on a daemon thread, parses the version string, stores in `self._tokensave_current_version`. Also kicks off `_check_tokensave_updates` for the initial check. |
| `App._tokensave_update_poll_loop()` | Daemon thread that calls `_check_tokensave_updates` once per `tokensave_update_poll_hours` (default 1.0, floor 0.25). Polls GitHub's releases API for the latest tag, compares with installed via `_version_lt`, populates `_tokensave_available_version` if newer. Logs a peach hint on fresh-discovery transitions (skips re-confirms). |
| `App._check_tokensave_updates()` | Single-shot check: hits `api.github.com/repos/aovestdipaperino/tokensave/releases/latest`, extracts tag, compares with `_tokensave_current_version`. Silent on offline/rate-limited. |
| `App._extract_doctor_stale_paths(output_lines)` | Pure parser for `tokensave doctor`'s `! N stale project(s) in global DB` warning. Returns the list of bulleted paths (accepts `•`, `*`, `-` bullets). Used by the Doctor button's purge-offer flow. |
| `App._offer_doctor_purge(path, stale_paths)` | Pops a confirmation modal listing stale entries; on yes, calls `_run_doctor_purge` which re-invokes doctor with `y\ny\ny\ny\ny\n` piped to stdin. The piped-stdin approach often fails (tokensave's prompt uses `is_terminal()` — a real TTY check). Auto-detection: a third silent run checks whether stale entries are still present; if so, offers a follow-up `_offer_doctor_in_cmd` dialog that spawns `cmd.exe /k ""<tokensave>" doctor"` in a NEW console window (CREATE_NEW_CONSOLE flag) where the user can answer the TTY prompt for real. |

### Dialog classes added in this cycle ([Unreleased])

| Class | Purpose | Opened from |
|---|---|---|
| `AICodeReviewDialog` | Stage 1 AI Code Review — diff + streaming severity-coloured review | Right-click → 🔍 AI Code Review… |
| `OllamaModelManagerDialog` | Browse / pull / delete Ollama models via native REST API | Settings → 🦙 Manage Ollama Models… |
| `MCPConfigDialog` | Classify + edit tokensave MCP entries in both Claude configs | Settings → 🔌 Manage MCP wiring… (or auto-launched at startup if drift detected) |
| `MergePRDialog` | Pick an open GitHub PR + merge strategy, then confirm | Git tab → 🐙 Merge PR… |

### AskTab — Stage 2 agent chat (not a class)

The 🤖 Ask tab is built as methods on the `App` class (`_build_ask_tab`, `_ask_send`, `_ask_stop`, `_ask_clear`, `_ask_append`, `_ask_refresh_header`, `_ask_set_intro`) rather than a separate dialog because the chat log + input row + status line all live inside the notebook tab directly. State: `self._ask_messages` (the running message history), `self._ask_stop_event` (current `threading.Event` for cancellation), `self._ask_thread` (worker thread reference), `self._ask_path` (currently-selected project — synced from the Projects tab selection in `_on_tab_changed`). The agent itself lives in `src/agent.py` (`LocalAgent`); the tools live in `src/agent_tools.py` (`build_tools(project_path, tokensave_exe)`). See `docs/AGENT_ARCHITECTURE.md` for the full design rationale.

### Configuration (`manager-config.json`)

All machine-specific values live in `manager-config.json` at the project root.
Loaded at startup by both `app.py` (via `ManagerConfig.load()` → `helpers/config.py::_load_config()`) and `tokensave-wrapper.py` (directly via `_load_config()`).
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
| `commit_message_llm` | Dict. AI-commit-message + general LLM settings. Keys: `enabled` (bool), `provider` (str — `anthropic`/`openai`/`openai_compatible`/`ollama`), `model` (str), `api_key_env` (str — env var name), `base_url` (str — for OpenAI-compatible local servers), `min_diff_lines` (int — default 30), `max_diff_chars` (int — default 24000), `timeout_seconds` (int — default 90), `use_for_sync_autocommit` (bool), `num_ctx` (int — Ollama context window, default 32768, only Ollama). The agent in `src/agent.py` reads this whole dict. |
| `mcp_skip_warnings` | List of absolute file paths. Each path corresponds to an MCP config file (`claude_desktop_config.json` or `.claude.json`) the user has chosen NOT to be warned about further. `_check_config` honours this list to keep the startup banner silent for explicitly-dismissed configs. Managed via `MCPConfigDialog`'s Skip button. |
| `tokensave_update_poll_hours` | Float, default 1.0 (min 0.25). How often the daemon background poller hits the GitHub releases API to check for tokensave updates. Tunable to balance freshness against GitHub API rate limits (60/hr unauthenticated). |

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
   with CREATE_NO_WINDOW flag and EXPLICIT stdio handle pass-through
6. sys.exit(proc.wait()) — becomes the MCP server process
```

**Critical:** the `.tokensave` dir check must happen **before** `dirnames[:] = [d for d in dirnames if not d.startswith(".")]`
prunes it from the walk. This is a known footgun — the check is `has_ts = ".tokensave" in dirnames` before the prune line.

**ALSO CRITICAL ([Unreleased] fix):** the `subprocess.Popen` call MUST pass `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` explicitly:

```python
proc = subprocess.Popen(args,
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    creationflags=CREATE_NO_WINDOW)
```

Without those explicit args, Python's default "inherit standard handles" behaviour does NOT reliably propagate piped stdio handles to a console child process when the parent is `pythonw.exe`. The tokensave child gets unusable standard handles, never sees MCP messages from Claude Desktop, and Desktop times out at 30 s with `MCP server tokensave connection timed out after 30000ms`. Diagnosed in [Unreleased] by running the wrapper directly with `subprocess.PIPE` stdio and feeding a real MCP `initialize` request — default-inheritance Popen produced zero bytes from tokensave, explicit pass-through returned a valid response in <100 ms. **The wrapper script must stay single-threaded** — adding `import threading` and daemon threads introduces additional subtle stdio-handling issues under pythonw.exe. Live in-session pin reloading is deferred and must be implemented out-of-process. See `docs/MCP_INTEGRATION_GOTCHAS.md` for the full forensic write-up.

---

## System Tray

Uses `pystray` + `Pillow`. The tray icon is generated at runtime (64×64 dark circle with blue star).
Closing the window (`WM_DELETE_WINDOW`) and minimizing both call `withdraw()` — the process stays alive.
Quit is only available from the tray right-click menu.
Single-instance lock via Windows named mutex (`CreateMutexW`) prevents duplicate manager windows.
