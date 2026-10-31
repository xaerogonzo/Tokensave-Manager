# Agent Backends

How `dispatch_llm` (in `helpers/doc_drafter.py`) and `_call_llm` (in
`helpers/llm.py`) resolve the configured provider to a concrete transport,
and what each tier supports.

---

## Which agent CLI, and what `"claude_cli"` means now

The persisted backend values — `draft_pr_backend`, `commit_message_backend`,
`precommit_review_backend`, the Ask tab's `provider` — still spell the CLI
option `"claude_cli"`. **What that value NAMES changed.** It used to mean Claude
Code specifically; it now means *the agent CLI selected in
Settings → AI → Agent CLI*, resolved through `cfg.resolve_agent_cli()`.

This is a deliberate semantic migration rather than a rename, and the reason is
blast radius: renaming would have meant rewriting a per-feature setting in every
existing `manager-config.json`. Nothing rewrites user config. A configuration
saved before Cursor existed has no `agent_cli` key at all, defaults to Claude,
and behaves exactly as it did — locked down by `tests/test_backend_compat.py`.

`resolve_agent_cli()` returns three states, and callers must keep them apart:

| State | Meaning | What to tell the user |
|---|---|---|
| `ok` | agent known, binary found | — |
| `unavailable` | right agent, nothing to run | set its path in Settings → Paths & Tools |
| `unknown_agent` | config names something unrecognised | fix `agent_cli`; **no agent is run** |

An unknown id is never quietly mapped onto the default. Doing so would run one
agent while the settings file named another, which is the failure the three-way
split exists to prevent.

**One surface stays pinned to Claude on purpose:** the Reference tab's skill
runner (`controllers/snippets.py`). Claude Skills live in `.claude/skills` and
are a Claude Code feature; handing that file to another agent CLI would open a
terminal that cannot interpret it, so "follow the selected agent" would be the
wrong behaviour there rather than a missing feature.

---

## The registry: `helpers/agent_cli.py`

One capability table, one row per agent. Fields describe what an agent
*supports*, not which vendor it is — that is what keeps "a new agent is one
table row" true rather than aspirational. A field named `is_cursor` would
re-create the cascade the table replaced.

| Capability | Claude Code | Cursor Agent |
|---|---|---|
| `print_args` | `--print` | `-p --output-format text` |
| `trust_args` | none | `--trust` (headless stops at "Workspace Trust Required" without it) |
| `read_only_args` | none established | `--mode ask` (`-p` alone can write files and run shell) |
| `prompt_transport` | `stdin` | `stdin` (measured 2026-10-31; the argv budgets below still guard any argv-only agent) |
| `system_prompt_mode` | `native` (`--append-system-prompt`) | `prepend` |
| `model_flag` | `--model` | `--model` |
| `interactive` | supported | supported |
| default model | `claude-haiku-4-5-20251001` | `""` (let Cursor choose) |
| binaries probed on PATH | `claude.cmd`, `claude` | `cursor-agent.{cmd,exe}`, `cursor-agent` (never a bare `agent`) |
| extra install dirs | `%APPDATA%\npm` | `%LOCALAPPDATA%\cursor-agent` (current installer), `~/.local/bin` |

Three runners, deliberately NOT derived from one another: `call_print`
(captured one-shot), `spawn` (terminal **with** an instruction) and
`spawn_interactive` (terminal, **no** trailing prompt argument). That last
distinction is load-bearing: appending an empty instruction hands the CLI a
blank positional argument, which it treats as an empty one-shot prompt instead
of entering interactive mode.

`helpers/claude_cli.py` remains as a thin shim with unchanged signatures.

### One default, optional overrides per feature

`agent_cli` is still the default agent. `agent_overrides` (Settings → AI → Agent per feature) lets one job use another: commit message, Draft PR, code review, test generation, the Ask tab, the refactor scout and the integration audit. It is one table on purpose; the orchestration work extends this structure rather than adding a second key.

| Situation | Result |
|---|---|
| no override, `""`, or `"inherit"` | the default agent |
| a named agent that is installed | that agent, with that agent's own path and model |
| a named agent that is NOT installed | `unavailable`, naming the feature. It does **not** fall back to the default |
| an id this build does not know | `unknown_agent`, naming the feature and the `agent_overrides` key |
| `agent_overrides` is not an object | `unknown_agent` with an explanation, never "inherit" |

### Cursor's own Claude importer, and why the Manager does not patch it

Cursor ships a "Claude Code import" that, on this machine, listed nothing. A patch script the person wrote with Cursor's help (from a forum report) shows the cause: Cursor's importer encoded a project folder into Claude's directory name differently from Claude, and compared the recorded `cwd` case- and slash-sensitively. Claude turns **every** character outside `[A-Za-z0-9]` into `-` (`helpers/claude_tasks.encode_project_path`, verified against 23 real directories), so any path with a space or a `.claude/worktrees/` segment missed. The script fixes it by editing two strings in Cursor's installed `workbench.glass.main.js` and writing two flags into its `state.vscdb`.

**The Manager does not run, ship or depend on that.** It rewrites another application's installed files, breaks on any Cursor update (the script stops with "pattern not found"), needs Cursor fully closed, and is tied to one version's code and one machine's paths. The handoff above needs none of it and works whether or not Cursor fixes the bug. The patched importer lists a session only when the folder it ran in is the **open workspace**, which is why a worktree's chats appear only after opening that worktree (Tasks tab: *Open folder in Cursor*).

Measured facts that other work relies on: Cursor writes a transcript for every run, hooks or not, at `~/.cursor/projects/<name>/agent-transcripts/<id>/<id>.jsonl` where `<name>` collapses each run of non-alphanumerics to one dash (`D:\_scratch\hooks-test` is `D-scratch-hooks-test`, **unlike** Claude's encoding), so a transcript is found by its id, never by rebuilding the folder name. In a **headless** `agent -p` run only the `sessionStart` and `sessionEnd` hooks fired; `beforeSubmitPrompt`, `afterAgentResponse` and `stop` did not. `sessionEnd` carries `session_id`, `reason`, `final_status` and `transcript_path` (and `CURSOR_TRANSCRIPT_PATH` is set). Project hooks run with the project root as the working directory.

### Local, git-ignored instructions

Measured 2026-10-31 with a canary line in each of two git-excluded files: Claude Code loaded `CLAUDE.local.md`; Cursor loaded **both** `CLAUDE.local.md` and `.cursor/rules/*.mdc`. So a single `CLAUDE.local.md` reaches both agents, and a second Cursor-specific file would load the same instruction twice (the same duplication the baseline had). Observed on one machine with one Cursor configuration; Cursor does not promise it. The file is hidden from git with `.git/info/exclude`, resolved by `git rev-parse --git-path` so a worktree finds the right one, and that file is local to the clone and changes nothing in the repository.

### The Cursor `sessionEnd` hook

`helpers/cursor_hooks.py` builds (and, only on a person's yes, writes) one `sessionEnd` entry in `~/.cursor/hooks.json` plus `~/.cursor/hooks/tokensave_session_marker.py`. Measured in a headless run: the payload's `workspace_roots` is a list of plain Windows paths, and `reason` / `final_status` are `completed`. **Not yet measured: a USER-level `hooks.json`.** The project-level one was exercised; the user-level one is documented to run from `~/.cursor/` and is exercised for the first time by the check made right after installing (one headless run in a scratch project, then read the ledger). It fires for every Cursor session on the machine, so the Manager's CLI refuses (quietly, with the prerequisite exit code) for any project without the handoff rule, and while `enable_agent_handoff` is off nothing is written anywhere.

### Models, and tools in a headless call

**Models.** `agent --list-models` (needs a login) is read by `helpers/cursor_models.py` when you click *Refresh list* in Settings → Paths & Tools. Measured on this account: 270 models. The list is what Cursor accepts as a name, **not** what a plan allows: `gpt-5.4-nano-none` is listed and was refused ("not included in your weekly usage"). A pinned model that is missing from the list is kept and reported; with no usable list (not signed in, CLI missing, unreadable output) membership is *unknown*, never "not listed".

**Tools.** A headless Cursor call can reach tokensave only with `--approve-mcps` (the tools become visible) and `--force` (each call approved). `--force` is "run everything", so `build_print_argv(..., mcp_tools=True)` refuses to add it unless the run is read-only AND the agent's row has a read-only mode (`--mode ask`). Measured once, on one model: with all of those set, a request to write a file and to run a shell command was refused and nothing was created. That is evidence, not proof the block is in the tool layer. No Manager feature requests tools yet; this is the guarded capability the first one will use.

There is no fallback policy yet. One is added when something needs it, as an explicit setting; running a job with an agent the person did not pick is the failure this table exists to prevent. Every print-mode call from the Manager still runs read-only for Cursor (`--mode ask`).

### Two asymmetries, stated rather than papered over

**Cursor has no `--append-system-prompt`.** For `system_prompt_mode = prepend`
the system prompt is folded into the user prompt with one frozen serialization
(`PREPEND_TEMPLATE` in `agent_cli.py`):

```
[System instructions]
<system prompt>

[User request]
<user prompt>
```

Frozen as a constant because two agents drifting into two different prompt
structures is invisible until their output quality diverges and nobody can say
why. The fallback **never** fires for a `native` agent — Claude keeps using its
flag, and prepending as well would duplicate the instructions.

**An argv-transport agent has a size budget.** (Cursor was one until it was measured to read stdin; the guard stays for the next agent that is.) Claude uses
stdin precisely to dodge Windows argv mangling, and this manager's prompts are
routinely multi-KB. The budget belongs to the **runner**, not the agent, because
the two routes reach the OS differently:

| Runner | Route | Practical limit |
|---|---|---|
| `call_print` | argv list → `CreateProcess` | ~32767 chars |
| `spawn` / `spawn_interactive` | `cmd.exe /k "..."` | ~8191 chars |

The check measures the **rendered** command line, so quoting expansion is
counted rather than estimated. Over budget returns a distinct
`"prompt too large for positional CLI input"`. It never truncates, never
silently drops the prompt, and never pretends stdin is available. Windows only:
a POSIX `ARG_MAX` ceiling would invent a failure the platform does not have.

### Verification status

**Live-verified 2026-10-31** against `agent` 2026.10.22-27dcf5f on the machine this was built on (installer, login, then headless turns with `cursor-grok-4.5-low`, the one model family this account's plan allowed):

| Fact | Measured |
|---|---|
| Install location | `%LOCALAPPDATA%\cursor-agent\{agent,cursor-agent}.cmd`; nothing in `~/.local/bin`; the folder is added to the USER path, so a process started earlier does not see it |
| stdin prompt with `-p` | accepted; the reply carried a unique token. An auth error would NOT have proved it (it arrives before parsing) |
| `--mode ask` with `-p` | works |
| headless trust | `-p` needs `--trust` or it stops at a prompt |
| `--output-format json` | carries `usage` (input, output, cache tokens) |
| models | `agent --list-models` / `agent models` need login; a listed model can still be refused by plan ("GPT-5.4 Nano is not included in your weekly usage") |
| MCP | `.cursor/mcp.json` is seen; the server shows `needs approval` until `agent mcp enable tokensave`; `agent mcp list-tools tokensave` lists 12 tools |
| MCP in a headless turn | needs `--approve-mcps` (tools visible) AND `--force` (per-call approval); with `--mode ask` the turn stays read-only. A headless tool call then answered from the worktree's own index |
| worktrees | the CLI has `-w/--worktree` and `--worktree-base` |

Still **fixture-verified only**: everything below the next paragraph.

**Every other Cursor path in this manager is fixture-verified, not live-verified.**
Cursor was not installed on the machine this was built on, and there is no
Cursor subscription on the account. Those are two different blockers, and they
gate different things, so the open items are split by what each actually needs.

**Answerable by installing the CLI alone** — no IDE, no login, no plan.
Argument parsing happens before authentication, which is what makes the most
important one free to settle:

* **Whether `cursor-agent -p` reads the prompt from stdin.** Run
  `echo hi | cursor-agent -p`. A *"missing prompt"* error means stdin is NOT
  read, so `prompt_transport` stays `argv` and the command-line budget above is
  load-bearing. An auth or credit error means the prompt WAS accepted from
  stdin — flip `prompt_transport` to `stdin`, and the budget stops applying to
  Cursor (the guard stays for any future argv-only agent). This governs whether
  multi-KB prompts from code review and test generation silently hit a ceiling,
  so it is the first thing to measure.
* The exact binary filenames the Windows installer writes to `~/.local/bin`
  (assumed `cursor-agent` and `agent`; only the former is trusted against PATH).
  If these are wrong, `_detect_cursor_cli` returns "" forever and the feature is
  invisible rather than broken.
* The real `--help` spellings of `print_args`, `--model`, `--output-format`.
* That `tokensave install --agent cursor` writes `~/.cursor/mcp.json`, and that
  the MCP dialog's Cursor panel renders its populated branch rather than the
  "not detected" line.

**Needs a working agent** — i.e. an account that can complete a turn. Cursor's
published pricing lists only paid tiers, and the headless CLI appears to be tied
to a Pro-level key, so treat "the free tier can run a turn" as unresolved:

* the `~/.cursor/chats/*/meta.json` schema. This is the one item built on
  community reverse-engineering rather than documentation, so it is the one that
  most deserves a live check — and also the one that fails most safely, since
  `helpers/cursor_tasks.py` is version-gated, skips bad records individually and
  logs why.
* end-to-end generation: a commit message, a PR draft or a review actually
  produced by Cursor rather than Claude.

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

**CLI path** is set in Settings → Paths & Tools → Claude Code CLI.  An empty path returns a
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
`cfg.raw["enable_llm_grounding"]`. Settings → AI →
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
