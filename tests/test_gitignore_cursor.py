"""Two different classes of file now live under `.cursor/`, and .gitignore
has to tell them apart:

    .cursor/rules/tokensave.mdc   shared project configuration  -> COMMITTED
    .cursor/mcp.json              machine-local exe paths        -> IGNORED

This is precisely the distinction a glob blurs. A `.cursor/` directory ignore
would look correct in review, pass any test that only inspects the pattern
text, and silently stop the rules files the manager writes from ever being
committed — the exact opposite of the point of writing them.

So these tests do not read the pattern. They run **real `git check-ignore`**
against a real repository and let git answer, because git's matcher is the
thing that actually decides, and it is the one component here nobody has
reimplemented.
"""
import os
import subprocess

import pytest

from constants import CREATE_NO_WINDOW
from helpers.mcp_cursor import (
    CURSOR_MCP_IGNORE_PATTERN,
    GITIGNORE_CURSOR_MCP_KEY,
    ensure_cursor_mcp_ignored,
)

GIT = os.environ.get("GIT_EXE") or "git"


def _git(repo, *args):
    return subprocess.run([GIT, "-C", repo, *args],
                          capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW)


def _is_ignored(repo, relpath):
    """Ask git, not the pattern. Exit 0 means ignored."""
    return _git(repo, "check-ignore", "-q", relpath).returncode == 0


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    r = str(root)
    if _git(r, "init").returncode != 0:
        pytest.skip("git is not available")
    os.makedirs(os.path.join(r, ".cursor", "rules"), exist_ok=True)
    for rel in (".cursor/mcp.json", ".cursor/rules/tokensave.mdc", "AGENTS.md"):
        full = os.path.join(r, *rel.split("/"))
        with open(full, "w", encoding="utf-8") as fh:
            fh.write("{}")
    return r


# ── The split ────────────────────────────────────────────────────────────────

def test_cursor_mcp_json_is_ignored_when_enabled(repo):
    added, detail = ensure_cursor_mcp_ignored(repo, {})
    assert added, detail
    assert _is_ignored(repo, ".cursor/mcp.json")


def test_cursor_rules_are_NOT_ignored(repo):
    """The load-bearing assertion. A `.cursor/` ignore would fail here."""
    ensure_cursor_mcp_ignored(repo, {})
    assert not _is_ignored(repo, ".cursor/rules/tokensave.mdc")


def test_the_rules_directory_itself_is_not_ignored(repo):
    ensure_cursor_mcp_ignored(repo, {})
    assert not _is_ignored(repo, ".cursor/rules")


def test_agents_md_is_not_ignored(repo):
    """AGENTS.md is shared project configuration too."""
    ensure_cursor_mcp_ignored(repo, {})
    assert not _is_ignored(repo, "AGENTS.md")


def test_the_pattern_names_the_file_not_the_directory():
    """Belt and braces alongside the git-driven tests above."""
    assert CURSOR_MCP_IGNORE_PATTERN == ".cursor/mcp.json"
    assert not CURSOR_MCP_IGNORE_PATTERN.endswith("/")


# ── The opt-out ──────────────────────────────────────────────────────────────

def test_nothing_is_ignored_when_the_setting_is_off(repo):
    added, _ = ensure_cursor_mcp_ignored(repo, {GITIGNORE_CURSOR_MCP_KEY: False})
    assert added is False
    assert not _is_ignored(repo, ".cursor/mcp.json")


def test_the_setting_defaults_to_on(repo):
    """An absent key must behave as enabled, matching the Claude-side default."""
    added, _ = ensure_cursor_mcp_ignored(repo, {})
    assert added
    added_again, _ = ensure_cursor_mcp_ignored(repo, None)
    assert added_again is False          # already covered, not re-added


def test_rules_stay_committable_even_with_the_setting_off(repo):
    ensure_cursor_mcp_ignored(repo, {GITIGNORE_CURSOR_MCP_KEY: False})
    assert not _is_ignored(repo, ".cursor/rules/tokensave.mdc")


# ── Idempotency + safety ─────────────────────────────────────────────────────

def test_running_twice_adds_one_entry(repo):
    ensure_cursor_mcp_ignored(repo, {})
    first = open(os.path.join(repo, ".gitignore"), encoding="utf-8").read()
    ensure_cursor_mcp_ignored(repo, {})
    second = open(os.path.join(repo, ".gitignore"), encoding="utf-8").read()
    assert first == second
    assert second.count(CURSOR_MCP_IGNORE_PATTERN) == 1


def test_existing_gitignore_content_is_preserved(repo):
    path = os.path.join(repo, ".gitignore")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# mine\n*.log\n")
    ensure_cursor_mcp_ignored(repo, {})
    text = open(path, encoding="utf-8").read()
    assert "# mine" in text
    assert "*.log" in text
    assert CURSOR_MCP_IGNORE_PATTERN in text


def test_a_non_repo_is_a_quiet_no_op(tmp_path):
    """Failing to ignore must never be reported as a failed binding."""
    added, detail = ensure_cursor_mcp_ignored(str(tmp_path), {})
    assert added is False
    assert isinstance(detail, str)
