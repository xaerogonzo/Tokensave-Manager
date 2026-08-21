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
from tkinter import ttk

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
    # Saved entry is preserved; new defaults (e.g. .zsc) are merged in
    # for keys not already in the saved map.
    save_shadow_map(str(tmp_path), {".gd": ".py"})
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    content = dialog._map_text.get("1.0", tk.END)
    assert ".gd = .py" in content          # saved entry kept
    assert ".zsc = .cpp" in content        # default merged in (wasn't in saved map)
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
    return SimpleNamespace(_root=tk_root, _do_shadow_links=lambda *a: None,
                           _cfg=None)


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


# ── Dialog: SL5 scanner ──────────────────────────────────────────────────

def _cfg_with_llm(provider="ollama"):
    return SimpleNamespace(raw={"ask_tab_llm": {"provider": provider}},
                           claude_cli_exe="")


def test_scan_renders_candidate_rows(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions",
                 return_value={".py"})
    mocker.patch("dialogs.shadow_links.suggest_shadow_candidates",
                 return_value=[(".gd", 12), (".uc", 3)])
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._on_scan()
    assert [ext for _c, ext, _s in dialog._candidate_rows] == [".gd", ".uc"]
    assert str(dialog._add_checked_btn.cget("state")) == "normal"
    dialog.destroy()


def test_scan_no_index_shows_sync_hint(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions", return_value=None)
    sugg = mocker.patch("dialogs.shadow_links.suggest_shadow_candidates")
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._on_scan()
    assert "sync" in dialog._scan_status.cget("text").lower()
    assert dialog._candidate_rows == []
    sugg.assert_not_called()          # short-circuits before the walk
    dialog.destroy()


def test_scan_nothing_unindexed(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions",
                 return_value={".py"})
    mocker.patch("dialogs.shadow_links.suggest_shadow_candidates",
                 return_value=[])
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._on_scan()
    assert "No unindexed" in dialog._scan_status.cget("text")
    assert str(dialog._add_checked_btn.cget("state")) == "disabled"
    dialog.destroy()


def test_add_checked_appends_lines(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions",
                 return_value={".py"})
    mocker.patch("dialogs.shadow_links.suggest_shadow_candidates",
                 return_value=[(".gd", 12), (".uc", 3)])
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._on_scan()
    # Uncheck the second row; change first suffix.
    dialog._candidate_rows[1][0].set(False)
    dialog._candidate_rows[0][2].set(".py")
    dialog._on_add_checked()
    content = dialog._map_text.get("1.0", tk.END)
    assert ".gd = .py" in content
    assert ".uc" not in content          # was unchecked
    dialog.destroy()


def test_add_checked_skips_already_mapped(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions",
                 return_value={".py"})
    mocker.patch("dialogs.shadow_links.suggest_shadow_candidates",
                 return_value=[(".gd", 12)])
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._on_scan()
    dialog._candidate_rows[0][2].set(".cpp")
    dialog._on_add_checked()
    dialog._on_add_checked()             # second add is a no-op
    content = dialog._map_text.get("1.0", tk.END)
    assert content.count(".gd = .cpp") == 1
    dialog.destroy()


def test_ai_button_absent_without_cfg(tk_root, tmp_path, mocker):
    """No cfg → no AI button rendered (only the controller passes cfg)."""
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    labels = []
    def _walk(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Button):
                labels.append(str(c.cget("text")))
            _walk(c)
    _walk(dialog)
    assert any("Scan" in t for t in labels)
    assert not any("AI" in t for t in labels)
    dialog.destroy()


def test_ai_suggest_updates_row_suffixes(tk_root, tmp_path, mocker):
    mocker.patch("dialogs.shadow_links.indexed_extensions",
                 return_value={".py"})
    mocker.patch("dialogs.shadow_links.suggest_shadow_candidates",
                 return_value=[(".gd", 12), (".uc", 3)])
    mocker.patch("dialogs.shadow_links.ai_suggest_suffixes",
                 return_value={".gd": ".py"})
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock(),
                               cfg=_cfg_with_llm())
    dialog._on_scan()
    dialog._on_ai_suggest()
    suffixes = {ext: suf.get() for _c, ext, suf in dialog._candidate_rows}
    assert suffixes[".gd"] == ".py"      # AI refined
    assert suffixes[".uc"] == ".cpp"     # unchanged prefill
    dialog.destroy()


# ── Dialog: SL2 auto-refresh flag + SL3 status ───────────────────────────

def test_auto_shadow_checkbox_defaults_off(tk_root, tmp_path, mocker):
    """Opt-in: it adds a tree walk to every sync of this project."""
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    assert dialog._var_auto.get() is False
    dialog.destroy()


def test_auto_shadow_checkbox_reflects_the_saved_flag(tk_root, tmp_path,
                                                      mocker):
    save_shadow_map(str(tmp_path), {".zsc": ".cpp"}, auto_shadow=True)
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    assert dialog._var_auto.get() is True
    dialog.destroy()


def test_apply_persists_the_auto_shadow_flag(tk_root, tmp_path, mocker):
    from helpers.shadow_links import load_shadow_config
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    dialog._var_auto.set(True)
    dialog._apply()
    assert load_shadow_config(str(tmp_path)).auto_shadow is True


def test_cleanup_button_is_hidden_when_nothing_is_stale(tk_root, tmp_path,
                                                        mocker):
    """A cleanup action with nothing to clean invites a pointless click."""
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    assert dialog._stale_count == 0
    assert not dialog._cleanup_btn.winfo_ismapped()
    dialog.destroy()


def test_cleanup_button_appears_only_for_provable_stale_shadows(
        tk_root, tmp_path, mocker):
    """An unprovable lookalike must not summon the cleanup button.

    If it did, the button's count would promise to remove a file the manager
    has no evidence it created.
    """
    import os
    from helpers.shadow_links import refresh_shadows, supports_hardlinks
    if not supports_hardlinks(str(tmp_path)):
        pytest.skip("temp volume does not support hardlinks")

    (tmp_path / "Blood.zsc").write_text("x", encoding="utf-8")
    refresh_shadows(str(tmp_path), {".zsc": ".cpp"})
    (tmp_path / "Handmade.zsc.cpp").write_text("mine", encoding="utf-8")

    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    assert dialog._stale_count == 0, "nothing stale yet — source still present"
    dialog.destroy()

    os.remove(tmp_path / "Blood.zsc")      # now the recorded one IS stale
    dialog = ShadowLinksDialog(tk_root, str(tmp_path), mocker.MagicMock())
    assert dialog._stale_count == 1, "the handmade file must not be counted"
    dialog.destroy()
