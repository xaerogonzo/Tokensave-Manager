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

### 🔮 Stage 1 — AI Code Review panel
*Status: planned, next up*

Right-click any project → **🔍 AI Code Review…** opens a dialog showing the pending diff alongside an AI-generated structured review (high/medium/low severity findings, observations). Pure read-only — the AI never modifies anything. Smallest viable feature that proves whether local models give useful reviews on your hardware.

**Why first:** ~1 day of work, maximum reuse of existing infrastructure, zero autonomy concerns.

### ✅ Stage 2 — Project Q&A chat with tool calling
*Status: shipped*

New manager tab: **🤖 Ask**. Chat interface where the AI calls read-only tools (`read_file`, `list_directory`, `git_log`, `git_diff`, `tokensave_search`, `tokensave_context`) to answer questions about the active project. Sample questions:

- "What does this project do?"
- "Where is the commit-message generator?"
- "Why is `_pending_diff` using `HEAD` instead of `--cached`?"
- "Show me everything that calls `_classify_commits_for_changelog`."

All tools are read-only — the agent CANNOT write files, run commits, or modify config. The agent loop is bounded (default 8 iterations) and has a cumulative context budget (~40 000 chars across all tool outputs) so repeated 50 KB reads can't saturate small local-model context windows. Lives in `src/agent.py` (`LocalAgent`) and `src/agent_tools.py` (`ToolSpec` registry).

Provider support: Ollama / OpenAI / OpenAI-compatible (LM Studio, vLLM, etc.) all do tool calling natively. Anthropic falls back to a one-shot completion without tools — adding native Anthropic tool-use is a known follow-up.

### 🔮 Stage 3 — CHANGELOG drafter
*Status: planned*

Right-click → **📝 Draft CHANGELOG entry…**. Agent reads commits since the last release tag, classifies them, drafts CHANGELOG bullets. A ProposalDialog presents the old-vs-new diff with **Apply / Reject / Edit then Apply** buttons. First feature with a write tool, but every write goes through the same approval gate.

### 🔮 Stage 4 — Refactor scout
*Status: planned*

Right-click → **🔬 Refactor scout…**. Agent calls tokensave's analytics tools (`tokensave_dead_code`, `tokensave_god_class`, `tokensave_circular`, etc.) and produces a structured report with plain-English explanations per finding. Each finding has Investigate / Ignore actions. Findings marked Ignore persist in `manager-config.json` and won't reappear.

### 💭 Stage 5 — Limited autonomous mode
*Status: considering, revisit after Stages 1-4 ship*

Adds opt-in autonomous execution for specific tool categories (e.g. "auto-write CHANGELOG without asking"). Per-tool allow/deny configuration in Settings. Session-level kill switch. Audit log of every autonomous action. **Not committed** — only revisited after lived experience with propose-only confirms the workflows are valuable.

---

### 💭 Stage 6 — Workflow accelerators
*Status: considering*

Bundles three commit/release workflow features that share the same "AI drafts → user approves → manager applies" pattern:

- **Pre-commit AI review hook** — Claude Code Stop hook (or pre-commit git hook) that runs the Stage 1 reviewer on the pending diff and blocks the commit if any 🔴 High-severity findings are present. Override via `--no-verify` or a "Commit anyway" button.
- **PR description generator** — Right-click → 🐙 Draft PR description… Agent calls `tokensave_pr_context` + `tokensave_diff_context` + `git_log` for the branch and produces a PR title + body with Summary, Changes, Testing, and Review Questions sections. Opens GitHub's compare page with the description pre-filled (uses `gh pr create --web --body`).
- **Release-notes narrative writer** — Extends Release Wizard with an AI-generated summary paragraph above the bullet list ("This release focuses on X and Y…"). Drafts inside the existing wizard textarea so the user can edit before publishing.

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

This file is updated whenever a stage ships or its design materially changes. Last updated: 2026-05-23 (Stage 2 shipped — agent chat tab + tool registry + LocalAgent loop. Ollama deep integration: streaming responses in AI Code Review, Model Manager dialog. Pin-watcher fix in `tokensave-wrapper.py`).
