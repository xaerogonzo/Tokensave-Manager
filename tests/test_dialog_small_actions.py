"""tests/test_dialog_small_actions.py — three more untested action dialogs.

`SwitchBranchDialog`, `AssignCategoryDialog` and `SnippetEditDialog`. Small
dialogs, but each has a rule that is invisible from the widgets and wrong in
a way that would not look wrong:

* switching to the **separator row** — the branch list draws a divider
  between local and remote, and it is a selectable listbox row. Acting on it
  would hand a divider string to `git checkout`;
* remote branches are drawn with a `↓ ` prefix that is decoration, not part
  of the branch name;
* a blank snippet body is **rejected** for a user snippet and **accepted**
  for a built-in override, where blank is how you revert to the default. A
  guard that treated them alike would either block the revert or write empty
  prompts.

Same construction as `tests/test_dialog_gitignore.py`: ``object.__new__``
plus the attributes each method actually reads.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.assign_category import AssignCategoryDialog
from dialogs.snippet_edit import SnippetEditDialog
from dialogs.switch_branch import SwitchBranchDialog


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _Recorder:
    def __init__(self):
        self.calls: list = []
        self.destroyed = False

    def callback(self, *args):
        self.calls.append(args)

    def destroy(self):
        self.destroyed = True


@pytest.fixture()
def no_warnings(monkeypatch):
    shown: list = []

    class _Box:
        @staticmethod
        def showwarning(title, message, **kw):
            shown.append(title)

    for mod in ("switch_branch", "assign_category", "snippet_edit"):
        monkeypatch.setattr(f"dialogs.{mod}.messagebox", _Box)
    return shown


# ── SwitchBranchDialog ──────────────────────────────────────────────────

class _Listbox:
    def __init__(self, rows, selection):
        self._rows = rows
        self._sel = selection

    def curselection(self):
        return self._sel

    def get(self, idx):
        return self._rows[idx]


_ROWS = ["main", "feature/x", "── remote ──", "↓ origin/main"]


def _switcher(selection, remote_start=2):
    rec = _Recorder()
    dlg = object.__new__(SwitchBranchDialog)
    dlg._lb = _Listbox(_ROWS, selection)
    dlg._remote_start = remote_start
    dlg._path = "C:/repo"
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_switch_refuses_with_nothing_selected(no_warnings):
    dlg, rec = _switcher(())
    dlg._switch()
    assert rec.calls == []


def test_switch_ignores_the_separator_row(no_warnings):
    """The divider is a real, selectable listbox row.

    Without this guard the dialog would close and hand '── remote ──' to
    git checkout.
    """
    dlg, rec = _switcher((2,))
    dlg._switch()
    assert rec.calls == []
    assert not rec.destroyed


def test_switch_passes_a_local_branch_unchanged(no_warnings):
    dlg, rec = _switcher((1,))
    dlg._switch()
    assert rec.calls == [("C:/repo", "feature/x")]
    assert rec.destroyed


def test_switch_strips_the_remote_arrow_decoration(no_warnings):
    """`↓ ` is how the list marks a remote; it is not part of the name."""
    dlg, rec = _switcher((3,))
    dlg._switch()
    assert rec.calls == [("C:/repo", "origin/main")]


# ── AssignCategoryDialog ────────────────────────────────────────────────

def _assigner(cat, subcat=""):
    rec = _Recorder()
    dlg = object.__new__(AssignCategoryDialog)
    dlg._cat_var = _Var(cat)
    dlg._sub_var = _Var(subcat)
    dlg._path = "C:/repo"
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_assign_category_refuses_an_empty_category(no_warnings):
    dlg, rec = _assigner("   ")
    dlg._ok()
    assert rec.calls == []


def test_assign_category_passes_category_and_subcategory(no_warnings):
    dlg, rec = _assigner("  Tools  ", "  CLI  ")
    dlg._ok()
    assert rec.calls == [("C:/repo", "Tools", "CLI")]
    assert rec.destroyed


def test_clearing_the_override_sends_none_not_an_empty_string(no_warnings):
    """None means 'restore the default'. An empty string would be a category
    literally named "", which is a different and wrong instruction — and
    `_clear` deliberately bypasses the empty-category guard to say it."""
    dlg, rec = _assigner("")
    dlg._clear()
    assert rec.calls == [("C:/repo", None, "")]
    assert rec.destroyed


# ── SnippetEditDialog ───────────────────────────────────────────────────

class _Body:
    def __init__(self, text):
        self._text = text

    def get(self, _start, _end):
        return self._text


def _editor(title, body, read_only_title=False):
    rec = _Recorder()
    dlg = object.__new__(SnippetEditDialog)
    dlg._title_var = _Var(title)
    dlg._body_txt = _Body(body)
    dlg._read_only_title = read_only_title
    dlg._edit_meta = {"key": "demo"}
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_snippet_refuses_an_empty_title(no_warnings):
    dlg, rec = _editor("   ", "some prompt text")
    dlg._save()
    assert rec.calls == []


def test_snippet_flattens_a_multiline_title():
    """A newline in the title would break the single-line list rendering."""
    dlg, rec = _editor("two\nlines", "body")
    dlg._save()
    assert rec.calls[0][0] == "two lines"


def test_snippet_refuses_an_empty_body_for_a_user_snippet(no_warnings):
    dlg, rec = _editor("Title", "   ")
    dlg._save()
    assert rec.calls == [], "an empty user snippet would save a blank prompt"


def test_snippet_ALLOWS_an_empty_body_for_a_builtin_override(no_warnings):
    """Blank is how a built-in override is reverted to the default.

    The asymmetry with the test above is the whole rule: same empty body,
    opposite correct behaviour, decided by `_read_only_title`.
    """
    dlg, rec = _editor("Built-in", "  ", read_only_title=True)
    dlg._save()
    assert rec.calls == [("Built-in", "", {"key": "demo"})]
    assert rec.destroyed
