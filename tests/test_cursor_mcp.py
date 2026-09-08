"""Tests for helpers/mcp_cursor.py — Cursor's MCP config.

Cursor's file format is byte-identical in shape to Claude Code's, which is
exactly why this needs its own tests: a shape match is not a semantics match,
and the tempting move is to reuse the Claude reasoning wholesale. Two things
are checked hardest here:

  * the five-state verdict, and specifically that `malformed` never collapses
    into `absent` (absence is never success);
  * that writing never destroys anything it did not put there — unrelated
    `mcpServers` entries, unrelated top-level keys, or a file mid-edit.

Plus the negative space: no Claude trust-gate state is read or written, and
neither scope's write can reach the other's file.
"""
import json
import os

import pytest

from helpers.mcp_cursor import (
    SERVER_KEY,
    STATE_ABSENT,
    STATE_MALFORMED,
    STATE_PRESENT_CORRECT,
    STATE_PRESENT_WRONG_TARGET,
    bind_cursor_project,
    cursor_binding_state,
    cursor_global_mcp_path,
    cursor_project_mcp_path,
    read_cursor_mcp,
    unbind_cursor_project,
)

ENTRY = {"command": "tokensave", "args": ["serve", "-p", "."]}


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


# ── Paths ────────────────────────────────────────────────────────────────────

def test_project_path_is_dot_cursor_mcp_json(project):
    assert cursor_project_mcp_path(project) == os.path.join(
        project, ".cursor", "mcp.json")


def test_global_path_follows_the_redirected_home(fake_home):
    """Resolved at call time, or the fake_home fixture would be bypassed (G-L)."""
    assert cursor_global_mcp_path().startswith(str(fake_home))


def test_global_path_is_not_the_project_path(project, fake_home):
    assert cursor_global_mcp_path() != cursor_project_mcp_path(project)


# ── The five states ──────────────────────────────────────────────────────────

def test_missing_file_is_absent(project):
    state, _ = cursor_binding_state(project)
    assert state == STATE_ABSENT


def test_bound_project_is_present_correct(project):
    _write(cursor_project_mcp_path(project), {"mcpServers": {SERVER_KEY: ENTRY}})
    state, _ = cursor_binding_state(project)
    assert state == STATE_PRESENT_CORRECT


def test_file_without_our_server_is_absent(project):
    """Someone else's servers are present; ours is not."""
    _write(cursor_project_mcp_path(project),
           {"mcpServers": {"other": {"command": "x"}}})
    state, _ = cursor_binding_state(project)
    assert state == STATE_ABSENT


def test_entry_pointing_elsewhere_is_wrong_target(project, tmp_path):
    other = str(tmp_path / "somewhere-else")
    _write(cursor_project_mcp_path(project), {
        "mcpServers": {SERVER_KEY: {"command": "tokensave",
                                    "args": ["serve", "-p", other]}}})
    state, detail = cursor_binding_state(project)
    assert state == STATE_PRESENT_WRONG_TARGET
    assert other in detail


def test_absolute_path_to_this_same_project_is_correct(project):
    """Case and separator variants name one directory, not two."""
    _write(cursor_project_mcp_path(project), {
        "mcpServers": {SERVER_KEY: {"command": "tokensave",
                                    "args": ["serve", "-p", project]}}})
    state, _ = cursor_binding_state(project)
    assert state == STATE_PRESENT_CORRECT


def test_malformed_json_is_not_reported_as_absent(project):
    """The load-bearing distinction: broken config != no config."""
    _write(cursor_project_mcp_path(project), "{not valid json")
    state, _ = cursor_binding_state(project)
    assert state == STATE_MALFORMED
    assert state != STATE_ABSENT


def test_valid_json_of_the_wrong_document_type_is_malformed(project):
    """A bare list parses fine and would KeyError a json.load-only check."""
    _write(cursor_project_mcp_path(project), ["not", "an", "object"])
    state, _ = cursor_binding_state(project)
    assert state == STATE_MALFORMED


def test_bom_prefixed_file_still_parses(project):
    """Windows editors write UTF-8 BOMs; utf-8-sig is why this is not malformed."""
    path = cursor_project_mcp_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as fh:
        json.dump({"mcpServers": {SERVER_KEY: ENTRY}}, fh)
    state, _ = cursor_binding_state(project)
    assert state == STATE_PRESENT_CORRECT


# ── Binding preserves what it did not write ──────────────────────────────────

def test_bind_creates_the_file_and_the_directory(project):
    ok, err = bind_cursor_project(project, ENTRY)
    assert ok, err
    data, _ = read_cursor_mcp(cursor_project_mcp_path(project))
    assert data["mcpServers"][SERVER_KEY] == ENTRY


def test_bind_preserves_unrelated_servers(project):
    """This file is shared with everything else the user wired into Cursor."""
    _write(cursor_project_mcp_path(project), {
        "mcpServers": {"other": {"command": "other-server", "args": ["--x"]}}})
    ok, err = bind_cursor_project(project, ENTRY)
    assert ok, err
    data, _ = read_cursor_mcp(cursor_project_mcp_path(project))
    assert data["mcpServers"]["other"] == {"command": "other-server",
                                           "args": ["--x"]}
    assert data["mcpServers"][SERVER_KEY] == ENTRY


def test_bind_preserves_unrelated_top_level_keys(project):
    _write(cursor_project_mcp_path(project),
           {"mcpServers": {}, "someOtherCursorKey": {"keep": "me"}})
    bind_cursor_project(project, ENTRY)
    data, _ = read_cursor_mcp(cursor_project_mcp_path(project))
    assert data["someOtherCursorKey"] == {"keep": "me"}


def test_bind_refuses_to_overwrite_a_malformed_file(project):
    """Overwriting here would discard whatever the user was mid-way through."""
    path = cursor_project_mcp_path(project)
    _write(path, "{half written")
    ok, err = bind_cursor_project(project, ENTRY)
    assert ok is False
    assert "not valid JSON" in err
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == "{half written"


def test_bind_is_idempotent(project):
    bind_cursor_project(project, ENTRY)
    first = open(cursor_project_mcp_path(project), encoding="utf-8").read()
    bind_cursor_project(project, ENTRY)
    second = open(cursor_project_mcp_path(project), encoding="utf-8").read()
    assert first == second


# ── Unbinding ────────────────────────────────────────────────────────────────

def test_unbind_removes_only_our_key(project):
    _write(cursor_project_mcp_path(project), {
        "mcpServers": {SERVER_KEY: ENTRY, "other": {"command": "x"}}})
    ok, err = unbind_cursor_project(project)
    assert ok, err
    data, _ = read_cursor_mcp(cursor_project_mcp_path(project))
    assert SERVER_KEY not in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_unbind_on_a_missing_file_is_a_no_op_success(project):
    ok, err = unbind_cursor_project(project)
    assert ok is True
    assert not os.path.exists(cursor_project_mcp_path(project))


def test_unbind_refuses_a_malformed_file(project):
    _write(cursor_project_mcp_path(project), "{broken")
    ok, _ = unbind_cursor_project(project)
    assert ok is False


# ── Negative space: scope isolation and no Claude state ──────────────────────

def test_project_bind_does_not_touch_the_global_file(project, fake_home):
    bind_cursor_project(project, ENTRY)
    assert not os.path.exists(cursor_global_mcp_path())


def test_project_bind_does_not_touch_claude_config(project, fake_home):
    """Cursor has no trust gate; nothing here may read or write ~/.claude.json."""
    claude_json = os.path.join(str(fake_home), ".claude.json")
    _write(claude_json, {"projects": {}, "mcpServers": {}})
    before = open(claude_json, encoding="utf-8").read()
    bind_cursor_project(project, ENTRY)
    cursor_binding_state(project)
    assert open(claude_json, encoding="utf-8").read() == before


def test_project_bind_does_not_create_a_claude_mcp_json(project, fake_home):
    bind_cursor_project(project, ENTRY)
    assert not os.path.exists(os.path.join(project, ".mcp.json"))


def test_no_trust_vocabulary_leaks_into_the_verdicts():
    """Cursor cannot produce a trust verdict, so we must never report one."""
    from helpers import mcp_cursor
    source = " ".join(mcp_cursor.STATE_LABELS.values()).lower()
    assert "trust" not in source


# ── Phase 2a: the pre-existing Cursor rows in the agent tables ───────────────
#
# `"cursor"` was already listed in _TOKENSAVE_AGENTS and _CODEGRAPH_AGENTS
# before any of this existed, so the wiring pickers could always target it.
# Nothing had ever exercised that path; these are the tests it never had.

import shutil

from helpers.mcp import (
    _CODEGRAPH_AGENTS,
    _TOKENSAVE_AGENTS,
    _codegraph_agent_destination_path,
    _tokensave_agent_destination_path,
    _tokensave_agent_installed,
    _tokensave_agent_wired,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "cursor_mcp")


def _seed_global(fake_home, fixture_name):
    dest = cursor_global_mcp_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(os.path.join(_FIXTURES, fixture_name), dest)
    return dest


def test_cursor_is_listed_for_both_tools():
    assert "cursor" in dict(_TOKENSAVE_AGENTS)
    assert "cursor" in dict(_CODEGRAPH_AGENTS)


def test_both_tools_agree_on_cursors_destination(fake_home):
    """Two tables naming two different files for one agent would be a bug
    nobody notices until a wire lands somewhere Cursor never reads."""
    assert (_tokensave_agent_destination_path("cursor") ==
            _codegraph_agent_destination_path("cursor"))


def test_cursor_destination_is_the_documented_global_path(fake_home):
    assert _tokensave_agent_destination_path("cursor") == cursor_global_mcp_path()


def test_cursor_reports_not_installed_on_a_clean_machine(fake_home):
    """No ~/.cursor at all — the state this development machine is actually in."""
    assert _tokensave_agent_installed("cursor") is False


def test_cursor_reports_installed_once_its_config_dir_exists(fake_home):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    assert _tokensave_agent_installed("cursor") is True


def test_wired_detects_tokensave_in_a_realistic_config(fake_home):
    _seed_global(fake_home, "global_with_tokensave.json")
    assert _tokensave_agent_wired("cursor") is True


def test_wired_is_false_when_other_servers_are_present(fake_home):
    """"Has some MCP servers" is not "has ours" — the distinction the
    nag-filtering depends on."""
    _seed_global(fake_home, "global_without_tokensave.json")
    assert _tokensave_agent_wired("cursor") is False


def test_wired_is_false_with_no_config_at_all(fake_home):
    assert _tokensave_agent_wired("cursor") is False
