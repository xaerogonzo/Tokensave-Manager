"""tests/test_no_console_flash.py — every subprocess call must be windowless.

The manager is launched by `Launch TokenSave Manager.bat` through the
`python_exe` in manager-config.json, which is **pythonw.exe** — an
interpreter with no console. On Windows a console child of a console-less
parent is given a brand-new console window unless the parent passes
CREATE_NO_WINDOW, and redirecting stdio does not prevent it. Measured
directly:

    parent (pythonw) console handle          : 0
    child WITHOUT flag, capture_output=True  : console_hwnd=83169620
    child WITH    flag, capture_output=True  : console_hwnd=0

So a missing flag is not cosmetic sloppiness — it is a window that opens on
the user's screen. `scan_worktrees` alone opened thirteen of them in a
two-second burst at startup, one per indexed project, which is what sent
anyone looking for this in the first place.

This is a guard rather than a lint rule because the failure is invisible in
development: run the same code under `python.exe` (a pytest run, a terminal
launch) and the child inherits the existing console, so nothing flashes and
the bug cannot be reproduced.
"""
from __future__ import annotations

import ast
import os

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

_CALLS = {"run", "Popen", "call", "check_call", "check_output"}

# Hand-classified, one entry per function, each with the reason it is exempt.
# Keyed by function rather than line number so ordinary edits do not rot it.
_EXEMPT = {
    # POSIX-only branches. Each of these sits in the `else` of a
    # `sys.platform == "win32"` split whose Windows arm uses os.startfile,
    # so the call can never run on the platform that has this problem.
    ("controllers/ask_tab.py", "_open_session_log"),
    ("controllers/help_tab.py", "_open_doc"),
    # Same shape, but the Windows arm deliberately passes CREATE_NEW_CONSOLE:
    # these launch the Claude CLI for the user to look at and type into, so a
    # console is the feature. Suppressing it here would be a real regression.
    ("controllers/snippets.py", "_run_skill"),
    ("helpers/claude_cli.py", "spawn_claude_cli"),
    ("helpers/claude_cli.py", "spawn_claude_cli_interactive"),
}


def _subprocess_calls(tree: ast.AST):
    """Yield (lineno, enclosing_function, has_flag, has_splat) per call."""
    stack: list[str] = []

    def walk(node):
        pushed = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
            pushed = True
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in _CALLS
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "subprocess"):
                names = {k.arg for k in node.keywords}
                yield (node.lineno, stack[-1] if stack else "<module>",
                       "creationflags" in names, None in names)
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
        if pushed:
            stack.pop()

    yield from walk(tree)


def _all_sites():
    for dirpath, dirnames, filenames in os.walk(_SRC):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, _SRC).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=full)
            for lineno, func, has_flag, has_splat in _subprocess_calls(tree):
                yield rel, lineno, func, has_flag, has_splat


def test_every_subprocess_call_suppresses_its_console_window():
    offenders = [
        "%s:%d in %s()" % (rel, lineno, func)
        for rel, lineno, func, has_flag, has_splat in _all_sites()
        if not has_flag and not has_splat and (rel, func) not in _EXEMPT
    ]
    assert not offenders, (
        "These subprocess calls omit creationflags=CREATE_NO_WINDOW and will "
        "flash a console window when the manager runs under pythonw.exe:\n  "
        + "\n  ".join(offenders)
        + "\n\nAdd the flag, or add (file, function) to _EXEMPT with the "
          "reason it cannot run on Windows."
    )


def test_the_guard_actually_sees_the_calls_it_is_guarding():
    """A guard that silently matched nothing would pass forever.

    The count is asserted as a floor, not an exact figure, so adding a
    subprocess call does not fail an unrelated test.
    """
    sites = list(_all_sites())
    assert len(sites) > 100, "AST scan found only %d subprocess calls" % len(sites)
    assert sum(1 for s in sites if s[3]) > 100      # has_flag


@pytest.mark.parametrize("rel,func", sorted(_EXEMPT))
def test_each_exemption_still_points_at_real_code(rel, func):
    """Delete an exempt function and the stale entry should be noticed."""
    found = {(r, f) for r, _l, f, _h, _s in _all_sites()}
    assert (rel, func) in found, (
        "_EXEMPT lists %s::%s but no subprocess call lives there any more — "
        "remove the entry." % (rel, func))
