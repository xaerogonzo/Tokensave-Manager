"""tests/test_project_rename.py — the pure logic behind a coordinated rename.

A folder rename here is a migration across NTFS, manager-config.json,
~/.claude.json and (for a worktree) git's own metadata, per
helpers/project_rename.py. This file pins the parts that must never guess:
per-field rewrite semantics in manager-config.json, the non-clobbering merge
policy for ~/.claude.json, and the validators that stop an illegal or
dangerous destination before any OS call runs.

Filesystem facts are real (tmp_path); the git boundary is mocked, the same
split tests/test_worktree_cleanup.py already uses.
"""
from __future__ import annotations

import json
import os
import subprocess

from helpers import project_rename as pr


# ── name / destination validation ───────────────────────────────────────────

def test_rejects_empty_and_dot_names():
    assert not pr.validate_new_name("").ok
    assert not pr.validate_new_name(".").ok
    assert not pr.validate_new_name("..").ok


def test_rejects_trailing_dot_or_space():
    assert not pr.validate_new_name("Foo.").ok
    assert not pr.validate_new_name("Foo ").ok


def test_rejects_reserved_device_names():
    assert not pr.validate_new_name("CON").ok
    assert not pr.validate_new_name("con.txt").ok
    assert pr.validate_new_name("Console").ok  # not an exact reserved stem


def test_rejects_illegal_characters():
    check = pr.validate_new_name("a:b")
    assert not check.ok
    assert ":" in check.reason


def test_destination_same_path_is_rejected():
    assert not pr.validate_destination("D:/p/Foo", "D:/p/Foo").ok


def test_destination_nested_inside_old_path_is_rejected():
    check = pr.validate_destination("D:/p/Foo", "D:/p/Foo/Bar")
    assert not check.ok
    assert "inside" in check.reason


def test_destination_sibling_is_accepted():
    assert pr.validate_destination("D:/p/Foo", "D:/p/Bar").ok


def test_case_only_rename_is_not_a_collision(tmp_path):
    old = tmp_path / "Foo"
    old.mkdir()
    new = str(tmp_path / "foo")
    # On a case-insensitive filesystem os.path.exists(new) is True here
    # (it's literally the same directory) -- validate_destination must not
    # treat that as "a file already exists at the new path".
    check = pr.validate_destination(str(old), new)
    assert check.ok
    # normcase folds case only on Windows; on POSIX Foo and foo are two
    # different directories, so there is no case-only rename to detect.
    assert pr.is_case_only_rename(str(old), new) is (os.name == "nt")


def test_destination_collision_is_rejected(tmp_path):
    old = tmp_path / "Foo"
    old.mkdir()
    (tmp_path / "Bar").mkdir()
    check = pr.validate_destination(str(old), str(tmp_path / "Bar"))
    assert not check.ok
    assert "already exists" in check.reason


# ── move_directory ───────────────────────────────────────────────────────────

def test_move_directory_succeeds_and_reports_strategy(tmp_path):
    old = tmp_path / "Foo"
    old.mkdir()
    (old / "file.txt").write_text("hi", encoding="utf-8")
    new = tmp_path / "Bar"

    result = pr.move_directory(str(old), str(new))

    assert result.ok
    assert result.strategy == "rename"
    assert new.is_dir()
    assert (new / "file.txt").read_text(encoding="utf-8") == "hi"
    assert not old.exists()


def test_move_directory_failure_is_classified(tmp_path, monkeypatch):
    old = tmp_path / "Foo"
    old.mkdir()
    new = tmp_path / "Bar"

    def _raise(*a, **k):
        raise OSError(32, "in use")

    monkeypatch.setattr(os, "rename", _raise)
    result = pr.move_directory(str(old), str(new))

    assert not result.ok
    assert result.rename_failed_reason == "in_use"
    assert pr.is_in_use_failure(result)


def test_move_directory_contents_hazardous_reports_full_move(tmp_path):
    old = tmp_path / "Foo"
    old.mkdir()
    (old / "a.txt").write_text("a", encoding="utf-8")
    (old / "b.txt").write_text("b", encoding="utf-8")
    new = tmp_path / "Bar"

    result = pr.move_directory_contents_hazardous(str(old), str(new))

    assert result.ok
    assert sorted(result.moved_children) == ["a.txt", "b.txt"]
    assert not old.exists()
    assert (new / "a.txt").exists()


def test_move_directory_contents_hazardous_reports_partial_state(tmp_path, monkeypatch):
    old = tmp_path / "Foo"
    old.mkdir()
    (old / "a.txt").write_text("a", encoding="utf-8")
    (old / "b.txt").write_text("b", encoding="utf-8")
    new = tmp_path / "Bar"

    real_rename = os.rename
    calls = {"n": 0}

    def _flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "access denied")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", _flaky)
    result = pr.move_directory_contents_hazardous(str(old), str(new))

    assert not result.ok
    assert result.rollback_attempted
    assert result.rollback_succeeded
    assert result.old_exists is True
    # rollback returns the one moved child, so the old dir is whole again
    assert (old / "a.txt").exists()


# ── worktree helpers (git boundary mocked) ──────────────────────────────────

def _run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_is_linked_worktree_detects_a_dot_git_file(tmp_path):
    project = tmp_path / "wt"
    project.mkdir()
    (project / ".git").write_text("gitdir: ../main/.git/worktrees/wt", encoding="utf-8")
    assert pr.is_linked_worktree(str(project))


def test_is_linked_worktree_false_for_a_dot_git_directory(tmp_path):
    project = tmp_path / "main"
    project.mkdir()
    (project / ".git").mkdir()
    assert not pr.is_linked_worktree(str(project))


def test_worktree_locked_reads_the_porcelain_locked_field(tmp_path, monkeypatch):
    project = tmp_path / "wt"
    project.mkdir()
    porcelain = (
        f"worktree {project}\n"
        "HEAD abcdef0123456789\n"
        "branch refs/heads/feature\n"
        "locked\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _run(0, porcelain))
    assert pr.worktree_locked(str(project), "git") is True


def test_worktree_locked_false_when_not_locked(tmp_path, monkeypatch):
    project = tmp_path / "wt"
    project.mkdir()
    porcelain = f"worktree {project}\nHEAD abcdef0123456789\nbranch refs/heads/feature\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _run(0, porcelain))
    assert pr.worktree_locked(str(project), "git") is False


def test_worktree_locked_unknown_when_git_fails(tmp_path, monkeypatch):
    project = tmp_path / "wt"
    project.mkdir()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _run(1, "", "not a repo"))
    assert pr.worktree_locked(str(project), "git") is None


def test_nested_worktree_conflict_detects_a_worktree_inside_the_move(tmp_path):
    project = tmp_path / "main"
    project.mkdir()
    nested = project / ".claude" / "worktrees" / "feature"
    nested.mkdir(parents=True)
    conflict = pr.nested_worktree_conflict(
        str(project), [{"path": str(nested)}])
    assert conflict


def test_nested_worktree_conflict_false_for_a_sibling_worktree(tmp_path):
    project = tmp_path / "main"
    project.mkdir()
    sibling = tmp_path / "main-feature"
    sibling.mkdir()
    conflict = pr.nested_worktree_conflict(
        str(project), [{"path": str(sibling)}])
    assert not conflict


def test_move_linked_worktree_uses_git_worktree_move(tmp_path, monkeypatch):
    old = tmp_path / "wt"
    old.mkdir()
    new = tmp_path / "wt2"
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _run(0, "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = pr.move_linked_worktree(str(old), str(new), "git")

    assert result.ok
    assert result.strategy == "worktree_move"
    assert captured["cmd"][1:4] == ["-C", str(old), "worktree"]
    assert "move" in captured["cmd"]
    assert "--force" not in captured["cmd"]


def test_move_linked_worktree_force_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _run(0, ""))
    result = pr.move_linked_worktree(
        str(tmp_path / "wt"), str(tmp_path / "wt2"), "git", force=True)
    assert result.ok


def test_move_linked_worktree_reports_git_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _run(1, "", "worktree is locked"))
    result = pr.move_linked_worktree(
        str(tmp_path / "wt"), str(tmp_path / "wt2"), "git")
    assert not result.ok
    assert "locked" in result.reason


# ── manager-config.json repointing ──────────────────────────────────────────

def test_repoint_project_categories_renames_the_key(mock_config):
    old, new = "D:/p/Foo", "D:/p/Bar"
    mock_config.raw["project_categories"] = {old: {"category": "Games"}}

    touched = pr.repoint_manager_config(mock_config, old, new)

    assert touched["project_categories"] == [old]
    assert mock_config.raw["project_categories"] == {new: {"category": "Games"}}
    assert mock_config._saved


def test_repoint_instructions_skip_paths_replaces_the_entry(mock_config):
    old, new = "D:/p/Foo", "D:/p/Bar"
    mock_config.raw["instructions_skip_paths"] = [old, "D:/p/Other"]

    touched = pr.repoint_manager_config(mock_config, old, new)

    assert touched["instructions_skip_paths"] == [old]
    assert mock_config.raw["instructions_skip_paths"] == [new, "D:/p/Other"]


def test_repoint_search_roots_only_rewrites_an_exact_root(mock_config):
    old, new = "D:/p/Foo", "D:/p/Bar"
    # This root IS the project -> rewritten.
    exact = {"path": old, "label": "Foo"}
    # This root merely CONTAINS the project -> must NOT be touched.
    containing = {"path": "D:/p", "label": "Everything"}
    mock_config.raw["search_roots"] = [exact, containing]

    touched = pr.repoint_manager_config(mock_config, old, new)

    assert touched["search_roots"] == [old]
    assert mock_config.raw["search_roots"][0]["path"] == new
    assert mock_config.raw["search_roots"][0]["label"] == "Foo"
    assert mock_config.raw["search_roots"][1]["path"] == "D:/p"


def test_repoint_mcp_skip_warnings_rewrites_the_prefix_not_the_whole_entry(mock_config):
    old, new = "D:/p/Foo", "D:/p/Bar"
    entry = os.path.join(old, ".mcp.json")
    mock_config.raw["mcp_skip_warnings"] = [entry]

    touched = pr.repoint_manager_config(mock_config, old, new)

    assert touched["mcp_skip_warnings"] == [entry]
    assert mock_config.raw["mcp_skip_warnings"] == [os.path.join(new, ".mcp.json")]


def test_repoint_manager_config_no_op_when_nothing_matches(mock_config):
    mock_config.raw["project_categories"] = {"D:/p/Other": {"category": "X"}}
    touched = pr.repoint_manager_config(mock_config, "D:/p/Foo", "D:/p/Bar")
    assert touched == {}
    assert not mock_config._saved


# ── ~/.claude.json repointing ────────────────────────────────────────────────

def _write_claude_json(path, projects: dict) -> None:
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def test_repoint_claude_json_moves_when_destination_absent(tmp_path):
    cj = tmp_path / "claude.json"
    old = "D:/p/Foo"
    _write_claude_json(cj, {old: {"hasTrustDialogAccepted": True}})

    result = pr.repoint_claude_json(old, "D:/p/Bar", claude_json_path=str(cj))

    assert result.outcome == "moved"
    assert result.write_ok
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert "D:/p/Bar" in data["projects"]
    assert old not in data["projects"]


def test_repoint_claude_json_collapses_identical_destination(tmp_path):
    cj = tmp_path / "claude.json"
    old, new = "D:/p/Foo", "D:/p/Bar"
    record = {"hasTrustDialogAccepted": True}
    _write_claude_json(cj, {old: dict(record), new: dict(record)})

    result = pr.repoint_claude_json(old, new, claude_json_path=str(cj))

    assert result.outcome == "collapsed"
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert list(data["projects"].keys()) == [new]


def test_repoint_claude_json_unions_disjoint_destination_fields(tmp_path):
    cj = tmp_path / "claude.json"
    old, new = "D:/p/Foo", "D:/p/Bar"
    _write_claude_json(cj, {
        old: {"hasTrustDialogAccepted": True},
        new: {"mcpServers": {"tokensave": {}}},
    })

    result = pr.repoint_claude_json(old, new, claude_json_path=str(cj))

    assert result.outcome == "merged"
    data = json.loads(cj.read_text(encoding="utf-8"))
    merged = data["projects"][new]
    assert merged["hasTrustDialogAccepted"] is True
    assert "mcpServers" in merged


def test_repoint_claude_json_blocks_on_a_real_conflict(tmp_path):
    cj = tmp_path / "claude.json"
    old, new = "D:/p/Foo", "D:/p/Bar"
    _write_claude_json(cj, {
        old: {"hasTrustDialogAccepted": True},
        new: {"hasTrustDialogAccepted": False},
    })

    result = pr.repoint_claude_json(old, new, claude_json_path=str(cj))

    assert result.outcome == "blocked"
    assert "hasTrustDialogAccepted" in result.conflicting_fields
    # nothing is guessed -- both records are left exactly as they were
    data = json.loads(cj.read_text(encoding="utf-8"))
    assert data["projects"][old]["hasTrustDialogAccepted"] is True
    assert data["projects"][new]["hasTrustDialogAccepted"] is False


def test_repoint_claude_json_no_match_when_old_path_absent(tmp_path):
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {"D:/p/Other": {}})
    result = pr.repoint_claude_json("D:/p/Foo", "D:/p/Bar", claude_json_path=str(cj))
    assert result.outcome == "no_match"


def test_repoint_claude_json_no_match_when_file_absent(tmp_path):
    result = pr.repoint_claude_json(
        "D:/p/Foo", "D:/p/Bar", claude_json_path=str(tmp_path / "missing.json"))
    assert result.outcome == "no_match"
