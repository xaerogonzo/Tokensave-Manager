"""tests/test_dialog_shadow_links.py — ShadowLinksDialog + controller gate (Tk).

R9-SL1: the dialog loads a previously saved per-project map (falling back
to DEFAULT_SHADOW_EXT_MAP) and re-saves the parsed map on Apply.
R9-SL4: ShadowLinksController.cmd_shadow_links refuses to open the dialog
on volumes without hardlink support.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.shadow_links import ShadowLinksDialog
from controllers.shadowlinks_ctrl import ShadowLinksController
from helpers.shadow_links import save_shadow_map

pytestmark = pytest.mark.tk


# ── Dialog: SL1 persistence ──────────────────────────────────────────────

def test_dialog_defaults_when_no_saved_map(tk_root, tmp_path, mocker):
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    content = dialog._map_text.get("1.0", tk.END)
    assert ".zsc = .cpp" in content
    assert "DECORATE = .cpp" in content
    dialog.destroy()


def test_dialog_loads_saved_map_over_default(tk_root, tmp_path, mocker):
    save_shadow_map(str(tmp_path), {".gd": ".py"})
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    content = dialog._map_text.get("1.0", tk.END)
    assert ".gd = .py" in content
    assert ".zsc" not in content           # default fully replaced
    dialog.destroy()


def test_apply_saves_edited_map_and_calls_back(tk_root, tmp_path, mocker):
    from helpers.shadow_links import load_shadow_map
    callback = mocker.MagicMock()
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), callback)
    dialog._map_text.delete("1.0", tk.END)
    dialog._map_text.insert("1.0", ".uc = .cpp\n# comment\n.qc = .c\n")
    dialog._apply()
    expected = {".uc": ".cpp", ".qc": ".c"}
    assert load_shadow_map(str(tmp_path)) == expected
    callback.assert_called_once()
    assert callback.call_args[0][1] == expected


def test_apply_with_no_mappings_warns_and_keeps_dialog(tk_root, tmp_path,
                                                       mocker):
    callback = mocker.MagicMock()
    mock_warn = mocker.patch("dialogs.shadow_links.messagebox.showwarning")
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), callback)
    dialog._map_text.delete("1.0", tk.END)
    dialog._apply()
    mock_warn.assert_called_once()
    callback.assert_not_called()
    assert dialog.winfo_exists()
    dialog.destroy()


# ── Controller: SL4 preflight gate ───────────────────────────────────────

def _ctl_stub(tk_root):
    return SimpleNamespace(_root=tk_root, _do_shadow_links=lambda *a: None)


def test_cmd_refuses_without_hardlink_support(tk_root, mocker):
    mocker.patch("controllers.shadowlinks_ctrl.supports_hardlinks",
                 return_value=False)
    mock_dialog = mocker.patch("controllers.shadowlinks_ctrl.ShadowLinksDialog")
    mock_err = mocker.patch("controllers.shadowlinks_ctrl.messagebox.showerror")
    ShadowLinksController.cmd_shadow_links(_ctl_stub(tk_root), "X:/proj")
    mock_err.assert_called_once()
    assert "NTFS" in mock_err.call_args[0][1]
    mock_dialog.assert_not_called()


def test_cmd_opens_dialog_when_supported(tk_root, mocker):
    mocker.patch("controllers.shadowlinks_ctrl.supports_hardlinks",
                 return_value=True)
    mock_dialog = mocker.patch("controllers.shadowlinks_ctrl.ShadowLinksDialog")
    mock_err = mocker.patch("controllers.shadowlinks_ctrl.messagebox.showerror")
    stub = _ctl_stub(tk_root)
    ShadowLinksController.cmd_shadow_links(stub, "C:/proj")
    mock_dialog.assert_called_once()
    mock_err.assert_not_called()
