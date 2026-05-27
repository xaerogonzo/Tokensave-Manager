"""tests/test_dialog_codegraph_mcp_picker.py — picker dialog (Tk-marked).

Verifies the v4.7 CodegraphMCPPickerDialog: agent detection, target argv
construction (``codegraph install --target=<csv> --yes``), and the
``--no-permissions`` flag passthrough.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.codegraph_mcp_picker import CodegraphMCPPickerDialog

pytestmark = pytest.mark.tk


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── Construction + initial state ─────────────────────────────────────────

def test_dialog_constructs_with_all_agents(tk_root, mock_config, mocker):
    """The picker should expose all 4 known codegraph install targets."""
    # All agents "installed" so the checkbox enablement check passes.
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    assert set(dialog._agent_vars.keys()) == {"claude", "cursor", "codex", "opencode"}


def test_dialog_defaults_to_detected_agents_checked(tk_root, mock_config, mocker):
    """Only agents the user actually has installed should be pre-checked."""
    def _installed(agent_id):
        return agent_id == "claude"   # only Claude detected

    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 side_effect=_installed)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    assert dialog._agent_vars["claude"].get() is True
    assert dialog._agent_vars["cursor"].get() is False
    assert dialog._agent_vars["codex"].get() is False
    assert dialog._agent_vars["opencode"].get() is False


# ── _on_install argv construction ────────────────────────────────────────

def test_install_refuses_without_selection(tk_root, mock_config, mocker, tmp_path):
    """No agents checked → showinfo, no subprocess."""
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=False)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_info = mocker.patch("dialogs.codegraph_mcp_picker.messagebox.showinfo")
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run")

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    # All checkboxes unchecked.
    for var in dialog._agent_vars.values():
        var.set(False)
    dialog._on_install()
    mock_info.assert_called_once()
    mock_run.assert_not_called()


def test_install_refuses_without_codegraph_exe(tk_root, mock_config, mocker):
    """codegraph_exe not configured → showerror, no subprocess."""
    mock_config.raw["codegraph_exe"] = ""
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_err = mocker.patch("dialogs.codegraph_mcp_picker.messagebox.showerror")
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run")

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    dialog._agent_vars["claude"].set(True)
    dialog._on_install()

    mock_err.assert_called_once()
    mock_run.assert_not_called()


def test_install_builds_target_csv_argv(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """Selected agents must appear as a comma-separated list in --target=..."""
    cg = tmp_path / "codegraph.cmd"
    cg.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run",
                            return_value=_proc(rc=0, stdout="done"))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    patch_after(dialog)
    # Select claude + cursor only.
    dialog._agent_vars["claude"].set(True)
    dialog._agent_vars["cursor"].set(True)
    dialog._agent_vars["codex"].set(False)
    dialog._agent_vars["opencode"].set(False)
    dialog._on_install()

    wait_for(lambda: mock_run.called, timeout_s=3.0)
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == str(cg)
    assert cmd[1] == "install"
    # --target=claude,cursor — order matters because we iterate the dict.
    target_arg = next(a for a in cmd if a.startswith("--target="))
    targets = target_arg.split("=", 1)[1].split(",")
    assert set(targets) == {"claude", "cursor"}
    assert "--yes" in cmd


def test_install_includes_no_permissions_flag_when_checked(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    """The Advanced checkbox toggles --no-permissions in the argv."""
    cg = tmp_path / "codegraph.cmd"
    cg.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run",
                            return_value=_proc(rc=0))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._agent_vars["claude"].set(True)
    dialog._no_perms_var.set(True)
    dialog._on_install()

    wait_for(lambda: mock_run.called, timeout_s=3.0)
    cmd = mock_run.call_args[0][0]
    assert "--no-permissions" in cmd


def test_install_omits_no_permissions_when_unchecked(
    tk_root, mock_config, mocker, patch_after, wait_for, tmp_path
):
    cg = tmp_path / "codegraph.cmd"
    cg.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run",
                            return_value=_proc(rc=0))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    patch_after(dialog)
    dialog._agent_vars["claude"].set(True)
    dialog._no_perms_var.set(False)
    dialog._on_install()

    wait_for(lambda: mock_run.called, timeout_s=3.0)
    cmd = mock_run.call_args[0][0]
    assert "--no-permissions" not in cmd


def test_install_in_flight_guard_blocks_double_click(
    tk_root, mock_config, mocker, tmp_path
):
    """If a previous install is already running, ignore the click silently."""
    cg = tmp_path / "codegraph.cmd"
    cg.write_bytes(b"")
    mock_config.raw["codegraph_exe"] = str(cg)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_installed",
                 return_value=True)
    mocker.patch("dialogs.codegraph_mcp_picker._codegraph_agent_destination_path",
                 return_value="/fake/path")
    mocker.patch("dialogs.codegraph_mcp_picker._claude_code_mcp_has_codegraph",
                 return_value=(False, ""))
    mock_run = mocker.patch("dialogs.codegraph_mcp_picker.subprocess.run",
                            return_value=_proc(rc=0))

    dialog = CodegraphMCPPickerDialog(tk_root, mock_config)
    dialog._agent_vars["claude"].set(True)
    dialog._in_flight = True   # simulate a previous install still running

    dialog._on_install()
    mock_run.assert_not_called()   # second click is a silent no-op
