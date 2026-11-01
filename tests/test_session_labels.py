"""Readable names for the folders sessions ran in, worktrees especially.

A worktree is never a known project, so its rows used to show the encoded
directory name (`D--Claude-Co-worker-...--claude-worktrees-x`), which is lossy
and cannot be turned back into a path. The session's own recorded `cwd` can.
"""
import json
import os
import sys

import pytest

from helpers.claude_tasks import scan_sessions, transcript_cwd, worktree_label


@pytest.mark.parametrize("cwd,expected", [
    (r"D:\Claude Co worker\Token Save Manager Source\.claude\worktrees\token-save-cursor-28e8cb",
     "Token Save Manager Source (worktree token-save-cursor-28e8cb)"),
    ("D:/Random Projects/OpenChem Studio/.claude/worktrees/blissful-mclaren-b6fd1e/",
     "OpenChem Studio (worktree blissful-mclaren-b6fd1e)"),
    (r"D:\Random Projects\Fortuna Lab", "Fortuna Lab"),
    ("/home/me/proj/", "proj"),
    (r"D:\Claude Co worker\Source\.CLAUDE\WORKTREES\Name", "Source (worktree Name)"),
])
def test_worktree_label(cwd, expected):
    assert worktree_label(cwd) == expected


@pytest.mark.parametrize("cwd", ["", None])
def test_worktree_label_of_nothing_is_empty_not_an_error(cwd):
    assert worktree_label(cwd) == ""


def test_a_marker_with_nothing_after_it_is_just_the_last_segment():
    assert worktree_label("D:/x/.claude/worktrees/") == "worktrees"


def _session(folder, name, cwd):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "ai-title", "sessionId": name, "aiTitle": "T"}) + "\n")
        fh.write(json.dumps({"type": "user", "cwd": cwd,
                             "message": {"content": "hi"}}) + "\n")
    return path


def test_transcript_cwd_reads_the_recorded_folder(tmp_path):
    path = _session(str(tmp_path), "s", "D:\\a\\b")
    assert transcript_cwd(path) == "D:\\a\\b"
    assert transcript_cwd(str(tmp_path / "missing.jsonl")) == ""


def test_an_unknown_worktree_row_is_named_from_its_cwd_not_its_encoded_folder(fake_home):
    cwd = r"D:\Claude Co worker\Source\.claude\worktrees\wt-1"
    projects = os.path.join(fake_home, ".claude", "projects")
    _session(os.path.join(projects, "D--Claude-Co-worker-Source--claude-worktrees-wt-1"),
             "s1", cwd)
    row = scan_sessions([])[0]
    assert row["project_display"] == "Source (worktree wt-1)"
    assert row["cwd"] == cwd


@pytest.mark.skipif(sys.platform != "win32",
                    reason="the paths in this test are Windows paths")
def test_a_known_project_still_wins_over_the_cwd_label(fake_home):
    from helpers.claude_tasks import encode_project_path
    cwd = r"D:\Random Projects\Fortuna Lab"
    projects = os.path.join(fake_home, ".claude", "projects")
    _session(os.path.join(projects, encode_project_path(cwd)), "s2", cwd)
    assert scan_sessions([cwd])[0]["project_display"] == "Fortuna Lab"


def test_a_session_with_no_recorded_cwd_keeps_the_old_fallback(fake_home):
    folder = os.path.join(fake_home, ".claude", "projects", "D--some-encoded-name")
    os.makedirs(folder)
    with open(os.path.join(folder, "s3.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "ai-title", "sessionId": "s3", "aiTitle": "T"}) + "\n")
    row = scan_sessions([])[0]
    assert row["project_display"] == "D--some-encoded-name" and row["cwd"] == ""
