"""tests/test_dialog_git_actions.py — the guards on four git-action dialogs.

`NewBranchDialog`, `SetRemoteDialog`, `UntrackIgnoredDialog` and
`MergePRDialog` had no test of any kind. All four take user input and hand it
to something that changes a repository — a branch create, a remote rewrite,
`git rm --cached`, and `gh pr merge` — so the interesting part is not the
widgets but the gate in front of the callback: **which inputs are refused,
and does refusing actually stop the action.**

That distinction is the whole point. A dialog that shows a warning and then
invokes the callback anyway looks correct on screen and is worse than no
validation at all, because the warning implies nothing happened. Every test
here asserts on the CALLBACK, not on the warning.

Built with ``object.__new__`` and the handful of attributes each method
reads, following ``tests/test_dialog_gitignore.py``: standing up four
Toplevels would test Tk rather than the gates.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.merge_pr import MergePRDialog
from dialogs.new_branch import NewBranchDialog
from dialogs.set_remote import SetRemoteDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog


class _Var:
    """Stand-in for a Tk variable — the dialogs only ever call .get()/.set()."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _Recorder:
    """Captures the callback and whether the dialog closed."""

    def __init__(self):
        self.calls: list = []
        self.destroyed = False

    def callback(self, *args):
        self.calls.append(args)

    def destroy(self):
        self.destroyed = True


@pytest.fixture()
def no_warnings(monkeypatch):
    """Silence the modal boxes and record that one was raised.

    Patched at each dialog's own import site, which is where the name is
    looked up — patching ``tkinter.messagebox`` would miss.
    """
    shown: list = []

    class _Box:
        @staticmethod
        def showwarning(title, message, **kw):
            shown.append(("warning", title))

        @staticmethod
        def askyesno(title, message, **kw):
            shown.append(("askyesno", title))
            return _Box.answer

    _Box.answer = True
    for mod in ("new_branch", "set_remote", "untrack_ignored", "merge_pr"):
        monkeypatch.setattr(f"dialogs.{mod}.messagebox", _Box)
    return _Box, shown


# ── NewBranchDialog ─────────────────────────────────────────────────────

def _new_branch(name, switch=True):
    rec = _Recorder()
    dlg = object.__new__(NewBranchDialog)
    dlg._name_var = _Var(name)
    dlg._switch_var = _Var(switch)
    dlg._path = "C:/repo"
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_new_branch_refuses_an_empty_name(no_warnings):
    dlg, rec = _new_branch("   ")
    dlg._create()
    assert rec.calls == [], "an empty name must not reach the branch create"
    assert not rec.destroyed


def test_new_branch_refuses_a_name_with_spaces(no_warnings):
    """git would reject it, but only after the dialog has closed."""
    dlg, rec = _new_branch("my feature")
    dlg._create()
    assert rec.calls == []


def test_new_branch_passes_a_valid_name_through(no_warnings):
    dlg, rec = _new_branch("  my-feature  ", switch=False)
    dlg._create()
    assert rec.calls == [("C:/repo", "my-feature", False)], \
        "the name must arrive stripped, with the switch flag"
    assert rec.destroyed


# ── SetRemoteDialog ─────────────────────────────────────────────────────

def _set_remote(url):
    rec = _Recorder()
    dlg = object.__new__(SetRemoteDialog)
    dlg._url_var = _Var(url)
    dlg._path = "C:/repo"
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_set_remote_refuses_an_empty_url(no_warnings):
    dlg, rec = _set_remote("  ")
    dlg._save()
    assert rec.calls == []


def test_set_remote_refuses_something_that_is_not_a_remote(no_warnings):
    """A bare path here would be written into .git/config as an origin."""
    dlg, rec = _set_remote("github.com/user/repo")
    dlg._save()
    assert rec.calls == []


@pytest.mark.parametrize("url", [
    "https://github.com/user/repo.git",
    "http://example.com/repo.git",
    "git@github.com:user/repo.git",
])
def test_set_remote_accepts_the_forms_git_understands(no_warnings, url):
    dlg, rec = _set_remote(f"  {url} ")
    dlg._save()
    assert rec.calls == [("C:/repo", url)]


# ── UntrackIgnoredDialog ────────────────────────────────────────────────

def _untrack(selection):
    """*selection* is a list of (ticked, filename)."""
    rec = _Recorder()
    dlg = object.__new__(UntrackIgnoredDialog)
    dlg._file_vars = [(_Var(ticked), name) for ticked, name in selection]
    dlg._path = "C:/repo"
    dlg._on_confirm = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_untrack_refuses_when_nothing_is_ticked(no_warnings):
    dlg, rec = _untrack([(False, "a.txt"), (False, "b.txt")])
    dlg._apply()
    assert rec.calls == [], "git rm --cached must not run on an empty set"
    assert not rec.destroyed


def test_untrack_passes_only_the_ticked_files(no_warnings):
    """The unticked ones are the point — this is a destructive operation."""
    dlg, rec = _untrack([(True, "a.txt"), (False, "b.txt"), (True, "c.txt")])
    dlg._apply()
    assert rec.calls == [("C:/repo", ["a.txt", "c.txt"])]


def test_untrack_set_all_ticks_every_row():
    dlg, _ = _untrack([(False, "a.txt"), (False, "b.txt")])
    dlg._set_all(True)
    assert [v.get() for v, _ in dlg._file_vars] == [True, True]


def test_untrack_reads_the_path_before_closing(no_warnings):
    """`self._path` is read before destroy(); a Tk widget torn down first
    would raise on attribute access, so the order is load-bearing."""
    dlg, rec = _untrack([(True, "a.txt")])
    dlg._apply()
    assert rec.destroyed
    assert rec.calls[0][0] == "C:/repo"


# ── MergePRDialog ───────────────────────────────────────────────────────

class _Tree:
    def __init__(self, selection):
        self._sel = selection

    def selection(self):
        return self._sel


_PRS = [{"number": 7, "title": "Fix the thing", "headRefName": "fix",
         "baseRefName": "master", "additions": 3, "deletions": 1}]


def _merge(selection, delete_branch=True, changelog=False):
    rec = _Recorder()
    dlg = object.__new__(MergePRDialog)
    dlg._tv = _Tree(selection)
    dlg._prs = _PRS
    dlg._path = "C:/repo"
    dlg._var_delete_branch = _Var(delete_branch)
    dlg._var_changelog_body = _Var(changelog)
    dlg._callback = rec.callback
    dlg.destroy = rec.destroy
    return dlg, rec


def test_merge_does_nothing_without_a_selection(no_warnings):
    dlg, rec = _merge(())
    dlg._confirm("merge")
    assert rec.calls == []


def test_merge_ignores_a_selection_that_is_not_a_pr_number(no_warnings):
    """Treeview iids are strings; a non-numeric one must not raise."""
    dlg, rec = _merge(("not-a-number",))
    dlg._confirm("merge")
    assert rec.calls == []


def test_merge_ignores_a_pr_number_it_does_not_know(no_warnings):
    dlg, rec = _merge(("999",))
    dlg._confirm("merge")
    assert rec.calls == []


def test_merge_respects_a_declined_confirmation(no_warnings):
    """The last gate before `gh pr merge` pushes to GitHub."""
    box, _ = no_warnings
    box.answer = False
    dlg, rec = _merge(("7",))
    dlg._confirm("squash")
    assert rec.calls == []
    assert not rec.destroyed


def test_merge_forwards_the_strategy_and_both_options(no_warnings):
    dlg, rec = _merge(("7",), delete_branch=False, changelog=True)
    dlg._confirm("rebase")
    assert rec.calls == [
        ("C:/repo", 7, "rebase", False, "Fix the thing", True)
    ]
    assert rec.destroyed


def test_merge_asks_before_it_acts(no_warnings):
    """A merge that pushed without confirming would be unrecoverable."""
    _, shown = no_warnings
    dlg, _rec = _merge(("7",))
    dlg._confirm("merge")
    assert ("askyesno", "Merge Pull Request?") in shown
