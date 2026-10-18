"""live_sessions: which sessions are running now, and is tokensave serving each?

The process table is constructed, never read from the machine. The shapes are the
ones measured on 2026-10-18: a desktop-started session is a `claude.exe` carrying
`--output-format stream-json`, `--input-format stream-json` and `--resume=<uuid>`,
and its tokensave server is a child. The properties pinned here are the D1b ones:
failure to look is never "none running", and SERVING needs authoritative evidence.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers import live_sessions as ls
from helpers.tokensave_daemon import (
    AUTHORITATIVE, EnumerationFailed, HEURISTIC, TokensaveServer, UNATTRIBUTED,
)

SID_A = "2ab0c6fd-b193-4829-a196-282f567a03a2"
SID_B = "4aaecea2-70c0-4c25-9fff-55636fdfc051"
CMD = ("C:\\x\\claude.exe --output-format stream-json --verbose "
       "--input-format stream-json --effort=%s --model=%s "
       "--permission-prompt-tool=stdio --resume=%s --allowedTools=mcp__a")


def proc(pid, ppid, name, cmd="", started=1000.0):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd,
            "started_at": started}


def session_row(pid, sid=SID_A, effort="high", model="claude-sonnet-5-5",
                ppid=1, started=1000.0):
    return proc(pid, ppid, "claude.exe", CMD % (effort, model, sid), started)


def server(pid, project, attribution=AUTHORITATIVE):
    return TokensaveServer(pid=pid, command_line="", started_at=1.0,
                           project=project, attribution=attribution)


def run(rows, servers=(), project="", servers_error=None, tmp=None):
    def list_servers(exe, projects, strict=False):
        assert strict, "must ask strictly, or a failed scan reads as none"
        if servers_error:
            raise servers_error
        return list(servers)

    return ls.collect(
        "ts.exe", [], list_rows=lambda: rows, list_servers=list_servers,
        find_transcript=lambda sid: "t" if sid else "",
        last_cwd=lambda p: project, title_of=lambda p: "a title")


@pytest.fixture
def folders(tmp_path):
    root = tmp_path / "proj"
    wt = root / ".claude" / "worktrees" / "wt1"
    other = tmp_path / "elsewhere"
    for d in (root, wt, other):
        d.mkdir(parents=True)
    return str(root), str(wt), str(other)


# -- recognising a session --------------------------------------------------

def test_a_desktop_started_session_is_parsed_from_its_command_line():
    sid, model, effort = ls.parse_session_command(
        CMD % ("xhigh", "claude-opus-5", SID_A))
    assert (sid, model, effort) == (SID_A, "claude-opus-5", "xhigh")


def test_flag_order_does_not_matter():
    cmd = ("claude.exe --output-format stream-json --input-format stream-json "
           "--thinking=adaptive --model=m1 --effort=max --resume=%s" % SID_A)
    assert ls.parse_session_command(cmd) == (SID_A, "m1", "max")


def test_a_process_without_the_stream_flags_is_not_a_session():
    """The Desktop app's own helpers and a terminal `claude` look different."""
    assert ls.parse_session_command(
        "Claude.exe --type=renderer --user-data-dir=x") is None
    assert ls.parse_session_command("claude.exe --resume=%s" % SID_A) is None


def test_a_session_with_no_resume_id_is_still_a_session_with_an_unknown_id():
    cmd = ("claude.exe --output-format stream-json --input-format stream-json "
           "--model=m --effort=low")
    assert ls.parse_session_command(cmd) == ("", "m", "low")


# -- the process tree ---------------------------------------------------------

def test_descendants_walks_through_an_intermediate_wrapper():
    rows = [proc(1, 0, "claude.exe"), proc(2, 1, "python.exe"),
            proc(3, 2, "tokensave.exe"), proc(9, 0, "tokensave.exe")]
    assert [r["pid"] for r in ls.descendants(rows, 1)] == [2, 3]


def test_descendants_survives_a_cycle():
    rows = [proc(1, 2, "a"), proc(2, 1, "b")]
    assert {r["pid"] for r in ls.descendants(rows, 1)} == {2}


# -- tokensave state per session ----------------------------------------------

def test_serving_needs_an_authoritative_server_on_the_sessions_own_folder(folders):
    root, _wt, _other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe")],
               [server(11, root)], project=root)
    (s,) = snap.sessions
    assert s.state == ls.SERVING and s.server_pids == (11,)


def test_a_server_on_a_different_folder_is_reported_as_other_not_serving(folders):
    root, _wt, other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe")],
               [server(11, other)], project=root)
    assert snap.sessions[0].state == ls.OTHER_FOLDER
    assert snap.sessions[0].server_project == other


def test_a_worktree_session_served_by_its_parent_says_so(folders):
    root, wt, _other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe")],
               [server(11, root)], project=wt)
    s = snap.sessions[0]
    assert s.state == ls.OTHER_FOLDER
    assert "inside the served project" in s.detail


def test_a_guessed_server_project_is_never_serving(folders):
    root, _wt, _other = folders
    for attribution in (HEURISTIC, UNATTRIBUTED):
        snap = run([session_row(10), proc(11, 10, "tokensave.exe")],
                   [server(11, root, attribution)], project=root)
        assert snap.sessions[0].state == ls.UNVERIFIED, attribution


def test_an_unreadable_session_folder_is_unverified_not_other(folders):
    root, _wt, _other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe")],
               [server(11, root)], project="")
    s = snap.sessions[0]
    assert s.state == ls.UNVERIFIED and "folder could not be read" in s.detail


def test_no_tokensave_process_under_the_session_is_none(folders):
    root, _wt, _other = folders
    snap = run([session_row(10), proc(12, 1, "node.exe")], [], project=root)
    assert snap.sessions[0].state == ls.NO_SERVER


def test_two_servers_under_one_session_is_multiple_not_a_guess(folders):
    root, _wt, _other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe"),
                proc(12, 10, "tokensave.exe")],
               [server(11, root), server(12, root)], project=root)
    assert snap.sessions[0].state == ls.MULTIPLE
    assert snap.sessions[0].server_pids == (11, 12)


def test_a_failed_server_list_makes_servers_unverified_never_absent_or_serving(
        folders):
    root, _wt, _other = folders
    snap = run([session_row(10), proc(11, 10, "tokensave.exe")], [],
               project=root, servers_error=EnumerationFailed("boom"))
    s = snap.sessions[0]
    assert s.state == ls.UNVERIFIED
    # The reason must say the LIST failed, not that this process was missing
    # from it: those are different facts and only one is a statement about it.
    assert "could not be read" in s.detail
    assert snap.servers_failed == "boom"
    assert "server list unavailable" in snap.summary


# -- failure is not "none running" ----------------------------------------------

def test_an_unreadable_process_table_is_a_failure_not_an_empty_list():
    def broken():
        raise EnumerationFailed("PowerShell could not be located")
    snap = ls.collect("ts.exe", [], list_rows=broken)
    assert snap.failed and snap.sessions == ()
    assert "Could not check" in snap.summary
    assert "No Claude Code sessions" not in snap.summary


def test_no_sessions_running_is_a_real_zero():
    snap = run([proc(1, 0, "explorer.exe")])
    assert snap.failed == "" and snap.sessions == ()
    assert "No Claude Code sessions" in snap.summary


# -- servers under no session ------------------------------------------------------

def test_a_server_under_no_running_session_is_listed_as_a_stray(folders):
    root, _wt, other = folders
    rows = [session_row(10), proc(11, 10, "tokensave.exe"),
            proc(40, 5, "tokensave.exe")]
    snap = run(rows, [server(11, root), server(40, other)], project=root)
    assert [s.pid for s in snap.strays] == [40]
    assert "1 tokensave server under no running session" in snap.summary


def test_the_summary_counts_each_state_and_gives_no_single_verdict(folders):
    root, _wt, other = folders
    rows = [session_row(10, SID_A), proc(11, 10, "tokensave.exe"),
            session_row(20, SID_B), proc(21, 20, "tokensave.exe"),
            session_row(30, SID_B)]
    snap = run(rows, [server(11, root), server(21, other)], project=root)
    assert snap.counts == {ls.SERVING: 1, ls.OTHER_FOLDER: 1, ls.UNVERIFIED: 0,
                           ls.NO_SERVER: 1, ls.MULTIPLE: 0}
    assert snap.summary.startswith(
        "3 sessions running  ·  tokensave: 1 serving their folder, 1 serving "
        "another folder, 0 unconfirmed, 1 none")


# -- the process table parser --------------------------------------------------------

def test_parse_process_rows_handles_one_object_and_many():
    one = json.dumps({"ProcessId": 1, "ParentProcessId": 0, "Name": "Claude.exe",
                      "Cmd": "x", "Start": 5})
    assert ls.parse_process_rows(one)[0]["name"] == "claude.exe"
    many = json.dumps([{"ProcessId": 1, "ParentProcessId": 0, "Name": "a"},
                       {"ProcessId": 2, "ParentProcessId": 1, "Name": "b"}])
    assert [r["pid"] for r in ls.parse_process_rows(many)] == [1, 2]


def test_parse_process_rows_skips_a_row_without_ids_and_survives_garbage():
    assert ls.parse_process_rows("not json") == []
    assert ls.parse_process_rows("") == []
    rows = json.dumps([{"Name": "x"}, {"ProcessId": 3, "ParentProcessId": 1,
                                       "Name": "y", "Start": None}])
    assert [r["pid"] for r in ls.parse_process_rows(rows)] == [3]


# -- the session's folder, from the tail of its transcript -------------------------

def _transcript(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_the_last_cwd_wins_over_the_first(tmp_path):
    """A session can move; the measured one passed through 127 folders."""
    path = _transcript(tmp_path, [json.dumps({"cwd": "D:\\first"}),
                                  json.dumps({"x": 1}),
                                  json.dumps({"cwd": "D:\\last"}),
                                  json.dumps({"x": 2})])
    assert ls.read_last_cwd(path) == "D:\\last"


def test_only_the_tail_is_read_and_a_cut_first_line_is_tolerated(tmp_path):
    filler = json.dumps({"cwd": "D:\\old", "pad": "z" * 2000})
    path = _transcript(tmp_path, [filler] * 400
                       + [json.dumps({"cwd": "D:\\recent"})])
    # A tail far smaller than the file starts mid-line: that fragment must be
    # skipped, not crash, and the head of the file must not be needed.
    assert ls.read_last_cwd(path, tail_bytes=4096) == "D:\\recent"


def test_a_missing_or_cwd_less_transcript_is_unknown_not_a_guess(tmp_path):
    assert ls.read_last_cwd(str(tmp_path / "nope.jsonl")) == ""
    assert ls.read_last_cwd(_transcript(tmp_path, [json.dumps({"x": 1})])) == ""


def test_locate_transcript_finds_the_file_by_session_id(tmp_path, monkeypatch):
    d = tmp_path / "projects" / "D--some-project"
    d.mkdir(parents=True)
    (d / (SID_A + ".jsonl")).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ls, "_claude_projects_dir", lambda: str(tmp_path / "projects"))
    assert ls.locate_transcript(SID_A).endswith(SID_A + ".jsonl")
    assert ls.locate_transcript("") == ""
    assert ls.locate_transcript(SID_B) == ""


def test_enumeration_off_windows_is_a_failure_not_an_empty_table(monkeypatch):
    monkeypatch.setattr(ls.sys, "platform", "linux")
    with pytest.raises(EnumerationFailed):
        ls.enumerate_processes()


# -- wording ---------------------------------------------------------------------

def _live(**kw):
    base = dict(pid=1, started_at=0.0, session_id=SID_A, model="claude-sonnet-5-5",
                effort="high", project="", title="", state=ls.NO_SERVER)
    base.update(kw)
    return ls.LiveSession(**base)


def test_age_text_uses_the_largest_whole_unit_and_blanks_an_unknown_start():
    assert ls.age_text(1000, 1000 + 5 * 60) == "5m"
    assert ls.age_text(1000, 1000 + 3 * 3600 + 59) == "3h"
    assert ls.age_text(1000, 1000 + 2 * 86400) == "2d"
    assert ls.age_text(0, 99999) == ""
    assert ls.age_text(2000, 1000) == "0m", "a clock skew must not go negative"


def test_model_text_drops_the_vendor_prefix_and_tolerates_missing_parts():
    assert ls.model_text(_live()) == "sonnet-5-5 · high"
    assert ls.model_text(_live(model="", effort="max")) == "max"
    assert ls.model_text(_live(model="", effort="")) == ""


def test_server_text_says_what_is_known_and_no_more(folders):
    root, wt, other = folders
    name = os.path.basename
    assert ls.server_text(_live(state=ls.SERVING)) == "✓ serving this folder"
    assert ls.server_text(_live(state=ls.NO_SERVER)) == "✗ no tokensave server"
    assert ls.server_text(_live(state=ls.MULTIPLE, server_pids=(1, 2))) == "? 2 servers"
    assert ls.server_text(_live(state=ls.UNVERIFIED, detail="why")) == "? why"
    assert ls.server_text(_live(state=ls.UNVERIFIED)) == "? unconfirmed"
    assert ls.server_text(_live(state=ls.OTHER_FOLDER, server_project=other))         == "⚠ serving %s" % name(other)
    inside = ls.server_text(_live(state=ls.OTHER_FOLDER, server_project=root,
                                  detail="inside"))
    assert inside == "⚠ serving %s (this folder is inside it)" % name(root)


def test_running_session_ids_are_the_resume_ids_of_the_live_sessions(monkeypatch):
    rows = [session_row(10, SID_A), session_row(20, SID_B),
            proc(30, 1, "claude.exe", "Claude.exe --type=renderer"),
            proc(40, 1, "node.exe", CMD % ("low", "m", SID_A))]
    monkeypatch.setattr(ls, "enumerate_processes", lambda: rows)
    assert ls.running_session_ids() == frozenset({SID_A, SID_B})


def test_running_session_ids_is_none_not_empty_when_the_scan_fails(monkeypatch):
    def broken():
        raise EnumerationFailed("x")
    monkeypatch.setattr(ls, "enumerate_processes", broken)
    assert ls.running_session_ids() is None


# -- the Tasks tab ----------------------------------------------------------------

@pytest.fixture
def ctrl(tk_root):
    from tkinter import ttk
    from types import SimpleNamespace
    from controllers.tasks_tab import TasksController
    cfg = SimpleNamespace(git_exe="git", search_roots=[], tokensave_exe="",
                          codegraph_exe="")
    return TasksController(ttk.Notebook(tk_root), cfg,
                           get_project_path=lambda: None,
                           get_known_paths=lambda: [])


def _rows(ctrl):
    tree = ctrl._live_tree
    return {iid: (tree.item(iid, "values"), tree.item(iid, "tags"))
            for iid in tree.get_children()}


@pytest.mark.tk
def test_the_live_view_renders_sessions_and_strays(ctrl, folders):
    root, _wt, other = folders
    snap = ls.LiveSnapshot(
        sessions=(_live(pid=10, project=root, title="t", state=ls.SERVING,
                        started_at=1.0),
                  _live(pid=11, project=root, state=ls.OTHER_FOLDER,
                        server_project=other)),
        strays=(ls.StrayServer(99, 1.0, other, "authoritative"),))
    ctrl._apply_live(snap)
    rows = _rows(ctrl)
    assert set(rows) == {"live:10", "live:11", "stray:99"}
    assert rows["live:10"][0][4] == "✓ serving this folder"
    assert "other" in rows["live:11"][1], "a different folder is tinted, not green"
    assert rows["stray:99"][0][0] == "(under no running session)"
    assert ctrl._live_summary.cget("text") == snap.summary


@pytest.mark.tk
def test_a_failed_scan_leaves_the_history_marks_but_a_real_zero_clears_them(ctrl):
    ctrl._live_ids = frozenset({SID_A})
    ctrl._apply_live(ls.LiveSnapshot(failed="PowerShell could not be located"))
    assert ctrl._live_ids == frozenset({SID_A}), "could not look != none running"
    assert "Could not check" in ctrl._live_summary.cget("text")

    ctrl._apply_live(ls.LiveSnapshot())
    assert ctrl._live_ids == frozenset()


@pytest.mark.tk
def test_a_stale_live_scan_result_is_discarded(ctrl):
    ctrl._live_req_id = 5
    ctrl._apply_live_if_current(4, ls.LiveSnapshot(failed="old"))
    assert "old" not in ctrl._live_summary.cget("text")


def _history_row(sid, agent=None, recent=False):
    row = {"session_id": sid, "title": "t", "project_display": "p",
           "last_activity": 1.0, "is_recent": recent}
    if agent:
        row["agent"] = agent
    return row


@pytest.mark.tk
def test_a_running_session_is_marked_in_the_history_list(ctrl):
    ctrl._update_sess_tree([_history_row(SID_A), _history_row(SID_B, recent=True)],
                           frozenset({SID_A}))
    tree = ctrl._sess_tree
    status = {iid: tree.item(iid, "values")[0] for iid in tree.get_children()}
    assert status["sess:claude:%s" % SID_A] == "▶"
    assert status["sess:claude:%s" % SID_B] != "▶"


@pytest.mark.tk
def test_a_cursor_chat_is_never_marked_running_by_a_claude_id(ctrl):
    ctrl._update_sess_tree([_history_row(SID_A, agent="cursor")],
                           frozenset({SID_A}))
    iid = ctrl._sess_tree.get_children()[0]
    assert ctrl._sess_tree.item(iid, "values")[0] != "▶"


@pytest.mark.tk
def test_a_history_refresh_that_could_not_look_keeps_the_existing_marks(ctrl):
    ctrl._live_ids = frozenset({SID_A})
    ctrl._apply_results([], [_history_row(SID_A)], None)
    assert ctrl._live_ids == frozenset({SID_A})
    ctrl._apply_results([], [_history_row(SID_A)], frozenset())
    assert ctrl._live_ids == frozenset()


# -- the title -----------------------------------------------------------------------

def test_the_newest_custom_title_wins_over_an_older_one_and_over_ai_title(tmp_path):
    path = _transcript(tmp_path, [
        json.dumps({"type": "ai-title", "aiTitle": "Generated"}),
        json.dumps({"type": "custom-title", "customTitle": "First name"}),
        json.dumps({"x": 1}),
        json.dumps({"type": "custom-title", "customTitle": "Renamed"}),
        json.dumps({"x": 2})])
    assert ls.read_title(path) == "Renamed"


def test_title_falls_back_to_the_generated_one_then_to_the_head_of_the_file(tmp_path):
    only_ai = _transcript(tmp_path, [json.dumps({"aiTitle": "Generated"})])
    assert ls.read_title(only_ai) == "Generated"
    head_only = tmp_path / "h.jsonl"
    head_only.write_text(json.dumps({"type": "ai-title", "sessionId": "s",
                                     "aiTitle": "From the head"}) + "\n",
                         encoding="utf-8")
    assert ls.read_title(str(head_only)) == "From the head"


def test_a_transcript_with_no_title_anywhere_gives_empty_not_a_made_up_one(tmp_path):
    assert ls.read_title(_transcript(tmp_path, [json.dumps({"x": 1})])) == ""
    assert ls.read_title(str(tmp_path / "missing.jsonl")) == ""


def test_the_title_is_found_beyond_the_folder_tail(tmp_path):
    """A rename can be further back than the last cwd; the title tail is longer."""
    filler = json.dumps({"cwd": "D:\\x", "pad": "z" * 2000})
    path = _transcript(tmp_path, [json.dumps({"customTitle": "Old but current"})]
                       + [filler] * 300)
    assert ls.read_last_cwd(path, tail_bytes=4096) == "D:\\x"
    assert ls.read_title(path) == "Old but current"
