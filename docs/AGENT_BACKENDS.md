# Agent Backends

How `dispatch_llm` (in `helpers/doc_drafter.py`) and `_call_llm` (in
`helpers/llm.py`) resolve the configured provider to a concrete transport,
and what each tier supports.

---

## Three tiers

### Tier 1 — Anthropic API (`provider: "anthropic"`)

**Entry point:** `helpers/llm.py:_call_anthropic`

Direct HTTPS to `api.anthropic.com/v1/messages`.  Uses the native Anthropic
Messages API format (separate `system` + `messages` array with role content
blocks).  API key read from the environment variable named in
`cfg["api_key_env"]`.

| Capability                     | Supported |
|-------------------------------|-----------|
| Streaming (`on_token`)         | ✅        |
| One-shot completion            | ✅        |
| Tool use (function calling)    | ❌ (v1)   |
| Multi-turn conversation        | ❌ (one-shot only in current callers) |
| Theme B1 grounding injection   | ✅        |
| Theme B2 agentic tool loop     | ❌ (see asymmetry note below) |
| Few-shot examples (Theme C4)   | ❌ (local providers only) |
| `num_ctx` / `top_k` overrides  | ❌ (provider-specific params ignored) |

**Spend note:** `helpers/commit_messages.py` applies a `min_diff_lines` gate
before calling the cloud APIs to prevent trivial commits from consuming quota.
`dispatch_llm` has no such gate — doc-drafting always calls out.

---

### Tier 2 — Claude CLI print-mode (`provider: "claude_cli"`)

**Entry point:** `helpers/claude_cli.py:call_claude_cli_print`

Shells out to the Claude Code CLI binary (`claude --print`) using
`subprocess.run`.  The CLI authenticates via its own session (OAuth or API
key in `~/.claude`); no API key in manager config is needed.

```
claude --print [--model MODEL] [--system-prompt SYSTEM] PROMPT
```

Stdout is captured and returned as the draft text.  The process runs with
`--print` (non-interactive, single-turn) so there is no conversation state.

| Capability                     | Supported |
|-------------------------------|-----------|
| Streaming                      | ❌ (stdout captured after exit) |
| One-shot completion            | ✅        |
| Tool use                       | ❌        |
| Multi-turn                     | ❌        |
| Theme B1 grounding injection   | ✅ (text spliced into prompt before dispatch) |
| Theme B2 agentic tool loop     | ❌ (documented asymmetry — see below) |
| Few-shot examples (Theme C4)   | ❌ (local providers only) |
| `gen_params` overrides         | ❌ (CLI ignores temperature / top_p) |
| `num_ctx`                      | ❌        |

**CLI path** is set in Settings → Claude Code CLI.  An empty path returns a
user-visible error before the subprocess call.

---

### Tier 3 — Ollama / OpenAI-compatible (`provider: "ollama"` or `"openai_compatible"`)

**Entry points:**
- Plain completion: `helpers/llm.py:_call_openai_compat` (called via `_call_llm`)
- Agentic loop:    `helpers/doc_drafter.py:_dispatch_agentic` → `agent.py:LocalAgent`

`"ollama"` is a friendly alias for `"openai_compatible"` with `base_url`
defaulting to `http://localhost:11434`.  Any OpenAI Chat Completions-compatible
server works (LM Studio, vLLM, llama-server, LocalAI, …).

#### Plain completion path

`dispatch_llm` calls `_call_llm`, which calls `_call_openai_compat`.  Request
body: `POST <base_url>/v1/chat/completions` with JSON-encoded messages array.
Response: first choice's `message.content`.  Streaming supported via SSE when
`on_token` is provided.

#### Agentic path (Theme B2)

When the per-tab **🔍 Tokensave tools** checkbox is enabled,
`dispatch_llm` enters the agentic path instead of the plain completion path.
`_dispatch_agentic` spins a `LocalAgent` (bounded to 6 iterations) with two
read-only tools registered:

| Tool name            | What it does |
|---------------------|--------------|
| `tokensave_search`  | Symbol name search in the code graph |
| `tokensave_context` | Semantic context query (files, callers, callees) |

The agent may call either tool multiple times before producing its final text.
The final assistant message is returned to the dialog as the draft.

`LocalAgent` does its own HTTP (not via `_call_llm`) so it can inspect raw
`tool_calls` fields in the response.  Provider dispatch mirrors
`_call_openai_compat` but adds tool-call round trips.

| Capability                     | Supported |
|-------------------------------|-----------|
| Streaming                      | ✅ (plain path) / ❌ (agentic — full response per iteration) |
| One-shot completion            | ✅        |
| Tool use                       | ✅ (agentic path only) |
| Multi-turn (within one run)    | ✅ (up to 6 iterations) |
| Theme B1 grounding injection   | ✅ (spliced before agentic dispatch too) |
| Theme B2 agentic tool loop     | ✅        |
| Few-shot examples (Theme C4)   | ✅ (spliced into user prompt before dispatch) |
| `temperature`, `top_p`, `top_k` | ✅       |
| `num_ctx`                      | ✅ (Ollama extension, passed as `options.num_ctx`) |

---

## How `dispatch_llm` resolves the backend

```
dispatch_llm(llm_cfg, system, user, claude_cli_exe, cwd, ...)
│
├── gen_params merged into llm_cfg copy (DocType overrides win)
├── provider = llm_cfg["provider"]
│
├── examples spliced into user_prompt? ── provider in (ollama, openai_compatible)
│
├── enable_tokensave_tools=True AND provider in (ollama, openai_compatible)?
│   └── _dispatch_agentic(...)          ← LocalAgent with tokensave tools
│
├── provider == "claude_cli"?
│   └── call_claude_cli_print(exe, ...)  ← subprocess, single-shot
│
└── else
    └── _call_llm(llm_cfg, ...)          ← HTTP to Anthropic / OpenAI / Ollama
```

`dispatch_llm` returns `(text, None)` on success and `(None, error_string)`
on any failure.  Threading, cancellation, and UI updates are the caller's
responsibility (`dialogs/doc_drafter.py:_on_generate` runs `dispatch_llm` in
a daemon thread and uses `self.after(0, …)` to push results back to Tk).

---

## Known asymmetry: B1 vs B2

Theme B1 (grounding injection) and Theme B2 (agentic tool loop) are
**additive, not exclusive**:

| Scenario                              | B1 grounding | B2 tools |
|--------------------------------------|:------------:|:--------:|
| Anthropic API                         | ✅           | ❌       |
| Claude CLI (`claude --print`)         | ✅           | ❌       |
| Ollama / openai_compatible (checkbox off) | ✅       | ❌       |
| Ollama / openai_compatible (checkbox on)  | ✅       | ✅       |

The B2 checkbox is intentionally unavailable for Anthropic and Claude CLI:
- **Anthropic API:** tool calling in `LocalAgent` would require Anthropic
  tool-use format (different JSON schema); `LocalAgent` currently speaks the
  OpenAI tool-call wire format only.  Adding Anthropic tool support is a
  Roadmap-8 candidate.
- **Claude CLI:** the `--print` mode is a single subprocess call with no
  mechanism to feed tool results back.  Migrating to the Anthropic Agent SDK
  (structured multi-turn) would enable this — see the Roadmap-8 gate below.

---

## Theme B1 grounding injection

`helpers/doc_grounding.py:build_grounding_block(project_path, recipe, …)`
runs read-only `tokensave tool context` queries and returns a markdown block:

```markdown
## Code-graph context (from tokensave — facts you can cite verbatim)
<recipe output>
```

This block is spliced between the "Recent commits" section and the "Current
content" section of the user prompt before `dispatch_llm` is called.  The
splice happens unconditionally for all providers (including Anthropic and
Claude CLI) as long as `.tokensave/` exists in the project.

Recipe → DocType mapping:

| Recipe key             | DocTypes          | Queries run |
|-----------------------|-------------------|-------------|
| `commit_range_context` | changelog, readme | `diff_context`, `impact` |
| `architecture_overview`| architecture      | `dsm`, `module_api`, `coupling` |
| `roadmap_evidence`     | roadmap           | `diff_context`, `changelog` |
| `module_deep_dive`     | memory            | `node`, `callers`, `callees` |

Output is capped at 8 000 characters (truncated at the last complete line).
`build_grounding_block` returns `""` silently on all failure paths (missing
`tokensave` binary, missing `.tokensave/` directory, unknown recipe, timeout).

---

## v4.1 — Codegraph parallel grounding (additive)

Roadmap-7's cascade rounds added a second grounding source alongside
tokensave: **CodeGraph**, via `helpers/doc_grounding.py:build_codegraph_block`.
Mirror contract of `build_grounding_block`. For the `roadmap_evidence`
recipe it additionally invokes `codegraph affected --stdin` with the
changed-files list — this is the unique value-add codegraph has that
tokensave doesn't (test-impact mapping from a diff).

`build_combined_grounding` (v4.4 dedup-first-then-truncate) merges both
sources with per-source cap (default 4000 chars; combined cap = 8000
to match the v3 single-source ceiling). Line-level dedup eliminates
redundancy where both sources cite the same symbols.

Both blocks fail-open: a project with only one tool indexed gets only
that tool's block; a project with neither gets an empty grounding section
and the prompt proceeds normally.

### v4.3 — Freshness gate (`helpers/codegraph_freshness.py`)

`ensure_fresh(project_path, codegraph_exe)` runs immediately before
every `build_codegraph_block` call. If the index is `stale` (DB mtime
> 200 s behind the newest source file), it runs `codegraph sync`
synchronously (~2-5 s) and re-checks before the grounding call
proceeds. If the index is `broken` (under-indexed — < 30 % of the
tokensave file count or < 5 absolute), the block returns empty and the
caller's UI surfaces a once-per-session "run a full reindex" dialog.

### v4.2 — Master toggle

`ManagerConfig.enable_llm_grounding` gates the entire pipeline across
EVERY AI surface (commit messages, PR draft, AI Code Review, Ask tab
non-agentic, doc drafter). Default ON; persisted via
`cfg.raw["enable_llm_grounding"]`. Settings → AI backend selection →
"Code-graph grounding".

---

## Roadmap-8 decision gate: Anthropic Agent SDK migration

The current `LocalAgent` implementation uses stdlib `urllib.request` and the
OpenAI tool-call wire format.  It works with any OpenAI-compatible server but
cannot use Anthropic tool use natively.

Roadmap-8 should evaluate the **Anthropic Agent SDK** (`anthropic` Python
package, `client.messages.create(tools=[…])`).  Adopting it would:

1. Enable Theme B2 on the Anthropic API tier (structured multi-turn with
   `tokensave_search` / `tokensave_context` as native tool calls).
2. Potentially enable Claude CLI multi-turn if the SDK exposes a session-aware
   mode that maps onto the existing auth.
3. Introduce a new dependency (`anthropic` SDK) — weigh against the current
   zero-dependency stdlib approach.

Until the SDK is adopted, the asymmetry documented above stands: local
providers (Ollama/openai_compatible) are the only path to B2 agentic drafting.
