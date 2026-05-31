"""Claude Code CLI integration helper.

Spawns the `claude` CLI (`@anthropic-ai/claude-code`) in a detached native
terminal window. The CLI is a TTY-interactive application — we never try to
capture its stdout directly. Instead we construct the instruction inside the
manager and hand off execution to a new console window, leaving our Tkinter
mainloop completely unblocked.

Windows-specific notes
----------------------
* subprocess.CREATE_NEW_CONSOLE opens a genuine separate cmd window without
  routing through `cmd /c start` (which has the "first-quoted-arg-is-the-
  window-title" parsing bug).
* When both claude_exe and instruction contain spaces, Python's list→cmdline
  conversion makes cmd.exe strip the outermost quotes of the compound
  expression. We work around this with the canonical ``""outer""`` double-
  double-quote wrapper, passing a formatted string (not a list) on Windows.
* Newlines in the instruction are stripped before use — a stray \\n inside
  cmd.exe /k is treated as pressing Enter, dropping claude into an empty
  shell before the prompt lands.
"""

from __future__ import annotations

import subprocess
import sys
import threading

from constants import CREATE_NO_WINDOW
from helpers.runtime import log

# Per-thread record of WHY the most recent call_claude_cli_print returned None
# (timeout / missing-or-unrunnable binary / non-zero exit). Mirrors
# helpers.llm._tls so callers can surface the real cause instead of guessing.
# Must be read on the same thread that made the call.
_tls = threading.local()


def get_last_cli_error() -> "str | None":
    """Return the specific failure from the most recent call_claude_cli_print on
    THIS thread, or None if the last call succeeded / was never made here."""
    return getattr(_tls, "last_error", None)


def spawn_claude_cli(
    claude_exe: str,
    project_path: str,
    instruction: str,
    model: str = "",
) -> tuple[bool, str]:
    """Open a new terminal window running `claude` with *instruction*.

    The window uses ``/k`` so it stays open after claude exits, letting the
    user review output and continue interacting.

    Args:
        claude_exe:   Full path to claude or claude.cmd (from cfg.claude_cli_exe).
        project_path: Working directory for the new process.
        instruction:  Single-line imperative prompt for claude.
        model:        Pinned model ID; empty string uses Claude CLI's default.

    Returns:
        (success: bool, error_message: str)
    """
    if not claude_exe:
        return False, (
            "Claude Code CLI is not configured. "
            "Set the path in Settings → Claude Code CLI."
        )

    # Strip newlines — a stray \n inside cmd.exe /k fires Enter prematurely.
    instruction = instruction.replace("\r", " ").replace("\n", " ").strip()
    model_flag = f' --model "{model}"' if model else ""

    try:
        if sys.platform == "win32":
            # ""outer"" wrapper: satisfies cmd.exe's multi-quoted-args quoting rule.
            # Both claude_exe and instruction may contain spaces; passing a list
            # triggers the outermost-quote-strip bug, so we use a raw string here.
            cmd_str = f'cmd.exe /k ""{claude_exe}"{model_flag} "{instruction}""'
            subprocess.Popen(
                cmd_str,
                cwd=project_path,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            argv = [claude_exe]
            if model:
                argv += ["--model", model]
            argv.append(instruction)
            subprocess.Popen(argv, cwd=project_path)
        return True, ""
    except Exception as e:
        return False, str(e)


def call_claude_cli_print(
    claude_exe: str,
    prompt: str,
    system_prompt: str = "",
    timeout: int = 45,
    model: str = "",
    cwd: "str | None" = None,
) -> "str | None":
    """Invoke `claude --print` non-interactively and return stdout text.

    Used by the pre-commit review hook and the commit-message Claude CLI
    strategy. The prompt is piped via stdin rather than as a positional
    arg — argv mangles multi-line / backtick-laden content on Windows.

    Args:
        model: Pinned model ID (e.g. "claude-haiku-4-5-20251001"). Empty
               string omits --model so Claude CLI uses its own default
               from ~/.claude/settings.json.
        cwd:   Working directory for the subprocess. Claude Code loads
               <cwd>/CLAUDE.md and <cwd>/AGENTS.md as project context, so
               pass a neutral dir (e.g. expanduser("~")) for tasks where
               project context would derail the model into assistant mode
               (commit-message generation). Pass the project path for
               tasks that DO benefit from project context (code review).
               None inherits Python's cwd.

    Returns the stripped stdout string, or None on timeout / error / empty.
    On non-zero exit, the first 400 chars of stderr are printed to
    sys.stderr so users can diagnose typo'd model IDs or auth issues.
    """
    _tls.last_error = None
    if not claude_exe:
        _tls.last_error = "no Claude CLI path configured"
        return None
    cmd = [claude_exe, "--print"]
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        _tls.last_error = f"timed out after {timeout}s"
        return None
    except OSError as e:
        # Missing / non-executable binary, bad cwd, etc.
        _tls.last_error = f"executable not found or not runnable: {e}"
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        _tls.last_error = f"exited {proc.returncode}: {err[:400]}" if err \
            else f"exited {proc.returncode} (no stderr)"
        # Log through the manager's logger — always captured even under
        # pythonw.exe / windowed Nuitka builds where sys.stderr is None.
        log.warning("claude --print exited %d: %s", proc.returncode, err[:400])
        return None
    out = (proc.stdout or "").strip()
    if not out:
        _tls.last_error = "returned empty output"
        return None
    return out
