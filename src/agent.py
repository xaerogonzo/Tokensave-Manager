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
# reasoning + JSON-encoded args, so allow ~120s on slow local hardware.
DEFAULT_HTTP_TIMEOUT = 120


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
                        on_error("LLM request failed — check Settings → "
                                 "AI commit messages (provider / model / "
                                 "server running).")
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
        and return the parsed response dict, or None on failure."""
        provider = (self.cfg.get("provider") or "anthropic").lower()
        base_url = (self.cfg.get("base_url") or "").rstrip("/")
        model = self.cfg.get("model") or ""
        api_key_env = self.cfg.get("api_key_env") or ""
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        timeout = int(self.cfg.get("timeout_seconds", DEFAULT_HTTP_TIMEOUT))
        if timeout < 60:
            timeout = DEFAULT_HTTP_TIMEOUT

        # ollama → openai_compatible alias (matches _call_llm).
        if provider == "ollama":
            provider = "openai_compatible"
            if not base_url:
                base_url = "http://localhost:11434"

        if provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
        elif provider == "openai_compatible":
            if not base_url:
                return None
            url = base_url + "/v1/chat/completions"
        else:
            return None

        payload = {
            "model": model or "qwen2.5-coder:14b",
            "messages": messages,
            "temperature": 0.2,
            "tools": tools_as_openai_array(self.tools),
            "tool_choice": "auto",
        }
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
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError, OSError) as e:
            log.warning("chat completion failed: %s: %s", type(e).__name__, e)
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
