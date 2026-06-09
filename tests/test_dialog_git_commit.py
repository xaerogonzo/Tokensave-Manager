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
