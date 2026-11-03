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


# ── Continue in Cursor ───────────────────────────────────────────────────────
#
# The brief itself is tested in test_agent_handoff.py. What is checked here is
# the controller's contract: nothing is written before the person says yes, a
# Cursor row is not offered a Claude-to-Cursor handoff, and a missing CLI or
# folder is a message, not a traceback.

import types

import pytest

import controllers.tasks_tab as tasks_tab


class _SelTree(_FakeTree):
    def __init__(self, selected=()):
        super().__init__()
        self._selected = selected

    def selection(self):
        return self._selected


class _Inline:
    """threading.Thread stand-in that runs its target at start()."""

    def __init__(self, target=None, daemon=None, **_):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def handoff(monkeypatch):
    """A controller wired for the handoff path, with every side effect spied."""
    ctrl = object.__new__(TasksController)
    ctrl._tab = None
    ctrl._cfg = types.SimpleNamespace(cursor_cli_exe="agent.cmd", git_exe="git",
                                      cursor_cli_model="", claude_cli_exe="claude.cmd")
    ctrl._post = lambda fn, *a: fn(*a)
    ctrl._sess_tree = _SelTree(("sess:claude:abc",))
    ctrl._sess_rows = {"sess:claude:abc": {"session_id": "abc", "title": "T",
                                           "path": "/t/abc.jsonl"}}
    log = types.SimpleNamespace(errors=[], asked=[], prepared=[], launched=[])
    monkeypatch.setattr(tasks_tab.threading, "Thread", _Inline)
    monkeypatch.setattr(tasks_tab.messagebox, "showerror",
                        lambda title, msg, **k: log.errors.append((title, msg)))
    monkeypatch.setattr(tasks_tab.messagebox, "askyesno",
                        lambda title, msg, **k: log.asked.append(msg) or log.answer)
    log.answer = True
    import helpers.agent_handoff as ah
    import helpers.claude_tasks as ct
    # `_locate` reads the folder through claude_tasks, where the function lives.
    monkeypatch.setattr(ct, "transcript_cwd", lambda p: log.cwd)
    monkeypatch.setattr(ah, "prepare", lambda row, proj, git, source="claude": (
        log.prepared.append((row["session_id"], proj, source))
        or ah.Prepared(True, "b.md", proj, 1)))
    monkeypatch.setattr(ah, "launch_cursor", lambda prep, exe, model="": (
        log.launched.append((prep.project_path, exe)) or (True, "")))
    monkeypatch.setattr(ah, "launch_claude", lambda prep, exe, source="cursor": (
        log.launched.append((prep.project_path, exe)) or (True, "")))
    log.cwd = ""
    return ctrl, log


def test_update_remembers_each_row_by_iid_with_its_transcript_path():
    ctrl = _controller()
    ctrl._update_sess_tree([dict(_row("abc", "p", 1.0), path="/t/abc.jsonl")])
    assert ctrl._sess_rows["sess:claude:abc"]["path"] == "/t/abc.jsonl"


def test_nothing_is_written_when_the_person_declines(handoff, tmp_path):
    ctrl, log = handoff
    log.cwd = str(tmp_path)
    log.answer = False
    ctrl._sess_continue_in_cursor()
    assert log.asked and not log.prepared and not log.launched


def test_a_yes_prepares_then_launches_in_the_sessions_folder(handoff, tmp_path):
    ctrl, log = handoff
    log.cwd = str(tmp_path)
    ctrl._sess_continue_in_cursor()
    assert log.prepared == [("abc", str(tmp_path), "claude")]
    assert log.launched == [(str(tmp_path), "agent.cmd")]
    assert not log.errors


def test_the_confirmation_says_what_is_written_and_that_it_is_not_an_import(handoff, tmp_path):
    ctrl, log = handoff
    log.cwd = str(tmp_path)
    ctrl._sess_continue_in_cursor()
    text = log.asked[0]
    assert str(tmp_path) in text
    assert "not an import" in text
    assert "sends its contents to Cursor" in text


def test_a_vanished_folder_is_a_message_and_nothing_is_written(handoff, tmp_path):
    ctrl, log = handoff
    log.cwd = str(tmp_path / "gone")
    ctrl._sess_continue_in_cursor()
    assert log.errors and not log.asked and not log.prepared


def test_a_missing_cursor_cli_stops_before_anything_is_read(handoff):
    ctrl, log = handoff
    ctrl._cfg.cursor_cli_exe = ""
    ctrl._sess_continue_in_cursor()
    assert log.errors and "Cursor Agent CLI" in log.errors[0][0]
    assert not log.asked and not log.prepared


def test_a_cursor_row_is_not_handed_to_cursor(handoff):
    ctrl, log = handoff
    ctrl._sess_rows["sess:claude:abc"]["agent"] = "cursor"
    ctrl._sess_continue_in_cursor()
    assert not log.asked and not log.errors and not log.prepared


def test_no_selection_is_a_no_op(handoff):
    ctrl, log = handoff
    ctrl._sess_tree = _SelTree(())
    ctrl._sess_continue_in_cursor()
    assert not log.asked and not log.errors


def test_only_claude_rows_are_offered_the_handoff():
    ctrl = _controller()
    ctrl._update_sess_tree([_row("a", "p", 2.0), _row("b", "p", 1.0, agent="cursor")])
    assert ctrl._sess_can_hand_off("sess:claude:a") is True
    assert ctrl._sess_can_hand_off("sess:cursor:b") is False
    assert ctrl._sess_can_hand_off("sess:claude:unknown") is False


# ── The other direction, worktree folders, and the Cursor app ───────────────

def test_cursor_rows_can_go_to_claude_and_not_to_cursor():
    ctrl = _controller()
    ctrl._update_sess_tree([_row("a", "p", 2.0), _row("b", "p", 1.0, agent="cursor")])
    assert ctrl._sess_can_hand_off("sess:cursor:b", "claude") is True
    assert ctrl._sess_can_hand_off("sess:cursor:b", "cursor") is False
    assert ctrl._sess_can_hand_off("sess:claude:a", "claude") is False
    assert ctrl._sess_can_hand_off("sess:claude:a", "cursor") is True


def test_an_unknown_target_is_never_offered():
    ctrl = _controller()
    ctrl._update_sess_tree([_row("a", "p", 1.0)])
    assert ctrl._sess_can_hand_off("sess:claude:a", "codex") is False


def test_a_cursor_session_hands_off_to_claude_from_its_recorded_folder(handoff, tmp_path):
    ctrl, log = handoff
    ctrl._sess_rows = {"sess:cursor:c1": {"session_id": "c1", "title": "T",
                                          "agent": "cursor", "cwd": str(tmp_path)}}
    ctrl._sess_tree = _SelTree(("sess:cursor:c1",))
    ctrl._sess_continue_in_claude()
    # The row carried its folder, so no transcript was read to find it.
    assert log.prepared == [("c1", str(tmp_path), "cursor")]
    assert log.launched == [(str(tmp_path), "claude.cmd")]
    text = log.asked[0]
    assert "Claude's agent" in text and "Cursor" in text and "Anthropic" in text
    assert "not an import" in text


def test_the_claude_direction_needs_the_claude_cli_and_says_so(handoff):
    ctrl, log = handoff
    ctrl._cfg.claude_cli_exe = ""
    ctrl._sess_rows = {"sess:cursor:c1": {"session_id": "c1", "agent": "cursor", "cwd": "x"}}
    ctrl._sess_tree = _SelTree(("sess:cursor:c1",))
    ctrl._sess_continue_in_claude()
    assert log.errors and "Claude Code CLI" in log.errors[0][0]
    assert not log.asked and not log.prepared


def test_a_claude_row_is_not_handed_back_to_claude(handoff):
    ctrl, log = handoff
    ctrl._sess_continue_in_claude()          # the fixture's selected row is a Claude one
    assert not log.asked and not log.errors and not log.prepared


def test_a_rows_folder_is_its_recorded_cwd_only_while_it_exists(tmp_path):
    ctrl = _controller()
    ctrl._sess_rows = {"a": {"cwd": str(tmp_path)}, "b": {"cwd": str(tmp_path / "gone")},
                       "c": {}, "d": {"cwd": ""}}
    assert ctrl._sess_folder("a") == str(tmp_path)
    assert ctrl._sess_folder("b") == "" and ctrl._sess_folder("c") == ""
    assert ctrl._sess_folder("d") == "" and ctrl._sess_folder("missing") == ""


def test_double_click_opens_the_rows_own_folder_even_for_an_unknown_worktree(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(tasks_tab.os, "startfile", lambda p: opened.append(p), raising=False)
    ctrl = _controller()
    ctrl._sess_tree = _SelTree(("sess:claude:w",))
    ctrl._sess_rows = {"sess:claude:w": {"cwd": str(tmp_path)}}
    ctrl._get_known_paths = lambda: []          # a worktree is not a known project
    ctrl._sess_open_folder(None)
    assert opened == [str(tmp_path)]


def test_open_in_cursor_app_uses_the_rows_folder(monkeypatch, tmp_path):
    seen = []
    import helpers.cursor_ide as ide
    monkeypatch.setattr(ide, "open_folder", lambda folder, exe="": seen.append(folder) or (True, ""))
    ctrl = _controller()
    ctrl._tab = None
    ctrl._sess_tree = _SelTree(("sess:claude:w",))
    ctrl._sess_rows = {"sess:claude:w": {"cwd": str(tmp_path)}}
    ctrl._sess_open_in_cursor_app()
    assert seen == [str(tmp_path)]


def test_open_in_cursor_app_with_no_folder_does_nothing(monkeypatch):
    import helpers.cursor_ide as ide
    monkeypatch.setattr(ide, "open_folder", lambda *a, **k: pytest.fail("opened"))
    ctrl = _controller()
    ctrl._sess_tree = _SelTree(("sess:claude:w",))
    ctrl._sess_rows = {"sess:claude:w": {"cwd": ""}}
    ctrl._sess_open_in_cursor_app()


# -- a session whose worktree was removed -----------------------------------------

def _removed_worktree(tmp_path):
    repo = tmp_path / "Repo"
    (repo / ".git").mkdir(parents=True)
    wt = repo / ".claude" / "worktrees" / "wt"
    wt.mkdir(parents=True)                      # the empty leftover: it "exists"
    return repo, wt


def test_a_removed_worktree_continues_in_its_repository_and_says_so(handoff, tmp_path):
    ctrl, log = handoff
    repo, wt = _removed_worktree(tmp_path)
    log.cwd = str(wt)
    ctrl._sess_continue_in_cursor()
    assert "was removed" in log.asked[0] and str(wt) in log.asked[0]
    assert log.prepared == [("abc", str(repo), "claude")]
    assert log.launched == [(str(repo), "agent.cmd")]
    assert not log.errors


def test_a_removed_worktree_asks_before_writing_anything(handoff, tmp_path):
    ctrl, log = handoff
    repo, wt = _removed_worktree(tmp_path)
    log.cwd = str(wt)
    log.answer = False
    ctrl._sess_continue_in_cursor()
    assert log.asked and not log.prepared and not log.launched


def test_a_worktree_with_no_repository_behind_it_is_still_an_error(handoff, tmp_path):
    ctrl, log = handoff
    wt = tmp_path / "Repo" / ".claude" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    log.cwd = str(wt)
    ctrl._sess_continue_in_cursor()
    assert log.errors and not log.asked and not log.prepared


def test_the_empty_leftover_is_not_a_present_folder_for_the_row(tmp_path):
    ctrl = _controller()
    repo, wt = _removed_worktree(tmp_path)
    ctrl._sess_rows = {"a": {"cwd": str(wt)}}
    assert ctrl._sess_folder("a") == ""
