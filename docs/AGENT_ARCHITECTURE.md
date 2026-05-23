# TokenSave Manager — Agent Architecture

This document describes how local AI integration works in the manager. It explains the design decisions, the layering, and the rules that govern every AI feature. Read this before adding new AI capabilities.

---

## Core principle: propose-only

The manager's AI assistant **never modifies your files, runs git commands, or changes config without an explicit user click**. Every write action passes through a `ProposalDialog` that shows the proposed change as an old-vs-new diff with **Apply / Reject** buttons.

This is not a configurable setting in v1 — it's enforced by the type system. Tools that mutate state are marked `is_write=True` in the tool registry, and the dispatcher refuses to execute them directly; it queues a `Proposal` event that the UI must resolve before the agent continues.

Autonomous execution (Stage 5+) will be opt-in per tool with an explicit allowlist. It is **not** the default and will never be enabled implicitly.

---

## Stack

| Layer | Implementation |
|---|---|
| **Inference engine** | User's choice: Ollama (recommended), LM Studio, OpenAI, Anthropic. Configured in Settings → "AI commit messages". |
| **HTTP transport** | `urllib.request` from the Python stdlib. No `httpx`, no `requests`, no `openai` SDK — keeping dependencies minimal. |
| **LLM client** | `_call_llm(cfg, system_prompt, user_prompt, max_tokens, timeout) → str \| None` in `src/tokensave-manager.py`. Returns text on success, `None` on any failure. |
| **Agent loop** | Future: `LocalAgent` class in `src/agent.py` (Stage 2+). Custom 150-line implementation. NO LangChain / LlamaIndex / OpenAI-Agents-SDK. |
| **Tool registry** | Future: `ToolSpec` dataclass entries in `src/agent_tools.py` (Stage 2+). |
| **UI gate** | Future: `ProposalDialog(tk.Toplevel)` for any `is_write=True` tool call (Stage 3+). |

---

## What's built today (Stage 0 + Stage 1)

### Stage 0 — Smart commit-message generation (shipped)

Strategy chain in `_suggest_commit_message(repo_path, status_text)`:

1. **LLM call** via `_call_llm_for_commit_message` (if enabled in config)
2. **CHANGELOG bullet parsing** via `_extract_changelog_additions`
3. **Diff content** via `_suggest_from_diff_content` (added Python defs/classes, file kinds)
4. **File-name fallback** via `_suggest_from_filenames` (the v1.0.x behaviour)

Every result passes through `_sanitize_commit_message` which enforces conventional-commit format, imperative mood, and blocks filename-listing anti-patterns.

The 💡 Suggest button in the Git Commit dialog drives this. Async with a spinner — the GUI never freezes waiting for the LLM.

### Stage 1 — AI Code Review (shipped)

Right-click any project → 🔍 AI Code Review. New `AICodeReviewDialog(tk.Toplevel)`:

- Top pane: `git diff HEAD` with green/red colour-coding (matches the existing Git tab diff viewer)
- Bottom pane: AI-generated review with severity sections (⚠ High / ⚡ Medium / 💡 Low / ℹ Observations)
- Footer: **Copy** / **Regenerate** / **Stop** / **Close**

One `_call_llm` invocation with a locked system prompt. **No tools, no file writes, no autonomy concerns** — it's a pure read-only feature.

The system prompt is in `AICodeReviewDialog._SYSTEM_PROMPT` and is intentionally rigid about output format so the section-header colour tags can render it consistently.

---

## What's not built yet (Stage 2-5)

See `docs/ROADMAP.md` for the staged plan with status badges. Briefly:

- **Stage 2** — Project Q&A chat with tool-calling agent loop. Adds `LocalAgent`, `ToolSpec`, the tool registry (`read_file`, `tokensave_search`, etc.). All tools read-only.
- **Stage 3** — CHANGELOG drafter. First write tool (`patch_file`) gated by `ProposalDialog`.
- **Stage 4** — Refactor scout. Agent calls multiple tokensave analytics tools, produces structured report.
- **Stage 5** — Limited autonomy with per-tool allow/deny configuration. **Considering, not committed.**

---

## Locked architectural rules

These apply to every AI feature added to the manager. They are non-negotiable for v1 — changing any of them requires explicit user discussion, not silent reinterpretation by an LLM (or me) editing code:

### 1. Propose-only by default

Every write action goes through `ProposalDialog`. No exceptions in Stages 1-4. Stage 5 may add opt-in autonomous execution, but that's a deliberate user choice, never the default. **Mis-labelling a write tool as read is a security defect.**

### 2. No agent frameworks

The agent loop is custom Python in `src/agent.py`. No LangChain, no LlamaIndex, no OpenAI-Agents-SDK. These frameworks add huge dependency trees, slow imports, and abstractions that don't fit a single-process desktop app. The custom implementation is small enough that any contributor can read it end-to-end in one sitting.

### 3. tokensave IS the retrieval layer

The manager does not implement RAG, embeddings, or vector stores in v1. When the agent needs to find code, it calls `tokensave_search` / `tokensave_context` as tools — the model decides what to query. Adding embeddings later is non-breaking but won't happen until tokensave's coverage is shown to be insufficient.

### 4. Single tool registry

Every tool the agent can call lives in `src/agent_tools.py` as a `ToolSpec(name, description, parameters_json, handler, is_write)`. Adding a new tool means adding one entry. No scattered tool definitions, no ad-hoc shell-outs inside dialog code.

### 5. Async with cancellation, always

Every LLM call runs on a daemon `threading.Thread` with:
- A visible spinner showing it's working
- A Stop button that cancels via a token-bump pattern (the worker can't be killed mid-`urllib.urlopen`, but stale results are discarded)
- `self.after(0, ...)` to push results back to the main thread

Pattern copied verbatim from `GitCommitDialog._populate_suggestion` and `AICodeReviewDialog._start_review`. Follow it.

### 6. Bounded agent loops

`LocalAgent.run()` has a `max_iterations` cap (default 8). If hit, the agent stops with a clear "iteration cap reached" error rather than spinning forever or running up an API bill.

### 7. No reasoning models recommended

Reasoning models (Qwen 3.5 thinking, DeepSeek-R1, anything with "reasoning" in the name) routinely take 30-60+ seconds to "think" before producing output. They frequently exceed the 90-second timeout and produce empty `content` fields. Recommend non-reasoning instruction-tuned models: `qwen2.5:14b`, `qwen2.5-coder:14b`, `mistral-nemo:12b`, `llama3.1:8b`, or `claude-haiku-4-5` for cloud.

The manager doesn't *forbid* reasoning models, but the LM Studio / Ollama presets won't auto-detect them as the preferred default.

---

## Adding a new AI feature

Use this checklist:

1. **Is it read-only or read-write?** If read-write, your feature touches `ProposalDialog` (Stage 3+ infrastructure).
2. **Does it need tools?** If yes, you're building on top of `LocalAgent` (Stage 2+ infrastructure). If no, it's a one-shot `_call_llm` call like AI Code Review.
3. **Where does it live in the UI?** Right-click menu, Git tab button, dedicated notebook tab, or modal triggered from another flow.
4. **What's the system prompt?** Keep it in a class-level constant so it's findable and editable without grepping. Match the `AICodeReviewDialog._SYSTEM_PROMPT` pattern.
5. **What happens on failure?** Every LLM call MUST handle `None` return — the user must NEVER see a Python traceback in the UI because the LLM was unreachable.
6. **What happens on cancellation?** Stop button discards stale results via token bump. Copy from `AICodeReviewDialog._cancel`.
7. **What gets documented?** `CHANGELOG.md` entry, `docs/ROADMAP.md` status update, `BASIC_INSTRUCTIONS.md` canonical-pattern note if the feature establishes a new pattern.

---

## Why these rules exist

The manager exists to make Claude (and other AI tools) safer to use across many projects. It would be self-defeating if the manager itself became a "trust me, the AI knows what it's doing" pile of unreviewable automation. Every architectural rule here exists to keep the human in the loop where it matters — at the point of writing files, running commits, or changing config.

You can have powerful AI assistance AND human-in-the-loop safety. The rules above are how we get both.
