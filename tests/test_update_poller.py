"""Tests for UpdatePollerController."""

import os
import subprocess
import sys
import threading
from unittest.mock import MagicMock, Mock, patch, call

import pytest
from tkinter import messagebox

from controllers.update_poller import UpdatePollerController
from constants import CREATE_NO_WINDOW, _BASE_DIR


class TestUpdatePollerControllerInit:
    """Tests for UpdatePollerController initialization."""

    def test_init_sets_attributes(self):
        """Test that __init__ properly sets all attributes."""
        cfg = Mock()
        on_log = Mock()
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, on_log, on_run, root)

        assert controller._cfg is cfg
        assert controller._on_log is on_log
        assert controller._on_run is on_run
        assert controller._root is root
        assert controller._current_version is None
        assert controller._available_version is None
        assert controller._last_integration_report == ""
        assert controller._integration_dialog is None

    def test_init_with_none_values(self):
        """Test initialization with minimal mock objects."""
        cfg = Mock()
        on_log = Mock()
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, on_log, on_run, root)

        assert controller is not None


class TestCurrentVersionProperty:
    """Tests for current_version property."""

    def test_current_version_getter_default(self):
        """Test current_version getter returns None by default."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        assert controller.current_version is None

    def test_current_version_getter_after_set(self):
        """Test current_version getter returns set value."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.current_version = "1.2.3"
        assert controller.current_version == "1.2.3"

    def test_current_version_setter_with_string(self):
        """Test current_version setter accepts string values."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.current_version = "2.0.0"
        assert controller._current_version == "2.0.0"

    def test_current_version_setter_with_none(self):
        """Test current_version setter accepts None."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.current_version = "1.0.0"
        controller.current_version = None
        assert controller.current_version is None


class TestAvailableVersionProperty:
    """Tests for available_version property."""

    def test_available_version_getter_default(self):
        """Test available_version getter returns None by default."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        assert controller.available_version is None

    def test_available_version_getter_after_set(self):
        """Test available_version getter returns set value."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.available_version = "1.5.0"
        assert controller.available_version == "1.5.0"

    def test_available_version_setter_with_string(self):
        """Test available_version setter accepts string values."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.available_version = "3.0.0"
        assert controller._available_version == "3.0.0"

    def test_available_version_setter_with_none(self):
        """Test available_version setter accepts None."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.available_version = "1.0.0"
        controller.available_version = None
        assert controller.available_version is None


class TestStart:
    """Tests for start() method."""

    @patch('threading.Thread')
    def test_start_spawns_two_threads(self, mock_thread):
        """Test that start() spawns two daemon threads."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.start()

        assert mock_thread.call_count == 2

    @patch('threading.Thread')
    def test_start_probe_worker_thread(self, mock_thread):
        """Test that start() spawns version-probe thread."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.start()

        calls = mock_thread.call_args_list
        probe_call = calls[0]
        
        assert probe_call[1]['target'] == controller._probe_worker
        assert probe_call[1]['daemon'] is True
        assert probe_call[1]['name'] == "tokensave-version-probe"

    @patch('threading.Thread')
    def test_start_poll_loop_thread(self, mock_thread):
        """Test that start() spawns update-poll thread."""
        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.start()

        calls = mock_thread.call_args_list
        poll_call = calls[1]
        
        assert poll_call[1]['target'] == controller._poll_loop
        assert poll_call[1]['daemon'] is True
        assert poll_call[1]['name'] == "tokensave-update-poll"

    @patch('threading.Thread')
    def test_start_calls_thread_start(self, mock_thread):
        """Test that start() calls .start() on both threads."""
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.start()

        assert mock_instance.start.call_count == 2


class TestCmdUpgrade:
    """Tests for cmd_upgrade() method."""

    @patch('tkinter.messagebox.showwarning')
    def test_cmd_upgrade_no_tokensave_exe(self, mock_warning):
        """Test cmd_upgrade shows warning when tokensave_exe is not set."""
        cfg = Mock()
        cfg.tokensave_exe = None
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), Mock(), root)
        controller.cmd_upgrade()

        mock_warning.assert_called_once()
        assert "tokensave not found" in mock_warning.call_args[0]

    @patch('os.path.isfile')
    @patch('tkinter.messagebox.showwarning')
    def test_cmd_upgrade_tokensave_exe_not_found(self, mock_warning, mock_isfile):
        """Test cmd_upgrade shows warning when tokensave_exe file doesn't exist."""
        mock_isfile.return_value = False
        cfg = Mock()
        cfg.tokensave_exe = "/path/to/tokensave"
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), Mock(), root)
        controller.cmd_upgrade()

        mock_warning.assert_called_once()
        assert "tokensave not found" in mock_warning.call_args[0]

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_cmd_upgrade_user_declines(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test cmd_upgrade returns early if user declines upgrade."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = False
        mock_dirname.return_value = "/path/to"

        cfg = Mock()
        cfg.tokensave_exe = "/path/to/tokensave"
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), on_run, root)
        controller._available_version = "1.5.0"
        controller.cmd_upgrade()

        on_run.assert_not_called()

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_cmd_upgrade_user_accepts_with_target_version(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test cmd_upgrade runs when user accepts upgrade."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = True
        mock_dirname.return_value = "/path/to"

        cfg = Mock()
        cfg.tokensave_exe = "/path/to/tokensave"
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), on_run, root)
        controller._current_version = "1.0.0"
        controller._available_version = "1.5.0"
        controller.cmd_upgrade()

        on_run.assert_called_once_with(
            ["upgrade"],
            cwd="/path/to",
            label="upgrade"
        )
        assert controller.available_version is None

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_cmd_upgrade_clears_available_version(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test cmd_upgrade clears available_version on upgrade."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = True
        mock_dirname.return_value = "/dir"

        cfg = Mock()
        cfg.tokensave_exe = "/dir/tokensave"
        on_run = Mock()

        controller = UpdatePollerController(cfg, Mock(), on_run, Mock())
        controller._available_version = "2.0.0"
        controller.cmd_upgrade()

        assert controller.available_version is None

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_cmd_upgrade_message_with_versions(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test cmd_upgrade shows correct message when versions are known."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = False
        mock_dirname.return_value = "/dir"

        cfg = Mock()
        cfg.tokensave_exe = "/dir/tokensave"
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), Mock(), root)
        controller._current_version = "1.0.0"
        controller._available_version = "2.0.0"
        controller.cmd_upgrade()

        call_args = mock_askyesno.call_args
        message = call_args[0][1]
        assert "1.0.0" in message
        assert "2.0.0" in message

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_cmd_upgrade_message_without_target_version(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test cmd_upgrade shows correct message without target version."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = False
        mock_dirname.return_value = "/dir"

        cfg = Mock()
        cfg.tokensave_exe = "/dir/tokensave"
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), Mock(), root)
        controller._current_version = "1.0.0"
        controller._available_version = None
        controller.cmd_upgrade()

        call_args = mock_askyesno.call_args
        message = call_args[0][1]
        assert "1.0.0" in message


class TestCmdIntegrationCheck:
    """Tests for cmd_integration_check() method."""

    @patch('os.path.isfile')
    @patch('tkinter.messagebox.showinfo')
    def test_cmd_integration_check_script_not_found(self, mock_info, mock_isfile):
        """Test cmd_integration_check shows info when script is missing."""
        mock_isfile.return_value = False

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.cmd_integration_check()

        mock_info.assert_called_once()
        assert "Source-only tool" in mock_info.call_args[0]

    @patch('threading.Thread')
    @patch('os.path.isfile')
    def test_cmd_integration_check_spawns_thread(self, mock_isfile, mock_thread):
        """Test cmd_integration_check spawns a worker thread."""
        mock_isfile.return_value = True
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller.cmd_integration_check()

        mock_thread.assert_called_once()
        mock_instance.start.assert_called_once()

    @patch('threading.Thread')
    @patch('os.path.isfile')
    def test_cmd_integration_check_thread_config(self, mock_isfile, mock_thread):
        """Test cmd_integration_check thread is configured correctly."""
        mock_isfile.return_value = True
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        script_path = os.path.join(_BASE_DIR, "scripts", "check_tokensave_integration.py")
        controller.cmd_integration_check()

        call_kwargs = mock_thread.call_args[1]
        assert call_kwargs['target'] == controller._integration_check_worker
        assert call_kwargs['daemon'] is True
        assert call_kwargs['name'] == "tokensave-integration-check"
        assert call_kwargs['args'][0] == script_path


class TestRunIntegrationFix:
    """Tests for _run_integration_fix() method."""

    @patch('os.path.isfile')
    @patch('tkinter.messagebox.showinfo')
    def test_run_integration_fix_script_not_found(self, mock_info, mock_isfile):
        """Test _run_integration_fix shows info when script is missing."""
        mock_isfile.return_value = False

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._run_integration_fix()

        mock_info.assert_called_once()
        assert "Source-only tool" in mock_info.call_args[0]

    @patch('threading.Thread')
    @patch('os.path.isfile')
    def test_run_integration_fix_spawns_thread(self, mock_isfile, mock_thread):
        """Test _run_integration_fix spawns a worker thread."""
        mock_isfile.return_value = True
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._run_integration_fix()

        mock_thread.assert_called_once()
        mock_instance.start.assert_called_once()

    @patch('threading.Thread')
    @patch('os.path.isfile')
    def test_run_integration_fix_thread_config(self, mock_isfile, mock_thread):
        """Test _run_integration_fix thread is configured correctly."""
        mock_isfile.return_value = True
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        script_path = os.path.join(_BASE_DIR, "scripts", "check_tokensave_integration.py")
        controller._run_integration_fix()

        call_kwargs = mock_thread.call_args[1]
        assert call_kwargs['target'] == controller._integration_check_worker
        assert call_kwargs['daemon'] is True
        assert call_kwargs['name'] == "tokensave-integration-fix"
        assert call_kwargs['args'][0] == script_path
        assert call_kwargs['args'][1] is True


class TestIntegrationCheckWorker:
    """Tests for _integration_check_worker() method."""

    @patch('subprocess.run')
    def test_integration_check_worker_basic_run(self, mock_run):
        """Test _integration_check_worker runs subprocess without extra args."""
        mock_run.return_value = Mock(stdout="Success", returncode=0)

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._integration_check_worker("/path/to/script.py")

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0][0] == sys.executable
        assert call_args[0][0][1] == "/path/to/script.py"

    @patch('subprocess.run')
    def test_integration_check_worker_with_available_version(self, mock_run):
        """Test _integration_check_worker includes available version in args."""
        mock_run.return_value = Mock(stdout="Success", returncode=0)

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._available_version = "1.5.0"
        controller._integration_check_worker("/path/to/script.py")

        call_args = mock_run.call_args
        args = call_args[0][0]
        assert "--available" in args
        assert "1.5.0" in args

    @patch('subprocess.run')
    def test_integration_check_worker_with_fix_mode(self, mock_run):
        """Test _integration_check_worker includes --fix flag when fix_mode=True."""
        mock_run.return_value = Mock(stdout="Success", returncode=0)

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._integration_check_worker("/path/to/script.py", fix_mode=True)

        call_args = mock_run.call_args
        args = call_args[0][0]
        assert "--fix" in args

    @patch('subprocess.run')
    def test_integration_check_worker_subprocess_kwargs(self, mock_run):
        """Test _integration_check_worker calls subprocess.run with correct kwargs."""
        mock_run.return_value = Mock(stdout="Output", stderr="", returncode=0)

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._integration_check_worker("/path/to/script.py")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs['capture_output'] is True
        assert call_kwargs['text'] is True
        assert call_kwargs['timeout'] == 30
        assert call_kwargs['encoding'] == "utf-8"
        assert call_kwargs['errors'] == "replace"
        assert call_kwargs['cwd'] == _BASE_DIR
        assert call_kwargs['creationflags'] == CREATE_NO_WINDOW

    @patch('subprocess.run')
    def test_integration_check_worker_with_available_and_fix(self, mock_run):
        """Test _integration_check_worker with both available version and fix mode."""
        mock_run.return_value = Mock(stdout="Success", returncode=0)

        controller = UpdatePollerController(Mock(), Mock(), Mock(), Mock())
        controller._available_version = "2.0.0"
        controller._integration_check_worker("/path/to/script.py", fix_mode=True)

        call_args = mock_run.call_args
        args = call_args[0][0]
        assert "--available" in args
        assert "2.0.0" in args
        assert "--fix" in args


class TestReleasesAPI:
    """Tests for _RELEASES_API constant."""

    def test_releases_api_endpoint(self):
        """Test that _RELEASES_API points to correct GitHub endpoint."""
        expected = "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest"
        assert UpdatePollerController._RELEASES_API == expected


class TestIntegration:
    """Integration tests for UpdatePollerController."""

    def test_full_initialization_workflow(self):
        """Test complete initialization and property workflow."""
        cfg = Mock()
        cfg.tokensave_exe = "/path/to/tokensave"
        on_log = Mock()
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, on_log, on_run, root)
        controller.current_version = "1.0.0"
        controller.available_version = "1.5.0"

        assert controller.current_version == "1.0.0"
        assert controller.available_version == "1.5.0"

    @patch('threading.Thread')
    def test_start_and_version_update(self, mock_thread):
        """Test start() and subsequent version updates."""
        cfg = Mock()
        controller = UpdatePollerController(cfg, Mock(), Mock(), Mock())

        controller.start()
        assert mock_thread.call_count == 2

        controller.current_version = "1.0.0"
        controller.available_version = "2.0.0"
        assert controller.current_version == "1.0.0"
        assert controller.available_version == "2.0.0"

    @patch('os.path.dirname')
    @patch('os.path.isfile')
    @patch('tkinter.messagebox.askyesno')
    def test_upgrade_flow_full_path(self, mock_askyesno, mock_isfile, mock_dirname):
        """Test complete upgrade flow from button click to subprocess."""
        mock_isfile.return_value = True
        mock_askyesno.return_value = True
        mock_dirname.return_value = "/tokensave/dir"

        cfg = Mock()
        cfg.tokensave_exe = "/tokensave/dir/tokensave.exe"
        on_run = Mock()
        root = Mock()

        controller = UpdatePollerController(cfg, Mock(), on_run, root)
        controller.current_version = "1.0.0"
        controller.available_version = "2.0.0"
        
        controller.cmd_upgrade()

        on_run.assert_called_once()
        assert controller.available_version is None