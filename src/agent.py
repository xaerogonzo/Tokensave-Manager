"""
agent.py — LocalAgent for the Stage 2 chat tab (🤖 Ask).

A small, bounded tool-calling loop. Custom stdlib implementation per the
architectural rules in `docs/AGENT_ARCHITECTURE.md`:

  - No agent frameworks (no LangChain, LlamaIndex, openai-agents-sdk)
  - Stdlib HTTP only (urllib.request)
  - Bounded iterations (default 8) so a misbehaving model can't spin forever
  - All write tools (Stage 3+) go through ProposalDialog; v1 has no write tools
  - Async with cancellation: caller runs LocalAgent.run() on a daemon thread,
    cancellation via a threading.Event the agent checks between iterations
  - Tool exceptions never crash the agent — they're caught and fed back as
    role:"tool" content so the model can self-correct
  - Cumulative tool-output context budget prevents 8 iterations × 50 KB from
    blowing past local-model context windows

This module does its OWN HTTP (not via helpers/llm.py's _call_llm) because
tool calling requires inspecting the raw response for tool_calls, which
_call_llm doesn't expose. The provider dispatch mirrors _call_llm's
openai_compatible branch.

Anthropic provider is not currently supported for tool calling in this v1 —
the agent falls back to a one-shot completion (no tools) and surfaces a UI
note. This is a deliberate scope cut, not a fundamental limitation; adding
Anthropic tool-use is a known follow-up.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Callable

try:
    # Normal in-tree import (when invoked from src/).
    from agent_tools import ToolSpec, tools_as_openai_array
except ImportError:
    # Defensive: bundled Nuitka build may flatten imports.
    from .agent_tools import ToolSpec, tools_as_openai_array  # type: ignore

log = logging.getLogger("tokensave-manager.agent")

# How many chat-completion round trips the agent may make per .run() call.
DEFAULT_MAX_ITERATIONS = 8

# Cumulative byte budget for `role:"tool"` messages in the conversation.
# When the sum exceeds this, oldest tool outputs get replaced with a
# placeholder before the next LLM request. 40 000 chars ≈ 10 000 tokens
# which leaves room for system prompt + history + new completion inside a
# 32k-context local model.
DEFAULT_TOOL_BUDGET_CHARS = 40_000

# Per-request HTTP timeout. Tool-calling round trips include the model's
# reasoning + JSON-encoded args, AND each iteration's context grows
# (prior tool results accumulate). On slow local hardware running a 14B
# model, the third or fourth iteration of a non-trivial task can take
# 3-5 minutes per round trip — 300 seconds is a comfortable ceiling
# that still surfaces "the server actually hung" within a reasonable
# wait. Tunable via cfg["timeout_seconds"]; the agent floors anything
# under 60 s back up to the default since shorter values reliably break
# on tool-call sequences.
DEFAULT_HTTP_TIMEOUT = 300


# Cap on how many balanced-braces substrings we'll consider during
# tool-call rescue. Most assistant messages have at most one tool-call
# JSON object; this cap just prevents a pathological response with
# hundreds of `{`s from causing quadratic scanning.
_MAX_RESCUE_CANDIDATES = 16


def _extract_balanced_json_substrings(text: str) -> list[str]:
    """Return all syntactically valid JSON object substrings (top-level
    `{...}` blocks) found anywhere in `text`, ordered by length descending.

    Uses a simple brace-counter to find balanced regions, then tries
    json.loads on each. Designed for rescuing tool calls out of mixed
    prose-plus-JSON content emitted by local models. Naive single-pass
    scanner — not aware of strings (so an unescaped `}` inside a JSON
    string could confuse it), but json.loads validation downstream
    catches any false-positives.

    Bounded by `_MAX_RESCUE_CANDIDATES` to keep the worst case linear
    in input size even when the text has many `{`s.
    """
    import json as _json
    if not text:
        return []
    results: list[str] = []
    n = len(text)
    i = 0
    while i < n and len(results) < _MAX_RESCUE_CANDIDATES:
        if text[i] != "{":
            i += 1
            continue
        # Walk forward maintaining a brace counter. When it returns to 0
        # we've found a balanced region; try to parse it.
        depth = 0
        j = i
        while j < n:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    try:
                        _json.loads(candidate)
                        results.append(candidate)
                    except _json.JSONDecodeError:
                        pass
                    break
            j += 1
        # Advance past this `{` whether or not the region parsed.
        i += 1
    # Longest first — improves the odds that the first match is the
    # actual tool call rather than a small `{"path": "x"}` substring
    # nested inside it.
    results.sort(key=len, reverse=True)
    return results


def _is_tool_error(result: str) -> bool:
    """True when a tool result string indicates a failure that should NOT cache.

    Tool handlers signal failures with the `[tool error]` prefix (per the
    convention in agent_tools.py). `[tool note]` and successful outputs are
    cacheable. We never want to memoize a transient failure across cache
    hits within the same run.
    """
    head = result.lstrip()[:32].lower() if isinstance(result, str) else ""
    return head.startswith("[tool error]")


# Two-pass redaction. The header regex eats everything from the colon to the
# next whitespace AFTER a leading bearer keyword (if present), so
# "Authorization: Bearer sk-abc..." doesn't leak the token half. The bearer
# regex catches bare "Bearer <token>" sequences not preceded by a header name.
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|x-api-key|api[-_ ]?key)\s*[:=]\s*"
    r"(?:bearer\s+)?\S+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")


def _sanitize_error(text: str) -> str:
    """Compress an exception/HTTP error message into one safe chat-friendly line.

    - Trims to the first non-empty line (strips stack traces and verbose bodies).
    - Removes anything matching Authorization: / api-key: / Bearer <token>.
    - Caps length at 240 chars.

    Used by the loop-stall surface (iteration cap / unrecoverable HTTP error)
    so the chat window never receives raw tracebacks or partial auth headers.
    """
    if not text:
        return ""
    first = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    cleaned = _HEADER_SECRET_RE.sub(r"\1: <redacted>", first)
    cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
    return cleaned[:240]


# ───────────────────────────────────────────────────────────────────────
# LocalAgent
# ───────────────────────────────────────────────────────────────────────

class LocalAgent:
    """Bounded tool-calling loop over an OpenAI-compatible chat completion.

    Construction is cheap — make a fresh agent per Ask-tab conversation.
    `run()` blocks the calling thread until the loop terminates (either
    the model returned a final answer, the iteration cap was hit, the
    stop_event fired, or an HTTP error occurred). Callers must run this
    on a daemon thread.

    Streaming notes: this v1 does NOT stream individual deltas during tool
    calls (tool-call args arrive as a single chunk in non-streaming
    responses, and streaming partial tool-call args is provider-specific
    and fiddly). For the FINAL assistant message after the tool loop ends,
    streaming WOULD be valuable — but adding it later is non-breaking, so
    we ship without first.
    """

    # Read-only deterministic tools whose results are safe to cache for the
    # duration of a single run(). Excludes read_file / list_directory / git_*
    # because filesystem and repo state can change mid-run (write_file tool,
    # hooks, user editing externally). tokensave_* queries hit the indexed DB
    # which doesn't see disk changes until re-indexed, so they're stable.
    _CACHEABLE_TOOLS = frozenset({"tokensave_search", "tokensave_context"})

    def __init__(self, cfg: dict, project_path: str,
                 tools: dict[str, ToolSpec],
                 max_iterations: int = DEFAULT_MAX_ITERATIONS,
                 tool_budget_chars: int = DEFAULT_TOOL_BUDGET_CHARS,
                 on_write_proposal: Callable | None = None):
        """
        on_write_proposal: Optional bridge for is_write=True tools. Signature:
            (proposal: WriteProposal) -> tuple[bool, str | None]
        Called from the agent worker thread (this thread). Implementation
        is responsible for opening the ProposalDialog on the Tk main thread,
        blocking on user response, and returning (accepted, final_content).
        If None, write tools are refused with `[tool error]` — preserves
        the original read-only-agent invariant for callers that don't
        opt into write support.
        """
        self.cfg = cfg
        self.project_path = project_path
        self.tools = tools
        self.max_iterations = max_iterations
        self.tool_budget_chars = tool_budget_chars
        self._on_write_proposal = on_write_proposal
        # Populated by _chat_completion on the failure path so callers
        # can surface the actual exception detail (HTTP code + body,
        # network error, JSON parse failure) instead of a generic
        # "LLM request failed" message.
        self._last_error: str | None = None
        # Per-run tool-call cache (cleared at the top of each run()).
        # ONLY deterministic read-only tools are cached — see _CACHEABLE_TOOLS.
        # Filesystem, git, and read_file results are NOT cached because they
        # can legitimately change within a single agent run (write_file,
        # hooks, or the user editing files externally).
        self._tool_cache: dict[str, str] = {}

    # ── Public entry point ──────────────────────────────────────────────

    def run(self, messages: list[dict],
            on_tool_call: Callable[[str, dict], None] | None = None,
            on_tool_result: Callable[[str, str], None] | None = None,
            on_assistant_message: Callable[[str], None] | None = None,
            on_done: Callable[[str | None], None] | None = None,
            on_error: Callable[[str], None] | None = None,
            on_stream_delta: Callable[[str], None] | None = None,
            stop_event: threading.Event | None = None) -> None:
        """Run the tool-calling loop. Calls callbacks for UI updates.

        Parameters
        ----------
        messages
            OpenAI-format message list. Caller is responsible for the
            system prompt and the user's question. The agent appends
            assistant + tool messages to this list as the loop progresses
            (mutates in place — caller can read it back).
        on_tool_call(name, args)
            Called BEFORE a tool runs. Lets the UI log it.
        on_tool_result(name, result_string)
            Called AFTER a tool runs with the result string. Lets the UI
            log it in a collapsible row.
        on_assistant_message(text)
            Called whenever the assistant emits a non-streamed text message
            (interleaved between tool calls). NOT called for the streamed
            final answer when on_stream_delta is provided.
        on_done(final_text)
            Called once at the end of the run. `final_text` is the last
            assistant text, or None if no text was produced (e.g. all the
            iterations were tool calls and the cap was hit).
        on_error(message)
            Called instead of on_done when an unrecoverable error
            occurred (network failure, malformed response, etc.).
        on_stream_delta(chunk)
            If provided, enables streaming mode. Each token chunk from the
            LLM is forwarded here in real time. `on_assistant_message` is
            NOT called for turns where streaming was active (to prevent
            double-printing). Tool-call turns still stream, but only fire
            on_stream_delta for any content text the model emits alongside
            the tool_calls (uncommon but possible).
        stop_event
            Optional Event; if set, the loop aborts cleanly at the next
            iteration boundary (between LLM round trips).
        """
        provider = (self.cfg.get("provider") or "anthropic").lower()
        if provider == "anthropic":
            # No tool calling in v1 Anthropic path — fall back to one-shot.
            self._run_anthropic_oneshot(
                messages, on_assistant_message, on_done, on_error)
            return

        # Per-run tool cache: empty at the start of every conversation turn so
        # tokensave_search/tokensave_context results don't leak across turns.
        self._tool_cache.clear()
        last_assistant_text: str | None = None
        try:
            for _iteration in range(self.max_iterations):
                if stop_event is not None and stop_event.is_set():
                    if on_done:
                        on_done(last_assistant_text)
                    return

                self._enforce_tool_budget(messages)

                turn = self._run_one_turn(messages, on_stream_delta, on_assistant_message)
                if turn is None:
                    if on_error:
                        detail = _sanitize_error(self._last_error) or (
                            "no detail (check the manager's log file for "
                            "tokensave-manager.agent warnings)")
                        on_error(f"LLM request failed.  {detail}")
                    return
                content, tool_calls, last_text = turn
                if last_text:
                    last_assistant_text = last_text

                if not tool_calls:
                    if on_done:
                        on_done(content or last_assistant_text)
                    return

                self._execute_tool_calls(tool_calls, messages, on_tool_call, on_tool_result)
                # Loop again — model gets to see the tool outputs.

            # Iteration cap exhausted.
            if on_error:
                tail = _sanitize_error(self._last_error)
                detail = f"  Last error: {tail}" if tail else ""
                on_error(
                    f"Iteration cap ({self.max_iterations}) reached. "
                    "The model kept calling tools without producing a "
                    "final answer — try rephrasing the question, or "
                    f"raise max_iterations.{detail}")
        except Exception as e:
            log.exception("LocalAgent.run failed")
            if on_error:
                on_error(_sanitize_error(f"Agent crashed: {type(e).__name__}: {e}"))

    # ── Turn execution ──────────────────────────────────────────────────

    def _run_one_turn(
        self,
        messages: list[dict],
        on_stream_delta: "Callable[[str], None] | None",
        on_assistant_message: "Callable[[str], None] | None",
    ) -> "tuple[str | None, list, str | None] | None":
        """Execute one LLM round-trip (streaming or non-streaming).

        Returns ``(content, tool_calls, last_text)`` on success, ``None`` on
        LLM failure (caller should inspect ``self._last_error``).
        """
        if on_stream_delta is not None:
            result = self._chat_completion_stream(messages, on_stream_delta)
            if result is None:
                return None
            content, tool_calls = result
            # Append assistant turn to history (mirrors _parse_response).
            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            # on_assistant_message intentionally skipped — content was already
            # forwarded token-by-token via on_stream_delta.
            return content, tool_calls, content or None

        response = self._chat_completion(messages)
        if response is None:
            return None
        content, tool_calls = self._parse_response(response, messages)
        if content and on_assistant_message:
            on_assistant_message(content)
        return content, tool_calls, content or None

    # ── Response parsing ────────────────────────────────────────────────

    def _parse_response(self, response: dict,
                        messages: list[dict]) -> tuple[str, list]:
        """Extract content and tool_calls from a chat completion response.

        Appends the assistant message to `messages` (required by the API
        before any role:tool replies). Runs the tool-call rescue heuristic
        for local models that emit tool calls as JSON in the content field.
        Returns (content, tool_calls) — both empty/[] on an empty response.
        """
        choice = (response.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        # Always append the assistant message (even when only tool_calls —
        # the API requires the assistant message with tool_calls to precede
        # the role:tool replies).
        assistant_msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # ── Tool-call rescue ──────────────────────────────────────────────
        # Some local models (notably qwen2.5-coder via Ollama) emit the tool
        # call as JSON in the assistant content field instead of using the
        # proper tool_calls structure. Parse and synthesise a tool_calls
        # entry so the rest of the loop can proceed normally.
        if not tool_calls and content:
            rescued = self._rescue_tool_call_from_content(content)
            if rescued is not None:
                log.info("rescued tool_call from assistant content: "
                         f"{rescued.get('function', {}).get('name')}")
                tool_calls = [rescued]
                messages[-1]["tool_calls"] = tool_calls

        return content, tool_calls

    def _execute_tool_calls(self, tool_calls: list[dict], messages: list[dict],
                            on_tool_call, on_tool_result) -> None:
        """Execute each tool call in `tool_calls`, fire callbacks, and
        append role:tool reply messages to `messages`."""
        for call in tool_calls:
            call_id = call.get("id") or ""
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}

            if on_tool_call:
                try:
                    on_tool_call(name, args)
                except Exception:
                    log.exception("on_tool_call callback raised")

            cache_key = self._cache_key_for(name, args)
            cached = self._tool_cache.get(cache_key) if cache_key else None
            if cached is not None:
                result = cached
                # Annotate ONLY the UI callback — the LLM still sees the
                # original payload verbatim so it doesn't over-focus on the
                # "[cached]" marker and parrot it into its reasoning.
                display = "[cached]\n" + result
            else:
                result = self._dispatch_tool(name, args)
                display = result
                if cache_key and not _is_tool_error(result):
                    self._tool_cache[cache_key] = result

            if on_tool_result:
                try:
                    on_tool_result(name, display)
                except Exception:
                    log.exception("on_tool_result callback raised")

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result,
            })

    def _cache_key_for(self, name: str, args: dict) -> str | None:
        """Return a stable cache key for cacheable tool calls, or None.

        Uses sort_keys=True on the args JSON so two LLM calls with identical
        arguments in different key orders share a cache entry. Returns None
        for tools NOT in _CACHEABLE_TOOLS so the caller skips the cache path.
        """
        if name not in self._CACHEABLE_TOOLS:
            return None
        import hashlib
        try:
            args_blob = json.dumps(args or {}, sort_keys=True,
                                    ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None
        return f"{name}:{hashlib.md5(args_blob.encode('utf-8')).hexdigest()}"

    # ── Tool dispatch ───────────────────────────────────────────────────

    # ── Tool-call rescue helper ─────────────────────────────────────────

    @staticmethod
    def _rescue_tool_call_from_content(content: str) -> dict | None:
        """Try to parse `content` as a JSON-encoded tool call and return
        a synthesised tool_calls[] entry, or None if the content isn't a
        tool call.

        Local models — especially qwen2.5-coder, llama3.1, and several
        Mistral variants — frequently emit tool calls as JSON in the
        assistant `content` field instead of using the proper structured
        `tool_calls` array. This is a known training-data quirk: the
        models are fine-tuned on examples where tool calls are *visible*
        JSON, and they emit them in that shape regardless of whether the
        provider wraps them in tool_calls or not.

        Worse, they often surround the JSON with explanatory prose
        ("To understand the structure, I'll list the root directory…")
        and wrap it in ```json``` fences. This extractor tries several
        strategies to find the actual tool-call object:

          1. Treat the whole content as JSON.
          2. Find any ```json…``` or ```…``` fenced block, parse its body.
          3. Find the longest valid balanced-braces JSON substring.

        For each candidate it checks four object shapes:
          • {"name": "tool_name", "arguments": {...}}
          • {"tool": "tool_name", "arguments": {...}}
          • {"name": "tool_name", "parameters": {...}}
          • {"function": {"name": "tool_name", "arguments": {...}}}

        Returns a dict shaped like a real tool_calls[] entry:
          {"id": "rescued-<uuid>",
           "type": "function",
           "function": {"name": "X", "arguments": "<json-string>"}}

        Note that `arguments` is a STRING (matching the OpenAI spec —
        downstream code expects json.loads() on it). Returns None when
        no candidate parses or none has a valid tool-call shape.
        """
        import json as _json
        import uuid as _uuid
        import re as _re
        if not content or not isinstance(content, str):
            return None

        # ── Collect candidate JSON object strings ──────────────────────
        candidates: list[str] = []

        # 1. The whole content (after a quick strip).
        whole = content.strip()
        if whole.startswith("{") and whole.endswith("}"):
            candidates.append(whole)

        # 2. Anything inside a ```json ... ``` (or plain ``` ... ```) fence.
        #    Multiple fences supported — try each in order.
        for m in _re.finditer(
                r"```(?:json|JSON)?\s*\n?(.*?)\n?```",
                content, _re.DOTALL):
            body = m.group(1).strip()
            if body.startswith("{") and body.endswith("}"):
                candidates.append(body)

        # 3. The longest balanced-braces JSON substring anywhere in the
        #    content. Scans for `{` starts, then for each tries to parse
        #    progressively-longer substrings ending at each `}`. Picks
        #    the longest that parses as JSON. Bounded — caps the number
        #    of scan iterations to avoid pathological prose with many
        #    braces.
        candidates.extend(_extract_balanced_json_substrings(content))

        # ── Try each candidate ─────────────────────────────────────────
        for cand in candidates:
            try:
                obj = _json.loads(cand)
            except _json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            # Unwrap the {"function": {...}} variant.
            if "function" in obj and isinstance(obj["function"], dict):
                obj = obj["function"]
            # Find the tool name.
            name = obj.get("name") or obj.get("tool")
            if not isinstance(name, str) or not name:
                continue
            # Find the args.
            args = obj.get("arguments")
            if args is None:
                args = obj.get("parameters")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                continue
            return {
                "id": f"rescued-{_uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": name,
                    # OpenAI spec requires arguments as a JSON STRING.
                    "arguments": _json.dumps(args),
                },
            }
        return None

    def _dispatch_tool(self, name: str, args: dict) -> str:
        """Run the named tool and return its result as a string.

        Master try/except — anything the handler raises becomes a
        `[tool error] ...` string the model sees, never crashes the loop.

        Read tools (is_write=False) run via spec.handler directly.
        Write tools (is_write=True) are routed through three steps:
          1. spec.proposal_builder(args) -> WriteProposal | error str
          2. self._on_write_proposal(proposal) -> (accepted, content)
             (blocks the worker until user resolves the dialog)
          3. spec.post_accept(proposal, content) -> result str
        If the user rejects (or no bridge is configured), the write is
        refused and the model sees a `[write rejected ...]` string.
        """
        spec = self.tools.get(name)
        if spec is None:
            return (f"[tool error] unknown tool: {name}. "
                    f"Available: {', '.join(sorted(self.tools.keys()))}")
        if spec.is_write:
            return self._dispatch_write_tool(name, spec, args)
        try:
            result = spec.handler(args)
            if result is None:
                return "(tool returned no output)"
            return str(result)
        except Exception as e:
            log.exception("tool '%s' raised", name)
            return f"[tool error] {type(e).__name__}: {e}"

    def _dispatch_write_tool(self, name: str, spec: ToolSpec, args: dict) -> str:
        """Route an is_write=True tool through the proposal bridge."""
        if spec.proposal_builder is None or spec.post_accept is None:
            return (f"[tool error] write tool '{name}' is missing "
                    "proposal_builder or post_accept callable. This is a "
                    "registry-side defect; refusing to execute.")
        if self._on_write_proposal is None:
            return (f"[tool error] write tool '{name}' requires a "
                    "ProposalDialog bridge but none was configured for this "
                    "agent run. Refusing to execute.")
        try:
            proposal_or_err = spec.proposal_builder(args)
        except Exception as e:
            log.exception("write tool '%s' proposal_builder raised", name)
            return f"[tool error] {type(e).__name__}: {e}"
        if isinstance(proposal_or_err, str):
            # Validation failure — no dialog, no write
            return proposal_or_err
        proposal = proposal_or_err
        try:
            accepted, final_content = self._on_write_proposal(proposal)
        except Exception as e:
            log.exception("write tool '%s' bridge raised", name)
            return f"[tool error] proposal bridge failed: {type(e).__name__}: {e}"
        if not accepted:
            return "[write rejected by user]"
        try:
            result = spec.post_accept(proposal, final_content or "")
            return result if result is not None else "(write accepted, no output)"
        except Exception as e:
            log.exception("write tool '%s' post_accept raised", name)
            return f"[tool error] write failed: {type(e).__name__}: {e}"

    # ── Context-budget trimmer ──────────────────────────────────────────

    def _enforce_tool_budget(self, messages: list[dict]) -> None:
        """If the cumulative byte size of role:tool messages exceeds the
        budget, replace the OLDEST tool outputs with a short placeholder
        until under budget. Mutates `messages` in place.

        The most recent tool outputs are usually the ones the model needs
        to reference in its next reply, so we evict from the front. System
        message, user turns, and assistant messages are never trimmed.
        """
        budget = self.tool_budget_chars
        total = sum(
            len(m.get("content") or "") for m in messages if m.get("role") == "tool")
        if total <= budget:
            return

        # Indices of tool messages, oldest first.
        tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        for idx in tool_idxs:
            if total <= budget:
                break
            m = messages[idx]
            original_len = len(m.get("content") or "")
            name = m.get("name") or "tool"
            placeholder = (f"[earlier {name} output omitted — context "
                           f"budget exceeded; ask again with a more specific "
                           f"query if you need this data]")
            if len(placeholder) >= original_len:
                # Trimming wouldn't help. Move on.
                continue
            m["content"] = placeholder
            total -= (original_len - len(placeholder))

    # ── HTTP: OpenAI-compatible chat completion with tools ──────────────

    def _chat_completion_stream(
            self, messages: list[dict],
            on_delta: Callable[[str], None] | None,
    ) -> "tuple[str, list] | None":
        """Streaming variant of _chat_completion.

        Sends `stream: true` to the provider, reads SSE chunks, calls
        `on_delta(text)` per content chunk, accumulates tool_call deltas,
        and returns `(assembled_content, assembled_tool_calls)` — the same
        logical result as `_parse_response` but assembled from the stream.

        Runs the tool-call rescue heuristic on the assembled content when no
        structured tool_calls were received (for local models that emit JSON
        tool calls in the content field).

        Returns None and sets self._last_error on any transport or parse error.
        """
        provider = (self.cfg.get("provider") or "anthropic").lower()
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        timeout  = int(self.cfg.get("timeout_seconds", DEFAULT_HTTP_TIMEOUT))
        if timeout < 60:
            timeout = DEFAULT_HTTP_TIMEOUT
        provider, base_url, is_ollama = self._normalize_provider(provider, base_url)
        url = self._resolve_chat_url(provider, base_url)
        if url is None:
            return None

        api_key_env = self.cfg.get("api_key_env") or ""
        api_key  = os.environ.get(api_key_env, "") if api_key_env else ""
        model    = self.cfg.get("model") or ""

        payload = self._build_chat_payload(messages, is_ollama, model)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        result = self._execute_chat_stream(url, payload, headers, timeout, on_delta)
        if result is None:
            return None

        content, tool_calls = result
        # Rescue heuristic for models that emit tool calls as JSON in content.
        if content and not tool_calls:
            rescued = self._rescue_tool_call_from_content(content)
            if rescued is not None:
                log.info("rescued tool_call from streamed content: "
                         f"{rescued.get('function', {}).get('name')}")
                tool_calls = [rescued]
        return content, tool_calls

    def _execute_chat_stream(
            self, url: str, payload: dict,
            headers: dict, timeout: int,
            on_delta: Callable[[str], None] | None,
    ) -> "tuple[str, list] | None":
        """POST a streaming /v1/chat/completions request and read SSE chunks.

        Returns (assembled_content, assembled_tool_calls) or None on error.
        Calls on_delta(chunk) for each non-empty content delta received.

        tool_calls are accumulated by index: each chunk may carry a partial
        `id`, `name`, or `arguments` fragment; this method merges them into
        complete tool-call dicts in the OpenAI tool_calls[] shape.
        """
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        content_parts: list[str] = []
        # tool_calls_acc: index → {"id": str, "name": str, "args": list[str]}
        tool_calls_acc: dict[int, dict] = {}
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                    text = delta.get("content") or ""
                    if text:
                        content_parts.append(text)
                        if on_delta:
                            on_delta(text)
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index") or 0
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "", "name": "", "args": []}
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        if fn.get("name"):
                            tool_calls_acc[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls_acc[idx]["args"].append(fn["arguments"])
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except OSError:
                body = "(no body)"
            self._last_error = (
                f"HTTP {e.code} from {url}: {e.reason}. "
                f"Response body: {body}")
            log.warning("chat stream HTTP %d: %s", e.code, body[:200])
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", str(e))
            self._last_error = (
                f"Network error talking to {url}: {type(e).__name__}: "
                f"{reason}. Is the LLM server running and reachable?")
            log.warning("chat stream network failure: %s: %s",
                        type(e).__name__, reason)
            return None
        except (OSError, json.JSONDecodeError) as e:
            self._last_error = f"Stream read error: {type(e).__name__}: {e}"
            log.warning("chat stream error: %s", e)
            return None

        assembled_content = "".join(content_parts)
        assembled_tool_calls = [
            {
                "id": tool_calls_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_calls_acc[i]["name"],
                    "arguments": "".join(tool_calls_acc[i]["args"]),
                },
            }
            for i in sorted(tool_calls_acc)
            if tool_calls_acc[i]["name"]
        ]
        return assembled_content, assembled_tool_calls

    def _chat_completion(self, messages: list[dict]) -> dict | None:
        """POST one /v1/chat/completions request and return parsed JSON or None.

        Resolves provider settings, builds the payload, executes the request.
        On any failure, stores a human-readable message on self._last_error.
        """
        provider = (self.cfg.get("provider") or "anthropic").lower()
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        model    = self.cfg.get("model") or ""
        api_key_env = self.cfg.get("api_key_env") or ""
        api_key  = os.environ.get(api_key_env, "") if api_key_env else ""
        timeout  = int(self.cfg.get("timeout_seconds", DEFAULT_HTTP_TIMEOUT))
        if timeout < 60:
            timeout = DEFAULT_HTTP_TIMEOUT

        provider, base_url, is_ollama = self._normalize_provider(provider, base_url)
        url = self._resolve_chat_url(provider, base_url)
        if url is None:
            return None

        payload = self._build_chat_payload(messages, is_ollama, model)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        return self._execute_chat_request(url, payload, headers, timeout)

    def _normalize_provider(
            self, provider: str, base_url: str) -> tuple[str, str, bool]:
        """Normalize 'ollama' to 'openai_compatible' and detect the Ollama port.

        Returns (provider, base_url, is_ollama). is_ollama drives the
        num_ctx injection in _build_chat_payload.
        """
        is_ollama = (provider == "ollama")
        if provider == "ollama":
            provider = "openai_compatible"
            if not base_url:
                base_url = "http://localhost:11434"
        # openai_compatible pointed at the default Ollama port needs num_ctx too.
        if not is_ollama and provider == "openai_compatible" \
                and base_url and "11434" in base_url:
            is_ollama = True
        return provider, base_url, is_ollama

    def _resolve_chat_url(self, provider: str, base_url: str) -> str | None:
        """Return the /v1/chat/completions URL, or None (sets self._last_error)."""
        if provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        if provider == "openai_compatible":
            if not base_url:
                self._last_error = (
                    "openai_compatible provider has no base_url set — "
                    "open Settings → AI commit messages and configure it.")
                return None
            return base_url + "/v1/chat/completions"
        self._last_error = (
            f"provider '{provider}' is not supported for tool calling. "
            "Use 'ollama', 'openai', or 'openai_compatible'.")
        return None

    def _build_chat_payload(
            self, messages: list[dict], is_ollama: bool, model: str) -> dict:
        """Assemble the /v1/chat/completions request body.

        Injects options.num_ctx for Ollama to work around the 2048-token
        default context window (tunable via cfg["num_ctx"]).
        """
        payload: dict = {
            "model": model or "qwen2.5-coder:14b",
            "messages": messages,
            "temperature": 0.2,
            "tools": tools_as_openai_array(self.tools),
            "tool_choice": "auto",
        }
        if is_ollama:
            num_ctx = int(self.cfg.get("num_ctx", 32768))
            payload["options"] = {"num_ctx": num_ctx}
        return payload

    def _execute_chat_request(
            self, url: str, payload: dict,
            headers: dict, timeout: int) -> dict | None:
        """POST payload to url; parse and return JSON, or set self._last_error."""
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Ollama / OpenAI put useful diagnostics in the body
            # ("context length exceeded", "model not found", etc.).
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except OSError:
                body = "(no body)"
            self._last_error = (
                f"HTTP {e.code} from {url}: {e.reason}. "
                f"Response body: {body}")
            log.warning("chat completion HTTP %d: %s", e.code, body[:200])
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", str(e))
            self._last_error = (
                f"Network error talking to {url}: {type(e).__name__}: "
                f"{reason}. Is the LLM server running and reachable?")
            log.warning("chat completion network failure: %s: %s",
                        type(e).__name__, reason)
        except json.JSONDecodeError as e:
            self._last_error = (
                f"Server at {url} returned non-JSON response: {e}. "
                "Provider misconfiguration or wrong URL?")
            log.warning("chat completion JSON decode failed: %s", e)
        except OSError as e:
            self._last_error = (
                f"OS error talking to {url}: {type(e).__name__}: {e}")
            log.warning("chat completion OS error: %s", e)
        return None

    # ── Anthropic fallback (no tools in v1) ─────────────────────────────

    def _run_anthropic_oneshot(self, messages: list[dict],
                                on_assistant_message,
                                on_done, on_error):
        """Anthropic provider doesn't support tool calling in v1 of the
        agent. Run a single Messages API call and return the answer."""
        # Compose system + user from the messages list. We treat the first
        # 'system' message (if any) as the system prompt; the rest become
        # the user content.
        sys_parts = []
        user_parts = []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if role == "system":
                sys_parts.append(text)
            elif role == "user":
                user_parts.append(text)
            elif role == "assistant":
                # Squash prior assistant turns into the conversation context.
                user_parts.append(f"[previous assistant turn]\n{text}")
        system_prompt = "\n\n".join(p for p in sys_parts if p)
        user_prompt = "\n\n".join(p for p in user_parts if p)

        api_key_env = self.cfg.get("api_key_env") or "ANTHROPIC_API_KEY"
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            if on_error:
                on_error("Anthropic API key not set. Set the "
                         f"{api_key_env} environment variable, or switch "
                         "to Ollama in Settings for tool-calling support.")
            return

        payload = {
            "model": self.cfg.get("model") or "claude-haiku-4-5",
            "max_tokens": 2000,
            "system": (system_prompt + "\n\nNOTE: tool calling is not "
                       "available for Anthropic in this version of the "
                       "manager. Answer the question using your own "
                       "knowledge and the context already in this prompt."),
            "messages": [{"role": "user", "content": user_prompt}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError, OSError) as e:
            if on_error:
                on_error(f"Anthropic request failed: {type(e).__name__}: {e}")
            return
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text").strip()
        if on_assistant_message and text:
            on_assistant_message(text)
        if on_done:
            on_done(text or None)
