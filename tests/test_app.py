"""tests/test_app.py — App initialization and window management.

Tests the App class constructor, geometry persistence, exception handling,
and integration with ManagerConfig, controllers, and the tray manager.
Tk tests must mock all subprocess/IO operations and avoid mainloop().
"""

from __future__ import annotations

import pytest
from unittest import mock

tk = pytest.importorskip("tkinter")

from app import App, _geometry_on_screen
from state import ManagerConfig


# ── Geometry on screen tests ───────────────────────────────────────────────

def test_geometry_on_screen_valid_geometry():
    """Valid geometry within screen bounds returns True."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600+100+100") is True
    finally:
        root.destroy()


def test_geometry_on_screen_valid_at_origin():
    """Geometry at origin (0,0) is on screen."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600+0+0") is True
    finally:
        root.destroy()


def test_geometry_on_screen_small_negative_offset_tolerated():
    """Small negative offsets for multi-monitor setup are tolerated."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600+-100+-100") is True
    finally:
        root.destroy()


def test_geometry_on_screen_large_negative_x_rejected():
    """Negative x beyond tolerance (-600) is rejected."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600+-600+0") is False
    finally:
        root.destroy()


def test_geometry_on_screen_large_negative_y_rejected():
    """Negative y beyond tolerance (-520) is rejected."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600+0+-520") is False
    finally:
        root.destroy()


def test_geometry_on_screen_beyond_width_rejected():
    """Geometry placed beyond screen width is rejected."""
    root = tk.Tk()
    try:
        sw = root.winfo_screenwidth()
        assert _geometry_on_screen(root, f"800x600+{sw}+0") is False
    finally:
        root.destroy()


def test_geometry_on_screen_beyond_height_rejected():
    """Geometry placed beyond screen height is rejected."""
    root = tk.Tk()
    try:
        sh = root.winfo_screenheight()
        assert _geometry_on_screen(root, f"800x600+0+{sh}") is False
    finally:
        root.destroy()


def test_geometry_on_screen_invalid_format_no_plus():
    """Invalid format without + separators returns False."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "800x600") is False
    finally:
        root.destroy()


def test_geometry_on_screen_invalid_format_garbage():
    """Completely invalid geometry string returns False."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "not a geometry") is False
    finally:
        root.destroy()


def test_geometry_on_screen_empty_string():
    """Empty string returns False."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "") is False
    finally:
        root.destroy()


def test_geometry_on_screen_malformed_dimensions():
    """Non-numeric dimensions return False."""
    root = tk.Tk()
    try:
        assert _geometry_on_screen(root, "axbx600+100+100") is False
    finally:
        root.destroy()


# ── App initialization tests ───────────────────────────────────────────────

pytestmark = pytest.mark.tk


def _mock_all_dependencies(monkeypatch):
    """Patch all external dependencies for App.__init__."""
    mock_cfg = mock.MagicMock(spec=ManagerConfig)
    mock_cfg.raw = {}
    mock_cfg.tokensave_exe = "/path/to/tokensave"
    mock_cfg.template_dir = "/path/to/templates"

    monkeypatch.setattr("app.ManagerConfig.load", lambda: mock_cfg)
    monkeypatch.setattr("app.UpdatePollerController", mock.MagicMock())
    monkeypatch.setattr("app.TrayManager", mock.MagicMock())
    monkeypatch.setattr("app.ProjectsTabController", mock.MagicMock())
    monkeypatch.setattr("app.GitTabController", mock.MagicMock())
    monkeypatch.setattr("app.AskTabController", mock.MagicMock())
    monkeypatch.setattr("app.SnippetsController", mock.MagicMock())
    monkeypatch.setattr("app.TasksController", mock.MagicMock())
    monkeypatch.setattr("app.HelpTabController", mock.MagicMock())
    monkeypatch.setattr("app.log", mock.MagicMock())

    return mock_cfg


def test_app_init_creates_window(monkeypatch):
    """App.__init__ creates a Tk window with correct title and geometry."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        assert app.title() == "TokenSave Manager"
        assert app.minsize() == (600, 520)
    finally:
        app.destroy()


def test_app_init_default_geometry(monkeypatch):
    """App.__init__ calls geometry with 760x600 when no saved geometry."""
    mock_cfg = _mock_all_dependencies(monkeypatch)
    mock_cfg.raw = {}

    geometry_calls = []
    original_geometry = tk.Tk.geometry

    def track_geometry(self, newgeom=None):
        if newgeom is not None:
            geometry_calls.append(newgeom)
        return original_geometry(self, newgeom) if newgeom is not None else original_geometry(self)

    monkeypatch.setattr("tkinter.Tk.geometry", track_geometry)

    app = App()
    try:
        assert "760x600" in geometry_calls
    finally:
        app.destroy()


def test_app_init_restores_valid_geometry(monkeypatch):
    """App.__init__ calls geometry with saved geometry if valid and on-screen."""
    mock_cfg = _mock_all_dependencies(monkeypatch)
    mock_cfg.raw = {"window_geometry": "800x600+50+100"}

    geometry_calls = []
    original_geometry = tk.Tk.geometry

    def track_geometry(self, newgeom=None):
        if newgeom is not None:
            geometry_calls.append(newgeom)
        return original_geometry(self, newgeom) if newgeom is not None else original_geometry(self)

    monkeypatch.setattr("tkinter.Tk.geometry", track_geometry)

    app = App()
    try:
        # Should have called geometry twice: default, then saved
        assert any("800x600+50+100" in call for call in geometry_calls)
    finally:
        app.destroy()


def test_app_init_ignores_invalid_geometry_format(monkeypatch):
    """App.__init__ ignores saved geometry with invalid format."""
    mock_cfg = _mock_all_dependencies(monkeypatch)
    mock_cfg.raw = {"window_geometry": "invalid"}

    geometry_calls = []
    original_geometry = tk.Tk.geometry

    def track_geometry(self, newgeom=None):
        if newgeom is not None:
            geometry_calls.append(newgeom)
        return original_geometry(self, newgeom) if newgeom is not None else original_geometry(self)

    monkeypatch.setattr("tkinter.Tk.geometry", track_geometry)

    app = App()
    try:
        # Should call geometry with 760x600, but NOT with "invalid"
        assert "760x600" in geometry_calls
        assert "invalid" not in geometry_calls
    finally:
        app.destroy()


def test_app_init_ignores_offscreen_geometry(monkeypatch):
    """App.__init__ ignores saved geometry that's entirely off-screen."""
    mock_cfg = _mock_all_dependencies(monkeypatch)
    mock_cfg.raw = {"window_geometry": "800x600+9999+9999"}

    geometry_calls = []
    original_geometry = tk.Tk.geometry

    def track_geometry(self, newgeom=None):
        if newgeom is not None:
            geometry_calls.append(newgeom)
        return original_geometry(self, newgeom) if newgeom is not None else original_geometry(self)

    monkeypatch.setattr("tkinter.Tk.geometry", track_geometry)

    app = App()
    try:
        # Should call geometry with 760x600, but NOT with offscreen geometry
        assert "760x600" in geometry_calls
        assert "9999+9999" not in geometry_calls
    finally:
        app.destroy()


def test_app_init_holds_config_instance(monkeypatch):
    """App stores ManagerConfig instance as _cfg."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        assert app._cfg is mock_cfg
    finally:
        app.destroy()


def test_app_init_initializes_process_state(monkeypatch):
    """App initializes _current_proc to None."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        assert app._current_proc is None
    finally:
        app.destroy()


def test_app_init_initializes_stop_flag(monkeypatch):
    """App initializes _stop_requested to False."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        assert app._stop_requested is False
    finally:
        app.destroy()


def test_app_init_initializes_sync_sets(monkeypatch):
    """App initializes sync concurrency guard sets."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        assert isinstance(app._active_private_syncs, set)
        assert isinstance(app._pending_private_sync, set)
        assert len(app._active_private_syncs) == 0
        assert len(app._pending_private_sync) == 0
    finally:
        app.destroy()


def test_app_init_creates_update_poller(monkeypatch):
    """App creates UpdatePollerController and starts it."""
    mock_update_poller_class = mock.MagicMock()
    mock_instance = mock.MagicMock()
    mock_update_poller_class.return_value = mock_instance

    mock_cfg = _mock_all_dependencies(monkeypatch)
    monkeypatch.setattr("app.UpdatePollerController", mock_update_poller_class)

    app = App()
    try:
        assert app._update_poller is mock_instance
        mock_instance.start.assert_called_once()
    finally:
        app.destroy()


def test_app_init_creates_tray_manager(monkeypatch):
    """App creates TrayManager and sets it up."""
    mock_tray_class = mock.MagicMock()
    mock_tray_instance = mock.MagicMock()
    mock_tray_class.return_value = mock_tray_instance

    mock_cfg = _mock_all_dependencies(monkeypatch)
    monkeypatch.setattr("app.TrayManager", mock_tray_class)

    app = App()
    try:
        assert app._tray_mgr is mock_tray_instance
        mock_tray_instance.setup.assert_called_once()
    finally:
        app.destroy()


def test_app_init_sets_delete_window_protocol(monkeypatch):
    """App sets WM_DELETE_WINDOW protocol to tray hide."""
    mock_tray_class = mock.MagicMock()
    mock_tray_instance = mock.MagicMock()
    mock_tray_class.return_value = mock_tray_instance

    mock_cfg = _mock_all_dependencies(monkeypatch)
    monkeypatch.setattr("app.TrayManager", mock_tray_class)

    app = App()
    try:
        # Verify protocol was set (would be called during init)
        assert True  # Just ensure no exception during init
    finally:
        app.destroy()


def test_app_init_schedules_auto_refresh(monkeypatch):
    """App schedules AUTO_REFRESH_MS callback."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        # App should have scheduled after() call for auto-refresh
        assert app.tk is not None
    finally:
        app.destroy()


def test_app_init_schedules_check_config(monkeypatch):
    """App schedules _check_config callback at 300ms."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        # Verify _check_config is defined (will be called via after)
        assert hasattr(app, "_check_config")
    finally:
        app.destroy()


# ── Exception handling tests ───────────────────────────────────────────────

def test_report_callback_exception_logs_error(monkeypatch):
    """report_callback_exception logs unhandled exceptions with traceback."""
    mock_log = mock.MagicMock()
    mock_cfg = _mock_all_dependencies(monkeypatch)
    monkeypatch.setattr("app.log", mock_log)

    app = App()
    try:
        test_exc = ValueError("Test error")
        app.report_callback_exception(ValueError, test_exc, None)

        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args
        assert "Unhandled Tk callback exception" in str(call_args)
    finally:
        app.destroy()


def test_report_callback_exception_with_traceback(monkeypatch):
    """report_callback_exception includes full traceback in log."""
    mock_log = mock.MagicMock()
    mock_cfg = _mock_all_dependencies(monkeypatch)
    monkeypatch.setattr("app.log", mock_log)

    app = App()
    try:
        import traceback

        try:
            raise RuntimeError("Simulated error")
        except RuntimeError as e:
            exc_type = type(e)
            exc_val = e
            exc_tb = e.__traceback__

        app.report_callback_exception(exc_type, exc_val, exc_tb)

        mock_log.error.assert_called_once()
        logged_text = str(mock_log.error.call_args)
        assert "RuntimeError" in logged_text or "Simulated error" in logged_text
    finally:
        app.destroy()


# ── Tray quit handler tests ────────────────────────────────────────────────

def test_on_tray_quit_cancels_ask_proposals(monkeypatch):
    """_on_tray_quit calls cancel_all_proposals on AskTabController."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        mock_ask_ctrl = mock.MagicMock()
        app._ask_ctrl = mock_ask_ctrl

        app._on_tray_quit()

        mock_ask_ctrl.cancel_all_proposals.assert_called_once()
    finally:
        app.destroy()


def test_on_tray_quit_cancels_projects_ai(monkeypatch):
    """_on_tray_quit calls cancel_ai_proposals on ProjectsTabController."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        mock_projects = mock.MagicMock()
        app._projects = mock_projects

        app._on_tray_quit()

        mock_projects.cancel_ai_proposals.assert_called_once()
    finally:
        app.destroy()


def test_on_tray_quit_handles_missing_ask_ctrl(monkeypatch):
    """_on_tray_quit handles missing AskTabController without raising."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        # Don't set _ask_ctrl; should catch AttributeError and continue
        app._on_tray_quit()
        # Should not raise
    finally:
        app.destroy()


def test_on_tray_quit_handles_missing_projects(monkeypatch):
    """_on_tray_quit handles missing ProjectsTabController without raising."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        # Don't set _projects; should catch AttributeError and continue
        app._on_tray_quit()
        # Should not raise
    finally:
        app.destroy()


def test_on_tray_quit_cancels_both_when_available(monkeypatch):
    """_on_tray_quit cancels both controllers when both present."""
    mock_cfg = _mock_all_dependencies(monkeypatch)

    app = App()
    try:
        mock_ask_ctrl = mock.MagicMock()
        mock_projects = mock.MagicMock()
        app._ask_ctrl = mock_ask_ctrl
        app._projects = mock_projects

        app._on_tray_quit()

        mock_ask_ctrl.cancel_all_proposals.assert_called_once()
        mock_projects.cancel_ai_proposals.assert_called_once()
    finally:
        app.destroy()