"""Backward-compatibility guarantee for the agent-CLI selector.

Adding Cursor changed what the persisted string `"claude_cli"` *means*. It used
to name Claude Code specifically; it now names "whichever agent CLI is
selected". That is a deliberate design choice — it avoids rewriting every
per-feature backend setting on every user's machine — but a meaning change is
exactly the kind of thing that silently breaks existing configs.

So the guarantee is written down as a test rather than as a paragraph:

  * a configuration saved before Cursor existed loads unchanged,
  * every `"claude_cli"` backend value it contains still resolves to Claude,
  * and nothing rewrites the file behind the user's back.

Contract C's other half lives here too: an unrecognised `agent_cli` is a
configuration error, never a quiet fallback.
"""
import json

import pytest

from helpers.agent_cli import (
    CLAUDE,
    RESOLUTION_OK,
    RESOLUTION_UNAVAILABLE,
    RESOLUTION_UNKNOWN_AGENT,
)
from state import ManagerConfig


#: A configuration exactly as it looked before Cursor support existed — every
#: backend key at a value that named Claude Code.
PRE_CURSOR_CONFIG = {
    "tokensave_exe": r"C:\tools\tokensave.exe",
    "claude_cli_exe": r"C:\npm\claude.cmd",
    "claude_cli_model": "claude-haiku-4-5-20251001",
    "draft_pr_backend": "claude_cli",
    "commit_message_backend": "claude_cli",
    "precommit_review_backend": "claude_cli",
    "ask_tab_llm": {"provider": "claude_cli", "model": ""},
    "commit_message_llm": {"provider": "claude_cli"},
}


@pytest.fixture
def pre_cursor():
    return ManagerConfig(raw=dict(PRE_CURSOR_CONFIG))


# ── The guarantee ────────────────────────────────────────────────────────────

def test_pre_cursor_config_resolves_to_claude(pre_cursor):
    """No `agent_cli` key at all — the state every existing install is in."""
    assert "agent_cli" not in pre_cursor.raw
    res = pre_cursor.resolve_agent_cli()
    assert res.spec is CLAUDE
    assert res.state == RESOLUTION_OK
    assert res.exe == r"C:\npm\claude.cmd"


@pytest.mark.parametrize("key", [
    "draft_pr_backend", "commit_message_backend", "precommit_review_backend",
])
def test_backend_values_load_unchanged(pre_cursor, key):
    """The literals keep their spelling; only their resolution is indirected."""
    assert pre_cursor.raw[key] == "claude_cli"


def test_nested_provider_values_load_unchanged(pre_cursor):
    assert pre_cursor.raw["ask_tab_llm"]["provider"] == "claude_cli"
    assert pre_cursor.raw["commit_message_llm"]["provider"] == "claude_cli"


def test_reading_config_does_not_mutate_it(pre_cursor):
    """Resolution must not write a migration into raw as a side effect."""
    before = json.dumps(pre_cursor.raw, sort_keys=True)
    pre_cursor.resolve_agent_cli()
    pre_cursor.agent_cli
    pre_cursor.cursor_cli_model
    assert json.dumps(pre_cursor.raw, sort_keys=True) == before


def test_claude_model_default_is_preserved(pre_cursor):
    assert pre_cursor.agent_model_for(CLAUDE) == "claude-haiku-4-5-20251001"


def test_absent_agent_cli_key_reports_the_default(pre_cursor):
    assert pre_cursor.agent_cli == "claude"


# ── Contract C: unknown ids are errors, not fallbacks ────────────────────────

@pytest.mark.parametrize("bad", ["cursor_old", "codex", "Claude Code", "1"])
def test_unknown_agent_is_a_configuration_error(bad):
    """Never silently map onto the default — that runs the wrong agent."""
    res = ManagerConfig(raw={"agent_cli": bad}).resolve_agent_cli()
    assert res.state == RESOLUTION_UNKNOWN_AGENT
    assert res.spec is None
    assert res.exe == ""


def test_unknown_agent_message_quotes_the_offending_value():
    """The user needs to see what to fix, not just that something is invalid."""
    res = ManagerConfig(raw={"agent_cli": "cursor_old"}).resolve_agent_cli()
    assert "cursor_old" in res.error_message()


def test_unknown_agent_is_never_silently_repaired():
    res = ManagerConfig(raw={"agent_cli": "cursor_old"})
    res.resolve_agent_cli()
    assert res.raw["agent_cli"] == "cursor_old"


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_agent_cli_takes_the_default_rather_than_erroring(empty):
    """Absent/blank is "not set", which is legitimate; a wrong name is not."""
    res = ManagerConfig(raw={"agent_cli": empty,
                             "claude_cli_exe": "/x/claude"}).resolve_agent_cli()
    assert res.state == RESOLUTION_OK
    assert res.spec is CLAUDE


# ── Contract D: unavailable is distinct from unknown ─────────────────────────

def test_selected_but_missing_binary_is_unavailable_not_unknown(monkeypatch):
    """Cursor selected with nothing installed — a different fault, different text."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("helpers.agent_cli.os.path.isfile", lambda _p: False)
    res = ManagerConfig(raw={"agent_cli": "cursor"}).resolve_agent_cli()
    assert res.state == RESOLUTION_UNAVAILABLE
    assert res.spec is not None
    assert res.exe == ""


def test_unavailable_message_points_at_paths_not_at_the_agent_list(monkeypatch):
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("helpers.agent_cli.os.path.isfile", lambda _p: False)
    msg = ManagerConfig(raw={"agent_cli": "cursor"}).resolve_agent_cli().error_message()
    assert "Cursor Agent CLI" in msg
    assert "Settings" in msg
    assert "Known agents" not in msg


def test_ok_resolution_has_no_error_message():
    res = ManagerConfig(raw={"agent_cli": "cursor",
                             "cursor_cli_exe": "/x/cursor-agent"}).resolve_agent_cli()
    assert res.state == RESOLUTION_OK
    assert res.error_message() == ""


# ── Explicit path always beats detection ─────────────────────────────────────

def test_configured_path_is_not_overwritten_by_detection(monkeypatch):
    """A Settings save must not be silently reverted by whatever is on PATH."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which",
                        lambda _n: "/detected/cursor-agent")
    cfg = ManagerConfig(raw={"agent_cli": "cursor",
                             "cursor_cli_exe": "/manual/cursor-agent"})
    assert cfg.cursor_cli_exe == "/manual/cursor-agent"


def test_detection_fills_in_when_no_path_is_configured(monkeypatch):
    monkeypatch.setattr("helpers.agent_cli.shutil.which",
                        lambda n: "/detected/cursor-agent"
                        if n == "cursor-agent.cmd" else None)
    cfg = ManagerConfig(raw={"agent_cli": "cursor"})
    assert cfg.cursor_cli_exe == "/detected/cursor-agent"


def test_refresh_derived_repicks_after_a_settings_save(monkeypatch):
    """Rule 3: derived values are re-read, never snapshotted at construction."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("helpers.agent_cli.os.path.isfile", lambda _p: False)
    cfg = ManagerConfig(raw={"agent_cli": "cursor"})
    assert cfg.cursor_cli_exe == ""
    cfg.raw["cursor_cli_exe"] = "/newly/set/cursor-agent"
    cfg.refresh_derived()
    assert cfg.cursor_cli_exe == "/newly/set/cursor-agent"
    assert cfg.resolve_agent_cli().state == RESOLUTION_OK
