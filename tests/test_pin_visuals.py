"""tests/test_pin_visuals.py — an indicator without a control is worse than neither.

## The mistake this exists to stop repeating

`★ Set as Active` was removed from the right-click menu when Claude Desktop
chat is off, because nothing reads the pin in that state. The two things that
DRAW the pin were left alone: the green ★ against one project row, and the
header badge reading "OpenChem Studio (manager default)".

The result was the worst available combination — a visible setting, marked on
a specific project, with no way anywhere in the UI to change or clear it. A
user seeing it went looking for the control, which is strictly more work than
if it had never been drawn.

So the rule is: the ★ column and the header badge are claims ABOUT THE PIN,
and they render only while something reads it. `active_path` is separate and
survives, because the Git tab's "open this project when nothing is selected"
convenience has nothing to do with MCP.

No Tk here: `_pin_is_live` is a cached boolean and the badge decision is one
branch, so both are tested against a stub rather than a rendered window.
"""
from __future__ import annotations


import app as app_module


class _Badge:
    """Just enough tk.Label for the branch under test."""

    def __init__(self):
        self.text = None
        self.packed = True

    def config(self, text=None, **kw):
        self.text = text

    def pack(self, **kw):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def winfo_manager(self):
        return "pack" if self.packed else ""


class _App:
    def __init__(self, active_path, live):
        self.active_badge = _Badge()
        self.active_path = active_path
        self._live = live
        self._update = app_module.App._update_active_badge.__get__(self)
        self._pin_tag = app_module.App._pin_tag

    def _pin_is_live(self):
        return self._live


def test_the_badge_is_hidden_when_nothing_reads_the_pin(mocker):
    """Hidden, not blanked: the badge carries its own background and padding,
    so empty text leaves a coloured rectangle that reads as a render fault."""
    a = _App(r"D:\Random Projects\OpenChem Studio", live=False)

    a._update(False)

    assert a.active_badge.packed is False
    assert a.active_badge.text is None, "must not also write text"


def test_the_badge_returns_when_the_pin_is_live_again(mocker):
    mocker.patch("app.get_pinned",
                 return_value=r"D:\Random Projects\OpenChem Studio")
    a = _App(r"D:\Random Projects\OpenChem Studio", live=True)
    a.active_badge.packed = False

    a._update(True)

    assert a.active_badge.packed is True
    assert "OpenChem Studio" in a.active_badge.text
    assert "serves MCP" in a.active_badge.text


def test_an_unpinned_but_live_machine_says_auto(mocker):
    mocker.patch("app.get_pinned", return_value=None)
    a = _App(r"D:\Random Projects\Fortuna Lab", live=True)

    a._update(True)

    assert "auto" in a.active_badge.text


def test_no_projects_still_says_so(mocker):
    mocker.patch("app.get_pinned", return_value=None)
    a = _App(None, live=True)

    a._update(True)

    assert "No project" in a.active_badge.text


def test_the_tag_no_longer_has_a_state_for_a_dead_pin():
    """"manager default" was the label for a pin nothing read. It described a
    setting with no control, which is the whole defect — so the state is gone
    rather than reworded, and the badge is hidden instead."""
    assert app_module.App._pin_tag(True) == "pinned · serves MCP"
    assert app_module.App._pin_tag(False) == "auto"
    for value in (True, False):
        assert "manager default" not in app_module.App._pin_tag(value)


def test_the_star_column_is_driven_by_the_same_fact():
    """`refresh` passes None for the starred row when the pin is dead, which
    is what removes the ★ from the tree. Asserted structurally: the ★ comes
    from `is_active`, and `is_active` comes from that argument."""
    import ast
    import inspect

    src = inspect.getsource(app_module.App.refresh)
    call = next(n for n in ast.walk(ast.parse(src.strip()))
                if isinstance(n, ast.Call)
                and "rebuild_tree" in ast.unparse(n.func))
    starred = ast.unparse(call.args[1])

    assert "pin_live" in starred, (
        "the starred row must be gated on whether the pin is live; got %r"
        % starred)
