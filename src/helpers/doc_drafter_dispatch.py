"""LLM dispatch — Claude CLI / _call_llm / agentic loop routing.

Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).
Import via the ``helpers.doc_drafter`` facade — it re-exports
every name, so call sites and tests are unchanged.
"""

from __future__ import annotations

import os




# ── LLM dispatch ────────────────────────────────────────────────────────────

def _dispatch_agentic(llm_cfg, system_prompt, user_prompt,
                      project_path, tokensave_exe, timeout, stop_event):
    """Run a one-shot LocalAgent loop with tokensave tools and return (text, error).

    Used by dispatch_llm when enable_tokensave_tools=True and provider is
    ollama/openai_compatible.  The agent may call tokensave_search and
    tokensave_context before producing its final answer, which becomes the
    draft text returned to the dialog.
    """
    import threading

    try:
        from agent import LocalAgent
        from agent_tools import (
            make_tokensave_search_tool,
            make_tokensave_context_tool,
        )
    except ImportError:
        return None, "Agentic mode unavailable: agent module not found."

    tools = {}
    if tokensave_exe and project_path:
        ts_search = make_tokensave_search_tool(project_path, tokensave_exe)
        ts_ctx = make_tokensave_context_tool(project_path, tokensave_exe)
        tools[ts_search.name] = ts_search
        tools[ts_ctx.name] = ts_ctx

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    result_holder: list[str | None] = [None]
    error_holder:  list[str | None] = [None]
    done_event = threading.Event()

    def _on_done(text):
        result_holder[0] = text
        done_event.set()

    def _on_error(msg):
        error_holder[0] = msg
        done_event.set()

    agent = LocalAgent(cfg=llm_cfg, project_path=project_path, tools=tools,
                       max_iterations=6)
    agent_thread = threading.Thread(
        target=agent.run,
        kwargs=dict(
            messages=messages,
            on_done=_on_done,
            on_error=_on_error,
            stop_event=stop_event,
        ),
        daemon=True,
    )
    agent_thread.start()
    done_event.wait(timeout=timeout)
    if not done_event.is_set():
        return None, "Agentic call timed out."
    if error_holder[0]:
        return None, error_holder[0]
    text = result_holder[0]
    if not text:
        return None, "Agentic call returned no output."
    return text, None



def dispatch_llm(llm_cfg, system_prompt, user_prompt,
                 claude_cli_exe, cwd, timeout=120,
                 gen_params=None, examples=None,
                 enable_tokensave_tools=False, tokensave_exe="",
                 stop_event=None):
    """Call the configured LLM and return ``(text, error)``.

    Routes to ``call_claude_cli_print`` when ``llm_cfg["provider"] ==
    "claude_cli"``, otherwise to ``_call_llm`` (anthropic / openai /
    openai_compatible / ollama).  Returns ``(text, None)`` on success or
    ``(None, error_string)`` on failure.

    gen_params: dict of per-DocType overrides merged into llm_cfg
      (temperature, top_p, top_k, num_ctx).  Values in gen_params win.
    examples: list of (input_text, output_text) few-shot pairs spliced
      into user_prompt for ollama / openai_compatible providers.
    enable_tokensave_tools: when True and provider is ollama/openai_compatible,
      use a one-shot LocalAgent loop with tokensave_search + tokensave_context
      tools instead of a plain completion (Theme B2).  Claude CLI stays on the
      B1-injection path (documented asymmetry).
    tokensave_exe: path to tokensave binary; required when enable_tokensave_tools=True.
    stop_event: threading.Event for cooperative cancellation in agentic path.

    Pure dispatch — does NOT handle threading, cancellation, or UI.
    Caller wraps in a worker thread + stop_event check.
    """
    if gen_params:
        llm_cfg = {**(llm_cfg or {}), **gen_params}
    provider = (llm_cfg or {}).get("provider", "")

    # C4: splice few-shot examples for local providers (ollama / openai_compat)
    if examples and provider.lower() in ("ollama", "openai_compatible"):
        ex_parts = []
        for inp, out in examples[:2]:
            ex_parts.append(f"Example input:\n{inp}\n\nExpected output:\n{out}")
        ex_block = "\n\n---\n\n".join(ex_parts)
        user_prompt = ex_block + "\n\n---\n\n" + user_prompt

    # Theme B2: agentic path for local providers when tokensave tools requested.
    if enable_tokensave_tools and provider.lower() in ("ollama", "openai_compatible"):
        return _dispatch_agentic(
            llm_cfg, system_prompt, user_prompt,
            project_path=cwd or "",
            tokensave_exe=tokensave_exe or "",
            timeout=timeout,
            stop_event=stop_event,
        )

    try:
        if provider == "claude_cli":
            from helpers.claude_cli import call_claude_cli_print
            exe = (claude_cli_exe or "").strip()
            if not exe:
                return None, (
                    "Claude CLI not configured — set path in "
                    "Settings → Claude Code CLI."
                )
            model = (llm_cfg.get("model") or "").strip()
            # cwd=$HOME: Claude Code loads <cwd>/CLAUDE.md as project
            # context, which puts smaller models (Haiku) into "assistant
            # mode" — they reply conversationally to the prompt instead of
            # producing the requested draft. Pointing cwd at $HOME isolates
            # this call from any project CLAUDE.md. Project context that
            # actually matters (existing section text, commit list,
            # tokensave grounding) is already embedded in the prompt by
            # the caller, so the CLI process does not need to be in the
            # project directory. Same pattern used in
            # helpers.commit_messages._strat_claude_cli.
            result = call_claude_cli_print(
                exe, user_prompt,
                system_prompt=system_prompt,
                timeout=timeout,
                model=model,
                cwd=os.path.expanduser("~"),
            )
            if not result:
                return None, "Claude CLI returned no output (timeout or auth)."
            return result, None

        from helpers.llm import _call_llm
        # Adaptive token cap. After Roadmap-7 prompt hardening (U2 swapped
        # full existing-content dumps for header summaries / scope-prefix
        # vocab), typical prompts dropped below the previous "large" branch,
        # so the per-branch ceilings are tightened. The STOP marker added in
        # U1 also gives the model a positive termination signal, reducing
        # the need for headroom against ramble.
        prompt_chars = len(system_prompt or "") + len(user_prompt or "")
        if prompt_chars < 1500:
            max_tokens = 1000          # tiny range → speed mode
        elif prompt_chars < 4000:
            max_tokens = 1500          # was 2000 — typical post-U2
        else:
            max_tokens = 2000          # was 2500 — large-context ceiling
        result = _call_llm(llm_cfg, system_prompt, user_prompt,
                           max_tokens=max_tokens)
        if not result:
            return None, f"{provider or 'LLM'} returned empty result."
        return result, None
    except Exception as exc:
        return None, f"{provider or 'LLM'} call failed: {exc}"
