"""tests/test_tooltip_coverage.py — F5, pinned.

The Roadmap-9 novice audit found the Git tab explaining its buttons on hover
(19 tooltips) while Projects, Tasks, Run Checks and Test Manager had none.
The finding was that the *inconsistency* is the problem: a user who learns
that hovering explains things reasonably concludes the silent tabs have
nothing to explain — and those were the tabs carrying the unlabelled glyph
columns.

This is a structural floor, not a count to maximise. Blanketing every control
turns tooltips into noise people learn to ignore, which costs the ones that
matter, so the assertion is "this surface explains something" rather than
"every button here has a tooltip".
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: Surfaces the audit named as silent. Each must now explain at least one of
#: its controls.
_SURFACES = [
    "controllers/projects_tab.py",
    "controllers/tasks_tab.py",
    "dialogs/test_manager.py",
    "dialogs/checks_dialog.py",
    "controllers/git_tab.py",          # the one that set the standard
]


def _tooltip_count(rel: str) -> int:
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_Tooltip")


@pytest.mark.parametrize("rel", _SURFACES)
def test_every_audited_surface_explains_at_least_one_control(rel):
    assert _tooltip_count(rel) > 0, (
        "%s has no tooltips. The audit finding was the inconsistency: a tab "
        "that explains nothing reads as having nothing to explain." % rel)


def test_tooltips_say_what_happens_not_what_the_button_is_called():
    """A tooltip that restates the label is worse than none.

    Sampled on the controls whose consequences are least visible: each has to
    be a real sentence rather than an echo of its own caption.
    """
    text = (_SRC / "controllers/projects_tab.py").read_text(encoding="utf-8")
    for phrase in ("starts from nothing",        # Scaffold vs Add-to-project
                   "not moved",                   # what retrofit touches
                   "several minutes",             # Sync All's real cost
                   "does not sync"):              # Refresh is read-only
        assert phrase in text, "lost the explanation containing %r" % phrase


def test_the_destructive_test_manager_controls_are_explained():
    """Delete Test File removes a file from disk with no undo."""
    text = (_SRC / "dialogs/test_manager.py").read_text(encoding="utf-8")
    assert "Permanently delete" in text
    assert "worth fixing rather than" in text, (
        "the delete tooltip should steer away from deleting a test whose "
        "source still exists")
