"""tests/test_dialog_doc_drafter.py — DocDrafterDialog (Tk-marked).

Safety net for the Roadmap-8 oversize split: construction, tab registry,
placeholder, and teardown behavior. Written against the pre-split dialog;
must pass unchanged after _BackendResolver/_DraftTicker/build_tab move to
dialogs/doc_drafter_support.py.

Mocking: _refresh_range (git subprocess via dd.resolve_commit_range),
_maybe_warmup_ollama (network), and _list_picker_files (home-dir globs)
are stubbed on the class so construction touches neither git nor network.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.doc_drafter import DocDrafterDialog

pytestmark = pytest.mark.tk


def _build_dialog(tk_root, mock_config, tmp_path, mocker):
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n",
                                           encoding="utf-8")
    (tmp_path / "README.md").write_text("# Proj\n\n## Highlights\n",
                                        encoding="utf-8")
    mocker.patch.object(DocDrafterDialog, "_refresh_range")
    mocker.patch.object(DocDrafterDialog, "_maybe_warmup_ollama")
    mocker.patch.object(DocDrafterDialog, "_list_picker_files",
                        return_value=[])
    return DocDrafterDialog(tk_root, str(tmp_path), mock_config)


def test_constructs_without_error(tk_root, mock_config, tmp_path, mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    assert "Draft Doc Updates" in dialog.title()
    dialog.destroy()


def test_visible_tabs_match_registry_subset(tk_root, mock_config, tmp_path,
                                            mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    assert dialog._visible_tab_keys, "no tabs rendered"
    assert len(dialog._notebook.tabs()) == len(dialog._visible_tab_keys)
    assert "changelog" in dialog._visible_tab_keys
    dialog.destroy()


def test_tab_widget_registry_complete(tk_root, mock_config, tmp_path, mocker):
    """Every visible tab registers the full widget dict _on_generate_done
    and friends rely on."""
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    expected = {"frame", "text", "txt_wrap", "gen_btn", "apply_btn",
                "feedback_btn", "ts_tools_var", "btn_row", "status_var",
                "status_lbl", "warning_var", "warning_lbl", "target",
                "target_var"}
    for key in dialog._visible_tab_keys:
        assert set(dialog._tab_widgets[key]) == expected, key
    dialog.destroy()


def test_placeholder_rendered_in_each_tab(tk_root, mock_config, tmp_path,
                                          mocker):
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    for key in dialog._visible_tab_keys:
        content = dialog._tab_widgets[key]["text"].get("1.0", "end")
        assert content.startswith("(no draft yet"), key
    dialog.destroy()


def test_close_signals_all_tab_stop_events(tk_root, mock_config, tmp_path,
                                           mocker):
    """The WM_DELETE_WINDOW protocol stops every tab's worker, then destroys."""
    dialog = _build_dialog(tk_root, mock_config, tmp_path, mocker)
    states = list(dialog._tab_state.values())
    assert states and all(not s["stop"].is_set() for s in states)
    handler = dialog.protocol("WM_DELETE_WINDOW")
    dialog.tk.call(handler)          # simulate the close click
    assert all(s["stop"].is_set() for s in states)
    assert not dialog.winfo_exists()


# ── Tab strip fits the dialog (Roadmap-9 Phase 4.2 / audit F8) ───────────

class TestTabStripFits:
    """Tk notebooks do not scroll their tab strip.

    Once the strip is wider than the window, the overflowing tabs are
    unreachable rather than merely cramped — the content is gone, not just
    cropped. With every DocType key rendered as `key.upper()` the strip
    needed roughly 760px while the dialog's own minsize is 720 wide, so the
    last tab could not be clicked at the smallest supported size.
    """

    _MINSIZE_W = 720
    _PX_PER_CHAR = 8      # deliberate over-estimate for a 9pt UI font

    def _tab_texts(self):
        from dialogs.doc_drafter_support import _TAB_LABELS
        from helpers.doc_types import REGISTRY
        return [f"  {_TAB_LABELS.get(k, k.upper())}  " for k in REGISTRY]

    def test_the_strip_fits_at_the_dialogs_minimum_width(self):
        width = sum(len(t) for t in self._tab_texts()) * self._PX_PER_CHAR
        assert width < self._MINSIZE_W, (
            f"tab strip needs ~{width}px but minsize is {self._MINSIZE_W}px — "
            f"the last tab(s) would be unreachable")

    def test_the_declared_minsize_still_matches_what_we_assumed(self):
        """If the dialog shrinks, this budget has to be rechecked."""
        import pathlib
        src = pathlib.Path("src/dialogs/doc_drafter.py").read_text(
            encoding="utf-8")
        assert f"self.minsize({self._MINSIZE_W}, " in src, \
            "minsize changed — re-derive the tab-strip budget"

    def test_every_doctype_still_gets_a_label(self):
        from helpers.doc_types import REGISTRY
        assert len(self._tab_texts()) == len(REGISTRY)
        assert all(t.strip() for t in self._tab_texts())

    def test_overrides_only_shorten_never_rename_meaninglessly(self):
        from dialogs.doc_drafter_support import _TAB_LABELS
        for key, label in _TAB_LABELS.items():
            assert len(label) < len(key.upper()), (
                f"{key!r} override {label!r} is not shorter — the point is "
                f"width")
            assert label.isupper()
