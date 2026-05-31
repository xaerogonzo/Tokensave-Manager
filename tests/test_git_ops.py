"""Tests for GitOpsController."""

from __future__ import annotations

import os
import threading
from unittest.mock import Mock, MagicMock, patch, call, mock_open
from typing import Any

import pytest
import tkinter as tk
from tkinter import ttk

from controllers.git_ops_ctrl import GitOpsController


@pytest.fixture
def mock_cfg() -> Mock:
    """Mock ManagerConfig."""
    cfg = Mock()
    cfg.git_exe = "git"
    cfg.raw = {}
    return cfg


@pytest.fixture
def mock_callbacks() -> dict[str, Mock]:
    """Mock all callback dependencies."""
    return {
        "on_log": Mock(),
        "on_shell": Mock(),
        "on_refresh": Mock(),
        "on_commit_offer": Mock(),
        "on_commit": Mock(),
        "on_project_select": Mock(),
    }


@pytest.fixture
def mock_tab() -> Mock:
    """Mock tk.Frame with winfo_toplevel."""
    tab = Mock(spec=tk.Frame)
    mock_root = Mock(spec=tk.Tk)
    tab.winfo_toplevel.return_value = mock_root
    tab.after = Mock()
    return tab


@pytest.fixture
def mock_notebook() -> Mock:
    """Mock ttk.Notebook."""
    notebook = Mock(spec=ttk.Notebook)
    return notebook


@pytest.fixture
def controller(mock_tab: Mock, mock_notebook: Mock, mock_cfg: Mock, mock_callbacks: dict) -> GitOpsController:
    """Create a GitOpsController instance with mocked dependencies."""
    return GitOpsController(
        tab=mock_tab,
        notebook=mock_notebook,
        cfg=mock_cfg,
        on_log=mock_callbacks["on_log"],
        on_shell=mock_callbacks["on_shell"],
        on_refresh=mock_callbacks["on_refresh"],
        on_commit_offer=mock_callbacks["on_commit_offer"],
        on_commit=mock_callbacks["on_commit"],
        on_project_select=mock_callbacks["on_project_select"],
    )


class TestGitOpsControllerInit:
    """Tests for __init__ and property setup."""

    def test_initialization_stores_dependencies(self, mock_tab: Mock, mock_notebook: Mock, mock_cfg: Mock, mock_callbacks: dict):
        """Test that __init__ stores all dependencies correctly."""
        controller = GitOpsController(
            tab=mock_tab,
            notebook=mock_notebook,
            cfg=mock_cfg,
            **mock_callbacks,
        )
        assert controller._tab is mock_tab
        assert controller._notebook is mock_notebook
        assert controller._cfg is mock_cfg
        assert controller._on_log is mock_callbacks["on_log"]
        assert controller._on_shell is mock_callbacks["on_shell"]
        assert controller._on_refresh is mock_callbacks["on_refresh"]
        assert controller._on_commit_offer is mock_callbacks["on_commit_offer"]
        assert controller._on_commit is mock_callbacks["on_commit"]
        assert controller._on_project_select is mock_callbacks["on_project_select"]

    def test_root_property_returns_top_level_window(self, controller: GitOpsController, mock_tab: Mock):
        """Test that _root property returns the result of winfo_toplevel()."""
        mock_root = Mock(spec=tk.Tk)
        mock_tab.winfo_toplevel.return_value = mock_root
        assert controller._root is mock_root
        mock_tab.winfo_toplevel.assert_called_once()


class TestCmdGitLog:
    """Tests for cmd_git_log method."""

    def test_switches_to_git_tab_and_calls_project_select(self, controller: GitOpsController, mock_notebook: Mock, mock_callbacks: dict):
        """Test that cmd_git_log switches to Git tab and calls on_project_select."""
        mock_notebook.index.return_value = 3
        mock_notebook.tab.side_effect = lambda idx, key: {
            0: "Projects",
            1: "Git",
            2: "Other",
        }.get(idx, "Projects")

        path = "/path/to/project"
        controller.cmd_git_log(path)

        mock_notebook.select.assert_called_once_with(1)
        mock_callbacks["on_project_select"].assert_called_once_with(path)

    def test_handles_missing_git_tab_gracefully(self, controller: GitOpsController, mock_notebook: Mock, mock_callbacks: dict):
        """Test that cmd_git_log handles TclError when Git tab not found."""
        mock_notebook.index.side_effect = tk.TclError("error")

        path = "/path/to/project"
        controller.cmd_git_log(path)

        mock_callbacks["on_project_select"].assert_called_once_with(path)

    def test_finds_git_tab_by_exact_name_match(self, controller: GitOpsController, mock_notebook: Mock, mock_callbacks: dict):
        """Test that cmd_git_log finds Git tab with exact name matching."""
        mock_notebook.index.return_value = 5
        mock_notebook.tab.side_effect = lambda idx, key: {
            0: "Projects  ",
            1: "  Git  ",
            2: "Git Tab",
            3: "Other",
        }.get(idx, "Other")

        path = "/path/to/project"
        controller.cmd_git_log(path)

        mock_notebook.select.assert_called_once_with(1)


class TestCmdGitCommit:
    """Tests for cmd_git_commit method."""

    def test_calls_on_commit_with_path(self, controller: GitOpsController, mock_callbacks: dict):
        """Test that cmd_git_commit calls on_commit with the path."""
        path = "/path/to/project"
        controller.cmd_git_commit(path)
        mock_callbacks["on_commit"].assert_called_once_with(path)

    def test_cmd_git_commit_with_various_paths(self, controller: GitOpsController, mock_callbacks: dict):
        """Test cmd_git_commit with different path formats."""
        paths = ["/abs/path", "relative/path", ".", ".."]
        for path in paths:
            mock_callbacks["on_commit"].reset_mock()
            controller.cmd_git_commit(path)
            mock_callbacks["on_commit"].assert_called_once_with(path)


class TestCmdAiCodeReview:
    """Tests for cmd_ai_code_review method."""

    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_shows_error_if_not_git_repo(self, mock_is_git: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that error messagebox is shown when path is not a git repo."""
        mock_is_git.return_value = False

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/non-repo"
            controller.cmd_ai_code_review(path)

            mock_msgbox.assert_called_once()
            assert "Not a git repo" in mock_msgbox.call_args[0][0]
            assert mock_msgbox.call_args[1]["parent"] == controller._root

    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_shows_error_if_ai_not_enabled(self, mock_is_git: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that error messagebox is shown when AI is not enabled."""
        mock_is_git.return_value = True
        controller._cfg.raw = {"commit_message_llm": {"enabled": False}}

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/repo"
            controller.cmd_ai_code_review(path)

            mock_msgbox.assert_called_once()
            assert "AI is not enabled" in mock_msgbox.call_args[0][0]

    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_shows_error_if_llm_config_missing(self, mock_is_git: Mock, controller: GitOpsController):
        """Test that error is shown when llm config is completely missing."""
        mock_is_git.return_value = True
        controller._cfg.raw = {}

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/repo"
            controller.cmd_ai_code_review(path)

            mock_msgbox.assert_called_once()
            assert "AI is not enabled" in mock_msgbox.call_args[0][0]

    @patch("controllers.git_ops_ctrl.AICodeReviewDialog")
    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_opens_ai_code_review_dialog(self, mock_is_git: Mock, mock_dialog: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that AICodeReviewDialog is opened when conditions are met."""
        mock_is_git.return_value = True
        llm_cfg = {"enabled": True, "model": "gpt-4"}
        controller._cfg.raw = {"commit_message_llm": llm_cfg}

        path = "/path/to/repo"
        controller.cmd_ai_code_review(path)

        mock_dialog.assert_called_once_with(controller._root, path, llm_cfg, controller._cfg)

    @patch("controllers.git_ops_ctrl.AICodeReviewDialog")
    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_opens_ai_dialog_with_minimal_config(self, mock_is_git: Mock, mock_dialog: Mock, controller: GitOpsController):
        """Test opening dialog with minimal LLM config."""
        mock_is_git.return_value = True
        llm_cfg = {"enabled": True}
        controller._cfg.raw = {"commit_message_llm": llm_cfg}

        path = "/path/to/repo"
        controller.cmd_ai_code_review(path)

        mock_dialog.assert_called_once_with(controller._root, path, llm_cfg, controller._cfg)


class TestCmdGitInit:
    """Tests for cmd_git_init method."""

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_shows_error_if_already_git_repo(self, mock_is_repo: Mock, controller: GitOpsController):
        """Test that error is shown if directory is already a git repo."""
        mock_is_repo.return_value = True

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/repo"
            controller.cmd_git_init(path)

            mock_msgbox.assert_called_once()
            assert "Already a repository" in mock_msgbox.call_args[0][0]
            assert mock_msgbox.call_args[1]["parent"] == controller._root

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_runs_git_init_command(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that git init command is executed."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=True):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                path = "/path/to/repo"
                controller.cmd_git_init(path)

                mock_callbacks["on_shell"].assert_called()
                call_args = mock_callbacks["on_shell"].call_args_list[0]
                args = call_args[0][0]
                assert args[0] == "git"
                assert "-C" in args
                assert "init" in args

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_logs_git_init_output(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that git init output is logged."""
        mock_is_repo.return_value = False
        output_text = "Initialized empty Git repository in /path/to/repo/.git/"
        mock_callbacks["on_shell"].return_value = (output_text + "\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=True):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                path = "/path/to/repo"
                controller.cmd_git_init(path)

                mock_callbacks["on_log"].assert_called()
                logged_lines = [call[0][0] for call in mock_callbacks["on_log"].call_args_list]
                assert any("git init" in line for line in logged_lines)

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_handles_git_init_failure(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test behavior when git init command fails."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("fatal: Permission denied\n", 128)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=True):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                path = "/path/to/repo"
                controller.cmd_git_init(path)

                mock_callbacks["on_refresh"].assert_called_once()

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_creates_gitignore_if_missing(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that .gitignore is created if it doesn't exist."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=False):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                with patch("builtins.open", mock_open()) as m_open:
                    path = "/path/to/repo"
                    controller.cmd_git_init(path)

                    m_open.assert_called()

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_logs_gitignore_creation_success(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that gitignore creation is logged."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=False):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                with patch("builtins.open", mock_open()):
                    path = "/path/to/repo"
                    controller.cmd_git_init(path)

                    logged_lines = [call[0][0] for call in mock_callbacks["on_log"].call_args_list]
                    assert any("Created baseline .gitignore" in line for line in logged_lines)

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_handles_gitignore_write_error(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that OSError during gitignore write is handled gracefully."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=False):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                with patch("builtins.open", side_effect=OSError("Permission denied")):
                    path = "/path/to/repo"
                    controller.cmd_git_init(path)

                    logged_lines = [call[0][0] for call in mock_callbacks["on_log"].call_args_list]
                    assert any("Warning" in line and ".gitignore" in line for line in logged_lines)

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_creates_initial_commit_when_user_agrees(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that initial commit is created when user confirms."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=True):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=True):
                with patch("controllers.git_ops_ctrl.threading.Thread") as mock_thread:
                    path = "/path/to/repo"
                    controller.cmd_git_init(path)

                    mock_thread.assert_called_once()
                    assert mock_thread.return_value.start.called

    @patch("controllers.git_ops_ctrl._is_git_repo")
    def test_refresh_called_when_user_declines_initial_commit(self, mock_is_repo: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test that on_refresh is called when user declines initial commit."""
        mock_is_repo.return_value = False
        mock_callbacks["on_shell"].return_value = ("Initialized empty Git repository\n", 0)

        with patch("controllers.git_ops_ctrl.os.path.isfile", return_value=True):
            with patch("controllers.git_ops_ctrl.messagebox.askyesno", return_value=False):
                path = "/path/to/repo"
                controller.cmd_git_init(path)

                mock_callbacks["on_refresh"].assert_called_once()


class TestCmdManageGitignore:
    """Tests for cmd_manage_gitignore method."""

    @patch("controllers.git_ops_ctrl.GitignoreDialog")
    def test_opens_gitignore_dialog(self, mock_dialog: Mock, controller: GitOpsController):
        """Test that GitignoreDialog is opened with correct arguments."""
        path = "/path/to/repo"
        controller.cmd_manage_gitignore(path)

        mock_dialog.assert_called_once_with(controller._root, path, controller._cfg)

    @patch("controllers.git_ops_ctrl.GitignoreDialog")
    def test_gitignore_dialog_with_various_paths(self, mock_dialog: Mock, controller: GitOpsController):
        """Test gitignore dialog with various path types."""
        paths = ["/abs/path", "relative/path", "/path with spaces/repo"]
        for path in paths:
            mock_dialog.reset_mock()
            controller.cmd_manage_gitignore(path)
            mock_dialog.assert_called_once_with(controller._root, path, controller._cfg)


class TestCmdUntrackIgnored:
    """Tests for cmd_untrack_ignored method."""

    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_shows_error_if_not_git_repo(self, mock_is_repo: Mock, controller: GitOpsController):
        """Test that error is shown if not a git repo."""
        mock_is_repo.return_value = False

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/non-repo"
            controller.cmd_untrack_ignored(path)

            mock_msgbox.assert_called_once()
            assert "Not a git repo" in mock_msgbox.call_args[0][0]
            assert mock_msgbox.call_args[1]["parent"] == controller._root

    @patch("controllers.git_ops_ctrl._find_tracked_but_ignored")
    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_shows_info_if_no_tracked_ignored_files(self, mock_is_repo: Mock, mock_find: Mock, controller: GitOpsController):
        """Test that info messagebox is shown when no tracked-but-ignored files found."""
        mock_is_repo.return_value = True
        mock_find.return_value = []

        with patch("controllers.git_ops_ctrl.messagebox.showinfo") as mock_msgbox:
            path = "/path/to/repo"
            controller.cmd_untrack_ignored(path)

            mock_msgbox.assert_called_once()
            assert "Nothing to untrack" in mock_msgbox.call_args[0][0]
            assert mock_msgbox.call_args[1]["parent"] == controller._root

    @patch("controllers.git_ops_ctrl.UntrackIgnoredDialog")
    @patch("controllers.git_ops_ctrl._find_tracked_but_ignored")
    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_opens_untrack_dialog_with_files(self, mock_is_repo: Mock, mock_find: Mock, mock_dialog: Mock, controller: GitOpsController):
        """Test that UntrackIgnoredDialog is opened with found files."""
        mock_is_repo.return_value = True
        files = ["file1.py", "file2.pyc"]
        mock_find.return_value = files

        path = "/path/to/repo"
        controller.cmd_untrack_ignored(path)

        mock_dialog.assert_called_once()
        call_args = mock_dialog.call_args
        assert call_args[0][0] == controller._root
        assert call_args[0][1] == path
        assert call_args[0][2] == files
        assert "on_confirm" in call_args[1]

    @patch("controllers.git_ops_ctrl._find_tracked_but_ignored")
    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_calls_git_exe_when_finding_files(self, mock_is_repo: Mock, mock_find: Mock, controller: GitOpsController):
        """Test that git_exe is passed to find function."""
        mock_is_repo.return_value = True
        mock_find.return_value = []

        with patch("controllers.git_ops_ctrl.messagebox.showinfo"):
            path = "/path/to/repo"
            controller.cmd_untrack_ignored(path)

            mock_find.assert_called_once_with(path, controller._cfg.git_exe)


class TestCmdCreatePrivateRepo:
    """Tests for cmd_create_private_repo method."""

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_filters_noise_pyc_files(self, mock_find: Mock, controller: GitOpsController):
        """Test that .pyc files are filtered out."""
        raw_files = ["important.txt", "noisy.pyc", "module.pyo", "script.pyd"]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            mock_dialog.assert_called_once()
            filtered_files = mock_dialog.call_args[0][2]
            assert "important.txt" in filtered_files
            assert not any(f.endswith((".pyc", ".pyo", ".pyd")) for f in filtered_files)

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_filters_noise_log_and_db_files(self, mock_find: Mock, controller: GitOpsController):
        """Test that log and db files are filtered out."""
        raw_files = ["app.log", "data.db-wal", "backup.db-shm", "temp.db-wal2", "README.md"]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            assert "README.md" in filtered_files
            assert not any(f.endswith((".log", ".db-wal", ".db-shm", ".db-wal2")) for f in filtered_files)

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_filters_noise_directories(self, mock_find: Mock, controller: GitOpsController):
        """Test that noise directories are filtered out."""
        raw_files = [
            "__pycache__/module.pyc",
            ".tokensave/index.db",
            ".codegraph/graph.db",
            ".git/config",
            "node_modules/package.json",
        ]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            assert len(filtered_files) == 0

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_filters_venv_directories(self, mock_find: Mock, controller: GitOpsController):
        """Test that virtual environment directories are filtered."""
        raw_files = [
            ".venv/bin/python",
            "venv/lib/site-packages",
            ".tox/py39/lib",
            ".mypy_cache/3.9",
            ".pytest_cache/.gitkeep",
            ".ruff_cache/somefile",
            ".eggs/package.egg",
        ]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            assert len(filtered_files) == 0

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_keeps_important_files(self, mock_find: Mock, controller: GitOpsController):
        """Test that important files are not filtered."""
        raw_files = [
            "config.json",
            "secrets.env",
            "build/output.o",
            "dist/package.tar.gz",
        ]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            assert set(filtered_files) == set(raw_files)

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_sorts_by_path_depth_root_files_first(self, mock_find: Mock, controller: GitOpsController):
        """Test that files are sorted by path depth with root files first."""
        raw_files = [
            "deep/nested/dir/file.txt",
            "root.txt",
            "two/level.txt",
        ]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            root_idx = next((i for i, f in enumerate(filtered_files) if f == "root.txt"), -1)
            nested_idx = next((i for i, f in enumerate(filtered_files) if "deep/nested" in f), -1)
            assert root_idx < nested_idx

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_calls_private_repo_setup_dialog(self, mock_find: Mock, controller: GitOpsController):
        """Test that PrivateRepoSetupDialog is created with correct arguments."""
        raw_files = ["important.txt"]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            mock_dialog.assert_called_once()
            call_args = mock_dialog.call_args
            assert call_args[0][0] == controller._root
            assert call_args[0][1] == path

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_handles_empty_gitignored_files(self, mock_find: Mock, controller: GitOpsController):
        """Test behavior when no gitignored files are found."""
        mock_find.return_value = []

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            mock_dialog.assert_called_once()
            filtered_files = mock_dialog.call_args[0][2]
            assert filtered_files == []

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_calls_find_gitignored_with_git_exe(self, mock_find: Mock, controller: GitOpsController):
        """Test that git_exe is passed to find function."""
        mock_find.return_value = []

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog"):
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            mock_find.assert_called_once_with(path, controller._cfg.git_exe)

    @patch("controllers.git_ops_ctrl._find_gitignored_on_disk")
    def test_mixed_noise_and_important_files(self, mock_find: Mock, controller: GitOpsController):
        """Test filtering with a mix of noise and important files."""
        raw_files = [
            "important_config.json",
            ".git/HEAD",
            "src/module.py",
            "__pycache__/module.pyc",
            "docs/README.md",
            ".venv/bin/activate",
            "build/lib.so",
        ]
        mock_find.return_value = raw_files

        with patch("controllers.git_ops_ctrl.PrivateRepoSetupDialog") as mock_dialog:
            path = "/path/to/repo"
            controller.cmd_create_private_repo(path)

            filtered_files = mock_dialog.call_args[0][2]
            expected = ["important_config.json", "src/module.py", "docs/README.md", "build/lib.so"]
            assert set(filtered_files) == set(expected)


class TestIntegration:
    """Integration tests for GitOpsController."""

    def test_multiple_commands_in_sequence(self, controller: GitOpsController, mock_callbacks: dict):
        """Test that multiple commands can be executed in sequence."""
        path = "/path/to/repo"

        controller.cmd_git_commit(path)
        assert mock_callbacks["on_commit"].call_count == 1

        mock_callbacks["on_commit"].reset_mock()
        controller.cmd_git_commit(path)
        assert mock_callbacks["on_commit"].call_count == 1

    @patch("controllers.git_ops_ctrl._is_local_git_repo")
    def test_ai_review_then_commit(self, mock_is_git: Mock, controller: GitOpsController, mock_callbacks: dict):
        """Test workflow of AI review followed by commit."""
        mock_is_git.return_value = False

        path = "/path/to/repo"

        with patch("controllers.git_ops_ctrl.messagebox.showinfo"):
            controller.cmd_ai_code_review(path)

        controller.cmd_git_commit(path)
        mock_callbacks["on_commit"].assert_called_once_with(path)

    def test_dependencies_not_modified(self, controller: GitOpsController, mock_tab: Mock, mock_notebook: Mock, mock_cfg: Mock):
        """Test that command execution doesn't modify stored dependencies."""
        original_tab = controller._tab
        original_notebook = controller._notebook
        original_cfg = controller._cfg

        with patch("controllers.git_ops_ctrl.messagebox.showinfo"):
            with patch("controllers.git_ops_ctrl._is_local_git_repo", return_value=False):
                controller.cmd_ai_code_review("/path")

        assert controller._tab is original_tab
        assert controller._notebook is original_notebook
        assert controller._cfg is original_cfg