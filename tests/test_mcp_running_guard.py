"""tests/test_mcp_running_guard.py — "quit the app first" has to name the app.

Two Claude apps rewrite two different MCP configs from in-memory state, so an
edit made while one is live is silently undone. The dialog warned about this
and got it wrong in three ways at once:

  * `_is_claude_running` looked for `claude-code.exe`, a process name that has
    never existed. `code` was therefore permanently False, so the warning that
    mattered for `~/.claude.json` could not fire, and the Apply guard on the
    Claude Code row was dead code.
  * The banner joined both apps into one sentence — "Claude Desktop / Claude
    Code is currently running. It rewrites its own config file" — which is
    ungrammatical for two and, worse, never said WHICH file was at risk.
  * `_remove_user_scoped` writes `~/.claude.json` and had no guard at all,
    which is the one path a user following the migration advice actually takes.

`claude_code_active` answers from the config's mtime rather than the process
list. That is not a shortcut: Claude Code runs as `node.exe` from npm, as a
native binary, and hosted inside the desktop app as `claude.exe` — no
executable name distinguishes it, and the name-matching approach already went
stale silently once. The mtime is evidence of the actual risk.
"""
from __future__ import annotations

import os
import time

import pytest

import helpers.mcp as mcp_helpers
# Patch targets live on the OWNING module, not the facade: since the
# Roadmap-16 split `_is_claude_running` resolves `claude_code_active`
# from `helpers.mcp_scope`'s own globals, so patching the re-export
# would be a no-op that still passes some assertions. Same G-E rule
# as everywhere else -- mock at the import site.
import helpers.mcp_scope as mcp_scope
from dialogs.mcp_config import MCPConfigDialog
from helpers.mcp import _CLAUDE_JSON_ACTIVE_SECS, claude_code_active


def _aged(tmp_path, seconds: float):
    """A `.claude.json` whose mtime is `seconds` in the past."""
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    when = time.time() - seconds
    os.utime(path, (when, when))
    return str(path)


# ── is a session live? ────────────────────────────────────────────────────

def test_a_just_written_config_means_a_live_session(tmp_path):
    """The measured live case: 30 seconds old with one session open."""
    active, detail = claude_code_active(_aged(tmp_path, 30))
    assert active is True
    assert "30 seconds ago" in detail


def test_detail_switches_to_minutes_when_that_reads_better(tmp_path):
    active, detail = claude_code_active(_aged(tmp_path, 150))
    assert active is True
    assert "minutes ago" in detail


def test_an_old_config_is_not_a_live_session(tmp_path):
    """Yesterday's file must not read as a running session."""
    active, detail = claude_code_active(_aged(tmp_path, _CLAUDE_JSON_ACTIVE_SECS + 60))
    assert active is False
    assert detail == ""


def test_a_missing_config_is_not_a_live_session(tmp_path):
    """Absence of the file is absence of evidence, and must never block."""
    assert claude_code_active(str(tmp_path / "nope.json")) == (False, "")


def test_the_window_boundary_counts_as_live(tmp_path):
    """Ties resolve toward warning: a false warning costs a sentence."""
    active, _ = claude_code_active(_aged(tmp_path, _CLAUDE_JSON_ACTIVE_SECS - 5))
    assert active is True


def test_is_claude_running_reports_code_and_its_reason(tmp_path, monkeypatch):
    """The dict grew `code_detail` so the UI can say HOW it decided."""
    monkeypatch.setattr(mcp_scope, "claude_code_active",
                        lambda *a, **k: (True, "because I said so"))
    got = mcp_helpers._is_claude_running()
    assert got["code"] is True
    assert got["code_detail"] == "because I said so"
    assert "desktop" in got and "pids" in got


def test_is_claude_running_survives_a_broken_tasklist(monkeypatch):
    """Detection failing must not make the dialog unusable."""
    monkeypatch.setattr(mcp_scope, "claude_code_active",
                        lambda *a, **k: (False, ""))
    monkeypatch.setattr(
        mcp_scope.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no tasklist")))
    got = mcp_helpers._is_claude_running()
    assert got["desktop"] is False
    assert got["code"] is False


# ── what the banner says ──────────────────────────────────────────────────

def _warn(desktop, code, detail=""):
    return MCPConfigDialog._running_warning(
        {"desktop": desktop, "code": code, "code_detail": detail})


def test_banner_is_empty_when_nothing_is_running():
    assert _warn(False, False) == ""


def test_banner_names_the_file_desktop_rewrites():
    text = _warn(True, False)
    assert "claude_desktop_config.json" in text
    assert "~/.claude.json" not in text


def test_banner_names_the_file_a_code_session_rewrites():
    text = _warn(False, True)
    assert "~/.claude.json" in text
    assert "claude_desktop_config.json" not in text


def test_banner_covers_the_migration_button_not_just_a_row():
    """The removal is the path the migration advice sends people down."""
    assert "user-scoped entry" in _warn(False, True)


def test_banner_says_the_desktop_app_hosts_code_sessions():
    """The trap that made this wrong: a session inside claude.exe.

    Someone reading "quit Claude Code" while their session runs in the desktop
    app has nothing obvious to quit.
    """
    assert "desktop app" in _warn(False, True)


def test_banner_includes_the_evidence_when_there_is_some():
    assert "written 30 seconds ago" in _warn(False, True, "written 30 seconds ago")


def test_banner_omits_the_parenthetical_without_evidence():
    assert "()" not in _warn(False, True, "")


def test_banner_gives_each_app_its_own_sentence():
    """Never "Desktop / Code is running. It rewrites its own config file".

    One verb and one "it" cannot cover two apps that rewrite two files, and
    the old wording left the reader guessing which was at risk.
    """
    text = _warn(True, True)
    assert text.count("⚠") == 2
    assert "\n" in text
    assert "Claude Desktop is running" in text
    assert "A Claude Code session is live" in text


@pytest.mark.parametrize("desktop,code", [(True, False), (False, True), (True, True)])
def test_banner_never_uses_the_ungrammatical_joined_form(desktop, code):
    assert "Claude Desktop / Claude Code is" not in _warn(desktop, code)
