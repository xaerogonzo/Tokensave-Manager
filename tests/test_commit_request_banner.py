"""tests/test_commit_request_banner.py — CommitRequestBanner (Tk-marked).

The Git tab's commit-request handoff banner, extracted from
GitTabController in the Roadmap-8 god-class fix. Covers show/hide on
update(), the review click-through, and dismiss consuming the request.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from controllers.commit_request_banner import CommitRequestBanner
from helpers.commit_request import load_commit_request, write_commit_request

pytestmark = pytest.mark.tk


def _build(tk_root, mocker, path):
    tab = tk.Frame(tk_root)
    tab.pack()
    anchor = tk.Frame(tab)
    anchor.pack()
    on_commit = mocker.MagicMock()
    banner = CommitRequestBanner(
        tab,
        get_path=lambda: path,
        on_commit=on_commit,
        get_anchor=lambda: anchor,
    )
    return banner, on_commit, tab


def test_hidden_initially(tk_root, mocker, tmp_path):
    banner, _cb, tab = _build(tk_root, mocker, str(tmp_path))
    assert not banner._banner.winfo_manager()
    tab.destroy()


def test_update_shows_banner_with_note(tk_root, mocker, tmp_path):
    write_commit_request(str(tmp_path), ["src/a.py", "src/b.py"],
                         note="lazy-load pystray")
    banner, _cb, tab = _build(tk_root, mocker, str(tmp_path))
    banner.update(str(tmp_path), True)
    assert banner._banner.winfo_manager() == "pack"
    text = banner._lbl.cget("text")
    assert "2 file(s)" in text and "lazy-load pystray" in text
    tab.destroy()


def test_update_hides_when_no_request(tk_root, mocker, tmp_path):
    write_commit_request(str(tmp_path), ["src/a.py"])
    banner, _cb, tab = _build(tk_root, mocker, str(tmp_path))
    banner.update(str(tmp_path), True)
    assert banner._banner.winfo_manager() == "pack"
    # Request consumed elsewhere → next refresh hides the banner.
    from helpers.commit_request import clear_commit_request
    clear_commit_request(str(tmp_path))
    banner.update(str(tmp_path), True)
    assert not banner._banner.winfo_manager()
    tab.destroy()


def test_update_hides_for_non_repo(tk_root, mocker, tmp_path):
    write_commit_request(str(tmp_path), ["src/a.py"])
    banner, _cb, tab = _build(tk_root, mocker, str(tmp_path))
    banner.update(str(tmp_path), False)
    assert not banner._banner.winfo_manager()
    tab.destroy()


def test_review_opens_commit_dialog(tk_root, mocker, tmp_path):
    banner, on_commit, tab = _build(tk_root, mocker, str(tmp_path))
    banner._on_review()
    on_commit.assert_called_once_with(str(tmp_path))
    tab.destroy()


def test_dismiss_clears_request_and_hides(tk_root, mocker, tmp_path):
    write_commit_request(str(tmp_path), ["src/a.py"], note="x")
    banner, _cb, tab = _build(tk_root, mocker, str(tmp_path))
    banner.update(str(tmp_path), True)
    banner._on_dismiss()
    assert load_commit_request(str(tmp_path)) is None
    assert not banner._banner.winfo_manager()
    tab.destroy()
