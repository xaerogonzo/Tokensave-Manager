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


def spawn_claude_cli(
    claude_exe: str,
    project_path: str,
    instruction: str,
) -> tuple[bool, str]:
    """Open a new terminal window running `claude` with *instruction*.

    The window uses ``/k`` so it stays open after claude exits, letting the
    user review output and continue interacting.

    Args:
        claude_exe:   Full path to claude or claude.cmd (from cfg.claude_cli_exe).
        project_path: Working directory for the new process.
        instruction:  Single-line imperative prompt for claude.

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

    try:
        if sys.platform == "win32":
            # ""outer"" wrapper: satisfies cmd.exe's multi-quoted-args quoting rule.
            # Both claude_exe and instruction may contain spaces; passing a list
            # triggers the outermost-quote-strip bug, so we use a raw string here.
            cmd_str = f'cmd.exe /k ""{claude_exe}" "{instruction}""'
            subprocess.Popen(
                cmd_str,
                cwd=project_path,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(
                [claude_exe, instruction],
                cwd=project_path,
            )
        return True, ""
    except Exception as e:
        return False, str(e)
