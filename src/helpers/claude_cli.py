"""Claude Code CLI integration — a compatibility shim over `helpers/agent_cli`.

The mechanics that used to live here (the Windows ``""outer""`` quoting rule,
newline stripping, `--print` capture, the per-thread error record) moved to
``helpers/agent_cli.py`` when Cursor was added, so that a second agent did not
mean a second copy of all of it. Read that module's docstring for the reasoning.

This file remains because roughly a dozen call sites import these four names,
and their signatures are part of the Manager's internal contract. Every function
here delegates with the ``claude`` spec and changes nothing observable.

New code should prefer ``cfg.resolve_agent_cli()`` and the ``agent_cli`` runners
directly, so it follows the user's selected agent instead of pinning Claude.
"""

from __future__ import annotations

from helpers import agent_cli
from helpers.agent_cli import CLAUDE, get_last_cli_error  # re-exported

__all__ = [
    "get_last_cli_error",
    "spawn_claude_cli",
    "spawn_claude_cli_interactive",
    "call_claude_cli_print",
]


def spawn_claude_cli(
    claude_exe: str,
    project_path: str,
    instruction: str,
    model: str = "",
) -> tuple[bool, str]:
    """Open a new terminal window running `claude` with *instruction*.

    Args:
        claude_exe:   Full path to claude or claude.cmd (from cfg.claude_cli_exe).
        project_path: Working directory for the new process.
        instruction:  Single-line imperative prompt for claude.
        model:        Pinned model ID; empty string uses Claude CLI's default.

    Returns:
        (success: bool, error_message: str)
    """
    return agent_cli.spawn(CLAUDE, claude_exe, project_path, instruction, model)


def spawn_claude_cli_interactive(
    claude_exe: str,
    project_path: str,
    model: str = "",
) -> tuple[bool, str]:
    """Open a new terminal window running `claude` as an interactive TUI.

    No instruction is passed — claude starts at its own prompt.
    """
    return agent_cli.spawn_interactive(CLAUDE, claude_exe, project_path, model)


def call_claude_cli_print(
    claude_exe: str,
    prompt: str,
    system_prompt: str = "",
    timeout: int = 45,
    model: str = "",
    cwd: "str | None" = None,
) -> "str | None":
    """Invoke `claude --print` non-interactively and return stdout text.

    Returns the stripped stdout string, or None on timeout / error / empty.
    The specific cause is available from `get_last_cli_error()` on this thread.
    """
    # Kept here rather than in agent_cli: callers (and tests) have relied on
    # this exact wording since before the registry existed, and the generic
    # runner's message names the agent's display label instead.
    if not claude_exe:
        agent_cli._tls.last_error = "no Claude CLI path configured"
        return None
    return agent_cli.call_print(
        CLAUDE, claude_exe, prompt,
        system_prompt=system_prompt, timeout=timeout, model=model, cwd=cwd,
    )
