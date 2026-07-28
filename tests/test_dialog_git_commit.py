"""tests/test_dialog_git_commit.py — GitCommitDialog (Tk-marked).

Covers the novice-gotcha-#3 fix: clicking 💡 Suggest with no AI backend
configured shows ONE explanatory popup (with the Settings fix path) and
still runs the suggestion flow; with any AI backend configured no popup
appears.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.git_commit import GitCommitDialog

pytestmark = pytest.mark.tk

_STATUS = " M src/app.py\n?? notes.txt"


def _build_dialog(tk_root, mock_config, mocker):
    return GitCommitDialog(
        tk_root, "D:/some/project", _STATUS, True,
        callback=mocker.MagicMock(), cfg=mock_config,
    )


def test_suggest_without_ai_warns_once_and_still_suggests(
    tk_root, mock_config, mocker
):
    """No CLI exe + LLM disabled → showinfo fires on the FIRST click only;
    the suggestion flow still runs both times (heuristics are useful)."""
    mock_config.raw["claude_cli_exe"] = ""
    mock_config.raw["commit_message_llm"] = {"enabled": False}
    dialog = _build_dialog(tk_root, mock_config, mocker)
    mock_info = mocker.patch("dialogs.git_commit.messagebox.showinfo")
    mock_pop = mocker.patch.object(dialog, "_populate_suggestion")

    dialog._fill_suggestion()
    dialog._fill_suggestion()

    mock_info.assert_called_once()
    assert "Settings" in mock_info.call_args[0][1]
    assert mock_pop.call_count == 2


def test_suggest_with_cli_configured_no_warning(tk_root, mock_config, mocker):
    mock_config.raw["claude_cli_exe"] = "C:/npm/claude.cmd"
    mock_config.raw["commit_message_llm"] = {"enabled": False}
    dialog = _build_dialog(tk_root, mock_config, mocker)
    mock_info = mocker.patch("dialogs.git_commit.messagebox.showinfo")
    mocker.patch.object(dialog, "_populate_suggestion")

    dialog._fill_suggestion()
    mock_info.assert_not_called()


def test_suggest_with_llm_enabled_no_warning(tk_root, mock_config, mocker):
    mock_config.raw["claude_cli_exe"] = ""
    mock_config.raw["commit_message_llm"] = {"enabled": True,
                                              "provider": "ollama"}
    dialog = _build_dialog(tk_root, mock_config, mocker)
    mock_info = mocker.patch("dialogs.git_commit.messagebox.showinfo")
    mocker.patch.object(dialog, "_populate_suggestion")

    dialog._fill_suggestion()
    mock_info.assert_not_called()


# ── Commit-request handoff seeding ───────────────────────────────────────

def _build_with_request(tk_root, mock_config, mocker, tmp_path, files):
    from helpers.commit_request import write_commit_request
    write_commit_request(str(tmp_path), files, note="from chat")
    return GitCommitDialog(
        tk_root, str(tmp_path), _STATUS, True,
        callback=mocker.MagicMock(), cfg=mock_config,
    )


def test_request_prechecks_only_requested_files(
    tk_root, mock_config, mocker, tmp_path
):
    dialog = _build_with_request(tk_root, mock_config, mocker, tmp_path,
                                 ["src/app.py"])
    checks = {fname: var.get() for var, fname, _xy in dialog._file_vars}
    assert checks["src/app.py"] is True
    assert checks["notes.txt"] is False


def test_stale_request_is_ignored(tk_root, mock_config, mocker, tmp_path):
    """A request naming files absent from the status leaves defaults alone."""
    dialog = _build_with_request(tk_root, mock_config, mocker, tmp_path,
                                 ["gone/away.py"])
    assert dialog._commit_request is None
    assert all(var.get() for var, _f, _xy in dialog._file_vars)


def test_commit_consumes_request(tk_root, mock_config, mocker, tmp_path):
    from helpers.commit_request import load_commit_request
    dialog = _build_with_request(tk_root, mock_config, mocker, tmp_path,
                                 ["src/app.py"])
    dialog._msg_txt.insert("1.0", "fix(app): lazy-load pystray")
    dialog._apply()
    assert load_commit_request(str(tmp_path)) is None


def test_no_request_means_no_seeding(tk_root, mock_config, mocker):
    dialog = _build_dialog(tk_root, mock_config, mocker)
    assert dialog._commit_request is None
    assert all(var.get() for var, _f, _xy in dialog._file_vars)
