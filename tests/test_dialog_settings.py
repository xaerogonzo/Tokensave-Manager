"""tests/test_dialog_settings.py — SettingsDialog (Tk-marked).

Behavior-level smoke tests written BEFORE the Roadmap-8 section split so
the refactor has a safety net. They deliberately avoid asserting on
internal attribute placement (which the split will move) and instead
verify the public contract:

  * the dialog constructs with all sections
  * _save round-trips seeded raw values and materialises defaults
  * _save aborts (no save_fn call) when tokensave_exe doesn't exist

Mocking notes
-------------
* Detection helpers are mocked at the dialogs.settings import site (G-E)
  so no real PATH probing or subprocess spawns happen at build time.
* _mcp_configs is mocked to [] so the MCP summary row doesn't read the
  real ~/.claude.json.
* The paths section reads `self.master.cmd_upgrade_tokensave` (the App
  in production) — tests stub those attributes onto tk_root.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

pytestmark = pytest.mark.tk


def _build_dialog(tk_root, cfg, mocker, save_fn=None, callback=None):
    """Construct a SettingsDialog with detection + MCP helpers stubbed.

    Detection helpers are patched at each SECTION module's import site
    (G-E) — the Roadmap-8 split moved them out of dialogs.settings.
    """
    for det in ("_detect_git", "_detect_gh", "_detect_claude_cli"):
        mocker.patch(f"dialogs.settings_paths.{det}", return_value="")
    for det in ("_detect_codegraph", "_detect_npm"):
        mocker.patch(f"dialogs.settings_codegraph.{det}", return_value="")
    mocker.patch("dialogs.settings._mcp_configs", return_value=[])
    # No real subprocess spawns from verify handlers if a timer fires.
    for mod in ("settings_paths", "settings_codegraph"):
        mocker.patch(f"dialogs.{mod}.subprocess.run",
                     side_effect=AssertionError("unexpected subprocess.run"))
    # The upgrade row resolves these on the host (App in production).
    tk_root.cmd_upgrade_tokensave = lambda: None
    tk_root.cmd_integration_check = lambda: None

    from dialogs.settings import SettingsDialog
    return SettingsDialog(
        tk_root, cfg,
        save_fn or (lambda: None),
        callback or (lambda: None),
    )


def test_dialog_constructs_without_error(tk_root, mock_config, mocker):
    dialog = _build_dialog(tk_root, mock_config, mocker)
    assert dialog.title() == "Settings"
    dialog.destroy()


def test_save_round_trips_seeded_values(tk_root, mock_config, mocker):
    """Values loaded from raw must survive an untouched open→Save cycle."""
    raw = mock_config.raw
    raw.update({
        "template_dir":           "D:/templates",
        "git_exe":                "C:/git/git.exe",
        "claude_cli_exe":         "C:/npm/claude.cmd",
        "claude_cli_model":       "claude-sonnet-4-6",
        "draft_pr_backend":       "llm",
        "commit_message_backend": "claude_cli",
        "ollama_num_ctx":         8192,
        "ollama_warmup":          True,
        "commit_message_llm": {
            "enabled": True, "provider": "ollama",
            "model": "qwen2.5-coder:14b", "api_key_env": "",
            "base_url": "http://localhost:11434", "min_diff_lines": 5,
        },
    })
    saved = []
    called = []
    dialog = _build_dialog(tk_root, mock_config, mocker,
                           save_fn=lambda: saved.append(True),
                           callback=lambda: called.append(True))
    dialog._save()

    assert saved and called
    assert raw["template_dir"]           == "D:/templates"
    assert raw["git_exe"]                == "C:/git/git.exe"
    assert raw["claude_cli_exe"]         == "C:/npm/claude.cmd"
    assert raw["claude_cli_model"]       == "claude-sonnet-4-6"
    assert raw["draft_pr_backend"]       == "llm"
    assert raw["commit_message_backend"] == "claude_cli"
    assert raw["ollama_num_ctx"]         == 8192
    assert raw["ollama_warmup"]          is True
    llm = raw["commit_message_llm"]
    assert llm["provider"]       == "ollama"
    assert llm["model"]          == "qwen2.5-coder:14b"
    assert llm["min_diff_lines"] == 5
    # Defaults materialised by _save:
    assert raw["editor_cmd"] == "code"          # blank → "code"
    assert llm["max_diff_chars"]  == 24000
    assert llm["timeout_seconds"] == 90
    assert "ask_tab_llm" in raw
    # Grounding toggles persist as real bools.
    assert raw["enable_llm_grounding"]    is True
    assert raw["enable_commit_grounding"] is True
    assert raw["enable_pr_grounding"]     is True


def test_save_aborts_when_tokensave_exe_missing(tk_root, mock_config, mocker):
    """A non-existent tokensave_exe path blocks the save entirely."""
    mock_config.raw["tokensave_exe"] = "Z:/definitely/not/here/tokensave.exe"
    saved = []
    dialog = _build_dialog(tk_root, mock_config, mocker,
                           save_fn=lambda: saved.append(True))
    mock_warn = mocker.patch("dialogs.settings_paths.messagebox.showwarning")
    dialog._save()
    mock_warn.assert_called_once()
    assert not saved
    assert dialog.winfo_exists()        # dialog stays open for correction
    dialog.destroy()


def test_save_writes_ask_tab_llm_independently(tk_root, mock_config, mocker):
    """Ask-tab AI config is written as its own block, falling back to
    commit_message_llm values for first-run defaults."""
    mock_config.raw["ask_tab_llm"] = {
        "enabled": True, "provider": "claude_cli",
        "model": "", "api_key_env": "", "base_url": "",
    }
    dialog = _build_dialog(tk_root, mock_config, mocker)
    dialog._save()
    ask = mock_config.raw["ask_tab_llm"]
    assert ask["enabled"] is True
    assert ask["provider"] == "claude_cli"
