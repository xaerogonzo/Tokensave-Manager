# Changelog — TokenSave Manager
<!-- Created by Alexander L Corthell -->

## [Unreleased]

### Added
- **Config migration on manager startup auto-bumps old AI defaults.** `_migrate_config` runs once during `_load_config()` and treats any saved `timeout_seconds < 30` or `max_diff_chars < 16000` as "leftover from a previous version" and bumps them to the current defaults (90 and 24000 respectively). Saves back to disk if anything changed; no-ops on already-migrated configs. Users who explicitly picked intermediate values (e.g. `timeout_seconds: 60`) keep their choice. Fixes the v1.0.x → current upgrade trap where existing keys would never be touched by `setdefault()`.
- **Default `max_diff_chars` bumped from 8000 to 24000.** Diagnosed when a real commit produced a thin AI message that only mentioned the first file in alphabetical order — the diff was ~50 KB but the LLM only "saw" the first 8000 chars (≈ 2000 tokens) due to the prompt truncation guard. 8000 was a conservative default from before reasoning models forced max_tokens up to 1500; with that headroom now in place and modern local models all sporting 32k+ context windows, we can comfortably send 3× more diff context. AICodeReviewDialog and the commit-message orchestrator both share this default. Existing configs are migrated when the user re-saves Settings; the in-process default applies immediately for code paths that don't go through saved config.
- **Three sanitizer hardening fixes for `_sanitize_commit_message`** — closes gaps the qwen2.5-coder:14b model exposed in real use:
  - **Filename-listing regex now catches `and`/`;`/`or` connectors.** Previously only caught `update X.md, Y.md` (comma). Now also catches `update X.md and Y.md`, `update X.md, Y.md, and Z.md` (Oxford), `update X.md; Y.md`, etc. Verb list also broadened from `update/change/modify` to include `edit/refactor/fix`. Stops the anti-pattern at the door regardless of which connector the LLM picks.
  - **`docs:` escalates to `refactor:` when source files changed.** Mirrors the existing `chore:` → `refactor:` escalation. Local 14B models routinely mistype source-touching commits as `docs:` because they latch onto the first `.md` file in the diff. Scoped `docs(api):` is preserved (the scope is specific).
  - **Multi-bullet-on-one-line splitter in body normalisation.** Pre-processes `. -` and `: -` patterns by inserting a newline before the `-`, then wraps each bullet independently with hanging-indent continuation. Turns "jumbled paragraph" output from small quantized models into a scannable bulleted list. Doesn't detect logical nesting (would need to parse intent) but flat-but-readable beats jumbled.
- **Suppress tokensave's misleading "legacy `.codegraph/`" warning when CodeGraph is active.** Tokensave prints `warning: found legacy .codegraph/ directory at '<path>'. tokensave now uses .tokensave/ — the old directory can be safely deleted.` on every sync, but for projects that use BOTH tokensave AND CodeGraph as equal citizens (which the manager fully supports), this is wrong — deleting `.codegraph/` would wipe out the CodeGraph index. The manager now detects when `.codegraph/codegraph.db` exists at the project root and silently drops that specific warning, replacing it with a single one-line trace note `(suppressed tokensave's '.codegraph/ legacy' warning — CodeGraph is active in this project)` so the user knows filtering happened. Orphaned `.codegraph/` folders (no `codegraph.db`) still show the original warning — it's only suppressed when CodeGraph is genuinely active.
- **Reference tab prompt snippets refreshed — 12 → 26 snippets, hitting ~30 tokensave tools** (up from the original 9). Visually grouped into five categories via emoji prefixes:
  - **🧭 Exploration** (5) — Codebase overview, Find a symbol, Understand a feature, Onboarding tour, Module public API
  - **🔬 Analysis / Tracing** (4) — Bidirectional call chain, Impact, Trace a bug, Find similar code
  - **📊 Audits** (7) — Full health audit, Architecture report, Test coverage audit, Security / unsafe-patterns scan, Hotspot risk scan, Pre-release readiness, Documentation coverage report
  - **🪦 Findings / Cleanup** (4) — Dead code, TODOs, Circular deps, Largest/most-complex
  - **🛠 Workflows** (6) — Pre-commit checklist, PR review prep, Plan a refactor, Refactor rename preview, Generate CHANGELOG, Generate ARCHITECTURE.md draft
  The new audits chain multiple tokensave tools into single comprehensive prompts. The full-health-audit snippet alone uses 10 tools and produces a 🔴/🟡/🔵 categorised report.
- **Roadmap expanded with Stages 6-8 (💭 Considering).** See `docs/ROADMAP.md`:
  - **Stage 6 — Workflow accelerators**: pre-commit AI review hook, PR description generator, release-notes narrative writer
  - **Stage 7 — Quality assurance suite**: test gap analyzer, documentation freshness checker, dependency / license audit, secret leak scanner
  - **Stage 8 — Knowledge management**: decision log (`docs/DECISIONS.md`), FAQ extractor, cross-project pattern library
  Plus an "Open ideas" list (local-only mode, diff sanitizer, natural-language tokensave query, AI-tagged categories, commit-msg AI explanation, multilingual support) for smaller exploratory features.
- **🔍 AI Code Review dialog (Stage 1 of the agentic-AI roadmap).** Right-click any project → "🔍 AI Code Review…" opens a split-pane dialog: top pane shows the pending diff (`git diff HEAD`) with green/red colour-coding, bottom pane shows an AI-generated structured review (⚠ High / ⚡ Medium / 💡 Low / ℹ Observations) with appropriately-coloured section headers. Async with a spinner — never freezes the GUI. **Stop** button discards in-flight results via token cancellation. **Regenerate** re-runs the review on the same diff. **Copy review to clipboard** for pasting elsewhere. Pure read-only: no tools, no file writes, no autonomy concerns. Requires AI to be enabled in Settings; surfaces a helpful "AI is not enabled" message otherwise. See `docs/ROADMAP.md` for the full staged plan and `docs/AGENT_ARCHITECTURE.md` for the design philosophy.
- **`ollama` is now a first-class provider in Settings → AI commit messages.** Previously the Ollama preset configured `provider: "openai_compatible"` and pointed at `http://localhost:11434`. It still works that way — but `ollama` is now an explicit dropdown option that auto-falls-through to OpenAI-compatible dispatch with the default Ollama base URL when no base is set. The preset's auto-detected model now defaults to `qwen2.5-coder:14b` (instead of `llama3.2`) if no model is loaded — matches the roadmap's recommended starter pack.
- **`docs/ROADMAP.md`** — user-facing staged plan for the local AI assistant work (Stages 1-5 with status badges, model recommendations, locked architectural rules, non-goals). Linked from `README.md`.
- **`docs/AGENT_ARCHITECTURE.md`** — design doc covering the agent loop, propose-only philosophy, tool registry, and how to safely add new tools in future stages.

### Refactored
- **`_call_llm_for_commit_message` factored into a general `_call_llm`** that any feature can use. The commit-message wrapper now composes its prompt and delegates. AI Code Review uses the same `_call_llm` with a different system prompt. Future agentic stages will use it for tool-call dispatch too. Signature: `_call_llm(cfg, system_prompt, user_prompt, max_tokens=1500, timeout=None) -> str | None`. Returns `None` on every failure path (no key, network error, timeout, empty response) — caller decides how to fall back.

### Added (earlier in cycle)
- **Hotfix bump option in the Release Wizard** — fourth radio option below Patch/Minor/Major produces a 4-part version (`v1.0.4` → `v1.0.4.1`, `v1.0.4.1` → `v1.0.4.2`). Intended for small adjustments on top of an existing release without starting a new patch series. Not strict semver — but GitHub doesn't care and it's a clear human signal that "this is a small fix on top of 1.0.4, not 1.0.5." Picking patch / minor / major from a 4-part version normalises back to 3-part automatically, so a hotfix branch merges cleanly into the next regular release. `_bump_version` accepts the new `kind="hotfix"`; `_suggest_bump_kind` deliberately never returns it (stays a manual choice).
- **Live "Will publish as" preview in the Release Wizard** — green label below the version controls updates on every keystroke / radio change. Removes the "do I write `v1.0.5` or just `1.0.5`?" ambiguity entirely: whatever you type, the resolved tag is shown right there. The `Custom tag:` entry also gained an inline placeholder hint (`e.g. v1.0.5 or 1.0.5 or v1.0.4.1`). New `_refresh_resolved_tag` method centralises the cascade — radios, custom entry, title, and artefact preview all flow from the same single source.

- **Smart commit-message generation in the Git Commit dialog** — `_suggest_commit_message` is now a multi-strategy orchestrator that tries (in order) the LLM, CHANGELOG.md staged bullets, diff-content analysis (added Python `def`/`class` names, file kinds), and finally the legacy filename-pattern fallback. A new `_sanitize_commit_message` step enforces 72-char subjects, imperative mood, no filename listings in the subject, and escalates generic `chore:` to `refactor:` when source files changed. The dialog's 💡 Suggest button (and initial auto-populate) now produces multi-paragraph messages with proper conventional-commit prefixes and scope inference — `feat(release-wizard): hotfix bump option + 2 more` with a full body — instead of `chore: update X.md, Y.md + 1 more`. The initial selection in the message field is now just the first line so the user can replace the subject while preserving the AI/CHANGELOG-derived body.
- **Optional AI commit messages via Anthropic, OpenAI, LM Studio, or Ollama.** Settings → "AI commit messages" section adds a provider dropdown, model field, API-key env-var name, base URL (for OpenAI-compatible local servers), three preset buttons (Anthropic / LM Studio / Ollama), and a min-diff-lines threshold so trivial commits skip the LLM. A separate toggle ("Also use AI for sync auto-commit messages") opts the auto-commit-after-sync flow into LLM-generated messages — disables amend-stacking since each AI message is unique. All LLM failures (no key, network error, timeout, invalid response) silently fall back to the heuristic chain — never blocks the commit dialog.

### Fixed
- **AI commit-message generation now runs on a background thread with a visible spinner.** Previously the LLM call blocked the GUI's main thread for up to 12 seconds (the old timeout). Reasoning models (Qwen3, DeepSeek-R1, etc.) on consumer GPUs routinely take 30–60+ seconds to think, which always tripped the timeout → silent fallback. Now the orchestrator runs in a daemon thread; a small "⟳ Generating with AI… (can take 30–60s on local models)" label appears next to the 💡 Suggest button while it works; the result populates the message field via `self.after(0, ...)` when ready. Cancellation tokens prevent stale results from overwriting the field if you click Suggest a second time, and `_user_has_edited` guards against overwriting any keystrokes you've made.
- **LLM `max_tokens` bumped from 400 to 1500.** Reasoning models budget hundreds of tokens for internal "thinking" before they can emit the actual commit message; 400 was below that threshold for any meaningful prompt → `finish_reason: "length"` → empty `content` → silent fallback. 1500 is roomy enough for the worst-case reasoning model. Non-reasoning models stop on `<eos>` well before the ceiling, so for them this is free. Cloud APIs charge for tokens used, not allocated.
- **Default LLM timeout bumped from 12s to 90s.** 12s was too aggressive for any reasoning model on local hardware. Existing user configs with `timeout_seconds < 30` are auto-promoted to 90s at runtime — users who explicitly chose a different value (e.g. 60) keep their choice.
- **CHANGELOG-bullet commit-message strategy now sees bullets added to existing `### Section` headers.** `_extract_changelog_additions` used `-U0` context, which stripped the `### Changed` / `### Added` header lines from the diff output. Bullets added to existing sections (the common case — only fresh releases get brand-new section headers) were silently invisible to the parser, so the orchestrator skipped Strategy 1 and fell to the worse heuristics below. Switched to `-U20` context AND now tracks section state from BOTH context and added lines, with hunk-boundary resets so context from a far-away hunk doesn't leak into a different bullet's attribution.
- **`_suggest_from_diff_content` no longer fakes a scope using `basenames[0]`.** When the Python-source branch found no added top-level `def`/`class` AND `_dominant_directory` returned `None` (typical for cross-cutting commits touching files at the repo root + multiple subdirs), the helper used to return `refactor: update {first-alphabetical-filename}` — which is a misleading message because the first filename is not a scope. Now returns an empty string in that case so the orchestrator falls through to the generic backstop (`refactor: update sources`), which is at least honest about being generic.

### Changed
- **LM Studio / Ollama presets now auto-detect the loaded model.** Clicking the **LM Studio** or **Ollama** preset in Settings → "AI commit messages" hits the server's `/v1/models` endpoint and fills the Model field with the first loaded chat-tuned model (skipping embedding / rerank / whisper models that can't produce text). If the server isn't reachable, an amber hint appears telling the user to start the server first. The Anthropic preset gets a complementary blue hint pointing at console.anthropic.com.
- **Settings dialog is now scrollable and resizable.** The dialog grew tall enough that the new "AI commit messages" section was pushed below the visible screen area on 1080p displays with the taskbar showing. Content is now wrapped in a `Canvas + Frame` with a vertical scrollbar; mousewheel scrolling works on the body. Save / Cancel buttons stay anchored at the bottom (outside the scroll area) so they're always reachable. Dialog starts at 760×700, minsize 640×500, resizable on both axes.
- **Release Wizard release notes no longer inject placeholder text.** Earlier versions seeded the textarea with `Edit this summary, then click Publish.` as an inline prose hint. Some users shipped that text to GitHub by accident (the placeholder ended up in the published release body). The instruction now lives in a static label ABOVE the textarea where it can't leak into the release. `_render_release_notes` is also cleaner — `summary` parameter still exists for callers who want a real summary paragraph, but the default is empty.
- **`_suggest_commit_message` signature changed** from `(status_text)` to `(repo_path, status_text)`. The legacy file-pattern strategy moved to `_suggest_from_filenames` and is still callable; the orchestrator delegates to it when CHANGELOG/diff analysis yields nothing. Both internal call sites (initial dialog populate + 💡 Suggest button) updated to pass the project path.
- **Auto-commit-after-sync help text** now mentions the AI-generated alternative.

## [1.0.4] — 2026-05-22

Edit this summary, then click Publish.

### Changed
- update tokensave-manager.py
- update tokensave-manager.py
- update BASIC_INSTRUCTIONS.md, CHANGELOG.md + 2 more

### Docs
- update documentation

### Other
- Document the Merge button + remote-aware Delete Branch
- Fix SwitchBranchDialog.pick() calls — was duplicating 'parent' arg
- Add Merge button + remote-aware Delete Branch
- Make git operations project-aware in logs and dialog titles

### Added
- **📦 Release… button on the Git tab — one-button GitHub release wizard.** Right-click an existing release flow used to round-trip through Claude: read CHANGELOG, classify commits, write notes prose, build, zip, `gh release create`. The new wizard does it all locally — zero LLM tokens per release. Six stacked sections in a scrollable canvas:
  - **Version** — auto-detects the last tag via `git describe --tags --abbrev=0`, then offers Patch / Minor / Major radio buttons biased by commit content (any `feat:` → preselect Minor; any `BREAKING` / `!:` → preselect Major). Free-text override field for prerelease tags. First-release default is `v0.1.0`
  - **Title** — auto-filled from the highest-priority commit subject; editable
  - **Release notes** — auto-drafted from `<last_tag>..HEAD` via a new `_classify_commits_for_changelog` helper that groups by conventional-commit prefix (`feat:` → Added, `fix:` → Fixed, `chore:`/`refactor:`/`perf:`/`style:`/`test:`/`build:`/`ci:` → Changed, `docs:` → Docs, `!:` or `BREAKING CHANGE:` in body → Breaking, `auto:` → skipped). Scope is preserved as a parenthetical: `feat(ui): button` → `- (ui) button`. Subject-only regex so body text can't cause false matches. Unrecognised subjects land in `Other` so nothing is silently dropped. Editable textarea + 🔄 Regenerate + 📋 Copy to clipboard
  - **Build** — auto-detects `build.ps1` (preferred) or `build.bat`; checkbox to enable. `.bat` is invoked via `cmd.exe /c <name>` to avoid `WinError 193` on Windows
  - **Artefact preview** — read-only label showing dist/ contents and the resolved zip name (`<repo>-<tag>-windows.zip`). New `_zip_dist` helper uses `shutil.make_archive(root_dir=dist, base_dir=".")` for a flat archive (no `dist/` prefix inside the zip) and strips a trailing `.zip` from the base name so it never produces `foo.zip.zip`
  - **CHANGELOG.md sync** — checkbox (auto-disabled if no `## [Unreleased]` anchor exists). New `_patch_changelog` helper: idempotent insert-or-replace under the Unreleased anchor; atomic write via `.tmp` + `os.replace`; refuses to write rather than producing a malformed file if the anchor is missing
  - **Publish pipeline** — single threaded worker: build → zip → patch changelog → stage ONLY CHANGELOG.md (never blanket-commit so unrelated WIP can't slip into the release commit) → `git commit` → `git tag -a <tag> -m <title>` LOCALLY (so the local tree records the release; `gh release create` only tags remotely) → `git push origin HEAD --follow-tags` → `gh release create --notes-file <tmp>`. Each step short-circuits with a copy-pasteable recovery command on failure; the notes-file temp path is preserved on the final-step failure so the user can retry `gh release create` by hand without re-typing notes
  - **Pre-flight checks** — `cmd_git_release` refuses to open the wizard if the working tree is dirty in anything other than `CHANGELOG.md`, with a one-click handoff to the existing Git Commit dialog. The button is gated on `gh` being on PATH AND remote being set, separate from the other Git-tab buttons via a new `self._git_release_btns` list
  - Per-project — uses `self._git_path` like every other v1.0.3+ Git-tab button. Honours `_git_op_in_flight` so other buttons disable during the wizard run
- **⇄ Merge button on the Git tab** — merge another branch INTO the current one without dropping to the CLI. New `cmd_git_merge` opens a branch picker (reuses `SwitchBranchDialog.pick`), then runs `git merge --no-edit <source>` in a background worker. Two failure modes get dedicated dialogs: merge conflicts ("resolve in editor + Commit, or run `git merge --abort`") and dirty working tree ("commit or stash first"). Confirmation reads `"Merge 'X' INTO 'Y'?"` — direction is always explicit
- **Remote-aware Delete Branch** — after a successful local delete (both safe `-d` and force `-D` paths), the worker scans `git branch -r` for `origin/<branch>`. If present, a follow-up dialog offers to also run `git push origin --delete <branch>`. Same `_is_auth_error` detection as Push/Pull surfaces a helpful re-auth message on credential failures. Only prompts when the remote actually exists — never-pushed branches just delete silently
- **Project name in every git operation log line** — Push, Pull, Commit, Undo, Set Remote, New Branch, Switch Branch, Merge, Delete Branch (regular + force + remote), Open PR all prefix their log lines with `[<project-basename>] …`. Solves a real footgun: previously operations from the Git tab and operations from the Projects-tab right-click could target different projects depending on selection state, with no way to tell from the log which repo actually got the change. The active project also gets its own prominent "OPERATING ON" header label on the Git tab
- **Project name suffixed onto branch dialog titles** — `New Branch — MyProject`, `Switch Branch — MyProject`, `Set Remote — MyProject`, `Delete Branch — MyProject`, `Merge into master — MyProject`. The OS window title also identifies the target, so window managers / Alt-Tab disambiguate too. SwitchBranchDialog body now shows the project basename under its heading to match the existing NewBranchDialog pattern

### Fixed
- **`SwitchBranchDialog.pick()` calls were duplicating the `parent` argument** — both `cmd_git_delete_branch` (after the project-name-suffix UX change) and the brand-new `cmd_git_merge` called `pick(self, ..., parent=self)`. The signature is `pick(parent, title, branches, parent_widget=None)`, so the kwarg `parent=self` collided with the positional first arg → `TypeError: pick() got multiple values for argument 'parent'`. Tk's default behaviour for callback exceptions is to print to stderr only, so both buttons appeared inert ("nothing happens when clicked"). Fixed by switching to `parent_widget=self` in both call sites. The Delete Branch button had been silently broken since the UX-suffix commit earlier in the unreleased cycle; Merge was broken from its first commit. Architecture doc updated with a note about the calling convention to prevent recurrence

## [1.0.3] — 2026-05-22

Patch release. Fixes a UX dead-end in the Git tab and replaces the noisy auto-commit Stop hook with a smarter version that collapses consecutive auto-commits and writes useful messages.

### Fixed
- **Git-tab Commit button no longer requires a Projects-tab row selection.** The Commit button on the Git tab called `_selected_path()` (Projects-tab Treeview), so clicking it while the Git tab already had a project loaded produced "Click a project row first." Every other Git-tab button (Push, Pull, Undo, New Branch…) already used `self._git_path` — the project loaded on the tab — and Commit now does the same. Falls back to `_selected_path()` only when `_git_path` is unset (right-click from Projects tab before ever visiting the Git tab).

### Changed
- **Smart auto-commit Stop hook** — `_scaffold_git_hook` now writes a Python helper to `.claude/auto-commit-helper.py` alongside `settings.json`, and the Stop hook command becomes `python ".claude/auto-commit-helper.py"`. The helper:
  - **Amends** the previous commit if it was also an `auto:` commit, collapsing a long Claude Code session into a single commit instead of stacking identical entries
  - Builds a **useful message**: `auto: 3 files (14:22) - src/foo.py, docs/bar.md, +1 more`
  - Exits 0 silently when the working tree is already clean
- **Automatic upgrade of legacy Stop hooks on Retrofit** — `_scaffold_git_hook` detects the old `git add -A && … git commit -m "auto: Claude session"` oneliner and rewrites it to the new helper command in place, without adding a duplicate entry.

## [1.0.2] — 2026-05-21

Patch release. Fixes the **three-headed commit bug** that made the v1.0 / v1.0.1 Untrack-Ignored-Files workflow unusable in practice. Each fix peels off one layer of the onion:

### Fixed
- **`_do_git_commit` no longer runs `git reset` before staging.** The original reset was there to "clear the index so nothing already-staged sneaks into the commit" — but the cost was destroying intentional stagings like `git rm --cached` (the untracking operation the new 🧹 flow performs). The new behaviour: skip the reset entirely, use `git commit -m <msg> -- <paths>` (path-specific commit) so only the listed paths land in the new commit regardless of what else might be in the index. Standard git pattern; makes the index-reset unnecessary.
- **`_do_git_commit` no longer blindly `git add`s every selected file.** Files that are already fully staged (xy column 1 == `' '`, e.g. a `git rm --cached` queued by 🧹 Untrack Ignored Files appears as `D ` — staged-delete with no working-tree change) don't need re-adding. Worse: calling `git add` on a staged DELETION un-does the deletion and tries to re-add the file, which fails if the file matches a `.gitignore` rule — leading to the exact failure mode the 🧹 flow was supposed to prevent. Fix: GitCommitDialog now passes `(fname, xy)` tuples to the worker, and the worker only calls `git add` on files whose xy column 1 is not space.
- **Better error surfacing when `git add` does hit an ignored file** (e.g. user explicitly ticked an already-tracked-but-ignored file in the dialog). Previously the manager just logged `git add failed: <raw git output>`. Now it detects `ignored by one of your .gitignore files` in the output and shows a dedicated dialog with the affected paths listed and a one-line action to fix it.

### Added
- **Pre-flight tracked-but-ignored check in `_open_commit_dialog`.** Before opening the commit dialog, the manager runs `_find_tracked_but_ignored(path)`. If any matches exist, it shows a 3-way choice: **Yes** → open Untrack Ignored Files first (recommended); **No** → open the commit dialog anyway; **Cancel** → close, do nothing. This proactively breaks the cycle instead of waiting for the commit attempt to fail.

### Internal contract changes
- `GitCommitDialog` callback signature is now `callback(path, message, selected: list[tuple[str, str]])` — each tuple is `(fname, xy)`. The old `list[str]` form is still accepted for backward-compat (unknown XY is treated as needs-add, which is harmless because git add is idempotent).

## [1.0.1] — 2026-05-21

Patch release. Fixes the three bugs hit during the very first post-v1.0 retrofit attempt: a status-text parsing off-by-one that ate leading dots, a baseline `.gitignore` that over-corrected on `.codegraph/`, and the missing "Untrack ignored files" flow that left users stuck whenever a path was both tracked and ignored.

### Added
- **`🧹 Untrack Ignored Files…` right-click action** — finds every path that's currently tracked by git AND matches a pattern in the project's `.gitignore` (via `git ls-files -ci --exclude-standard`), and offers a checklist for selective untracking. New module helper `_find_tracked_but_ignored(path)`. New `UntrackIgnoredDialog` class. The untrack worker runs `git rm -r --cached -- <files>` so local copies are preserved (`--cached` keeps the working tree untouched). After the operation, the existing commit-after-change flow offers to commit the untracking as one atomic change
- **Auto-prompt for stale tracking after `.gitignore` save** — when `GitignoreDialog._on_save` writes new ignore patterns AND at least one already-tracked file now matches, a confirmation dialog appears: *"Your .gitignore now matches N files that are already tracked by git. Untrack them now?"*. Click Yes → `UntrackIgnoredDialog` opens pre-populated with the matched files. Solves the "I added .gitignore rule but git keeps showing it as modified" UX trap automatically

### Fixed
- **`git status --short` parsing dropped the leading character of the first file** when that file's status was working-tree-modified (`" M file"`). Three sites (`GitCommitDialog`, `_suggest_commit_message`, `_git_update_ui`) all called `status_text.strip().splitlines()` — the `.strip()` ate the leading space from the first line, shifting columns and making `line[3:]` skip one character too many. For dotfiles this dropped the leading dot, so `.claude/settings.json` displayed as `claude/settings.json` and `git add` failed with `pathspec did not match any files`. Fixed by removing the over-eager `.strip()` and splitting first, then filtering blanks per-line
- **`_BASELINE_GITIGNORE` no longer blanket-ignores `.codegraph/`** — CodeGraph deliberately wants `.codegraph/config.json` to be tracked (per-project indexing configuration, shared across machines). The previous blanket pattern over-corrected and would have made the config file invisible to git on every retrofitted project. Now only the SQLite DB files (`.codegraph/codegraph.db` and `codegraph.db-*` for the WAL/SHM journals) are ignored. Existing projects on the old baseline keep their old rule — use Manage .gitignore to update if needed; CodeGraph's own `.codegraph/.gitignore` already handles the DB correctly so this is mostly cosmetic

## [1.0.0] — 2026-05-21

First stable release. The manager has matured into a general Claude Code project manager rather than a tokensave-only wrapper: it now does full git management (branch / commit / push / pull / PR), structured .gitignore editing, and supports both tokensave and CodeGraph as equal-citizen code-graph backends. Drops the alpha tag.

### Added
- **CodeGraph support — equal citizen alongside tokensave** — right-click → 🧠 CodeGraph Init / Sync / Status / Remove Index works on any project. CodeGraph is an npm-distributed alternative MCP server (`@colbymchenry/codegraph`) that mirrors tokensave's role but auto-syncs via a native file watcher. The two use separate SQLite DBs (`.tokensave/tokensave.db` vs `.codegraph/codegraph.db`) and can both be active on the same project. New module-level helpers: `_detect_codegraph()` (Windows-`.cmd`-first PATH probe), `_detect_npm()`, `_is_codegraph_project()`, plus `CODEGRAPH_EXE` global. `find_projects()` adds `has_codegraph` to every project dict alongside `has_tokensave` and `has_git`. Projects tab gains a new **CG** column (✓/—) between Last Synced and Git. Settings dialog gains a CodeGraph section with **"Install via npm"** button (threaded subprocess.run with EPERM/EACCES surfacing, multi-line failures also pop up in a messagebox). `.codegraph/` added to `_BASELINE_GITIGNORE` (and therefore to the GitignoreDialog's `[+ Baseline]` template) so the SQLite DB is auto-excluded from git
- **`_is_local_git_repo(path)` helper** — strict local-only "is this folder a git repo root?" check using `os.path.exists` (supports git worktrees where `.git` is a flat pointer file, not a directory). Replaces `_is_git_repo` in `_offer_commit_after_change` to fix a latent bug: previously, a project nested inside a parent git repo would erroneously trigger commit prompts against the WRONG repository. This retroactively benefits the existing commit-prompt flows (Ensure .gitignore, Shadow Links, Scaffold, Retrofit)
- **Manage .gitignore dialog** (`GitignoreDialog`) — right-click → 📋 Manage .gitignore… opens a structured editor that replaces the old "Ensure .gitignore" quick-add. Features: scrollable list of current entries with per-row `×` button (real strikethrough font on removal, `↺` to undo); one-click template injection for 11 categories (Baseline, Python, Node.js, Rust, Java/JVM, .NET, VS Code, JetBrains, macOS, Windows, Nuitka — each as a push button, not a stateful checkbox, so reopens don't lie about category state); custom-entry field with dedup + sanity check (warns on whitespace-containing patterns); live "Pending changes" diff panel showing `+`/`−` lines; atomic file write via `_write_gitignore_lines()`; smart-clear conflict resolution (injecting a template containing a pattern marked for removal un-marks the removal instead of duplicating); blank-line and comment-line layout preservation. Save triggers the existing `_offer_commit_after_change` flow. The module-level `_ensure_gitignore` function stays for programmatic use by `cmd_git_init`
- **`_GITIGNORE_TEMPLATES`** module constant — category → list of patterns. Baseline derived programmatically from `_BASELINE_GITIGNORE` so the two lists stay in sync automatically
- **At-a-glance Git status column on the Projects tab** — new `Git` column shows ✓ (clean) / ● (uncommitted changes) / ↑N (ahead) / ↓N (behind) / ●↑N (mixed) / — (not a git repo). Row colour changes too: yellow for dirty, sky for ahead, red for behind, peach for mixed. Computed asynchronously in a background thread on refresh — caches per project via `.git/index` mtime so unchanged projects skip the subprocess call entirely. New module helpers `_parse_git_status_v2()` and `_format_git_status_cell()` are pure functions
- **Commit prompt after destructive manager actions** — `Ensure .gitignore`, `Shadow Links`, `Scaffold`, and `Retrofit` now offer to commit the resulting changes immediately. New helper `_offer_commit_after_change(path, summary_label)` checks `_is_git_repo` + `git status --porcelain`, then either shows a yes/no commit prompt or logs "Nothing to commit" / "Working tree left dirty — commit when you're ready". Reuses the existing `GitCommitDialog` via a new `_open_commit_dialog(path)` helper (factored out of `cmd_git_commit`)
- **Git button safety — disable during operations** — `_git_op_in_flight` flag + `_git_begin_op()`/`_git_end_op()` helpers prevent double-clicks. All 8 git operations (push, pull, commit, undo, new branch, switch branch, delete branch, set remote) now grey out every Git tab button while an op is in flight and re-enable them when the worker finishes (success OR failure). `_git_update_ui()` honours the flag so even an incidental refresh during an op can't accidentally re-enable buttons
- **Git tab** — full Git management panel (Push, Pull, Commit, Undo Last Commit, New Branch, Switch Branch, Delete Branch, Set Remote, Refresh) with live status, recent commits, working-tree listbox, and colour-coded diff viewer (capped at 2000 lines)
- **GitHub Setup wizard** (`GitHubSetupDialog`) — step-by-step first-time GitHub onboarding: git identity, sign-in/create-account, create repo, set remote, first push, plus a Releases section that shells out to `gh release create` when the GitHub CLI is on PATH
- **Per-file staging in `GitCommitDialog`** — every changed file appears as a checkbox row with a colour-coded status badge (M/A/D/R/?/!) and a plain-English description; Select All / Select None / Modified Only quick-pick buttons; commit only staged files via `git reset` → `git add -- <files>` → `git commit`
- **Conventional-commit message suggestions** — `_suggest_commit_message()` parses the current working-tree status (or just the *selected* subset) into messages like `"docs: update GITHUB_GUIDE.md"`, `"feat: add newfile.py"`, `"chore: update 5 files"`; the **💡 Suggest** button in `GitCommitDialog` and `GitHubSetupDialog` regenerates on demand
- **Auto-commit-after-sync amend behaviour** — when the previous commit was already `"chore: tokensave sync"`, the next auto-commit uses `git commit --amend --no-edit` instead of stacking duplicate commits
- **Project categories + sub-categories** — Treeview groups projects by category (root label or per-project override via right-click → 📁 Assign Category…); `project_categories` config key persists overrides
- **`_Tooltip` class** — hover tooltips on every Git tab button (650 ms delay, plain-English explanations of what each git command does)
- **Help tab expansion** — new sections: Project Categories, Git: What & Why, Git: Daily Workflow, Git Tab Buttons, GitHub Setup
- **`docs/GITHUB_GUIDE.md`** — comprehensive beginner GitHub guide (~300 lines): plain-English concepts, first-time setup, daily workflow, branches, releases, common problems, glossary
- **`manager-config.example.json`** — clean template config with placeholder paths for new users cloning the repo
- **Git Bash auto-detection** — `_detect_git()` finds `git.exe` via `shutil.which` then common Windows install paths (`C:\Program Files\Git\cmd\`, etc.); `GIT_EXE` module variable is used by every git subprocess call; Settings dialog gains a Git exe row with Browse / Auto-detect / verify (`git --version`)
- **`_BASELINE_GITIGNORE`** now also excludes `.claude/` and `logs/` in addition to Python cache, Nuitka output, `.tokensave/`, and venvs
- **`_ensure_gitignore()` + right-click → 📋 Ensure .gitignore** — non-destructive merge of baseline entries into any project's `.gitignore`; reads existing content, appends only the lines that are missing, works on projects with or without a git repo; safe to run repeatedly
- **Build pipeline docs staging** — `build.ps1` now copies `docs\GITHUB_GUIDE.md`, `docs\ARCHITECTURE.md`, and `docs\ARCHITECTURE_TOKENSAVE.md` into `dist\docs\`, plus `manager-config.example.json` and `CHANGELOG.md` next to the exes

### Changed
- `GitCommitDialog` callback signature changed from `(path, message, stage_all: bool)` to `(path, message, selected_files: list[str])` — `_do_git_commit()` updated to stage exactly the picked files and ignore everything else
- `cmd_git_log` no longer opens a popup dialog; it switches to the Git tab and refreshes (consolidates all git info in one place)
- Git tab action buttons packed **before** the diff viewer so they're always visible even when the window is short
- `build.ps1` cleaned to pure ASCII (em dashes and box-drawing chars caused PowerShell parser errors on some systems)

### Fixed
- `GitHubSetupDialog` rendering — `KeyError: 'subtext0'` (palette key is `subtext`, not `subtext0`) was silently breaking the wizard build; only Step 1 ever appeared. Fixed all four occurrences plus added a diagnostic try/except so any future widget-creation error surfaces as a messagebox instead of being swallowed by `pythonw`
- `.gitignore` now also covers `manager-config.json` (personal absolute paths) and `.claude/` (Claude Code local session settings) — prevents leaking machine-specific data when pushing to GitHub

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
