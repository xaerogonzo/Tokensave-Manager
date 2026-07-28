"""tests/test_doc_drafter_git.py — helpers/doc_drafter_git.py.

subprocess-dependent functions are tested by monkeypatching subprocess.run
at the import site (helpers.doc_drafter_git.subprocess.run).  Pure helpers
(is_sparse, resolve_commit_range routing) need no subprocess mock.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from helpers.doc_drafter_git import (
    is_sparse,
    _last_doc_commit_sha,
    _commit_touches_code,
    changed_file_paths,
    read_blueprint_context,
    resolve_commit_range,
)


# ── is_sparse ─────────────────────────────────────────────────────────────────

class TestIsSparse:
    def test_empty_commits_not_sparse(self):
        assert not is_sparse([])

    def test_long_subjects_not_sparse(self):
        commits = [{"subject": "feat(commit-dialog): add AI suggestion strategy chain"}]
        assert not is_sparse(commits)

    def test_short_subjects_sparse(self):
        commits = [{"subject": "fix"}]
        assert is_sparse(commits, threshold=10)

    def test_average_below_threshold(self):
        commits = [{"subject": "x"}, {"subject": "y"}, {"subject": "z"}]
        assert is_sparse(commits, threshold=5)

    def test_average_above_threshold(self):
        commits = [{"subject": "updated the connection pool retry logic"}]
        assert not is_sparse(commits, threshold=15)

    def test_ignores_empty_subjects(self):
        commits = [{"subject": ""}, {"subject": "   "}]
        assert not is_sparse(commits)  # no subjects → False


# ── _last_doc_commit_sha ──────────────────────────────────────────────────────

class TestLastDocCommitSha:
    def test_returns_sha_on_success(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "abc1234567890\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = _last_doc_commit_sha(str(tmp_path), "git")
        assert result == "abc1234567890"

    def test_returns_none_on_failure(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 1
        proc.stdout = ""
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = _last_doc_commit_sha(str(tmp_path), "git")
        assert result is None

    def test_returns_none_on_empty_stdout(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = ""
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = _last_doc_commit_sha(str(tmp_path), "git")
        assert result is None

    def test_returns_none_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
        result = _last_doc_commit_sha(str(tmp_path), "git")
        assert result is None

    def test_uses_custom_pathspecs(self, tmp_path, monkeypatch):
        captured = []
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "sha123\n"

        def fake_run(cmd, **kw):
            captured.append(cmd)
            return proc

        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", fake_run)
        _last_doc_commit_sha(str(tmp_path), "git", pathspecs=["ARCHITECTURE.md"])
        assert "ARCHITECTURE.md" in captured[0]


# ── _commit_touches_code ──────────────────────────────────────────────────────

class TestCommitTouchesCode:
    def test_returns_true_when_code_file(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "\nsrc/foo.py\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        assert _commit_touches_code(str(tmp_path), "abc", "git") is True

    def test_returns_false_when_only_docs(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "\nCHANGELOG.md\nREADME.md\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        assert _commit_touches_code(str(tmp_path), "abc", "git") is False

    def test_returns_false_when_only_docs_dir(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "\ndocs/GUIDE.md\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        assert _commit_touches_code(str(tmp_path), "abc", "git") is False

    def test_returns_false_on_subprocess_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
        assert _commit_touches_code(str(tmp_path), "abc", "git") is False


# ── changed_file_paths ────────────────────────────────────────────────────────

class TestChangedFilePaths:
    def test_returns_empty_for_empty_range(self, tmp_path, monkeypatch):
        result = changed_file_paths(str(tmp_path), "", "git")
        assert result == []

    def test_parses_output(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "src/foo.py\nsrc/bar.py\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = changed_file_paths(str(tmp_path), "HEAD~1..HEAD", "git")
        assert "src/foo.py" in result
        assert "src/bar.py" in result

    def test_deduplicates(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "src/foo.py\nsrc/foo.py\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = changed_file_paths(str(tmp_path), "HEAD~1..HEAD", "git")
        assert result.count("src/foo.py") == 1

    def test_caps_at_60_paths(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 0
        proc.stdout = "\n".join(f"file_{i}.py" for i in range(100)) + "\n"
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = changed_file_paths(str(tmp_path), "HEAD~1..HEAD", "git")
        assert len(result) == 60

    def test_returns_empty_on_failure(self, tmp_path, monkeypatch):
        proc = Mock()
        proc.returncode = 1
        proc.stdout = ""
        monkeypatch.setattr("helpers.doc_drafter_git.subprocess.run", lambda *a, **k: proc)
        result = changed_file_paths(str(tmp_path), "HEAD~1..HEAD", "git")
        assert result == []


# ── read_blueprint_context ────────────────────────────────────────────────────

class TestReadBlueprintContext:
    def test_reads_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project rules\nsome content")
        result = read_blueprint_context(str(tmp_path))
        assert "some content" in result

    def test_falls_back_to_basic_instructions(self, tmp_path):
        (tmp_path / "BASIC_INSTRUCTIONS.md").write_text("basic instructions")
        result = read_blueprint_context(str(tmp_path))
        assert "basic instructions" in result

    def test_prefers_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude content")
        (tmp_path / "BASIC_INSTRUCTIONS.md").write_text("basic content")
        result = read_blueprint_context(str(tmp_path))
        assert "claude content" in result
        assert "basic content" not in result

    def test_returns_empty_when_no_file(self, tmp_path):
        result = read_blueprint_context(str(tmp_path))
        assert result == ""

    def test_caps_at_max_chars(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("x" * 5000)
        result = read_blueprint_context(str(tmp_path), max_chars=100)
        assert len(result) <= 100


# ── resolve_commit_range routing ──────────────────────────────────────────────

class TestResolveCommitRangeRouting:
    def test_unknown_mode_returns_empty_commits(self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_git._resolve_since_last_doc",
                            lambda *a, **k: {})
        result = resolve_commit_range(str(tmp_path), "unknown_mode", "", "git")
        assert result["commits"] == []
        assert "unknown_mode" in result["range_label"]

    def test_custom_mode_empty_ref(self, tmp_path, monkeypatch):
        result = resolve_commit_range(str(tmp_path), "custom", "", "git")
        assert result["commits"] == []
        assert "empty" in result["range_label"].lower()

    def test_since_last_commit_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_git._commits_since",
                            lambda *a, **k: [{"hash": "abc", "subject": "test"}])
        result = resolve_commit_range(str(tmp_path), "since_last_commit", "", "git")
        assert result["range_spec"] == "HEAD~1..HEAD"
        assert len(result["commits"]) == 1
