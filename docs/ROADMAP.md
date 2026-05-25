# TokenSave Manager — Roadmap

This document tracks planned features and architectural direction. Items here are **proposals**, not commitments — each ships only after its design proves itself in earlier stages.

Status legend:
- ✅ Done — landed in a release
- 🟡 In progress — actively being built
- 🔮 Planned — design locked, not started
- 💭 Considering — not yet committed

---

## Major direction: local AI assistant integration

The manager is growing into a **propose-only AI assistant** for project maintenance. AI suggestions never auto-apply — every change waits for the user to click Apply.

### Inference engine: Ollama (recommended) or LM Studio

The manager talks to local AI through an OpenAI-compatible HTTP API. Settings → "AI commit messages" lets you point at:

- **Ollama** (recommended for scripted / agentic workflows) — runs as a Windows service, exposes `http://localhost:11434/v1`, has a built-in model package manager
- **LM Studio** — friendlier GUI for interactive use, exposes `http://localhost:1234/v1`
- **Anthropic Claude** (cloud API) — fastest, highest quality, ~$0.0005/commit
- **Any OpenAI-compatible server** — vLLM, llama.cpp's `llama-server`, LocalAI, etc.

### Model recommendations

For agentic / custodial work, you want **non-reasoning instruction-tuned** models with native tool calling:

| Model | Pull command | Best for |
|---|---|---|
| `qwen2.5-coder:14b` | `ollama pull qwen2.5-coder:14b` | Code review, diff analysis, refactor suggestions |
| `qwen2.5:14b` | `ollama pull qwen2.5:14b` | General Q&A, CHANGELOG drafting, tool orchestration |
| `mistral-nemo:12b` | `ollama pull mistral-nemo:12b` | Whole-project Q&A (128k context window) |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | Fast + reliable tool calling |
| Anthropic `claude-haiku-4-5` | (Anthropic API key) | Cloud — fastest, best quality, tiny cost |

**Avoid reasoning models** (Qwen 3.5, DeepSeek-R1, anything with "reasoning" in the name) for this use case — they spend 30-60+ seconds "thinking" before producing output, which often exceeds the manager's per-request timeout.

---

## Staged AI features

### ✅ Stage 0 — Smart commit message generation
*Status: shipped*

The 💡 Suggest button in the Git Commit dialog runs a multi-strategy orchestrator: AI call (if enabled) → CHANGELOG bullet parsing → diff content analysis → file-name fallback. All results pass a sanitiser that enforces conventional-commit format. Async with a spinner so the GUI never freezes. See `CHANGELOG.md` for the full feature list.

### ✅ Stage 1 — AI Code Review panel
*Status: shipped*

Right-click any project → **🔍 AI Code Review…** opens a split-pane dialog: top pane shows `git diff HEAD` with green/red colour-coding, bottom pane streams an AI-generated structured review (⚠ High / ⚡ Medium / 💡 Low / ℹ Observations) with severity-coloured section headers. Async with **token-by-token streaming** via byte-aligned SSE parsing — visible progress instead of a long opaque wait. Worker-side token batching (~50 ms / 8 tokens) prevents Tk event-loop saturation on fast local models. Stop / Regenerate / Copy buttons. Section-header colour tags applied in a final pass after streaming ends.

Pure read-only: one `_call_llm` call with a locked system prompt. No tools, no file writes, no autonomy concerns. `AICodeReviewDialog._SYSTEM_PROMPT` holds the prompt as a class-level constant — match this pattern for any future one-shot LLM dialog.

### ✅ Stage 2 — Project Q&A chat with tool calling
*Status: shipped*

New manager tab: **🤖 Ask**. Chat interface where the AI calls read-only tools (`read_file`, `list_directory`, `git_log`, `git_diff`, `tokensave_search`, `tokensave_context`) to answer questions about the active project. Sample questions:

- "What does this project do?"
- "Where is the commit-message generator?"
- "Why is `_pending_diff` using `HEAD` instead of `--cached`?"
- "Show me everything that calls `_classify_commits_for_changelog`."

All tools are read-only — the agent CANNOT write files, run commits, or modify config. The agent loop is bounded (default 8 iterations) and has a cumulative context budget (~40 000 chars across all tool outputs) so repeated 50 KB reads can't saturate small local-model context windows. Lives in `src/agent.py` (`LocalAgent`) and `src/agent_tools.py` (`ToolSpec` registry).

Provider support: Ollama / OpenAI / OpenAI-compatible (LM Studio, vLLM, etc.) all do tool calling natively. Anthropic falls back to a one-shot completion without tools — adding native Anthropic tool-use is a known follow-up.

### 🟡 Stage 3 — CHANGELOG drafter
*Status: write-tool plumbing shipped (Roadmap-2 Phase 1); CHANGELOG-specific drafter still TBD*

Right-click → **📝 Draft CHANGELOG entry…**. Agent reads commits since the last release tag, classifies them, drafts CHANGELOG bullets. A ProposalDialog presents the old-vs-new diff with **Apply / Reject / Edit then Apply** buttons. First feature with a write tool, but every write goes through the same approval gate.

**Roadmap-2 progress:** the write-tool path is fully shipped. `agent_tools.py:_tool_write_file` builds a `WriteProposal` (with race-safe `original_hash` + symlink-safe path containment + side-effect surfacing for `dirs_to_create`); the agent dispatches it through `LocalAgent.on_write_proposal` → `ProposalBridge` → `ProposalDialog`; on accept, `_atomic_write_file` re-checks the hash and writes via `.tmp` + `os.replace`. The CHANGELOG-specific drafter (commit classifier + bullet renderer + right-click entry) is the remaining work — straightforward now that the write path is proven.

### 🔮 Stage 4 — Refactor scout
*Status: planned*

Right-click → **🔬 Refactor scout…**. Agent calls tokensave's analytics tools (`tokensave_dead_code`, `tokensave_god_class`, `tokensave_circular`, etc.) and produces a structured report with plain-English explanations per finding. Each finding has Investigate / Ignore actions. Findings marked Ignore persist in `manager-config.json` and won't reappear.

### 💭 Stage 5 — Limited autonomous mode
*Status: considering, revisit after Stages 1-4 ship*

Adds opt-in autonomous execution for specific tool categories (e.g. "auto-write CHANGELOG without asking"). Per-tool allow/deny configuration in Settings. Session-level kill switch. Audit log of every autonomous action. **Not committed** — only revisited after lived experience with propose-only confirms the workflows are valuable.

---

### 🟡 Stage 6 — Workflow accelerators
*Status: PR draft + Open-on-GitHub + pre-commit hook shipped (Roadmap-1/2); release narrative still planned*

Bundles three commit/release workflow features that share the same "AI drafts → user approves → manager applies" pattern:

- **PR description generator** ✅ *(shipped Roadmap-1)* — `Draft PR…` button in the Git tab. Primary click runs the Claude Code CLI if configured (opens a detached terminal — `helpers/claude_cli.py`), otherwise calls the API path (`helpers/pr_draft.py` → in-app dialog with Copy button). Right-click / Shift+click pops a menu to explicitly override. **Roadmap-2 P5a** added an "🔗 Open PR on GitHub" button in the same dialog: writes the body to a temp `.md` and spawns `gh pr create --web --body-file <tmp>` so the GitHub New-PR page opens pre-filled.
- **Pre-commit AI review hook** ✅ *(shipped Roadmap-2 P5b)* — Right-click → 🔍 Pre-commit AI Review hook…. Installs a git pre-commit hook (POSIX shell script + Python reviewer at `src/precommit_review.py`) that reads `git diff --cached` and runs an AI code review. Three-value backend choice via `precommit_review_backend` in `manager-config.json`: `"auto"` (prefer Claude Code subscription via `claude --print`, fall back to configured LLM), `"claude_cli"` (force CC), `"llm"` (force per-token provider). Three-value severity threshold (`precommit_severity_threshold`: `"none"` warn-only default / `"medium"` / `"high"`) decides whether findings block. Fail-open invariant: every error path exits 0 with a stderr notice. Override always available via `git commit --no-verify`. Stop-hook variant deliberately deferred — see "Pre-commit AI review — Stop-hook variant" entry in the Roadmap-3 backlog.
- **Release-notes narrative writer** *(planned)* — Extends Release Wizard with an AI-generated summary paragraph above the bullet list ("This release focuses on X and Y…"). Drafts inside the existing wizard textarea so the user can edit before publishing. `helpers/changelog_patch.py:insert_changelog_release` (atomic idempotent `## [Unreleased]` patcher) is now wired into ReleaseWizard's publish path (Roadmap-2 P2), so the narrative just needs to fit into the existing notes string before insertion.

### 💭 Stage 7 — Quality assurance suite
*Status: considering*

Audit-focused features that consume the manager's full tokensave catalogue:

- **Test gap analyzer** — `tokensave_test_map` + `tokensave_doc_coverage` → AI proposes specific test cases for untested public symbols. Outputs as a checklist; "Generate test stub" button per item drafts a pytest skeleton via ProposalDialog.
- **Documentation freshness checker** — AI compares each `.md` doc against current code (via tokensave_search on symbols mentioned in docs). Flags stale references, missing entries, and code-doc mismatches.
- **Dependency / license audit** — Reads `requirements.txt` / `pyproject.toml` / `package.json`. AI summarises each dependency's purpose, license, and any obvious version-bump risks. Local-model-friendly (no cloud calls needed — the AI is just summarising local file contents).
- **Secret leak scanner** — Runs on staged diff before commit (or as a standalone scan over the working tree). AI flags likely API keys, tokens, passwords, private URLs. Pairs naturally with the pre-commit hook from Stage 6.

### 💭 Stage 8 — Knowledge management
*Status: considering, longest payoff horizon*

Turns the manager into a project-memory tool:

- **Decision log** — Right-click → 📓 Record a decision… AI prompts you for context ("what choice did you make? what were the alternatives? why?"), drafts a `docs/DECISIONS.md` entry, ProposalDialog confirms. Future agents read this file as context for "why is the code like this" questions.
- **FAQ extractor** — AI reads commit messages + PR descriptions + CHANGELOG + issue templates, extracts recurring patterns into a `docs/FAQ.md`. Re-run periodically to keep it fresh.
- **Cross-project pattern library** — Manager learns recurring code patterns across ALL your indexed projects ("you keep writing variants of this XML parser"). Suggests extracting shared utilities. Cross-project tokensave_similar.

---

## Code-health backlog

Round 4 split the monolith into subpackages (equality 0.16 → 0.57, quality 5,689 → 7,003). Round 5 extracted 9 sub-controllers from `ProjectsTabController` + 2 from `App`. Roadmap-1 added 6 new features + cleared the pyflakes baseline to zero. Roadmap-2 added the Doctor monolith audit (file/method/class/complexity caps surfaced automatically) and shipped Phases 0, 1, 2.

### Roadmap-2 scope — all shipped (2026-05-24)

- ✅ **Phase 0 — Anti-monolith governance + Doctor audit.** `BASIC_INSTRUCTIONS.md` rewritten with rules A–H (caps, doc discipline, tokensave-first, guard-rails, surface-don't-decide, refactor budget, metric hierarchy, governance hygiene). `DoctorController` gained a monolith-audit pass (AST-walks every `*.py`; non-Python line-count check; hybrid layout-method carve-out; top-of-file `# anti-monolith: exempt — <reason>` opt-out). Ask-tab system prompt steers code-health questions to `tokensave_god_class` / `tokensave_complexity` / `tokensave_largest`.
- ✅ **Phase 1 — `write_file` tool + `ProposalDialog` bridge.** Lifts the read-only-agent invariant. `ToolSpec` gains `proposal_builder` + `post_accept`. Race-safe via SHA-256 hash recheck at write time. Symlink-safe `_under_project` containment. `ProposalBridge` coordinates worker ↔ Tk main with `threading.Event`, lock-guarded `_resolve`, post-timeout expired-state UX (no auto-close), app-shutdown cancellation. Inline test harness covers 4 race-safety paths.
- ✅ **Phase 2 — `ReleaseWizard` → `changelog_patch` wiring + daemon control UI.** `insert_changelog_release` extended with idempotent-replace + boundary precision (next `^## \[` line stops the replacement). `helpers/release.py:_patch_changelog` deleted (single canonical patcher). Daemon footer indicator gained a right-click menu: Start / Stop / Install autostart / Disable autostart, with async + status-confirm pattern. Fixed pre-existing `toggle_daemon` bug (was passing nonexistent `--start` flag).
- ✅ **Phase 3 — `ReleaseWizardDialog._build_ui` split per wizard step.** 186-line `_build_ui` → 8-line orchestrator + 8 named section builders (header / version / title / notes / build / artefact / changelog / publish). Pure layout refactor.
- ✅ **Phase 4 — `BranchManagementController` extracted from `GitTabController`.** 13 methods moved to `src/controllers/branch_mgmt_ctrl.py` via callback injection. `cmd_git_merge` (complexity 14 inline) decomposed into 5 helpers. `GitTabController` 50 → 38 direct methods (under 40 cap).
- ✅ **Phase 4.5 — Dialog `__init__` split sweep.** Applied Phase 3's per-section split pattern to 6 dialog constructors: `merge_pr.py`, `untrack_ignored.py`, `ai_code_review.py` (`__init__` + `_start_review` behaviour split), `ollama_model_mgr.py`, `github_setup.py`, `gitignore.py`. One commit per dialog. Project audit dropped from 49 violations (start of P2) → 39.
- ✅ **Phase 5a — "Open PR on GitHub" button.** Added to the PR draft dialog next to Copy. Writes body to temp `.md`, spawns `gh pr create --web --body-file <tmp>` with `cwd=<project>`. `--body-file` (not `--body "..."`) sidesteps Windows command-line length limits and quote escaping. Button greyed + tooltipped when `gh` isn't on PATH.
- ✅ **Phase 5b — Pre-commit AI review hook (warn-only).** Right-click → 🔍 Pre-commit AI Review hook… installs a git pre-commit hook that runs `git diff --cached` through an AI code review. Three-value backend choice (`precommit_review_backend`: `"auto"` / `"claude_cli"` / `"llm"`). Fail-open invariant on every error path. POSIX shell script with hard-coded `python.exe` + reviewer paths; sentinel marker for install/remove symmetry; refuses to touch a hook the user installed themselves. **Stop-hook variant explicitly deferred** — see the "Pre-commit AI review — Stop-hook variant" entry below for rationale.

- 🔮 **W2 — `_render_block` complex branch tangle (dialogs/mcp_config.py:154, 109 lines)**. 11 branches handling different MCP config row states. Refactor into a dispatch table keyed on row classification. Worth doing before extending MCP support to additional editors. Deferred from Roadmap-2 scope; revisit in Roadmap-3.

### Roadmap-3 code-health backlog (post-Roadmap-2 Doctor snapshot)

Doctor's full project audit surfaced 49 violations across 22 files at the start of Roadmap-2. After Phases 3 + 4 + 4.5 close out, ~30 violations will remain — all genuine branching logic that needs per-helper refactoring, NOT template-able sweeps. Treat each cluster as an independent Roadmap-3 work item, not all-at-once.

**Helper complexity (highest value — central to AI feature stack):**
- 🔮 `helpers/commit_messages.py` — 5 functions over cap (`_suggest_from_diff_content`=21, `_extract_changelog_additions`=18, `_message_from_changelog`=18, `_suggest_from_filenames`=18, `_sanitize_commit_message`=13). Multi-strategy orchestrator chain — natural shape is each `_strat_*` function in its own helper module.
- 🔮 `helpers/llm.py` — 4 functions over cap (`_call_openai_compat`=20, `_call_llm`=15, `_call_anthropic`=14, `_iter_json_lines`=11). Provider-dispatch + SSE-parsing. Cleanest split is per-provider sub-module (`llm/anthropic.py`, `llm/openai_compat.py`, `llm/sse.py`).

**Helper complexity (lower-stakes — promote when actually touching the affected code):**
- 🔮 `helpers/git.py` `_format_git_status_cell`=16
- 🔮 `helpers/project_discovery.py` `find_projects`=16
- 🔮 `helpers/scaffold.py` `_scaffold_git_hook`=14
- 🔮 `helpers/gitignore.py` `_ensure_gitignore`=13
- 🔮 `helpers/release.py` `_classify_commits_for_changelog`=12, `_suggest_bump_kind`=11
- 🔮 `helpers/shadow_links.py` `remove_shadow_links`=11

**Controllers / dialogs (behaviour complexity, not layout — Phase 4.5 doesn't cover these):**
- 🔮 `controllers/projects_tab.py` class 44 methods (over 40 even after Round 5's 9 extractions) + `__init__` 109 lines + `rebuild_tree` complexity 13. Diminishing returns on further extraction; consider grandfathering via `doctor_skip_monolith_paths` if Phase 4 makes it the heaviest controller and no clean further split exists.
- 🔮 `controllers/snippets.py` `_on_snippet_saved`=11
- 🔮 `dialogs/mcp_config.py` `_apply`=11
- 🔮 `dialogs/ollama_model_mgr.py` `_fetch_context_length`=12, `_worker`=12
- 🔮 `dialogs/release_wizard.py` `_refresh_artefact_preview`=15
- 🔮 `dialogs/gitignore.py` `_on_save`=12
- 🔮 `dialogs/ai_code_review.py` `_render_review`=11

**Agent / app pre-existing (called out in Roadmap-2 plan as do-not-touch within Phase 1):**
- 🔮 `agent.py` `run()`=18, `_rescue_tool_call_from_content`=19/101 lines, `_run_anthropic_oneshot`=20
- 🔮 `agent_tools.py` `_suggest_paths_for_missing_file`=11, `_read_file`=13, `_runner`=17
- 🔮 `app.py` `_check_config`=14, `worker`=14

### 💭 Genuine dead-code cleanup in `src/agent_tools.py` (10-minute pass)
Three functions look genuinely unused after Phase E (grep-verified, NOT Tk-callback false positives):
- `_read_file_range` at `agent_tools.py:218` — superseded by inline handling in the `read_file` tool handler. **Verify** no caller remains, then delete.
- `_suggest_paths_for_missing_file` at `agent_tools.py:249` — docstring claims it's called from `read_file`'s error path; verify the wiring still routes through it.
- `_slim_tokensave_context` at `agent_tools.py:452` — verify the `tokensave_context` tool handler still calls it.

Plus two genuine unused imports in the same file: `dataclasses.dataclass` (L26) and `typing.Callable` (L27) — leftover from the legacy ToolSpec class.

### 🔮 Stage 2 (Ask-tab agent) usability refinements

Surfaced via Roadmap-2 hands-on testing with `ollama` + `qwen2.5-coder:14b`. The agent works but quality is rough around the edges. Backlogged rather than fixed inline (the agent dispatch is a hot path; refinements deserve their own thought-out commit set).

- **Tool-call deduplication.** Observed: model called `git_log({})` immediately followed by `git_log({"n": 20})` — same result, wasted iteration (default `n` is 20). The dispatcher could keep a per-run cache of `(tool_name, args_hash) → result` and return a cached result with `[cached from earlier in this run]` prefix, OR push back to the model with a "you already ran this — pick a different angle" hint. Low-risk; doesn't change tool semantics.
- **Final-message streaming.** Currently the assistant's final answer arrives as one chunk at the end of the loop. Streaming the final-turn tokens (per the AGENT_ARCHITECTURE doc note) would make long answers feel responsive instead of opaque. Non-streaming the tool-call turns is correct — they're structured JSON.
- **Loop-stall surfacing.** When the agent hits the iteration cap OR an unrecoverable HTTP error, the chat just shows a generic "✗  Error" indicator. The detailed reason already lives in `LocalAgent._last_error` — surface it in the chat (with the same one-line summary the `on_error` callback already gets).
- **Single-source-of-truth for tool defaults in descriptions.** The `git_log` description says "Default 20" but the parameters JSON Schema doesn't set a default, so the model can't reliably pick "the default" without naming `n` explicitly. Either set `"default": 20` in the schema (preferred — lets the model omit `n`) or strip the "Default 20" from the prose so it doesn't mislead.

### 💭 Pre-commit AI review — Claude Code Stop-hook variant

When Roadmap-2 Phase 5b shipped the pre-commit AI review hook, we considered three variants: git pre-commit hook only / Claude Code Stop hook only / both. We went with **git pre-commit hook only** after weighing trade-offs:

- **Git pre-commit hook** fires at the single moment that matters (a commit is about to land), reviews exactly `git diff --cached` (no ambiguity about what's being reviewed), produces one signal per commit, and can actually block. Works for human changes too, not just Claude-driven ones.
- **Claude Code Stop hook** fires on EVERY Claude turn — including tiny ones and non-code ones — doesn't naturally know which files Claude touched vs. what was already dirty, can't block git operations anyway, and is the fastest path to "user disables the hook after a week of false positives."

The git hook covers ~95% of the safety value with cleaner semantics. Revisit only if real-world usage uncovers a specific gap the pre-commit hook can't fill — e.g. "I want a heads-up the moment Claude finishes a risky operation, before I even think about staging." Until then, ship the simpler thing.

### 🔮 Stage 0 (commit-message generation) quality refinements

Surfaced via Roadmap-2 hands-on testing — clicking Suggest on a small docs-only diff produced the generic `docs: update documentation`, never invoking the LLM.

- **`min_diff_lines` gate is too aggressive for small but meaningful changes.** Default 30 means a focused 5-line bug fix or a 10-line docs improvement falls through to the filename heuristic. Two paths: (a) lower the default to 10, accepting more LLM calls; (b) keep the line gate but add an opt-in path (e.g. shift-click Suggest = "use LLM regardless of size"). (b) is safer.
- **File-name fallback is too generic for `docs/upstream-issues/`-style paths.** The current heuristic produces "docs: update documentation" when ALL changed files are markdown — but the actual subject (which issue draft was edited) is right there in the filename. A small `_SCOPE_PATTERNS` addition keyed off `docs/upstream-issues/<name>.md` → `docs(upstream): <name>` would catch this class.
- **No surfacing of WHICH strategy fired.** Tooltip on the Suggest button or one-liner status ("via LLM" / "via CHANGELOG" / "via diff" / "via filename") would let the user know whether the generic message is the best the chain could do or whether the LLM was silently bypassed.

### 🔮 Skill awareness / prompting

Same class of problem the manager exists to solve: Claude Code skills (e.g. `consolidate-memory`, `verify`, `code-review`, `security-review`, the custom plugins) are powerful but only useful if the user remembers they exist at the right moment. After Roadmap-2 the user noted: *"I forgot consolidate-memory existed until Claude suggested it before a new chat."* That's the failure mode — by-memory discovery.

**Design space (pick when ready; not committing yet):**

- **Skills catalog in the Reference tab.** Read `~/.claude/skills/` + `~/.claude/plugins/*/skills/` at startup; list each skill name + description (from frontmatter) alongside the existing CLI cheatsheet. Pure read-only surface; zero notification cost. Cheapest first step.

- **Workflow-moment prompts.** Context-aware nudges at specific Git-tab actions:
  - Before clicking Release… → "Have you updated CHANGELOG.md? Skill `consolidate-memory` can refresh project memory before the release."
  - After a Roadmap-N branch merges → "Consider running `consolidate-memory` to capture this round's architectural deltas before starting Roadmap-N+1."
  - Before opening a PR with >500 lines → "Skill `code-review` would surface bugs first."
  - Manager startup or `Doctor` button → "Skill `tech-debt` would scan this project for refactor candidates."

  Risk: notification fatigue. Mitigation: per-prompt opt-out checkboxes saved in `cfg.raw["skill_prompt_suppressed"]`.

- **Last-used tracking.** Per skill, per project, write a small timestamp to `cfg.raw["skill_last_used"]`. Surface "It's been 30 days since `consolidate-memory` ran on this project" as a passive footer indicator (like the daemon dot). Non-modal; user-driven.

- **Right-click → "Suggest skill for this project"** as an explicit pull. Manager scans recent git activity / file changes / current branch and proposes 1–3 relevant skills with a one-line rationale each. User picks (or dismisses). Pairs naturally with the existing Doctor right-click action.

- **Skill launcher integration.** Add a "Run with Claude Code" button next to each catalog entry that spawns `claude --print` (for non-interactive skills) or `claude` in a detached terminal (for interactive ones — re-uses `helpers/claude_cli.spawn_claude_cli` from R1). Most useful for skills that operate on the current project directory.

- **Workflow bundles.** Curate "Pre-PR", "Pre-release", "End-of-roadmap" bundles that chain multiple skills (e.g. Pre-PR = lint → tests → `code-review` → draft PR → open PR via R2-P5a). Bigger UX commitment but maps directly to the user's "smoother projects" goal.

**Trigger:** when the user finds themselves re-discovering a skill the manager should have surfaced. Each rediscovery makes the case for one of the options above more concrete. Catalog tab is the safest first step; the prompting variants are higher value but higher fatigue risk.

### 🔮 Draft PR refinements (surfaced via Roadmap-2 post-ship dogfooding)

Surfaced when the user clicked Draft PR on the Roadmap-2 branch at PR-creation time. Three independent refinements:

- **CLI instruction prompt is wrong for PR-creation use.** The Roadmap-1 Claude-CLI hand-off sends `"Review my uncommitted git changes and write a PR description to PR_DRAFT.md in the project root."` — but at PR time the working tree is usually clean (you commit, push, then click Draft PR). Claude correctly reports "no uncommitted changes" instead of drafting anything useful. The instruction should be `git log master..HEAD` + `git diff master..HEAD` based (commits between current branch and the upstream / default branch). The base-branch detection logic from `_call_anthropic`'s commit-message path may be reusable. Located in the CLI-path callsite in `src/controllers/git_tab.py` (or wherever the instruction string lives).

- **Draft PR backend preference setting** (analogous to `precommit_review_backend` from P5b). Currently the routing is hard-coded: CLI if `claude_cli_exe` set, else API. A user with both Claude Code AND Ollama configured has no way to say "I want to default to Ollama for PR drafts even though I have Claude CLI installed." Add `draft_pr_backend` in `manager-config.json` with values `"auto"` (current behaviour: prefer CLI), `"claude_cli"`, `"llm"`. Right-click menu still gives per-click override.

- **Phase 5a "Open PR on GitHub" button needs end-to-end verification** in actual use. Untested by the user during Roadmap-2 because (a) left-click defaults to the CLI hand-off when `claude_cli_exe` is set so the dialog never opens, and (b) the API path would burn Anthropic credits which the user wanted to avoid. Verification path: confirm the API route via Ollama (`commit_message_llm` provider) produces text in the dialog, then verify the new button correctly writes a temp file + spawns `gh pr create --web --body-file`. Should "just work" given Phase 5a's design but isn't proven against an Ollama backend yet.

- **Investigate Claude Code hook path-quoting errors.** Roadmap-2 dogfooding surfaced repeated `UserPromptSubmit hook error: Failed with non-blocking status code: /usr/bin/bash: line 1: D:/Claude: No such file or directory` and matching `Stop hook error` messages in the user's Claude Code terminal. Cause: a hook entry somewhere in `~/.claude/settings.json` or project `.claude/settings.json` references an unquoted Windows path containing spaces (`D:\Claude Co worker\...`), so bash splits at the first space and tries to execute `D:/Claude`. Candidates: the Roadmap-1 `_scaffold_git_hook` auto-commit Stop hook, or some manually-added entry. Fix: audit the hook commands and ensure paths-with-spaces are double-quoted. Not blocking but noisy.

### 💭 File the two upstream tokensave issues
Drafts in `docs/upstream-issues/`:
- `tokensave-health-details.md` — request `tokensave_health details=true` to return the sub-score breakdown in one call
- `tokensave-redundancy-tool.md` — request `tokensave_redundancy` AST-level functional-duplication detector

Both have "strip any proprietary code before filing" notes per `~/.claude/CLAUDE.md`. The redundancy ask would let `tokensave_health` surface a real Redundancy sub-score (currently impossible — there's no project-wide aggregate primitive).

---

## Open ideas (no stage yet)

Smaller / exploratory ideas that don't justify a full stage:

- **Local-only mode flag** — refuses Anthropic/OpenAI cloud providers; only allows Ollama/LM Studio. Hardware-enforced privacy guarantee.
- **Diff sanitizer** — strips API keys, tokens, private URLs from any text sent to cloud AI providers (silent for local since data never leaves).
- **Natural-language tokensave query** — chat box that turns "show me everything that calls foo and was modified this week" into composed tokensave queries.
- **AI-tagged project categories** — auto-assign project categories based on README/code analysis when the user hasn't set one.
- **Commit message dialog AI explanation** — tooltip / expandable panel showing what the AI based its suggestion on (which CHANGELOG bullets matched, which diff content was emphasised).
- **Multilingual support** — generate commit messages / CHANGELOG / docs in a user-preferred language.

Promote any of these to "Considering" / "Planned" when you find yourself needing them more than a few times.

---

## Architectural rules (locked, apply to all stages)

These principles govern every AI feature added to the manager:

1. **Propose-only by default.** Every write action goes through `ProposalDialog` with a visible diff and explicit Apply / Reject. Stage 5 may add narrow opt-in autonomy, but that's a deliberate choice the user makes, never the default.

2. **No new heavy dependencies.** The agent loop is ~150 lines of stdlib Python. NO LangChain, NO LlamaIndex, NO OpenAI-Agents-SDK. The manager already speaks OpenAI-compatible HTTP via `urllib.request`; we extend that, we don't replace it.

3. **Tools live in a single registry.** All tools the agent can call are defined in `src/agent_tools.py` with a `ToolSpec` dataclass (name, description, JSON Schema for arguments, handler function, `is_write` flag). Adding a new tool means adding one entry — no scattered tool definitions.

4. **Write tools are explicit.** `ToolSpec(is_write=True)` triggers the ProposalDialog gate. Read tools execute directly. Mis-labelling a write tool as read is a security defect.

5. **No external retrieval / RAG / embeddings in v1.** tokensave IS the retrieval layer. The agent calls `tokensave_search` and `tokensave_context` as tools; the model decides what to query. Adding embeddings later is non-breaking but won't be done until tokensave's coverage is shown to be insufficient.

6. **Agent loops are bounded.** Every `LocalAgent.run()` has a `max_iterations` cap (default 8). If hit, the agent stops with a clear "iteration cap reached" error rather than spinning forever.

7. **All AI calls are cancellable.** Async with spinner pattern (same as commit-message generation). A Stop button always exists; cancellation never orphans threads or leaks Word/COM processes.

---

## Non-goals

To be explicit about what we're NOT building:

- **Multi-agent coordination** — single agent per task, no swarm patterns
- **Web search / external API tool calls** — purely local + project-scoped
- **Long-running background daemons** — every AI task is user-initiated
- **Conversation persistence across sessions** — chat history clears when you close the Ask tab. Could add later but not v1.
- **Voice / image input** — text-only

---

## Status updates

This file is updated whenever a stage ships or its design materially changes.

**Last updated: 2026-05-24** — Roadmap-2 shipped in full (Phases 0, 1, 2, 3, 4, 4.5, 5a, 5b). Highlights:

- **Phase 0 — Anti-monolith governance.** `BASIC_INSTRUCTIONS.md` rewritten with 8 working rules (A–H); Doctor gained an AST-based monolith audit with hybrid layout-method carve-out.
- **Phase 1 — Stage 3 unlock.** `write_file` agent tool gated through `ProposalDialog` + race-safe `ProposalBridge`. Lifts the read-only-agent invariant; unblocks the CHANGELOG drafter and most of Stage 7.
- **Phase 2 — Daemon UI + canonical changelog patcher.** Footer indicator right-click menu (Start / Stop / Install autostart); `insert_changelog_release` extended with idempotent replace + boundary precision and wired into ReleaseWizard. Fixed pre-existing `toggle_daemon` `--start` bug.
- **Phases 3 + 4 + 4.5 — Targeted architectural cleanup.** `ReleaseWizardDialog._build_ui` split per wizard step; `BranchManagementController` extracted from `GitTabController` (50 → 38 methods, under 40 cap); 6 dialog `__init__` constructors split using the same per-section template.
- **Phase 5a — `gh pr create --web` button** in the PR draft dialog. `--body-file` (not `--body`) sidesteps Windows command-line quoting.
- **Phase 5b — Pre-commit AI review hook** with three-value backend choice (`"auto"` / `"claude_cli"` / `"llm"`) and three-value severity threshold (`"none"` warn-only default / `"medium"` / `"high"`). Fail-open invariant on every error path. Stop-hook variant deliberately deferred (logged with rationale in the Roadmap-3 backlog).
- **Doctor monolith audit dropped project violations from 49 → 39** across Phases 3 + 4 + 4.5 + 5b combined, without bundling unrelated cleanup. Remaining 30+ violations logged in the Roadmap-3 code-health backlog by priority.

**Last updated: 2026-05-23** — Stages 0, 1, AND 2 all shipped this cycle. Highlights:

- **Stage 0** (smart commit messages) shipped earlier; this cycle extended `_call_llm` with optional streaming via an `on_token` callback.
- **Stage 1** (AI Code Review) shipped with streaming response display, byte-aligned SSE parsing, and worker-side token batching to avoid Tk event-loop saturation.
- **Stage 2** (🤖 Ask tab) shipped end-to-end. `src/agent.py` (`LocalAgent`) + `src/agent_tools.py` (`ToolSpec` registry with 6 read-only tools). Includes: bounded iteration loop, cumulative context budget, per-tool error wrapping, path-containment validation, tool-call rescue for local models that emit calls as JSON-in-content, Ollama `num_ctx=32768` bump, detailed HTTP error reporting via `_last_error`, `read_file` line-range support, basename-match suggestions on not-found errors.
- **🦙 Ollama Model Manager** shipped — Settings → "🦙 Manage Ollama Models…" using Ollama's native REST API.
- **🔄 Upgrade tokensave from the manager** shipped — sync-output regex parser + hourly GitHub releases poller.
- **🔌 MCP Integration configurator** shipped — UWP-aware path detection, label-aware classification, backup-first writes, refuses to write over running Claude.
- **Doctor button** enhanced with stale-entry purge offer + cmd.exe spawn fallback for TTY-gated prompts.
- **`tokensave-wrapper.py`** stdio bug fixed (explicit `sys.stdin/stdout/stderr` to Popen). Live in-session pin reloading via wrapper-side watcher deferred — see `docs/MCP_INTEGRATION_GOTCHAS.md` for the three viable paths forward.
- **🐙 Merge PR button** added to Git tab — full GitHub PR merge flow without leaving the manager.

Next up: Stage 3 (CHANGELOG drafter — first write tool, ProposalDialog gate) when there's lived-with experience with Stages 0–2 to inform the proposal-dialog design.
