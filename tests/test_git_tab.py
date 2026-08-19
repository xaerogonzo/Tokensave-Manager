"""Tests for controllers/git_tab.py — GitTabController and helper functions."""
import pytest
import subprocess
import queue
from types import SimpleNamespace
from unittest.mock import Mock

tk = pytest.importorskip("tkinter")
from tkinter import ttk

from constants import CREATE_NO_WINDOW
from controllers.git_tab import (
    GitTabController,
    _detect_base_branch,
    _extract_pr_title,
    _recommended_test_selection,
    _build_merge_cmd,
)

pytestmark = pytest.mark.tk


class TestDetectBaseBranch:
    """Test _detect_base_branch fallback chain."""

    def test_upstream_ref_different_from_current_branch(self, monkeypatch):
        """Step 1: return tracked upstream if not tracking self."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=0, stdout="origin/main\n", stderr="")
            elif "--abbrev-ref" in cmd and "HEAD" in cmd:
                return SimpleNamespace(returncode=0, stdout="feature-branch\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "origin/main"

    def test_upstream_ref_same_as_current_branch(self, monkeypatch):
        """Step 1: skip if upstream == origin/<current> (branch tracking itself)."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=0, stdout="origin/feature\n", stderr="")
            elif "--abbrev-ref" in cmd and "HEAD" in cmd:
                return SimpleNamespace(returncode=0, stdout="feature\n", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result is None

    def test_origin_head_pointer(self, monkeypatch):
        """Step 2: use origin/HEAD pointer if upstream fails."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=0, stdout="refs/remotes/origin/main\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "origin/main"

    def test_origin_main_exists(self, monkeypatch):
        """Step 3: check origin/main exists."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/main" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "origin/main"

    def test_origin_master_exists(self, monkeypatch):
        """Step 4: check origin/master exists."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/main" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/master" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "origin/master"

    def test_main_local_branch(self, monkeypatch):
        """Step 5: main as a local branch."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/main" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/master" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif cmd[-1] == "main":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "main"

    def test_master_local_branch(self, monkeypatch):
        """Step 6: master as a local branch."""
        def mock_run(cmd, *args, **kwargs):
            if "--symbolic-full-name" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "symbolic-ref" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/main" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif "refs/remotes/origin/master" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif cmd[-1] == "main":
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            elif cmd[-1] == "master":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result == "master"

    def test_complete_failure(self, monkeypatch):
        """Step 7: return None if all detection steps fail."""
        def mock_run(cmd, *args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result is None

    def test_uses_custom_git_exe(self, monkeypatch):
        """Uses custom git_exe when provided."""
        calls = []

        def mock_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        _detect_base_branch("/repo", "custom-git")
        assert any(c[0] == "custom-git" for c in calls)

    def test_defaults_to_git_when_exe_empty(self, monkeypatch):
        """Uses 'git' when git_exe is empty or None."""
        calls = []

        def mock_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        _detect_base_branch("/repo", "")
        assert any(c[0] == "git" for c in calls)

    def test_subprocess_timeout(self, monkeypatch):
        """Handles subprocess timeout gracefully."""
        def mock_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired("git", 5)

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result is None

    def test_subprocess_exception(self, monkeypatch):
        """Handles subprocess exceptions gracefully."""
        def mock_run(cmd, *args, **kwargs):
            raise RuntimeError("git not found")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        result = _detect_base_branch("/repo", "git")
        assert result is None

    def test_creationflags_passed_to_subprocess(self, monkeypatch):
        """Passes CREATE_NO_WINDOW creationflags to subprocess."""
        received_kwargs = {}

        def mock_run(cmd, *args, **kwargs):
            received_kwargs.update(kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        _detect_base_branch("/repo", "git")
        assert received_kwargs.get("creationflags") == CREATE_NO_WINDOW

    def test_subprocess_parameters(self, monkeypatch):
        """Subprocess called with correct parameters."""
        received_kwargs = {}

        def mock_run(cmd, *args, **kwargs):
            received_kwargs.update(kwargs)
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("controllers.git_tab.subprocess.run", mock_run)
        _detect_base_branch("/repo", "git")
        assert received_kwargs.get("capture_output") is True
        assert received_kwargs.get("text") is True
        assert received_kwargs.get("timeout") == 5
        assert received_kwargs.get("encoding") == "utf-8"
        assert received_kwargs.get("errors") == "replace"


class TestExtractPRTitle:
    """Test _extract_pr_title markdown parsing."""

    def test_extracts_first_bullet_under_summary(self):
        """Extract first non-empty bullet under '## Summary of Changes'."""
        text = """## Summary of Changes
- Fixed the bug
- Added a feature
"""
        assert _extract_pr_title(text) == "Fixed the bug"

    def test_extracts_from_summary_with_asterisk_bullet(self):
        """Handle * bullets."""
        text = """## Summary of Changes
* Test feature
Other text
"""
        assert _extract_pr_title(text) == "Test feature"

    def test_extracts_from_summary_with_dot_bullet(self):
        """Handle • bullets."""
        text = """## Summary of Changes
• Another feature
"""
        assert _extract_pr_title(text) == "Another feature"

    def test_skips_headers_in_summary(self):
        """Fallback when headers encountered in summary section."""
        text = """## Summary of Changes
### Sub-header
- Actual feature
"""
        assert _extract_pr_title(text) == "- Actual feature"

    def test_truncates_to_120_chars(self):
        """Truncate result to 120 characters."""
        long_title = "x" * 150
        text = f"""## Summary of Changes
- {long_title}
"""
        result = _extract_pr_title(text)
        assert len(result) == 120

    def test_fallback_to_first_non_header_line(self):
        """Fallback to first non-empty non-header line if no summary section."""
        text = """Some preamble

Not a header line

# Header at the end
"""
        assert _extract_pr_title(text) == "Some preamble"

    def test_fallback_skips_headers(self):
        """Fallback doesn't pick lines starting with #."""
        text = """# Header 1
## Header 2
Some content
"""
        assert _extract_pr_title(text) == "Some content"

    def test_case_insensitive_summary_search(self):
        """Case-insensitive match for '## summary'."""
        text = """## SUMMARY of changes
- Feature line
"""
        assert _extract_pr_title(text) == "Feature line"

    def test_empty_text_returns_default(self):
        """Return default when text is empty."""
        assert _extract_pr_title("") == "PR description"

    def test_no_summary_section_returns_default(self):
        """Return default when no summary and no non-header lines."""
        text = """# Header 1
## Header 2
"""
        assert _extract_pr_title(text) == "PR description"

    def test_whitespace_handling(self):
        """Strip leading/trailing whitespace."""
        text = """## Summary of Changes
-   Padded title   
"""
        assert _extract_pr_title(text) == "Padded title"

    def test_empty_bullet_skipped(self):
        """Skip empty bullets and continue to next."""
        text = """## Summary of Changes
- Real title
- Another item
"""
        assert _extract_pr_title(text) == "Real title"

    def test_non_bullet_text_in_summary_section(self):
        """Use first non-empty non-bullet text in summary section."""
        text = """## Summary of Changes
Some plain text here
"""
        assert _extract_pr_title(text) == "Some plain text here"


class TestRecommendedTestSelection:
    """Test _recommended_test_selection logic."""

    def test_returns_true_for_requires_automation(self):
        """Return True for suggestions with requires_automation=True."""
        class Suggestion:
            requires_automation = True

        suggestions = [Suggestion()]
        result = _recommended_test_selection(suggestions)
        assert result == [True]

    def test_returns_false_for_no_automation(self):
        """Return False for suggestions with requires_automation=False."""
        class Suggestion:
            requires_automation = False

        suggestions = [Suggestion()]
        result = _recommended_test_selection(suggestions)
        assert result == [False]

    def test_returns_false_for_missing_attribute(self):
        """Return False for suggestions without requires_automation."""
        class Suggestion:
            pass

        suggestions = [Suggestion()]
        result = _recommended_test_selection(suggestions)
        assert result == [False]

    def test_handles_mixed_suggestions(self):
        """Handle mix of different suggestion types."""
        class AutoSuggestion:
            requires_automation = True

        class NoAutoSuggestion:
            requires_automation = False

        class NoAttrSuggestion:
            pass

        suggestions = [
            AutoSuggestion(),
            NoAutoSuggestion(),
            NoAttrSuggestion(),
        ]
        result = _recommended_test_selection(suggestions)
        assert result == [True, False, False]

    def test_empty_suggestions_list(self):
        """Return empty list for empty suggestions."""
        result = _recommended_test_selection([])
        assert result == []

    def test_multiple_true_suggestions(self):
        """Handle multiple automation suggestions."""
        class Suggestion:
            def __init__(self, auto):
                self.requires_automation = auto

        suggestions = [Suggestion(True), Suggestion(True), Suggestion(False)]
        result = _recommended_test_selection(suggestions)
        assert result == [True, True, False]


class TestGitTabControllerInit:
    """Test GitTabController initialization."""

    def test_initializes_with_notebook(self, tk_root):
        """Initialize controller with notebook and store references."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"
        get_path = Mock(return_value="/test/repo")
        on_log = Mock()
        on_shell = Mock()
        on_commit = Mock()

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=get_path,
            on_log=on_log,
            on_shell=on_shell,
            on_commit=on_commit,
        )

        assert controller._notebook is notebook
        assert controller._cfg is cfg
        assert controller._get_path is get_path
        assert controller._on_log is on_log
        assert controller._on_shell is on_shell
        assert controller._on_commit is on_commit


    def test_initializes_log_queue(self, tk_root):
        """Initialize log queue."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=Mock(),
            on_log=Mock(),
            on_shell=Mock(),
            on_commit=Mock(),
        )

        assert isinstance(controller._log_queue, queue.Queue)

    def test_initializes_dialog_refs_to_none(self, tk_root):
        """Initialize dialog references to None."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=Mock(),
            on_log=Mock(),
            on_shell=Mock(),
            on_commit=Mock(),
        )

        assert controller._test_manager_ref is None
        assert controller._pr_draft_dialog is None
        assert controller._pr_draft_dirty is False

    def test_creates_git_tab_frame(self, tk_root):
        """Create and add Git tab frame to notebook."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=Mock(),
            on_log=Mock(),
            on_shell=Mock(),
            on_commit=Mock(),
        )

        assert isinstance(controller._tab, tk.Frame)
        tabs = notebook.tabs()
        assert len(tabs) == 1

    def test_initializes_branch_management_controller(self, tk_root):
        """Initialize BranchManagementController sub-controller."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=Mock(),
            on_log=Mock(),
            on_shell=Mock(),
            on_commit=Mock(),
        )

        assert controller._branch_mgmt is not None

    def test_root_property_returns_toplevel(self, tk_root):
        """_root property returns the top-level window."""
        notebook = ttk.Notebook(tk_root)
        cfg = Mock()
        cfg.git_exe = "git"

        controller = GitTabController(
            notebook=notebook,
            cfg=cfg,
            get_path=Mock(),
            on_log=Mock(),
            on_shell=Mock(),
            on_commit=Mock(),
        )

        assert controller._root is tk_root

class TestCmdOpenClaudeCli:
    """cmd_open_claude_cli guards + delegation, tested unbound on a stub."""

    @staticmethod
    def _stub(git_path="/repo", cli_exe="/usr/bin/claude",
              cli_model="claude-haiku"):
        cfg = SimpleNamespace(claude_cli_exe=cli_exe,
                              claude_cli_model=cli_model)
        return SimpleNamespace(_git_path=git_path, _cfg=cfg, _root=None)

    def test_no_project_selected_is_noop(self, mocker):
        mock_spawn = mocker.patch(
            "helpers.claude_cli.spawn_claude_cli_interactive")
        mock_info = mocker.patch("controllers.git_tab.messagebox.showinfo")
        GitTabController.cmd_open_claude_cli(self._stub(git_path=None))
        mock_spawn.assert_not_called()
        mock_info.assert_not_called()

    def test_no_cli_configured_shows_info(self, mocker):
        mock_spawn = mocker.patch(
            "helpers.claude_cli.spawn_claude_cli_interactive")
        mock_info = mocker.patch("controllers.git_tab.messagebox.showinfo")
        GitTabController.cmd_open_claude_cli(self._stub(cli_exe=""))
        mock_spawn.assert_not_called()
        mock_info.assert_called_once()

    def test_spawns_interactive_with_project_and_model(self, mocker):
        mock_spawn = mocker.patch(
            "helpers.claude_cli.spawn_claude_cli_interactive",
            return_value=(True, ""))
        mock_warn = mocker.patch(
            "controllers.git_tab.messagebox.showwarning")
        GitTabController.cmd_open_claude_cli(self._stub())
        mock_spawn.assert_called_once_with(
            "/usr/bin/claude", "/repo", model="claude-haiku")
        mock_warn.assert_not_called()

    def test_spawn_failure_shows_warning(self, mocker):
        mocker.patch(
            "helpers.claude_cli.spawn_claude_cli_interactive",
            return_value=(False, "boom"))
        mock_warn = mocker.patch(
            "controllers.git_tab.messagebox.showwarning")
        GitTabController.cmd_open_claude_cli(self._stub())
        mock_warn.assert_called_once()
        assert "boom" in mock_warn.call_args[0]


class TestMergeBodyFromChangelog:
    """`_do_merge_pr` with the opt-in CHANGELOG body (Roadmap-9 Phase 2.3).

    The value is in the argv: these bullets carry backticks, ampersands and
    equals signs, so the body must travel as a separate argv element and
    never be interpolated into a shell string.
    """

    _CL = ("# Changelog\n\n## [Unreleased]\n\n### Fixed\n"
           "- uses `--cov-fail-under=14` & handles # and = now\n")

    def _write_changelog(self, tmp_path, text=None):
        (tmp_path / "CHANGELOG.md").write_text(
            text if text is not None else self._CL, encoding="utf-8")
        return str(tmp_path)

    def test_reads_and_renders_the_unreleased_block(self, tmp_path):
        path = self._write_changelog(tmp_path)
        body = GitTabController._changelog_merge_body(path)
        assert body.startswith("Fixed:")
        assert "`--cov-fail-under=14`" in body
        assert "&" in body and "#" in body

    def test_missing_changelog_yields_empty_not_an_error(self, tmp_path):
        assert GitTabController._changelog_merge_body(str(tmp_path)) == ""

    def test_empty_unreleased_yields_empty(self, tmp_path):
        path = self._write_changelog(
            tmp_path, "# Changelog\n\n## [Unreleased]\n\n## [1.0] — x\n- old\n")
        assert GitTabController._changelog_merge_body(path) == ""

    # ── argv shape (pure builder) ─────────────────────────────────────

    def test_opt_out_adds_no_subject_or_body(self):
        cmd = _build_merge_cmd(12, "merge", False, "PR title", "")
        assert cmd == ["gh", "pr", "merge", "12", "--merge"]
        assert "--body" not in cmd and "--subject" not in cmd

    def test_body_travels_as_its_own_argv_element(self):
        body = "Fixed:" + chr(10) + "- uses `--cov-fail-under=14` & handles # and ="
        cmd = _build_merge_cmd(12, "squash", False, "PR title", body)
        assert cmd[cmd.index("--body") + 1] == body,             "the body must be one argv element, never shell-interpolated"
        assert cmd[cmd.index("--subject") + 1] == "PR title"

    def test_delete_branch_flag_is_independent_of_the_body(self):
        body = "Fixed:" + chr(10) + "- x"
        cmd = _build_merge_cmd(7, "merge", True, "t", body)
        assert "--delete-branch" in cmd
        assert "--body" in cmd

    def test_empty_body_never_writes_a_blank_over_the_default(self):
        """An empty CHANGELOG must fall through to GitHub's own message."""
        for strategy in ("merge", "squash", "rebase"):
            cmd = _build_merge_cmd(1, strategy, False, "t", "")
            assert "--body" not in cmd
