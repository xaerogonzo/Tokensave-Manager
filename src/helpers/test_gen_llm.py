"""AI-powered test content generation.

Dispatches to Claude CLI (``claude --print``) or the configured LLM provider
(Ollama / Anthropic API / OpenAI-compat) to generate a real pytest file for a
given source file — replacing the template-stub ``assert True`` placeholders
that ``test_scaffold.generate_test_file`` writes.

No Tkinter imports.  The caller is responsible for threading.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback example used when the project has no small test file yet
# ---------------------------------------------------------------------------

_FALLBACK_EXAMPLE = '''\
"""Tests for helpers/example.py — minimal structure reference."""
import pytest
from helpers import example


def test_basic():
    """Smoke test: module imports cleanly."""
    assert example is not None


def test_returns_expected_value():
    result = example.main_function("input")
    assert result == "expected"
'''

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert Python test engineer writing pytest tests for a project that \
uses these conventions:

- Test files live under tests/ and are named test_<module>.py.
- Use the fixtures: tk_root (session-scoped Tk instance), mock_config (returns a \
ManagerConfig with sensible defaults).
- Mark Tkinter tests with @pytest.mark.tk.
- Mock imports at the import site, not inside the function: use \
unittest.mock.patch("module.under.test.dependency") rather than \
patch("dependency").
- Keep tests focused and independent — no shared mutable state between tests.
- Write ONLY the file content, starting with the module docstring (triple \
quotes). Do not include any explanation, markdown fences, or commentary \
outside the Python code.

Example of a well-structured test file from this project:
{example_test}
"""

_USER_PROMPT_TEMPLATE = """\
Generate a complete pytest test file for the following source file.

Source file path: {rel_path}

Source code:
```python
{source_code}
```

Write the full content of tests/{test_filename} below.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_ai_test_content(
    source_path: str,
    project_root: str,
    backend: str,           # "claude_cli" | "llm" | "auto"
    cfg,                    # ManagerConfig — for llm + claude_cli settings
    cancel_event: "threading.Event | None" = None,
    max_source_chars: int = 8_000,
) -> "tuple[str | None, str | None]":
    """Generate test file content using an AI backend.

    Args:
        source_path:    Absolute path to the source file to test.
        project_root:   Absolute path to the project root.
        backend:        One of ``"claude_cli"``, ``"llm"``, or ``"auto"``.
        cfg:            Live ``ManagerConfig`` instance.
        cancel_event:   Optional ``threading.Event``; checked before dispatch.
        max_source_chars: Cap on source code sent to the model (avoids huge
                          token bills on large files).

    Returns:
        ``(content, None)`` on success or ``(None, error_message)`` on failure.
    """
    if cancel_event and cancel_event.is_set():
        return None, "Cancelled."

    # Resolve backend
    effective_backend = _resolve_backend(backend, cfg)
    if effective_backend is None:
        return None, (
            "No AI backend is configured.\n"
            "Set a Claude Code CLI path or an LLM provider in Settings."
        )

    # Build prompts
    try:
        system_prompt, user_prompt = _build_prompts(
            source_path, project_root, max_source_chars
        )
    except Exception as exc:
        return None, f"Could not read source file: {exc}"

    if cancel_event and cancel_event.is_set():
        return None, "Cancelled."

    # Dispatch
    if effective_backend == "claude_cli":
        result = _dispatch_claude_cli(cfg, system_prompt, user_prompt, project_root)
    else:
        result = _dispatch_llm(cfg, system_prompt, user_prompt)

    if result is None:
        return None, "AI backend returned no output — check your settings."

    cleaned = _strip_markdown_fences(result)
    if not cleaned.strip():
        return None, "AI returned an empty response."

    return cleaned, None


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------

def _resolve_backend(backend: str, cfg) -> "str | None":
    """Return the concrete backend to use, or None if nothing is configured."""
    if backend == "auto":
        if getattr(cfg, "claude_cli_exe", ""):
            return "claude_cli"
        # Fall back to LLM if any provider is configured
        llm_cfg = cfg.raw.get("commit_message_llm", {})
        if llm_cfg.get("provider") or llm_cfg.get("api_key"):
            return "llm"
        return None
    if backend == "claude_cli":
        return "claude_cli" if getattr(cfg, "claude_cli_exe", "") else None
    if backend == "llm":
        llm_cfg = cfg.raw.get("commit_message_llm", {})
        if llm_cfg.get("provider") or llm_cfg.get("api_key"):
            return "llm"
        return None
    return None


def _dispatch_claude_cli(cfg, system_prompt: str, user_prompt: str,
                          project_root: str) -> "str | None":
    """Call Claude CLI in --print mode with the combined prompt."""
    from helpers.claude_cli import call_claude_cli_print
    return call_claude_cli_print(
        claude_exe=cfg.claude_cli_exe,
        prompt=user_prompt,
        system_prompt=system_prompt,
        timeout=90,
        model=getattr(cfg, "claude_cli_model", "") or "",
        cwd=project_root,
    )


def _dispatch_llm(cfg, system_prompt: str, user_prompt: str) -> "str | None":
    """Call the configured LLM provider with the combined prompt."""
    from helpers.llm import _call_llm
    return _call_llm(
        cfg=cfg,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=2500,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_prompts(
    source_path: str,
    project_root: str,
    max_source_chars: int,
) -> "tuple[str, str]":
    """Return ``(system_prompt, user_prompt)``."""
    # Read source
    with open(source_path, encoding="utf-8", errors="replace") as fh:
        source_code = fh.read(max_source_chars)
    if len(source_code) == max_source_chars:
        source_code += "\n# ... [truncated]"

    # Find example test file (smallest under 100 lines, scoped to project_root)
    example_test = _find_example_test(project_root)

    # Relative path for display
    try:
        rel_path = os.path.relpath(source_path, project_root).replace("\\", "/")
    except ValueError:
        rel_path = os.path.basename(source_path)

    # Derive expected test filename
    base = os.path.splitext(os.path.basename(source_path))[0]
    test_filename = f"test_{base}.py"

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(example_test=example_test)
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        rel_path=rel_path,
        source_code=source_code,
        test_filename=test_filename,
    )
    return system_prompt, user_prompt


def _find_example_test(project_root: str) -> str:
    """Return the content of the smallest test file ≤ 100 lines under project_root.

    The search is STRICTLY bounded to ``project_root`` — never walks above it.
    Falls back to :data:`_FALLBACK_EXAMPLE` when no suitable file is found.
    """
    tests_dir = os.path.join(project_root, "tests")
    if not os.path.isdir(tests_dir):
        return _FALLBACK_EXAMPLE

    best_path: "str | None" = None
    best_lines = 101  # only accept ≤ 100

    try:
        for fname in os.listdir(tests_dir):
            if not (fname.startswith("test_") and fname.endswith(".py")):
                continue
            fpath = os.path.join(tests_dir, fname)
            # Guard: ensure we haven't escaped project_root via a symlink
            try:
                real_file = os.path.realpath(fpath)
                real_root = os.path.realpath(project_root)
                if not real_file.startswith(real_root):
                    continue
            except OSError:
                continue
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                if len(lines) < best_lines:
                    best_lines = len(lines)
                    best_path = fpath
            except OSError:
                continue
    except OSError:
        return _FALLBACK_EXAMPLE

    if best_path is None:
        return _FALLBACK_EXAMPLE

    try:
        with open(best_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return _FALLBACK_EXAMPLE


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove ```python ... ``` fences if the model wrapped its output in them."""
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
