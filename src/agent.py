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

This module does its OWN HTTP (not via tokensave-manager.py's _call_llm)
because tool calling requires inspecting the raw response for tool_calls,
which _call_llm doesn't expose. The provider dispatch mirrors _call_llm's
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

    def __init__(self, cfg: dict, project_path: str,
                 tools: dict[str, ToolSpec],
                 max_iterations: int = DEFAULT_MAX_ITERATIONS,
                 tool_budget_chars: int = DEFAULT_TOOL_BUDGET_CHARS):
        self.cfg = cfg
        self.project_path = project_path
        self.tools = tools
        self.max_iterations = max_iterations
        self.tool_budget_chars = tool_budget_chars
        # Populated by _chat_completion on the failure path so callers
        # can surface the actual exception detail (HTTP code + body,
        # network error, JSON parse failure) instead of a generic
        # "LLM request failed" message.
        self._last_error: str | None = None

    # ── Public entry point ──────────────────────────────────────────────

    def run(self, messages: list[dict],
            on_tool_call: Callable[[str, dict], None] | None = None,
            on_tool_result: Callable[[str, str], None] | None = None,
            on_assistant_message: Callable[[str], None] | None = None,
            on_done: Callable[[str | None], None] | None = None,
            on_error: Callable[[str], None] | None = None,
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
            Called whenever the assistant emits a text message (either
            interleaved between tool calls or as the final answer).
        on_done(final_text)
            Called once at the end of the run. `final_text` is the last
            assistant text, or None if no text was produced (e.g. all the
            iterations were tool calls and the cap was hit).
        on_error(message)
            Called instead of on_done when an unrecoverable error
            occurred (network failure, malformed response, etc.).
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

        last_assistant_text: str | None = None
        try:
            for iteration in range(self.max_iterations):
                if stop_event is not None and stop_event.is_set():
                    if on_done:
                        on_done(last_assistant_text)
                    return

                self._enforce_tool_budget(messages)

                response = self._chat_completion(messages)
                if response is None:
                    if on_error:
                        detail = self._last_error or (
                            "no detail (check the manager's log file for "
                            "tokensave-manager.agent warnings)")
                        on_error(f"LLM request failed.  {detail}")
                    return

                choice = (response.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                finish_reason = choice.get("finish_reason") or ""

                # Always append the assistant message to history (even if
                # only tool_calls — the API requires the assistant message
                # with tool_calls to precede the role:tool replies).
                assistant_msg = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                # ── Tool-call rescue ───────────────────────────────────
                # Some local models (notably qwen2.5-coder via Ollama) emit
                # the tool call as JSON in the assistant content field
                # instead of populating the proper tool_calls structure.
                # When we see no tool_calls but the content looks like a
                # tool-call JSON object, parse it and synthesise a
                # tool_calls entry so the rest of the loop can proceed
                # normally. Without this, the agent treats the JSON-as-
                # text as the final answer and dies.
                #
                # Detected shapes (handled in order):
                #   1. {"name": "tool", "arguments": {...}}
                #   2. {"tool": "tool", "arguments": {...}}
                #   3. Same as 1/2 but wrapped in markdown ```json fences
                #   4. Same as 1/2 but with `parameters` instead of `arguments`
                if not tool_calls and content:
                    rescued = self._rescue_tool_call_from_content(content)
                    if rescued is not None:
                        log.info("rescued tool_call from assistant content: "
                                 f"{rescued.get('function', {}).get('name')}")
                        tool_calls = [rescued]
                        # Re-write the assistant message we just appended
                        # so it carries the synthesised tool_calls. The
                        # text-content stays so on-screen rendering still
                        # shows what the model emitted.
                        messages[-1]["tool_calls"] = tool_calls

                if content and on_assistant_message:
                    on_assistant_message(content)
                    last_assistant_text = content

                if not tool_calls:
                    # Final answer.
                    if on_done:
                        on_done(content or last_assistant_text)
                    return

                # Execute each tool call and append the role:tool reply.
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

                    result = self._dispatch_tool(name, args)

                    if on_tool_result:
                        try:
                            on_tool_result(name, result)
                        except Exception:
                            log.exception("on_tool_result callback raised")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result,
                    })

                # Loop again — model gets to see the tool outputs.
            # Iteration cap exhausted.
            if on_error:
                on_error(f"Iteration cap ({self.max_iterations}) reached. "
                         "The model kept calling tools without producing a "
                         "final answer — try rephrasing the question, or "
                         "raise max_iterations.")
        except Exception as e:
            log.exception("LocalAgent.run failed")
            if on_error:
                on_error(f"Agent crashed: {type(e).__name__}: {e}")

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
        """
        spec = self.tools.get(name)
        if spec is None:
            return (f"[tool error] unknown tool: {name}. "
                    f"Available: {', '.join(sorted(self.tools.keys()))}")
        if spec.is_write:
            # Defence-in-depth: v1 should never have any is_write tools in
            # the registry. If somehow one slips in, refuse to execute
            # rather than write-without-approval.
            return (f"[tool error] tool '{name}' is marked is_write=True "
                    "but write tools require ProposalDialog gating, which "
                    "is not wired in this build. Refusing to execute.")
        try:
            result = spec.handler(args)
            if result is None:
                return "(tool returned no output)"
            return str(result)
        except Exception as e:
            log.exception("tool '%s' raised", name)
            return f"[tool error] {type(e).__name__}: {e}"

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

    def _chat_completion(self, messages: list[dict]) -> dict | None:
        """POST one /v1/chat/completions request with the tools array
        and return the parsed response dict, or None on failure.

        On failure, stores a detailed error message on `self._last_error`
        (and logs it) so the calling `run()` can pass that detail to
        `on_error` instead of a generic "LLM request failed" message.

        For Ollama: passes `options.num_ctx` (default 32768) to bump the
        per-request context window. Ollama's default `num_ctx` is 2048
        tokens — drastically below what most modern models actually
        support, and easily exceeded after 2-3 tool-call iterations
        where each tool result can be hundreds of bytes. Symptom: 4xx
        or 5xx response after a few successful round trips with no
        obvious cause. Setting this explicitly via the OpenAI-compat
        `options` extension fixes it. Non-Ollama providers ignore the
        field. Tunable via `cfg["num_ctx"]`.
        """
        provider = (self.cfg.get("provider") or "anthropic").lower()
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        model = self.cfg.get("model") or ""
        api_key_env = self.cfg.get("api_key_env") or ""
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        timeout = int(self.cfg.get("timeout_seconds", DEFAULT_HTTP_TIMEOUT))
        if timeout < 60:
            timeout = DEFAULT_HTTP_TIMEOUT
        is_ollama = (provider == "ollama")

        # ollama → openai_compatible alias (matches _call_llm).
        if provider == "ollama":
            provider = "openai_compatible"
            if not base_url:
                base_url = "http://localhost:11434"
        # Also flag a configured openai_compatible pointing at the default
        # Ollama port — same context-window concern applies.
        if not is_ollama and provider == "openai_compatible" \
                and base_url and "11434" in base_url:
            is_ollama = True

        if provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
        elif provider == "openai_compatible":
            if not base_url:
                self._last_error = ("openai_compatible provider has no "
                                    "base_url set — open Settings → AI "
                                    "commit messages and configure it.")
                return None
            url = base_url + "/v1/chat/completions"
        else:
            self._last_error = (
                f"provider '{provider}' is not supported for tool calling. "
                "Use 'ollama', 'openai', or 'openai_compatible'.")
            return None

        payload = {
            "model": model or "qwen2.5-coder:14b",
            "messages": messages,
            "temperature": 0.2,
            "tools": tools_as_openai_array(self.tools),
            "tool_choice": "auto",
        }
        # Ollama-specific: bump num_ctx via the options field. Other
        # OpenAI-compatible servers (LM Studio, vLLM) ignore this.
        if is_ollama:
            num_ctx = int(self.cfg.get("num_ctx", 32768))
            payload["options"] = {"num_ctx": num_ctx}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Read the body — Ollama / OpenAI both put useful diagnostics
            # there (e.g. "context length exceeded", "model not found",
            # "invalid tool schema").
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except OSError:
                body = "(no body)"
            self._last_error = (
                f"HTTP {e.code} from {url}: {e.reason}. "
                f"Response body: {body}")
            log.warning("chat completion HTTP %d: %s", e.code, body[:200])
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", str(e))
            self._last_error = (
                f"Network error talking to {url}: {type(e).__name__}: "
                f"{reason}. Is the LLM server running and reachable?")
            log.warning("chat completion network failure: %s: %s",
                        type(e).__name__, reason)
            return None
        except json.JSONDecodeError as e:
            self._last_error = (
                f"Server at {url} returned non-JSON response: {e}. "
                "Provider misconfiguration or wrong URL?")
            log.warning("chat completion JSON decode failed: %s", e)
            return None
        except OSError as e:
            self._last_error = (
                f"OS error talking to {url}: {type(e).__name__}: {e}")
            log.warning("chat completion OS error: %s", e)
            return None
        return data

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
