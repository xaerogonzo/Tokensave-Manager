"""Tests for helpers/git.py — low-level git helpers (mock at the import site)."""

import os
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from constants import CREATE_NO_WINDOW
from helpers import git


class TestIsGitRepo:
    """Tests for _is_git_repo — walks upward via git rev-parse."""

    def test_returns_true_when_git_succeeds(self, monkeypatch):
        """Returns True when git rev-parse --git-dir returns 0."""
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0)
        )
        assert git._is_git_repo("/some/path", "/usr/bin/git") is True

    def test_returns_false_when_git_fails(self, monkeypatch):
        """Returns False when git rev-parse --git-dir returns non-zero."""
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=128)
        )
        assert git._is_git_repo("/some/path", "/usr/bin/git") is False

    def test_returns_false_when_git_not_found(self, monkeypatch):
        """Returns False when git_exe is not found (FileNotFoundError)."""
        mock_run = mock.Mock(side_effect=FileNotFoundError())
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        assert git._is_git_repo("/some/path", "/nonexistent/git") is False

    def test_calls_git_with_correct_args(self, monkeypatch):
        """Calls git rev-parse --git-dir in the given path."""
        mock_run = mock.Mock(return_value=SimpleNamespace(returncode=0))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        git._is_git_repo("/home/user/project", "/usr/bin/git")
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/bin/git", "-C", "/home/user/project", "rev-parse", "--git-dir"]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["creationflags"] == CREATE_NO_WINDOW


class TestFindGitignoredOnDisk:
    """Tests for _find_gitignored_on_disk — lists gitignored files on disk."""

    def test_returns_list_on_success(self, monkeypatch):
        """Returns list of gitignored files when git succeeds."""
        output = "file1.pyc\nfile2.o\n__pycache__/\n"
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output)
        )
        result = git._find_gitignored_on_disk("/path", "/git")
        assert result == ["file1.pyc", "file2.o"]

    def test_excludes_directories_with_trailing_slash(self, monkeypatch):
        """Excludes directories (entries with trailing /) from result."""
        output = "file.txt\ndir/\nother.log\n"
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output)
        )
        result = git._find_gitignored_on_disk("/path", "/git")
        assert result == ["file.txt", "other.log"]
        assert "dir/" not in result

    def test_returns_empty_on_non_repo(self, monkeypatch):
        """Returns [] when git fails (not a repo)."""
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=128, stdout="")
        )
        result = git._find_gitignored_on_disk("/path", "/git")
        assert result == []

    def test_returns_empty_on_git_not_found(self, monkeypatch):
        """Returns [] when git_exe is not found (FileNotFoundError)."""
        mock_run = mock.Mock(side_effect=FileNotFoundError())
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._find_gitignored_on_disk("/path", "/nonexistent")
        assert result == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        """Returns [] when git command times out."""
        mock_run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", timeout=10))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._find_gitignored_on_disk("/path", "/git")
        assert result == []

    def test_filters_empty_lines(self, monkeypatch):
        """Filters out empty/whitespace-only lines."""
        output = "file1.txt\n\n  \nfile2.log\n"
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output)
        )
        result = git._find_gitignored_on_disk("/path", "/git")
        assert result == ["file1.txt", "file2.log"]

    def test_calls_git_with_correct_args(self, monkeypatch):
        """Calls git ls-files with correct flags."""
        mock_run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        git._find_gitignored_on_disk("/repo", "/git")
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["/git", "-C", "/repo", "ls-files", "--others", "--ignored", "--exclude-standard"]
        assert kwargs["timeout"] == 10
        assert kwargs["creationflags"] == CREATE_NO_WINDOW


class TestStagedDeletions:
    """Tests for _staged_deletions — files staged for deletion."""

    def test_returns_list_on_success(self, monkeypatch):
        """Returns list of files staged for deletion."""
        output = "old_file.py\nremoved.txt\n"
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output)
        )
        result = git._staged_deletions("/path", "/git")
        assert result == ["old_file.py", "removed.txt"]

    def test_strips_whitespace(self, monkeypatch):
        """Strips whitespace from each line."""
        output = "  file1.py  \nfile2.txt\n  \nfile3.log\n"
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output)
        )
        result = git._staged_deletions("/path", "/git")
        assert result == ["file1.py", "file2.txt", "file3.log"]

    def test_returns_empty_on_non_repo(self, monkeypatch):
        """Returns [] when git fails."""
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
        )
        result = git._staged_deletions("/path", "/git")
        assert result == []

    def test_returns_empty_on_git_not_found(self, monkeypatch):
        """Returns [] when git_exe is not found."""
        mock_run = mock.Mock(side_effect=FileNotFoundError())
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._staged_deletions("/path", "/nonexistent")
        assert result == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        """Returns [] when git command times out."""
        mock_run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", timeout=10))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._staged_deletions("/path", "/git")
        assert result == []

    def test_calls_git_with_correct_args(self, monkeypatch):
        """Calls git diff --cached --name-only --diff-filter=D."""
        mock_run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        git._staged_deletions("/repo", "/git")
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["/git", "-C", "/repo", "diff", "--cached", "--name-only", "--diff-filter=D"]
        assert kwargs["timeout"] == 10
        assert kwargs["creationflags"] == CREATE_NO_WINDOW


class TestFindTrackedButIgnored:
    """Tests for _find_tracked_but_ignored — stale tracking detection."""

    def test_returns_tracked_but_ignored_files(self, monkeypatch):
        """Returns files that are tracked but match .gitignore."""
        stale_output = "config.ini\nlocal_settings.py\n"
        deletion_output = ""
        outputs = [
            SimpleNamespace(returncode=0, stdout=stale_output),
            SimpleNamespace(returncode=0, stdout=deletion_output)
        ]
        mock_run = mock.Mock(side_effect=outputs)
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        result = git._find_tracked_but_ignored("/path", "/git")
        assert result == ["config.ini", "local_settings.py"]

    def test_returns_empty_on_non_repo(self, monkeypatch):
        """Returns [] when git fails."""
        monkeypatch.setattr(
            "helpers.git.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=128, stdout="")
        )
        result = git._find_tracked_but_ignored("/path", "/git")
        assert result == []

    def test_returns_empty_on_git_not_found(self, monkeypatch):
        """Returns [] when git_exe is not found."""
        mock_run = mock.Mock(side_effect=FileNotFoundError())
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._find_tracked_but_ignored("/path", "/nonexistent")
        assert result == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        """Returns [] when git command times out."""
        mock_run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", timeout=10))
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        result = git._find_tracked_but_ignored("/path", "/git")
        assert result == []

    def test_filters_empty_lines(self, monkeypatch):
        """Filters out empty/whitespace-only lines."""
        stale_output = "file1.txt\n\n  \nfile2.log\n"
        deletion_output = ""
        outputs = [
            SimpleNamespace(returncode=0, stdout=stale_output),
            SimpleNamespace(returncode=0, stdout=deletion_output)
        ]
        mock_run = mock.Mock(side_effect=outputs)
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        result = git._find_tracked_but_ignored("/path", "/git")
        assert result == ["file1.txt", "file2.log"]

    def test_excludes_already_staged_deletions(self, monkeypatch):
        """Excludes files that are already staged for deletion."""
        stale_output = "tracked.py\ndelete_me.txt\nconfig.ini\n"
        deletion_output = "delete_me.txt\n"
        
        outputs = [
            SimpleNamespace(returncode=0, stdout=stale_output),
            SimpleNamespace(returncode=0, stdout=deletion_output)
        ]
        mock_run = mock.Mock(side_effect=outputs)
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        result = git._find_tracked_but_ignored("/path", "/git")
        
        assert result == ["tracked.py", "config.ini"]
        assert "delete_me.txt" not in result

    def test_handles_windows_path_separators(self, monkeypatch):
        """Normalizes backslash path separators when filtering deletions."""
        stale_output = "dir\\file1.txt\ndir/file2.txt\n"
        deletion_output = "dir/file1.txt\n"
        
        outputs = [
            SimpleNamespace(returncode=0, stdout=stale_output),
            SimpleNamespace(returncode=0, stdout=deletion_output)
        ]
        mock_run = mock.Mock(side_effect=outputs)
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        result = git._find_tracked_but_ignored("/path", "/git")
        
        assert result == ["dir/file2.txt"]

    def test_calls_git_with_correct_args(self, monkeypatch):
        """Calls git ls-files -ci --exclude-standard."""
        outputs = [
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="")
        ]
        mock_run = mock.Mock(side_effect=outputs)
        monkeypatch.setattr("helpers.git.subprocess.run", mock_run)
        
        git._find_tracked_but_ignored("/repo", "/git")
        
        first_call = mock_run.call_args_list[0]
        args, kwargs = first_call
        assert args[0] == ["/git", "-C", "/repo", "ls-files", "-ci", "--exclude-standard"]
        assert kwargs["timeout"] == 10
        assert kwargs["creationflags"] == CREATE_NO_WINDOW


class TestIsLocalGitRepo:
    """Tests for _is_local_git_repo — strict local check (pure function)."""

    def test_returns_true_when_dotgit_exists(self, monkeypatch):
        """Returns True when .git exists in the path."""
        mock_exists = mock.Mock(return_value=True)
        monkeypatch.setattr("helpers.git.os.path.exists", mock_exists)
        assert git._is_local_git_repo("/some/path") is True

    def test_returns_false_when_dotgit_missing(self, monkeypatch):
        """Returns False when .git does not exist."""
        mock_exists = mock.Mock(return_value=False)
        monkeypatch.setattr("helpers.git.os.path.exists", mock_exists)
        assert git._is_local_git_repo("/some/path") is False

    def test_checks_for_dotgit_file(self, monkeypatch):
        """Checks for .git using os.path.exists (handles worktrees)."""
        mock_exists = mock.Mock(return_value=True)
        monkeypatch.setattr("helpers.git.os.path.exists", mock_exists)
        
        git._is_local_git_repo("/repo")
        
        expected = os.path.join("/repo", ".git")
        mock_exists.assert_called_once_with(expected)

    def test_uses_exists_not_isdir(self, monkeypatch):
        """Uses os.path.exists, not isdir, to handle git worktrees."""
        mock_exists = mock.Mock(return_value=True)
        monkeypatch.setattr("helpers.git.os.path.exists", mock_exists)
        
        git._is_local_git_repo("/worktree")
        
        expected = os.path.join("/worktree", ".git")
        mock_exists.assert_called_once_with(expected)


class TestParseGitStatusV2:
    """Tests for _parse_git_status_v2 — parse git status output (pure)."""

    def test_returns_default_dict_on_empty_input(self):
        """Returns default dict on empty input."""
        result = git._parse_git_status_v2("")
        assert result == {"dirty": False, "ahead": 0, "behind": 0, "has_remote": False}

    def test_parses_branch_upstream_line(self):
        """Sets has_remote=True when # branch.upstream line is present."""
        text = "# branch.upstream origin/main\n"
        result = git._parse_git_status_v2(text)
        assert result["has_remote"] is True
        assert result["dirty"] is False

    def test_parses_branch_ab_line(self):
        """Parses # branch.ab line for ahead/behind counts."""
        text = "# branch.ab +3 -2\n"
        result = git._parse_git_status_v2(text)
        assert result["ahead"] == 3
        assert result["behind"] == 2

    def test_detects_dirty_with_tracked_modified(self):
        """Sets dirty=True when tracked-modified (1) files are present."""
        text = "1 .M N... 100644 100644 abc def file.txt\n"
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is True

    def test_detects_dirty_with_renamed(self):
        """Sets dirty=True when renamed/copied (2) files are present."""
        text = "2 .R N... 100644 100644 abc def old.txt new.txt\n"
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is True

    def test_detects_dirty_with_unmerged(self):
        """Sets dirty=True when unmerged (u) files are present."""
        text = "u .U N... 100644 100644 100644 abc def ghi file.txt\n"
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is True

    def test_detects_dirty_with_untracked(self):
        """Sets dirty=True when untracked (?) files are present."""
        text = "? file.txt\n"
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is True

    def test_ignores_comments_without_data(self):
        """Ignores lines starting with # that don't match known patterns."""
        text = "# comment line\n"
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is False
        assert result["has_remote"] is False

    def test_handles_malformed_branch_ab(self):
        """Handles malformed # branch.ab lines gracefully."""
        text = "# branch.ab invalid\n"
        result = git._parse_git_status_v2(text)
        assert result["ahead"] == 0
        assert result["behind"] == 0

    def test_combined_status_example(self):
        """Parses a realistic combined status output."""
        text = """# branch.oid abc123def456
# branch.head main
# branch.upstream origin/main
# branch.ab +5 -2
1 .M N... 100644 100644 abc def src/file.py
? new_file.txt
"""
        result = git._parse_git_status_v2(text)
        assert result["has_remote"] is True
        assert result["ahead"] == 5
        assert result["behind"] == 2
        assert result["dirty"] is True

    def test_handles_negative_ahead_behind(self):
        """Parses negative ahead/behind correctly."""
        text = "# branch.ab +0 -3\n"
        result = git._parse_git_status_v2(text)
        assert result["ahead"] == 0
        assert result["behind"] == 3

    def test_never_raises_on_invalid_input(self):
        """Pure function never raises, even on bad input."""
        bad_inputs = [
            "garbage text",
            "# branch.ab +a -b",
            "1 2 3 4 5 6",
        ]
        for bad_input in bad_inputs:
            result = git._parse_git_status_v2(bad_input)
            assert isinstance(result, dict)
            assert "dirty" in result and "ahead" in result


class TestFormatGitStatusCell:
    """Tests for _format_git_status_cell — format status for Projects tab."""

    def test_returns_none_tag_when_no_git(self):
        """Returns ('—', 'git_none') when has_git is False."""
        result = git._format_git_status_cell(None, has_git=False)
        assert result == ("—", "git_none")

    def test_returns_pending_tag_when_status_none(self):
        """Returns ('…', 'git_pending') when status is None."""
        result = git._format_git_status_cell(None, has_git=True)
        assert result == ("…", "git_pending")

    def test_returns_tuple_for_no_remote(self):
        """Returns tuple when repo exists but has no remote."""
        status = {"dirty": False, "ahead": 0, "behind": 0, "has_remote": False}
        result = git._format_git_status_cell(status, has_git=True)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)

    def test_returns_tuple_for_clean_status(self):
        """Returns tuple for clean status."""
        status = {"dirty": False, "ahead": 0, "behind": 0, "has_remote": True}
        result = git._format_git_status_cell(status, has_git=True)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_tuple_for_dirty_status(self):
        """Returns tuple for dirty status."""
        status = {"dirty": True, "ahead": 0, "behind": 0, "has_remote": True}
        result = git._format_git_status_cell(status, has_git=True)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_tuple_for_ahead_behind(self):
        """Returns tuple with ahead/behind counts."""
        status = {"dirty": False, "ahead": 3, "behind": 1, "has_remote": True}
        result = git._format_git_status_cell(status, has_git=True)
        assert isinstance(result, tuple)
        assert len(result) == 2