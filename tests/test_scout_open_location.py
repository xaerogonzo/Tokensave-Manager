"""tests/test_scout_open_location.py — jumping from a finding to its line.

A refactor-scout finding is the only Manager output that already knows a
location: it carries `file` and `line`. (Doctor violations are plain strings —
`"_render_projects_section() complexity 20 (cap 10)"` — with no line number, so
they cannot drive this without first resolving the symbol.)

`_open_location` is exercised directly on a stub rather than through a
constructed dialog. It reads exactly one attribute, so building a Toplevel to
reach it would test Tk rather than the behaviour, and would put a `tk` marker
on something that has nothing to do with the display.

The contract worth pinning is the negative one: with no handler injected the
method must do nothing. The dialog only paints the label as a link when a
handler exists, and a link that silently does nothing is worse than plain text.
"""
from __future__ import annotations

from dialogs.refactor_scout import RefactorScoutDialog
from helpers.refactor_scout import Finding


def _finding(file="src/dialogs/mcp_config.py", line=345) -> Finding:
    return Finding(
        id="f1", kind="god_method", file=file, line=line,
        symbol="_render_projects_section", message="124 lines (cap 100)")


class _Stub:
    """Just enough of the dialog for the method under test."""

    def __init__(self, handler=None):
        self._on_open_location = handler

    _open_location = RefactorScoutDialog._open_location


def test_the_handler_receives_the_findings_file_and_line():
    seen = []
    _Stub(lambda f, ln: seen.append((f, ln)))._open_location(_finding())
    assert seen == [("src/dialogs/mcp_config.py", 345)]


def test_without_a_handler_it_does_nothing():
    """The label is not painted as a link in this case; clicking cannot happen,
    but the method must still be safe if it ever is called."""
    _Stub(None)._open_location(_finding())      # must not raise


def test_the_path_is_passed_through_unresolved():
    """Findings carry repo-relative paths; joining them to the project root is
    the controller's job, because only it knows which project this is."""
    seen = []
    _Stub(lambda f, ln: seen.append(f))._open_location(_finding(file="a/b.py"))
    assert seen == ["a/b.py"], "the dialog must not resolve paths itself"


def test_the_line_survives_as_an_integer():
    """`goto_argv` coerces, but a float reaching it would mean the finding was
    mangled somewhere upstream."""
    seen = []
    _Stub(lambda f, ln: seen.append(ln))._open_location(_finding(line=1))
    assert seen == [1]
    assert isinstance(seen[0], int)
