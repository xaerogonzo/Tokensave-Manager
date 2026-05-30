"""AI-powered test content generation.

Dispatches to Claude CLI (``claude --print``) or the configured LLM provider
(Ollama / Anthropic API / OpenAI-compat) to generate a real pytest file for a
given source file — replacing the template-stub ``assert True`` placeholders
that ``test_scaffold.generate_test_file`` writes.

No Tkinter imports.  The caller is responsible for threading.
"""

from __future__ import annotations

import ast
import logging
import os
import re
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

- Test files live under tests/ and are named test_<module>.py \
  (dialogs use test_dialog_<name>.py).
- Available conftest fixtures:
    * tk_root          — function-scoped withdrawn Tk root; asserts no daemon \
threads leaked at teardown (G-D). Use for all dialog tests.
    * _tk_root_session — session-scoped backing root; never reference directly \
in tests.
    * wait_for(pred, timeout_s)  — drives root.update() each iteration so \
after(0,...) callbacks fire; use instead of time.sleep() when waiting for \
worker threads (G-G/G-M).
    * patch_after(dialog) → AfterHarness — captures self.after() into a queue; \
call harness.advance(ms) / harness.drain() for tick-based after() loops (G-A/G-H).
    * fake_home        — redirects HOME/USERPROFILE/APPDATA to tmp_path; use \
for helpers that read ~/.claude or %APPDATA% paths (G-F).
    * mock_config      — minimal ManagerConfig stub; check cfg._saved is True \
after any operation that should persist config.
- Mark ALL Tkinter tests at module level: pytestmark = pytest.mark.tk
- Guard the tk import: tk = pytest.importorskip("tkinter")
- Mock at the import site (G-E): patch("module.under.test.subprocess.run"), \
never patch("subprocess.run") globally — the latter breaks pytest's own \
subprocess infrastructure.
- Windows subprocess safety (G-WIN): when the code under test passes \
creationflags=CREATE_NO_WINDOW to Popen/run/check_output, assert it in the \
test: _, kwargs = mock.call_args; assert kwargs.get("creationflags") == \
CREATE_NO_WINDOW
- Keep tests focused and independent — no shared mutable state between tests.

OUTPUT CONTRACT (critical):
- Respond with ONLY the Python source of the test file, as your message text.
- Do NOT create or write any files. Do NOT use any tools. Do NOT ask for
  permission. Do NOT include any explanation, preamble, markdown fences, or
  commentary — only the .py file contents.
- Begin your reply with the module docstring (triple quotes).

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

Output the full Python source for tests/{test_filename} as your reply text —
ONLY the code, no prose, no markdown fences, no file-writing, no permission
requests. Start with the module docstring.
"""

# Appended to the user prompt on a single repair retry when the first reply
# didn't parse as Python (e.g. the model emitted prose or a permission request).
_REPAIR_PROMPT_TEMPLATE = """\
Your previous reply was NOT valid Python and could not be parsed:
  {error}

Reply again with ONLY the corrected Python source of the test file — no prose,
no markdown fences, no commentary, no tool use. Start with the module docstring.
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

    def _run(up: str) -> "str | None":
        if effective_backend == "claude_cli":
            return _dispatch_claude_cli(cfg, system_prompt, up, project_root)
        return _dispatch_llm(cfg, system_prompt, up)

    # First attempt.
    raw = _run(user_prompt)
    if raw is None:
        return None, "AI backend returned no output — check your settings."
    code = _extract_code(raw)
    if not code.strip():
        return None, "AI returned an empty response."
    err = _looks_like_python(code)

    # One repair pass when the reply isn't valid Python — covers the case where
    # the model emitted prose or a permission request ("May I write this file?")
    # instead of code. NEVER return un-parseable text (the caller writes it to disk).
    if err is not None:
        if cancel_event and cancel_event.is_set():
            return None, "Cancelled."
        repair_up = user_prompt + "\n\n" + _REPAIR_PROMPT_TEMPLATE.format(error=err)
        raw2 = _run(repair_up)
        if raw2:
            code2 = _extract_code(raw2)
            err2 = _looks_like_python(code2)
            if err2 is None and code2.strip():
                return code2, None
            err, code = (err2 or err), (code2 or code)
        first_line = (code.strip().splitlines() or [""])[0][:80]
        return None, (
            f"AI did not return valid Python ({err}). "
            f"First line was: {first_line!r}"
        )

    return code, None


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
    """Call Claude CLI in --print mode with the combined prompt.

    cwd is a NEUTRAL dir (~), NOT project_root: running `claude --print` inside
    the repo loads CLAUDE.md/AGENTS.md and pushes Claude into agentic mode, where
    a "generate a test file" task makes it try to USE its Write tool and ask for
    permission (which it returns as prose) instead of printing the code. The
    source is already inlined in the prompt, so project context isn't needed.
    (Same rationale as commit-message generation — see call_claude_cli_print.)
    """
    from helpers.claude_cli import call_claude_cli_print
    return call_claude_cli_print(
        claude_exe=cfg.claude_cli_exe,
        prompt=user_prompt,
        system_prompt=system_prompt,
        timeout=90,
        model=getattr(cfg, "claude_cli_model", "") or "",
        cwd=os.path.expanduser("~"),
    )


def _dispatch_llm(cfg, system_prompt: str, user_prompt: str) -> "str | None":
    """Call the configured LLM provider with the combined prompt."""
    from helpers.llm import _call_llm
    return _call_llm(
        cfg=cfg.raw.get("commit_message_llm", {}),
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

_CODE_START_PREFIXES = ('"""', "'''", "import ", "from ", "#", "@", "def ", "class ")


def _extract_code(text: str) -> str:
    """Pull the Python source out of an LLM reply.

    Handles three shapes:
      1. A fenced ``` ```python … ``` ``` (or bare ``` ``` ```) block anywhere →
         return the first block's body.
      2. A prose preamble followed by code (the failure mode that wrote 8 junk
         files) → drop leading lines until the first code-ish line.
      3. Already-clean code → returned unchanged.

    Returns the best-effort code; :func:`_looks_like_python` is the real gate.
    """
    if not text:
        return ""
    fence = re.search(r"```[A-Za-z0-9_]*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip("\n")
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(_CODE_START_PREFIXES):
            return "\n".join(lines[i:]).strip("\n")
    return text.strip()


def _looks_like_python(text: str) -> "str | None":
    """Return None if *text* parses as a Python module, else the error message."""
    try:
        ast.parse(text)
        return None
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
