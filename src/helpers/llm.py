"""LLM transport — pure-function callers for Anthropic / OpenAI-compat / Ollama.

These functions never touch global config. The caller passes a `cfg` dict
(the AI-features config block from manager-config.json) plus the prompts;
the function dispatches to the right provider and returns raw text or None.

Used by the commit-message orchestrator, AI Code Review, and the
LocalAgent in src/agent.py. Stays propose-only by design — these functions
return text; what the caller does with it is their concern.

Logging via stdlib `logging.getLogger(__name__)` so each module's failures
land in the manager's existing log subsystem without cross-module coupling.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_tls = threading.local()

# base_urls already probed for Ollama version (one warning per URL per process).
_ollama_ctx_warned: set = set()


def _warn_if_old_ollama(base_url: str) -> None:
    """One-time best-effort check that Ollama honours top-level ``num_ctx``.

    Ollama added top-level ``num_ctx`` support for ``/v1/chat/completions`` in
    PR #6137 (~v0.3.x). Older builds silently truncate context to 4096 tokens —
    which would quietly defeat the larger PR-draft diff budget. Probe
    ``GET {base_url}/api/version`` once per base_url and warn if it's old.
    Never raises (not-Ollama / unreachable → silent).
    """
    if not base_url or base_url in _ollama_ctx_warned:
        return
    _ollama_ctx_warned.add(base_url)
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/version")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ver = (json.loads(resp.read().decode("utf-8")) or {}).get("version", "")
        parts = [int(x) for x in str(ver).split("-")[0].split(".")[:2]]
    except Exception:
        return  # not Ollama, unreachable, or unparseable — stay quiet
    if len(parts) >= 2 and (parts[0], parts[1]) < (0, 3):
        log.warning(
            "Ollama %s may ignore top-level num_ctx (truncates context to 4096). "
            "Upgrade Ollama, or set `PARAMETER num_ctx` in a Modelfile and run "
            "`ollama create` to bake in a larger context.", ver,
        )


def get_last_llm_error() -> "str | None":
    """Return the specific error from the most recent _call_llm call on THIS thread.

    Thread-local: must be called on the same thread that called _call_llm.
    Returns None if no error occurred or _call_llm was never called on this thread.
    """
    return getattr(_tls, "last_error", None)


def _is_auth_error(text: str) -> bool:
    """Return True if git output looks like an authentication failure."""
    t = text.lower()
    return any(s in t for s in (
        "authentication failed",
        "could not read username",
        "permission denied",
        "fatal: authentication",
        "remote: repository not found",
        "invalid username or password",
    ))


def _build_llm_prompt(diff: str, recent: list, max_diff_chars: int,
                      grounding: str = "") -> tuple:
    """Construct (system, user) prompt text for the LLM call.

    v4.2: ``grounding`` is an optional tokensave/codegraph context block
    spliced into the user message between the recent-commit reference and
    the diff. Empty by default — caller decides whether to build it.
    """
    system = (
        "You write conventional-commit messages. Output ONE commit message:\n"
        "- Subject line MUST be 72 chars or less, imperative mood "
        "(use add/fix/update, NOT added/fixed/updated).\n"
        "- Start with a conventional-commit prefix: "
        "feat / fix / chore / docs / refactor / perf / test / build / ci.\n"
        "- Scope (if used) MUST be ≤20 chars with NO slashes or dots. "
        "Use the short module name: write feat(update_poller) "
        "NOT feat(src/controllers/update_poller.py).\n"
        "- ALWAYS add a blank line then a body. The body MUST have one "
        "bullet point per changed file or subsystem explaining WHAT changed "
        "and WHY. Wrap at 72 chars per line.\n"
        "- Match the existing tone from the recent commit subjects — "
        "but DO NOT copy them. The recent-subjects list shows you the "
        "STYLE (prefix vocabulary, scope length, imperative mood); your "
        "subject must describe the STAGED DIFF below, not echo any "
        "previous commit. If the diff doesn't clearly match any recent "
        "subject's topic, write a fresh subject from the diff.\n"
        "Example of a correct commit message:\n\n"
        "feat(update_poller): add integration check after upgrade\n\n"
        "- update_poller: hook App._run so integration check dialog opens\n"
        "  automatically when upgrade subprocess exits rc=0; replaces the\n"
        "  old 15 s timer hack\n"
        "- commit_messages: add _clean_prefix_scope() to strip file-path\n"
        "  scopes that local LLMs copy verbatim from diff headers\n"
        "- llm: tighten system prompt with scope constraint and few-shot\n"
        "  examples so local models produce complete messages\n\n"
        "Output ONLY the commit message. NO preamble, NO markdown fences, "
        "NO quotes, NO explanation."
    )
    recent_lines = "\n".join(f"- {s}" for s in recent[:5]) if recent else "(no prior commits)"
    grounding_section = (
        f"Repository context (auto-attached from tokensave/codegraph):\n{grounding}\n\n"
        if grounding else ""
    )
    user = (
        f"Recent commit subjects (tone reference):\n{recent_lines}\n\n"
        f"{grounding_section}"
        f"Staged diff (truncated to {max_diff_chars} chars):\n"
        f"```diff\n{diff[:max_diff_chars]}\n```"
    )
    return system, user


def _iter_sse_events(response):
    """Yield decoded `data:` payloads from an HTTPResponse byte stream.

    Both Anthropic and OpenAI-compatible streaming use SSE (`data: <json>\\n`
    lines, terminator `data: [DONE]` for OpenAI). Network buffering can split
    a JSON payload mid-line, so we accumulate raw bytes in a bytearray and
    only yield once we've seen a complete `\\n`-terminated line. CRLF is
    handled via `rstrip("\\r")`. Non-data lines (event:, id:, retry:, blank
    keep-alives, SSE comments starting with `:`) are skipped.

    The generator stops when the underlying socket closes — the caller does
    not need to handle StopIteration specially.
    """
    buf = bytearray()
    while True:
        try:
            chunk = response.read(4096)
        except (OSError, ConnectionError):
            return
        if not chunk:
            # Final partial line (rare for well-behaved servers).
            if buf:
                line = buf.decode("utf-8", errors="replace").rstrip("\r")
                if line.startswith("data: "):
                    yield line[6:]
            return
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(buf[:i])
            del buf[:i + 1]
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line.startswith("data: "):
                yield line[6:]


def _iter_json_lines(response):
    """Yield decoded JSON objects from a newline-delimited JSON byte stream.

    Used for Ollama's /api/pull progress stream — each line is a complete
    JSON object terminated by `\\n`. Same byte-aligned accumulator pattern
    as `_iter_sse_events` (network buffering can split a line in half).
    Decode errors on individual lines are silently skipped — the next valid
    line usually has the same status info we missed.
    """
    import json as _json
    buf = bytearray()
    while True:
        try:
            chunk = response.read(4096)
        except (OSError, ConnectionError):
            return
        if not chunk:
            if buf:
                line = buf.decode("utf-8", errors="replace").strip()
                if line:
                    try:
                        yield _json.loads(line)
                    except _json.JSONDecodeError:
                        pass
            return
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(buf[:i])
            del buf[:i + 1]
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield _json.loads(line)
            except _json.JSONDecodeError:
                continue


def _stream_anthropic_response(resp, on_token) -> str:
    """Accumulate streamed text from an Anthropic SSE response.

    Parses `content_block_delta` events and calls `on_token` for each chunk.
    Returns the full accumulated text (may be empty if the stream was empty).
    """
    pieces = []
    for event in _iter_sse_events(resp):
        try:
            data = json.loads(event)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "content_block_delta":
            delta = (data.get("delta") or {}).get("text", "")
            if delta:
                pieces.append(delta)
                try:
                    on_token(delta)
                except Exception:
                    log.exception("on_token callback raised")
    return "".join(pieces).strip()


def _stream_openai_response(resp, on_token) -> str:
    """Accumulate streamed text from an OpenAI-compatible SSE response.

    Parses `choices[0].delta.content` events and calls `on_token` per chunk.
    Returns the full accumulated text (may be empty if the stream was empty).
    """
    pieces = []
    for event in _iter_sse_events(resp):
        if event == "[DONE]":
            break
        try:
            data = json.loads(event)
        except json.JSONDecodeError:
            continue
        choices = data.get("choices") or []
        if not choices:
            continue
        delta = (choices[0].get("delta") or {}).get("content") or ""
        if delta:
            pieces.append(delta)
            try:
                on_token(delta)
            except Exception:
                log.exception("on_token callback raised")
    return "".join(pieces).strip()


def _urlopen_retrying(req, timeout, retries: int = 2, backoff: float = 2.0):
    """``urllib.request.urlopen`` with retry on transient HTTP 5xx.

    The first chat request after a model is unloaded can return HTTP 500 while
    the server loads the model + allocates its context window (reproducibly so
    with Ollama cold-starts at large num_ctx); a brief retry lands once it's
    warm. 4xx client errors (bad model name, etc.) are NOT retried. URLError /
    timeouts propagate immediately. Returns the response (use as a context
    manager); raises the last error if every attempt fails.
    """
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and attempt < retries:
                log.warning("LLM HTTP %s — retrying (%d/%d) after cold-start/"
                            "transient server error", exc.code, attempt + 1, retries)
                time.sleep(backoff * (attempt + 1))
                attempt += 1
                continue
            raise


def _call_anthropic(api_key: str, model: str, system_prompt: str, user_prompt: str,
                    max_tokens: int, timeout: int, on_token) -> str | None:
    """Anthropic Messages API — streaming and non-streaming. Pure execution layer;
    validation and error handling live in the _call_llm dispatcher."""
    payload = {
        "model": model or "claude-haiku-4-5",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if on_token is not None:
        payload["stream"] = True
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
    if on_token is not None:
        with _urlopen_retrying(req, timeout) as resp:
            return _stream_anthropic_response(resp, on_token) or None
    with _urlopen_retrying(req, timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.strip() or None


def _call_openai_compat(url: str, api_key: str, model: str,
                        system_prompt: str, user_prompt: str,
                        max_tokens: int, timeout: int, on_token,
                        temperature: float = 0.3,
                        top_p: float | None = None,
                        top_k: int | None = None,
                        num_ctx: int | None = None) -> str | None:
    """OpenAI Chat Completions — covers openai, openai_compatible, and ollama.
    Caller resolves the endpoint URL before dispatching here."""
    payload = {
        "model": model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k is not None:
        payload["top_k"] = top_k
    if num_ctx is not None:
        # Ollama extension — silently ignored by non-Ollama OpenAI-compat servers
        payload["num_ctx"] = num_ctx
        # Advisory: warn once if this Ollama build predates top-level num_ctx.
        _warn_if_old_ollama(url.split("/v1/")[0])
    if on_token is not None:
        payload["stream"] = True
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    if on_token is not None:
        with _urlopen_retrying(req, timeout) as resp:
            return _stream_openai_response(resp, on_token) or None
    with _urlopen_retrying(req, timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip() or None


def _call_llm(cfg: dict, system_prompt: str, user_prompt: str,
              max_tokens: int = 1500, timeout: int | None = None,
              on_token=None) -> str | None:
    """General-purpose LLM call. Returns raw text or None on ANY failure.

    Used by the commit-message orchestrator AND the AI Code Review feature
    AND any future agentic stages. Stays propose-only by design — this
    function returns text; what the caller does with that text is their
    concern (e.g. displaying in a dialog, parsing tool calls, etc.).

    Supported providers (`cfg["provider"]`):
      * "anthropic"        — native Messages API at api.anthropic.com
      * "openai"           — OpenAI Chat Completions at api.openai.com
      * "openai_compatible" — any OpenAI-compatible endpoint
            (Ollama at http://localhost:11434, LM Studio at :1234,
             vLLM, llama-server, LocalAI, etc.)
      * "ollama"           — friendly alias for openai_compatible with the
            default Ollama base URL filled in if none was set.

    Returns None on any error: no key, missing model, network failure,
    timeout, provider error, empty response. Caller falls back appropriately.

    Streaming (when `on_token` is provided): the function sends
    `"stream": true` to the provider and calls `on_token(delta_text)` for
    each text chunk as it arrives. The full accumulated text is still
    returned at the end (so existing callers can continue to use the return
    value unchanged). If the provider doesn't support streaming for the
    given configuration, the function silently falls back to the blocking
    path — `on_token` simply doesn't get called.

    The `on_token` callback runs on whichever thread called `_call_llm`.
    Callers that need to push deltas to a Tk UI must wrap it in a
    `self.after(0, ...)` schedule (see AICodeReviewDialog._start_review).
    """
    if not cfg.get("enabled"):
        return None

    # Reasoning models on consumer GPUs can take 30-60+ seconds. Auto-promote
    # any timeout below 30 to 90 (users who explicitly picked 30/60 keep their value).
    if timeout is None:
        raw_timeout = int(cfg.get("timeout_seconds", 90))
        timeout = 90 if raw_timeout < 30 else raw_timeout

    provider    = (cfg.get("provider") or "anthropic").lower()
    model       = cfg.get("model") or ""
    base_url    = (cfg.get("base_url") or "").rstrip("/")
    api_key_env = cfg.get("api_key_env") or ""
    api_key     = os.environ.get(api_key_env, "") if api_key_env else ""

    # "ollama" is a friendly alias — falls through to OpenAI-compatible with
    # the default Ollama base URL if none was set.
    if provider == "ollama":
        provider = "openai_compatible"
        if not base_url:
            base_url = "http://localhost:11434"

    _tls.last_error = None
    try:
        if provider == "anthropic":
            if not api_key:
                return None
            return _call_anthropic(api_key, model, system_prompt, user_prompt,
                                   max_tokens, timeout, on_token)
        if provider in ("openai", "openai_compatible"):
            if provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
            else:
                if not base_url:
                    return None
                url = base_url + "/v1/chat/completions"
            _temp = cfg.get("temperature")
            temperature = float(_temp) if _temp is not None else 0.3
            _top_p = cfg.get("top_p")
            top_p = float(_top_p) if _top_p is not None else None
            _top_k = cfg.get("top_k")
            top_k = int(_top_k) if _top_k is not None else None
            _num_ctx = cfg.get("num_ctx")
            num_ctx = int(_num_ctx) if _num_ctx is not None else None
            return _call_openai_compat(url, api_key, model, system_prompt, user_prompt,
                                       max_tokens, timeout, on_token,
                                       temperature=temperature, top_p=top_p,
                                       top_k=top_k, num_ctx=num_ctx)
    except urllib.error.HTTPError as exc:        # HTTPError is a URLError subclass — match first
        if exc.code >= 500:
            _tls.last_error = (
                f"HTTP {exc.code} from {provider} (after retries) — the model "
                f"likely failed to load (cold start / low memory). It often works "
                f"on a second try once warm; otherwise lower num_ctx, or pre-load "
                f"with `ollama run {model or '<model>'}`."
            )
        else:
            _tls.last_error = f"HTTP {exc.code} from {provider}: {exc.reason}"
        return None
    except urllib.error.URLError as exc:
        _tls.last_error = f"Connection failed ({exc.reason}) — is {provider} running?"
        return None
    except TimeoutError:
        _tls.last_error = (
            f"Timed out after {timeout}s — diff may be too large or {provider} is slow"
        )
        return None
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        _tls.last_error = f"Unexpected error: {exc}"
        return None

    return None


def warmup_ollama(base_url: str, model: str = "", timeout: int = 10) -> bool:
    """Send a 1-token completion to warm up the Ollama model.

    Fires before the first Generate click when "Warm up Ollama" is enabled in
    Settings.  Returns True if the server responded, False on any error (the
    caller always continues — warmup is best-effort).
    """
    import urllib.request
    import urllib.error
    url = (base_url or "http://localhost:11434").rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model or "qwen2.5-coder:14b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False
