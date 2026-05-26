# TokenSave Manager — Roadmap

This document tracks planned features and architectural direction. Items here are **proposals**, not commitments — each ships only after its design proves itself in earlier stages.

Status legend:
- ✅ Done — landed in a release
- 🟡 In progress — actively being built
- 🔮 Planned — design locked, not started
- 💭 Considering — not yet committed

---

## Roadmap 6

### ✅ Tasks tab — Claude Code session + worktree visibility. Shipped Roadmap-5 (2026-05-25). Details in CHANGELOG.md.

### ✅ Claude CLI commit message fix (`--append-system-prompt` removed). Shipped Roadmap-5 (2026-05-25). Details in CHANGELOG.md.

### ✅ Code-health audit + remediation (2026-05-25)
- Rebuilt tokensave index (eliminated Windows dual-path artifact)
- Extracted `TrayManager` from `App` (`src/helpers/tray_manager.py`)
- Reduced `_do_draft_changelog` CC 12 → ≤4 (`src/controllers/ai_tasks_ctrl.py`)
- Filed upstream tokensave bugs: path-normalization (#87), health details (#82), redundancy tool (#83), install path-with-spaces (#81)

### ✅ Commit-message scope heuristic fix (2026-05-25)
Multi-doc-file commits now use the dominant directory as scope (e.g. `docs(upstream-issues): update`) instead of listing filename stems. (`src/helpers/commit_messages.py`)

### ✅ `mcp_config._apply` CC reduction (2026-05-25)
Extracted `_log_to_app` + `_apply_running_guard` from `_apply`; CC dropped ~11 → ~4. (`src/dialogs/mcp_config.py`)

### ✅ Ask tab — final-message streaming (2026-05-25)
Agent loop now streams the final-turn tokens to the UI in real time via a buffered 50 ms flush loop (prevents Tkinter widget-toggle jitter). Tool-call turns use non-streaming as before. (`src/agent.py`, `src/controllers/ask_tab.py`)

### ✅ Ask tab overhaul (2026-05-25)
Separate `ask_tab_llm` config independent of commit-message model; Claude CLI provider path; project context injection on fresh conversations; auto-clear on project switch; session log persistence (`logs/ask_sessions.md`); tokensave CLI subcommand fix (`tokensave tool search` / `tokensave tool context` after upstream removal); help tab comprehensive static content rewrite. (`src/controllers/ask_tab.py`, `src/dialogs/settings.py`, `src/agent_tools.py`, `src/controllers/help_tab.py`)

### ✅ Gitignore AI Suggest (2026-05-25)
One-click AI-powered gitignore pattern recommendations in the `.gitignore` editor. CodeGraph SQLite (`.codegraph/codegraph.db`) used as zero-cost project file listing when available; falls back to `os.listdir`. Reuses `ask_tab_llm → commit_message_llm` config chain. Two redundancy checks (exact match + path-scoped basename match) prevent suggestions like `src/__pycache__/` when `__pycache__/` is already ignored. (`src/dialogs/gitignore.py`, `src/helpers/readme_patch.py`)

### ✅ Doc Update Automation — Tier A + B subset (2026-05-25)
**Tier A:** `📝 Documentation` snippet category in Reference tab with 7 curated copy-paste prompts (README feature bullet, CHANGELOG entry, architecture section, memory file entry, consistency check, migration note, PR from CHANGELOG). (`src/prompts.py`)

**Tier B subset:** New `📝 Doc Updates…` right-click dialog drafts CHANGELOG.md `[Unreleased]` bullets AND README.md "Recent highlights" sub-section content from a commit range. Per-tab thread isolation (`_tab_state[key]["stop"]`), WM_DELETE_WINDOW cancels ALL tabs, Apply routes through ProposalBridge for old-vs-new diff review. Mixed-commit edge case (commit touching both code AND docs) included with explicit boundary note. Sparse-commit safety net (avg subject length < 15 chars) appends changed-file paths to the prompt. README path uses append-only sub-section insertion (`insert_readme_highlights_subsection`) instead of full-block regeneration — preserves all existing sub-sections even with small local models. (`src/dialogs/doc_drafter.py`, `src/helpers/doc_drafter.py`, `src/helpers/readme_patch.py`, `src/controllers/projects_tab.py`)

**Deferred to Roadmap-7:** Architecture tab (`docs/*.md` picker + CLAUDE.md blueprint injection), Memory tab (frontmatter + path-encoding via `claude_tasks.py`), Tier C auto-suggest-after-commit banner.

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

### ✅ Stage 0 — Smart commit message generation. Shipped 2026-05-23. Details in CHANGELOG.md.

### ✅ Stage 1 — AI Code Review panel. Shipped 2026-05-23. Details in CHANGELOG.md.

### ✅ Stage 2 — Project Q&A chat with tool calling (Ask tab). Shipped 2026-05-23. Details in CHANGELOG.md.

### ✅ Stage 3 — CHANGELOG drafter. Shipped 2026-05-24. Details in CHANGELOG.md.

### ✅ Stage 4 — Refactor scout. Shipped 2026-05-24. Details in CHANGELOG.md.

### 💭 Stage 5 — Limited autonomous mode
*Status: considering, revisit after Stages 1-4 ship*

Adds opt-in autonomous execution for specific tool categories (e.g. "auto-write CHANGELOG without asking"). Per-tool allow/deny configuration in Settings. Session-level kill switch. Audit log of every autonomous action. **Not committed** — only revisited after lived experience with propose-only confirms the workflows are valuable.

---

### 🟡 Stage 6 — Workflow accelerators
*Status: PR draft + pre-commit hook shipped; release narrative still planned*

Bundles three commit/release workflow features that share the same "AI drafts → user approves → manager applies" pattern:

- **PR description generator** ✅ — Shipped Roadmap-1 / extended Roadmap-5. Details in CHANGELOG.md.
- **Pre-commit AI review hook** ✅ — Shipped Roadmap-2 P5b. Details in CHANGELOG.md.
- **Release-notes narrative writer** *(planned)* — Extends Release Wizard with an AI-generated summary paragraph above the bullet list ("This release focuses on X and Y…"). Drafts inside the existing wizard textarea so the user can edit before publishing. `helpers/changelog_patch.py:insert_changelog_release` is already wired into ReleaseWizard's publish path, so the narrative just needs to fit into the existing notes string before insertion.

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

### ✅ Roadmap-2 — Anti-monolith governance + Phases 0–5b. Shipped 2026-05-24. Details in CHANGELOG.md.

### ✅ Roadmap-3 — Code-health sweep (Phases 1–6). Shipped 2026-05-24. Details in CHANGELOG.md.

Remaining 🔮 items from the Roadmap-3 Doctor snapshot (promote when touching the affected code):

**Helper complexity:**
- 🔮 `helpers/git.py` `_format_git_status_cell`=16
- 🔮 `helpers/project_discovery.py` `find_projects`=16
- 🔮 `helpers/scaffold.py` `_scaffold_git_hook`=14
- 🔮 `helpers/gitignore.py` `_ensure_gitignore`=13
- 🔮 `helpers/release.py` `_classify_commits_for_changelog`=12, `_suggest_bump_kind`=11
- 🔮 `helpers/shadow_links.py` `remove_shadow_links`=11

**Controllers / dialogs:**
- 🔮 `controllers/projects_tab.py` class 44 methods (over 40 even after Round 5's 9 extractions) + `__init__` 109 lines + `rebuild_tree` complexity 13. Diminishing returns on further extraction; consider grandfathering via `doctor_skip_monolith_paths` if no clean further split exists.
- 🔮 `controllers/snippets.py` `_on_snippet_saved`=11
- 🔮 `dialogs/mcp_config.py` `_apply`=11
- 🔮 `dialogs/ollama_model_mgr.py` `_fetch_context_length`=12, `_worker`=12
- 🔮 `dialogs/release_wizard.py` `_refresh_artefact_preview`=15
- 🔮 `dialogs/gitignore.py` `_on_save`=12
- 🔮 `dialogs/ai_code_review.py` `_render_review`=11

**Agent / app pre-existing (do-not-touch during feature work):**
- 🔮 `agent.py` `run()`=18, `_rescue_tool_call_from_content`=19/101 lines, `_run_anthropic_oneshot`=20
- 🔮 `agent_tools.py` `_suggest_paths_for_missing_file`=11, `_read_file`=13, `_runner`=17
- 🔮 `app.py` `_check_config`=14, `worker`=14

### 💭 Genuine dead-code cleanup in `src/agent_tools.py` (10-minute pass)
Three functions look genuinely unused (grep-verified, NOT Tk-callback false positives):
- `_read_file_range` at `agent_tools.py:218` — superseded by inline handling in the `read_file` tool handler. **Verify** no caller remains, then delete.
- `_suggest_paths_for_missing_file` at `agent_tools.py:249` — docstring claims it's called from `read_file`'s error path; verify the wiring still routes through it.
- `_slim_tokensave_context` at `agent_tools.py:452` — verify the `tokensave_context` tool handler still calls it.

### Stage 2 (Ask-tab agent) usability refinements

✅ Tool-call deduplication, loop-stall surfacing, `git_log` default — Shipped Roadmap-3 Phase 5. Details in CHANGELOG.md.

- 🔮 **Final-message streaming.** Still planned. Currently the assistant's final answer arrives as one chunk at the end of the loop. Streaming the final-turn tokens would make long answers feel responsive. Non-streaming tool-call turns is correct — they're structured JSON.

### 💭 Pre-commit AI review — Claude Code Stop-hook variant

When Roadmap-2 Phase 5b shipped the pre-commit AI review hook, we considered three variants: git pre-commit hook only / Claude Code Stop hook only / both. We went with **git pre-commit hook only** after weighing trade-offs:

- **Git pre-commit hook** fires at the single moment that matters (a commit is about to land), reviews exactly `git diff --cached` (no ambiguity about what's being reviewed), produces one signal per commit, and can actually block. Works for human changes too, not just Claude-driven ones.
- **Claude Code Stop hook** fires on EVERY Claude turn — including tiny ones and non-code ones — doesn't naturally know which files Claude touched vs. what was already dirty, can't block git operations anyway, and is the fastest path to "user disables the hook after a week of false positives."

The git hook covers ~95% of the safety value with cleaner semantics. Revisit only if real-world usage uncovers a specific gap the pre-commit hook can't fill.

### Stage 0 (commit-message generation) quality refinements

✅ `min_diff_lines` gate lowered (30 → 10), strategy badge UI — Shipped Roadmap-3 Phase 5. Details in CHANGELOG.md.

- 🔮 **File-name fallback is too generic for `docs/upstream-issues/`-style paths.** The current heuristic produces "docs: update documentation" when ALL changed files are markdown — a `_SCOPE_PATTERNS` addition keyed off `docs/upstream-issues/<name>.md` → `docs(upstream): <name>` would catch this class.

### ✅ Skill awareness / prompting — catalog shipped. Shipped Roadmap-3 Phase 3, 2026-05-24. Details in CHANGELOG.md.

Remaining design space (not yet committed):

- **Workflow-moment prompts** — Context-aware nudges at specific Git-tab actions (e.g. before clicking Release… → prompt to run `consolidate-memory`). Risk: notification fatigue. Mitigation: per-prompt opt-out in `cfg.raw["skill_prompt_suppressed"]`.
- **Last-used tracking** — Per skill, per project, write a timestamp to `cfg.raw["skill_last_used"]`. Surface "It's been 30 days since `consolidate-memory` ran" as a passive footer indicator.
- **Right-click → "Suggest skill for this project"** — Manager scans recent git activity / file changes and proposes 1–3 relevant skills with rationale.
- **Workflow bundles** — Curate "Pre-PR", "Pre-release", "End-of-roadmap" bundles chaining multiple skills.

### ✅ Draft PR refinements (Roadmap-3 Phase 2 — all shipped). Shipped 2026-05-24. Details in CHANGELOG.md.

### ✅ File upstream tokensave issues (2026-05-25)
All four filed and closed:
- #87 Windows dual-path indexing (path-normalization bug)
- #82 `tokensave_health details=true` sub-score breakdown
- #83 `tokensave_redundancy` AST-level functional-duplication detector
- #81 `tokensave install --agent claude` path-with-spaces quoting bug

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

**Last updated: 2026-05-25** — Roadmap-6 in progress. Code-health audit + remediation; commit-message scope fix; `mcp_config._apply` CC reduction; Ask tab final-message streaming. All upstream tokensave issues filed (see above).

✅ **2026-05-25 (earlier)** — Roadmap-5 initial ship. Highlights: Claude CLI model selector; pre-commit hook hang fix (Opus → Haiku); commit-message Claude CLI parity; Draft PR direct GitHub PR creation. Details in CHANGELOG.md.

✅ **2026-05-24** — Roadmap-3 shipped in full (Phases 1–6). Details in CHANGELOG.md.

✅ **2026-05-24** — Roadmap-2 shipped in full (Phases 0–5b). Details in CHANGELOG.md.

✅ **2026-05-23** — Stages 0, 1, and 2 (smart commit messages, AI Code Review, Ask tab) shipped. Ollama Model Manager, Upgrade tokensave, MCP Integration configurator also shipped. Details in CHANGELOG.md.
