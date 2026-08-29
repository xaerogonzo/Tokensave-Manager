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
        assert result == {"dirty": False, "ahead": 0, "behind": 0,
                          "has_remote": False, "changed_files": [],
                          "changed_truncated": False}

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

# ── _current_branch (Roadmap-9: CI badge branch resolution) ────────────────────

class TestCurrentBranch:
    """The CI badge asks which branch to poll; a wrong answer shows the wrong
    branch's build status, which is worse than showing none."""

    @staticmethod
    def _proc(out="", rc=0):
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def test_returns_the_branch_name(self):
        with mock.patch.object(git.subprocess, "run",
                               return_value=self._proc("Roadmap-9\n")):
            assert git._current_branch("/p", "git") == "Roadmap-9"

    def test_surrounding_whitespace_is_stripped(self):
        with mock.patch.object(git.subprocess, "run",
                               return_value=self._proc("  Roadmap-9  ")):
            assert git._current_branch("/p", "git") == "Roadmap-9"

    def test_detached_head_is_none_not_the_string_HEAD(self):
        """`rev-parse --abbrev-ref` prints the literal "HEAD" when detached.

        Returning it would make the badge query a branch named HEAD and report
        "no CI result for HEAD" — nonsense dressed as a fact.
        """
        with mock.patch.object(git.subprocess, "run",
                               return_value=self._proc("HEAD")):
            assert git._current_branch("/p", "git") is None

    def test_non_repo_is_none(self):
        with mock.patch.object(git.subprocess, "run",
                               return_value=self._proc("", rc=128)):
            assert git._current_branch("/p", "git") is None

    def test_missing_git_is_none_not_an_exception(self):
        with mock.patch.object(git.subprocess, "run",
                               side_effect=FileNotFoundError):
            assert git._current_branch("/p", "git") is None

    def test_empty_output_is_none(self):
        with mock.patch.object(git.subprocess, "run",
                               return_value=self._proc("   ")):
            assert git._current_branch("/p", "git") is None


class TestFmtAgeKeepsTheYear:
    """Last Synced dropped the year, hiding the stale projects it exists to show.

    `"%b %d"` rendered "May 13 this year" and "May 13 three years ago"
    identically — on a column whose purpose is spotting a stale index, that
    is the one distinction that matters.
    """

    @staticmethod
    def _ts(**delta):
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(**delta)).timestamp()

    def test_recent_stays_relative(self):
        from helpers.project_discovery import fmt_age
        assert fmt_age(self._ts(seconds=30)) == "just now"
        assert fmt_age(self._ts(minutes=20)) == "20m ago"
        assert fmt_age(self._ts(hours=5)) == "5h ago"
        assert fmt_age(self._ts(days=3)) == "3d ago"

    def test_this_year_shows_month_and_day(self):
        from datetime import datetime
        from helpers.project_discovery import fmt_age
        ts = self._ts(days=60)
        out = fmt_age(ts)
        if datetime.fromtimestamp(ts).year == datetime.now().year:
            assert out[-4:].strip().isdigit() is False or len(out) <= 6
            assert str(datetime.now().year) not in out

    def test_a_previous_year_carries_the_year(self):
        from datetime import datetime
        from helpers.project_discovery import fmt_age
        ts = self._ts(days=800)
        out = fmt_age(ts)
        expected_year = str(datetime.fromtimestamp(ts).year)
        assert expected_year in out, (
            f"{out!r} does not say which year — a two-year-old index is "
            f"indistinguishable from a two-month-old one")

    def test_two_dates_two_years_apart_do_not_render_alike(self):
        """The exact collision the old format produced."""
        from datetime import datetime
        from helpers.project_discovery import fmt_age
        now = datetime.now()
        a = now.replace(year=now.year - 1, month=5, day=13).timestamp()
        b = now.replace(year=now.year - 3, month=5, day=13).timestamp()
        assert fmt_age(a) != fmt_age(b)


class TestChangedFiles:
    """The per-file records `_parse_git_status_v2` used to walk past.

    `cli.py`'s `status` is documented as the one command a UI may call on every
    refresh and has to stay in the tens of milliseconds, so its docstring says
    anything not cheap "belongs in its own command". Collecting these costs one
    list append inside a loop that already ran — which is what lets `status`
    answer "which files changed" without a second subprocess or a new command.

    `-z` is the other half. `core.quotePath` defaults to true, so without it
    git C-quotes any path containing non-ASCII bytes, and a rename arrives with
    its two paths TAB-separated inside one line. Both silently corrupt exactly
    the paths a user is most likely to notice.
    """

    NUL = chr(0)

    def _z(self, *records: str) -> str:
        """Join records the way `-z` emits them: NUL-terminated."""
        return "".join(r + self.NUL for r in records)

    def test_argv_passes_dash_z(self):
        """The parser's guarantees depend on it, so the argv is pinned."""
        assert "-z" in git.git_status_argv("/repo", "git")

    def test_modified_file_is_named(self):
        text = self._z("1 .M N... 100644 100644 100644 aaa bbb src/app.py")
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is True
        assert result["changed_files"] == [
            {"path": "src/app.py", "status": "modified"}]

    def test_added_and_deleted_come_from_the_xy_field(self):
        text = self._z(
            "1 A. N... 000000 100644 100644 000 aaa src/new.py",
            "1 .D N... 100644 100644 000000 aaa bbb src/gone.py",
        )
        result = git._parse_git_status_v2(text)
        assert result["changed_files"] == [
            {"path": "src/new.py", "status": "added"},
            {"path": "src/gone.py", "status": "deleted"},
        ]

    def test_untracked_files_are_named(self):
        text = self._z("? notes.txt")
        result = git._parse_git_status_v2(text)
        assert result["changed_files"] == [
            {"path": "notes.txt", "status": "untracked"}]

    def test_unmerged_files_are_named(self):
        text = self._z(
            "u UU N... 100644 100644 100644 100644 aaa bbb ccc src/conflict.py")
        result = git._parse_git_status_v2(text)
        assert result["changed_files"] == [
            {"path": "src/conflict.py", "status": "unmerged"}]

    def test_a_rename_reports_the_new_path_with_the_old_one_beside_it(self):
        """`path` is where the file IS; `old_path` is where it came from.

        Round the wrong way, a commit-request picker would offer a path that no
        longer exists.
        """
        text = self._z(
            "2 R. N... 100644 100644 100644 aaa bbb R100 src/new_name.py",
            "src/old_name.py",
        )
        result = git._parse_git_status_v2(text)
        assert result["changed_files"] == [{
            "path": "src/new_name.py",
            "status": "renamed",
            "old_path": "src/old_name.py",
        }]

    def test_a_renames_extra_field_does_not_swallow_the_next_record(self):
        """The original path is consumed, not mistaken for another record."""
        text = self._z(
            "2 R. N... 100644 100644 100644 aaa bbb R100 b.py", "a.py",
            "1 .M N... 100644 100644 100644 aaa bbb c.py",
        )
        result = git._parse_git_status_v2(text)
        assert [f["path"] for f in result["changed_files"]] == ["b.py", "c.py"]

    def test_paths_with_spaces_survive(self):
        """The reference machine's own checkout lives under a spaced path."""
        text = self._z(
            "1 .M N... 100644 100644 100644 aaa bbb Token Save Manager/app.py")
        result = git._parse_git_status_v2(text)
        assert result["changed_files"][0]["path"] == "Token Save Manager/app.py"

    def test_non_ascii_paths_are_not_quoted_under_dash_z(self):
        """The `core.quotePath` hazard, in the mode the argv actually uses.

        With `-z` git emits the raw bytes; the parser must not re-interpret
        them. Without it the same path would arrive wrapped in quotes with
        backslash escapes.
        """
        text = self._z("1 .M N... 100644 100644 100644 aaa bbb src/caf\u00e9.py")
        result = git._parse_git_status_v2(text)
        assert result["changed_files"][0]["path"] == "src/caf\u00e9.py"

    def test_branch_fields_still_parse_alongside_files(self):
        """The pre-existing answers must be unchanged by the new ones."""
        text = self._z(
            "# branch.oid abc123",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +3 -2",
            "1 .M N... 100644 100644 100644 aaa bbb src/app.py",
        )
        result = git._parse_git_status_v2(text)
        assert result["has_remote"] is True
        assert result["ahead"] == 3
        assert result["behind"] == 2
        assert result["dirty"] is True
        assert len(result["changed_files"]) == 1

    def test_a_clean_tree_reports_no_files(self):
        text = self._z("# branch.oid abc123", "# branch.head main")
        result = git._parse_git_status_v2(text)
        assert result["dirty"] is False
        assert result["changed_files"] == []
        assert result["changed_truncated"] is False

    def test_the_cap_is_reported_not_applied_silently(self):
        """A short list that says nothing reads as "that is all of them"."""
        records = [f"1 .M N... 100644 100644 100644 aaa bbb f{i}.py"
                   for i in range(git.MAX_CHANGED_FILES + 25)]
        result = git._parse_git_status_v2(self._z(*records))
        assert len(result["changed_files"]) == git.MAX_CHANGED_FILES
        assert result["changed_truncated"] is True
        assert result["dirty"] is True          # still true for every record

    def test_a_truncated_run_still_counts_every_record_as_dirty(self):
        records = [f"1 .M N... 100644 100644 100644 aaa bbb f{i}.py"
                   for i in range(git.MAX_CHANGED_FILES + 1)]
        assert git._parse_git_status_v2(self._z(*records))["dirty"] is True

    def test_a_malformed_record_is_skipped_not_crashed_on(self):
        text = self._z("1 .M", "1 .M N... 100644 100644 100644 aaa bbb ok.py")
        result = git._parse_git_status_v2(text)
        assert [f["path"] for f in result["changed_files"]] == ["ok.py"]


class TestLegacyNewlineSeparatedStatus:
    """A caller that builds the argv itself and forgets `-z`.

    Not a convenience branch. Without it that caller hands the parser one
    enormous field: `# branch.upstream` is missed, `has_remote` comes back
    False for a repository that has an upstream, and a single mangled path is
    reported. Answering a slightly harder question correctly beats answering
    the wrong question quietly.
    """

    def test_newline_separated_input_still_parses(self):
        text = ("# branch.upstream origin/main\n"
                "# branch.ab +1 -0\n"
                "1 .M N... 100644 100644 100644 aaa bbb src/app.py\n")
        result = git._parse_git_status_v2(text)
        assert result["has_remote"] is True
        assert result["ahead"] == 1
        assert result["changed_files"] == [
            {"path": "src/app.py", "status": "modified"}]

    def test_legacy_renames_use_the_tab_separator(self):
        """Without `-z`, the original path hides behind a TAB on the same line."""
        text = ("2 R. N... 100644 100644 100644 aaa bbb R100 "
                "new.py\told.py\n")
        result = git._parse_git_status_v2(text)
        assert result["changed_files"] == [{
            "path": "new.py", "status": "renamed", "old_path": "old.py"}]
