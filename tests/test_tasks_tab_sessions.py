"""Tasks tab session list: one row per session, whatever the scanners return.

Found by the live drive ledger: `TclError: Item sess:claude:<id> already
exists` from `_update_sess_tree`. The same session id really does occur twice
on disk -- a worktree's transcript folder and its sibling each hold a file that
announces the same `sessionId` -- so the scanner is not wrong to report both.
The list is the place that must not raise.

No Tk window is created: the controller is built without `__init__` and given
a stand-in tree that refuses a duplicate iid exactly as ttk.Treeview does.
"""
import tkinter as tk

from controllers.tasks_tab import TasksController


class _FakeTree:
    """Just enough Treeview: insert() raises TclError on an existing iid."""

    def __init__(self):
        self.rows = {}

    def get_children(self):
        return tuple(self.rows)

    def delete(self, iid):
        del self.rows[iid]

    def item(self, iid, values=None):
        self.rows[iid] = values

    def insert(self, parent, index, iid=None, values=()):
        if iid in self.rows:
            raise tk.TclError("Item %s already exists" % iid)
        self.rows[iid] = values


def _controller():
    ctrl = object.__new__(TasksController)
    ctrl._sess_tree = _FakeTree()
    return ctrl


def _row(session_id, project, activity, agent=None, title="t"):
    row = {"session_id": session_id, "title": title,
           "project_display": project, "last_activity": activity,
           "is_recent": False}
    if agent:
        row["agent"] = agent
    return row


def test_a_duplicate_session_id_does_not_raise_and_shows_one_row():
    ctrl = _controller()
    ctrl._update_sess_tree([
        _row("abc", "wt-a", 200.0),
        _row("abc", "wt-b", 100.0),
    ])
    assert list(ctrl._sess_tree.rows) == ["sess:claude:abc"]


def test_the_most_recent_duplicate_wins_whatever_the_input_order():
    for rows in ([_row("abc", "newer", 200.0), _row("abc", "older", 100.0)],
                 [_row("abc", "older", 100.0), _row("abc", "newer", 200.0)]):
        ctrl = _controller()
        ctrl._update_sess_tree(rows)
        values = ctrl._sess_tree.rows["sess:claude:abc"]
        assert values[3] == "newer"


def test_a_duplicate_does_not_hide_a_different_session():
    ctrl = _controller()
    ctrl._update_sess_tree([
        _row("abc", "a", 300.0),
        _row("xyz", "b", 250.0),
        _row("abc", "c", 100.0),
    ])
    assert set(ctrl._sess_tree.rows) == {"sess:claude:abc", "sess:claude:xyz"}


def test_same_id_under_different_agents_is_still_two_rows():
    ctrl = _controller()
    ctrl._update_sess_tree([
        _row("abc", "a", 2.0, agent="cursor"),
        _row("abc", "b", 1.0),
    ])
    assert set(ctrl._sess_tree.rows) == {"sess:cursor:abc", "sess:claude:abc"}


def test_a_refresh_with_a_duplicate_updates_in_place():
    ctrl = _controller()
    ctrl._update_sess_tree([_row("abc", "a", 1.0)])
    ctrl._update_sess_tree([_row("abc", "a", 2.0), _row("abc", "b", 1.0)])
    assert list(ctrl._sess_tree.rows) == ["sess:claude:abc"]
