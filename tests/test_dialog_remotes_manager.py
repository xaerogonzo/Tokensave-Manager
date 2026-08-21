"""tests/test_dialog_remotes_manager.py — choosing push targets.

The dialog configures two different things that are easy to conflate: which
remotes receive a push, and which single remote the branch tracks. Ticking
"track" on several would mean `-u` runs more than once and the branch ends up
following whichever push finished last, so the dialog stores one and the
push helper applies it to one.

The other job here is defaulting: a project that has never opened this dialog
must behave exactly as the old single-remote button did.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from helpers.multi_remote import Remote
import dialogs.remotes_manager as rm
from dialogs.remotes_manager import (
    RemotesManagerDialog,
    load_selection,
    load_upstream,
)

pytestmark = pytest.mark.tk

PATH = "D:/work/proj"


def _remotes(*names):
    return [Remote(n, fetch_urls=("https://example.com/%s.git" % n,))
            for n in names]


def _dialog(tk_root, mock_config, mocker, remotes):
    mocker.patch.object(rm, "list_remotes", return_value=remotes)
    return RemotesManagerDialog(tk_root, PATH, mock_config)


# ── defaults ─────────────────────────────────────────────────────────────

def test_an_unconfigured_project_defaults_to_origin(mock_config):
    """Matches what the single-remote Push button always did."""
    assert load_selection(mock_config, PATH, _remotes("origin", "gitlab")) \
        == ("origin",)
    assert load_upstream(mock_config, PATH, _remotes("origin", "gitlab")) \
        == "origin"


def test_a_project_without_origin_falls_back_to_the_first_remote(mock_config):
    assert load_selection(mock_config, PATH, _remotes("upstream")) \
        == ("upstream",)


def test_a_project_with_no_remotes_selects_nothing(mock_config):
    assert load_selection(mock_config, PATH, []) == ()
    assert load_upstream(mock_config, PATH, []) == ""


def test_an_explicitly_empty_selection_is_respected(mock_config):
    """Distinct from "never configured" — the user unticked everything."""
    mock_config.raw["selected_push_remotes"] = {PATH: []}
    assert load_selection(mock_config, PATH, _remotes("origin")) == ()


# ── reconciliation against live git ──────────────────────────────────────

def test_a_remote_that_no_longer_exists_drops_out(mock_config):
    """Git is authoritative; the saved list is only a preference.

    Left in, it would either error at push time or quietly shrink the push
    while still reporting success for the remotes that remain.
    """
    mock_config.raw["selected_push_remotes"] = {PATH: ["origin", "deleted"]}
    assert load_selection(mock_config, PATH, _remotes("origin")) == ("origin",)


def test_an_upstream_pointing_at_a_missing_remote_is_cleared(mock_config):
    mock_config.raw["upstream_remote"] = {PATH: "deleted"}
    assert load_upstream(mock_config, PATH, _remotes("origin")) == ""


# ── the dialog ───────────────────────────────────────────────────────────

def test_saving_persists_selection_and_upstream(tk_root, mock_config, mocker):
    dlg = _dialog(tk_root, mock_config, mocker, _remotes("origin", "codeberg"))
    try:
        dlg._vars["origin"].set(True)
        dlg._vars["codeberg"].set(True)
        dlg._upstream_var.set("origin")
        dlg._on_save()
        assert mock_config.raw["selected_push_remotes"][PATH] == \
            ["origin", "codeberg"]
        assert mock_config.raw["upstream_remote"][PATH] == "origin"
        assert mock_config._saved is True
    finally:
        if dlg.winfo_exists():
            dlg.destroy()


def test_tracking_a_remote_that_will_not_be_pushed_to_is_cleared(
        tk_root, mock_config, mocker):
    """A contradiction git would never act on anyway.

    Tracking is updated by the push itself, so nominating a remote that
    receives no push would leave the branch pointing at something that never
    gets updated.
    """
    dlg = _dialog(tk_root, mock_config, mocker, _remotes("origin", "codeberg"))
    try:
        dlg._vars["origin"].set(True)
        dlg._vars["codeberg"].set(False)
        dlg._upstream_var.set("codeberg")
        dlg._on_save()
        assert mock_config.raw["upstream_remote"][PATH] == ""
    finally:
        if dlg.winfo_exists():
            dlg.destroy()


def test_every_remote_gets_a_row(tk_root, mock_config, mocker):
    dlg = _dialog(tk_root, mock_config, mocker,
                  _remotes("origin", "gitlab", "codeberg"))
    try:
        assert set(dlg._vars) == {"origin", "gitlab", "codeberg"}
    finally:
        dlg.destroy()


def test_a_repo_with_no_remotes_says_so(tk_root, mock_config, mocker):
    dlg = _dialog(tk_root, mock_config, mocker, [])
    try:
        assert dlg._vars == {}
        texts = _labels(dlg)
        assert any("No remotes configured" in t for t in texts)
    finally:
        dlg.destroy()


def test_credentials_in_a_remote_url_are_not_displayed(tk_root, mock_config,
                                                       mocker):
    """`git remote -v` will hand back an embedded token verbatim."""
    remote = Remote("origin",
                    fetch_urls=("https://bob:ghp_secret@github.com/o/r.git",))
    dlg = _dialog(tk_root, mock_config, mocker, [remote])
    try:
        blob = " ".join(_labels(dlg))
        assert "ghp_secret" not in blob
        assert "***@github.com" in blob
    finally:
        dlg.destroy()


def test_a_remote_with_several_push_urls_is_flagged(tk_root, mock_config,
                                                    mocker):
    """Git pushes to all of them, which surprises people."""
    remote = Remote("origin", fetch_urls=("https://f/r.git",),
                    push_urls=("https://a/r.git", "https://b/r.git"))
    dlg = _dialog(tk_root, mock_config, mocker, [remote])
    try:
        assert any("all 2 destinations" in t for t in _labels(dlg))
    finally:
        dlg.destroy()


def _labels(widget) -> list:
    out = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            out.append(str(child.cget("text")))
        out.extend(_labels(child))
    return out
