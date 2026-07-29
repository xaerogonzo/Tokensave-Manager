"""tests/test_dialog_codegraph_daemon_manager.py — daemon picker (Tk-marked).

Modelled on test_dialog_tokensave_mcp_picker.py. `refresh()` is patched away
during construction (it always fires from __init__) so each test controls
exactly when the listing "arrives" via _on_refresh_done.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.codegraph_daemon_manager import CodegraphDaemonManagerDialog

pytestmark = pytest.mark.tk

_SAMPLE = [
    {"pid": 111, "version": "1.5.0", "uptime": "5m 7s",
     "path": r"D:\Random Projects\OpenChem Studio"},
    {"pid": 222, "version": "1.5.0", "uptime": "5m 14s",
     "path": r"D:\Claude Co worker\Token Save Manager Source"},
]


def _dialog_no_autorefresh(tk_root, mock_config, mocker):
    """Construct with refresh() no-op'd so tests control listing arrival."""
    mocker.patch.object(CodegraphDaemonManagerDialog, "refresh")
    mock_config.raw["codegraph_exe"] = "codegraph.cmd"
    return CodegraphDaemonManagerDialog(tk_root, mock_config)


# ── Rendering ──────────────────────────────────────────────────────────────

def test_renders_one_row_per_daemon(tk_root, mock_config, mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._daemons = _SAMPLE
    dialog._populate_list(_SAMPLE)
    assert set(dialog._row_widgets.keys()) == {111, 222}


def test_empty_list_shows_empty_state_not_crash(tk_root, mock_config, mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._populate_list([])
    assert dialog._row_widgets == {}


def test_on_refresh_done_updates_state(tk_root, mock_config, mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._on_refresh_done(_SAMPLE)
    assert dialog._daemons == _SAMPLE
    assert len(dialog._row_widgets) == 2


# ── Stop flow ──────────────────────────────────────────────────────────────

def test_stop_confirms_before_killing(tk_root, mock_config, mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._on_refresh_done(_SAMPLE)
    kill = mocker.patch(
        "dialogs.codegraph_daemon_manager.kill_codegraph_daemon")
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=False)
    dialog._on_stop(111)
    kill.assert_not_called()


def test_stop_declined_leaves_daemon_untouched(tk_root, mock_config, mocker):
    """Same assertion, phrased as the user-facing guarantee."""
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._on_refresh_done(_SAMPLE)
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=False)
    kill = mocker.patch(
        "dialogs.codegraph_daemon_manager.kill_codegraph_daemon")
    dialog._on_stop(222)
    assert 222 not in dialog._busy_pids
    kill.assert_not_called()


def test_stop_confirmed_calls_kill_with_correct_pid(tk_root, mock_config,
                                                    mocker, patch_after,
                                                    wait_for):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    harness = patch_after(dialog)
    dialog._on_refresh_done(_SAMPLE)
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=True)
    kill = mocker.patch(
        "dialogs.codegraph_daemon_manager.kill_codegraph_daemon",
        return_value=(True, "terminated"))
    mocker.patch.object(dialog, "refresh")   # avoid a real re-list afterward
    dialog._on_stop(111)
    wait_for(lambda: kill.called, timeout_s=5)
    harness.drain()
    assert kill.call_args.args[0] == 111


def test_stop_failure_shows_error_and_re_enables_button(tk_root, mock_config,
                                                        mocker, patch_after,
                                                        wait_for):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    harness = patch_after(dialog)
    dialog._on_refresh_done(_SAMPLE)
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=True)
    kill = mocker.patch(
        "dialogs.codegraph_daemon_manager.kill_codegraph_daemon",
        return_value=(False, "Access is denied"))
    err = mocker.patch(
        "dialogs.codegraph_daemon_manager.messagebox.showerror")
    dialog._on_stop(111)
    # wait_for drives the real Tk event loop; patch_after reroutes self.after
    # away from Tk entirely, so an effect that only happens INSIDE the
    # harnessed callback (like err.called) can never be observed by polling
    # here — wait on the real background thread's own call first, then
    # drain the harness synchronously to run the queued callback.
    wait_for(lambda: kill.called, timeout_s=5)
    harness.drain()
    assert err.called
    assert 111 not in dialog._busy_pids
    # ttk widgets return a Tcl index object from cget("state"), not a plain
    # str — compare via str() rather than against the tk.NORMAL constant.
    assert str(dialog._row_widgets[111]["stop_btn"].cget("state")) == "normal"


# ── Unlock (advanced fallback) ────────────────────────────────────────────

def test_unlock_empty_path_shows_info_no_subprocess(tk_root, mock_config,
                                                    mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._unlock_path_var.set("")
    info = mocker.patch(
        "dialogs.codegraph_daemon_manager.messagebox.showinfo")
    unlock = mocker.patch(
        "dialogs.codegraph_daemon_manager.unlock_codegraph_project")
    dialog._on_unlock()
    info.assert_called_once()
    unlock.assert_not_called()


def test_unlock_declined_confirmation_runs_nothing(tk_root, mock_config,
                                                   mocker):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    dialog._unlock_path_var.set(r"D:\some\project")
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=False)
    unlock = mocker.patch(
        "dialogs.codegraph_daemon_manager.unlock_codegraph_project")
    dialog._on_unlock()
    unlock.assert_not_called()


def test_unlock_confirmed_calls_helper_with_path(tk_root, mock_config,
                                                 mocker, patch_after,
                                                 wait_for):
    dialog = _dialog_no_autorefresh(tk_root, mock_config, mocker)
    harness = patch_after(dialog)
    dialog._unlock_path_var.set(r"D:\some\project")
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.askyesno",
                return_value=True)
    unlock = mocker.patch(
        "dialogs.codegraph_daemon_manager.unlock_codegraph_project",
        return_value=(True, "Lock removed."))
    mocker.patch("dialogs.codegraph_daemon_manager.messagebox.showinfo")
    dialog._on_unlock()
    wait_for(lambda: unlock.called, timeout_s=5)
    harness.drain()
    assert unlock.call_args.args[1] == r"D:\some\project"
