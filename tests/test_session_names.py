"""The history list names a session the way the desktop app does, and says where it is NOW.

Measured on this machine: "Suite roadmap and AI integration ideas" is one 17,915-line
transcript. It starts in worktree `calculator-result-selection-ff3f27` (since removed),
moves to `blissful-mclaren-b6fd1e` at line 7,044 and passes through 28 branches. It has
`custom-title` records every ~17 lines and no `ai-title`, so the history list showed
"(untitled)" and a folder that no longer exists, for a session the person was using.
"""
import json
import os

from helpers.claude_tasks import scan_sessions
from helpers.live_sessions import read_tail_meta


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _msg(cwd, branch="", n=0):
    rec = {"type": "user", "cwd": cwd, "message": {"content": f"m{n}"}}
    if branch:
        rec["gitBranch"] = branch
    return rec


def _title(text):
    return {"type": "custom-title", "customTitle": text, "sessionId": "s"}


def test_the_newest_rename_wins_over_an_older_one_and_over_the_ai_title(tmp_path):
    p = _write(str(tmp_path / "s.jsonl"), [
        _title("Old name"), {"type": "ai-title", "aiTitle": "Generated"}, _msg("/a"),
        _title("New name"), _msg("/a")])
    assert read_tail_meta(p)["title"] == "New name"


def test_the_ai_title_is_used_when_there_is_no_rename(tmp_path):
    p = _write(str(tmp_path / "s.jsonl"), [{"type": "ai-title", "aiTitle": "Generated"}, _msg("/a")])
    assert read_tail_meta(p)["title"] == "Generated"


def test_the_cwd_and_branch_are_the_newest_not_the_first(tmp_path):
    p = _write(str(tmp_path / "s.jsonl"), [
        _msg("/old/wt", "claude/first"), _msg("/old/wt", "claude/second"),
        _msg("/new/wt", "claude/third")])
    meta = read_tail_meta(p)
    assert meta["cwd"] == "/new/wt" and meta["branch"] == "claude/third"


def test_a_detached_head_record_does_not_hide_the_real_branch(tmp_path):
    p = _write(str(tmp_path / "s.jsonl"), [_msg("/a", "claude/real"), _msg("/a", "HEAD")])
    assert read_tail_meta(p)["branch"] == "claude/real"


def test_nothing_found_is_empty_strings_never_an_error(tmp_path):
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    assert read_tail_meta(str(empty)) == {"title": "", "cwd": "", "branch": ""}
    assert read_tail_meta(str(tmp_path / "missing.jsonl")) == {"title": "", "cwd": "", "branch": ""}
    junk = tmp_path / "j.jsonl"
    junk.write_text("not json\n[1, 2]\n" + json.dumps(_msg("/x")) + "\n")
    assert read_tail_meta(str(junk))["cwd"] == "/x"


def test_a_tail_that_starts_mid_record_is_survived(tmp_path):
    big = {"type": "user", "cwd": "/early", "message": {"content": "x" * 5000}}
    p = _write(str(tmp_path / "s.jsonl"), [big, big, _msg("/late", "claude/b")])
    meta = read_tail_meta(p, tail_bytes=2000)             # begins inside a record
    assert meta["cwd"] == "/late" and meta["branch"] == "claude/b"


def test_a_session_that_moved_is_listed_where_it_is_now_with_its_name_and_branch(fake_home):
    repo = os.path.join(fake_home, "Repo")
    os.makedirs(os.path.join(repo, ".git"))
    gone = os.path.join(repo, ".claude", "worktrees", "started-here")        # removed since
    live = os.path.join(repo, ".claude", "worktrees", "now-here")
    os.makedirs(live)
    open(os.path.join(live, ".git"), "w").write("gitdir: x\n")
    folder = os.path.join(fake_home, ".claude", "projects", "enc--claude-worktrees-now-here")
    _write(os.path.join(folder, "s1.jsonl"), [
        _title("Suite roadmap"), _msg(gone, "claude/one"),
        _msg(live, "claude/two"), _title("Suite roadmap and AI ideas"),
        _msg(live, "claude/docs-assistant-handoff")])
    row = scan_sessions([])[0]
    assert row["title"] == "Suite roadmap and AI ideas"
    assert row["cwd"] == live and row["branch"] == "claude/docs-assistant-handoff"
    assert row["project_display"] == "Repo (worktree now-here) @ claude/docs-assistant-handoff"
    assert "removed" not in row["project_display"]


def test_a_session_with_no_branch_reads_exactly_as_before(fake_home):
    folder = os.path.join(fake_home, ".claude", "projects", "enc")
    _write(os.path.join(folder, "s1.jsonl"), [{"type": "ai-title", "sessionId": "s1", "aiTitle": "T"},
                                              _msg(os.path.join(fake_home, "Plain"))])
    row = scan_sessions([])[0]
    assert row["title"] == "T" and row["project_display"] == "Plain" and row["branch"] == ""


def test_a_head_only_title_is_still_found_when_the_tail_has_none(fake_home, monkeypatch):
    import helpers.live_sessions as ls
    monkeypatch.setattr(ls, "_META_TAIL_BYTES", 300)
    monkeypatch.setattr(ls, "read_tail_meta", lambda p, tail_bytes=300: {"title": "", "cwd": "", "branch": ""})
    folder = os.path.join(fake_home, ".claude", "projects", "enc")
    _write(os.path.join(folder, "s1.jsonl"), [{"type": "ai-title", "sessionId": "s1", "aiTitle": "Head title"},
                                              _msg(os.path.join(fake_home, "P"))])
    assert scan_sessions([])[0]["title"] == "Head title"
