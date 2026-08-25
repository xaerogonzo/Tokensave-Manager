"""tests/test_gitignore_pattern.py — ensure_pattern, and why it is fussy.

Used to keep a freshly written project `.mcp.json` out of version control. The
default is ON and that is a judgement call rather than an obvious one: the file
is portable precisely so it *can* be committed, but committing it hands every
collaborator an MCP server definition that only starts if they happen to have
tokensave on PATH. Opting people in silently is the ruder default.

Most of what is asserted here is restraint — what does NOT count as coverage,
and what it refuses to touch.
"""
from __future__ import annotations

import os

import pytest

from helpers.gitignore import ensure_pattern


@pytest.fixture
def repo(tmp_path):
    """A directory that looks like a git repository."""
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


def _gitignore(root):
    p = os.path.join(root, ".gitignore")
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else ""


def test_adds_the_pattern_with_its_comment(repo):
    added, detail = ensure_pattern(repo, ".mcp.json", comment="# why")

    assert added is True
    text = _gitignore(repo)
    assert ".mcp.json" in text
    assert "# why" in text
    assert ".mcp.json" in detail


def test_is_idempotent(repo):
    """Binding a project twice must not grow its .gitignore each time."""
    ensure_pattern(repo, ".mcp.json")
    first = _gitignore(repo)

    added, detail = ensure_pattern(repo, ".mcp.json")

    assert added is False
    assert "Already ignored" in detail
    assert _gitignore(repo) == first


def test_an_anchored_entry_already_counts(repo):
    """`/.mcp.json` ignores the same file; adding a second form is noise."""
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("/.mcp.json\n")

    added, _ = ensure_pattern(repo, ".mcp.json")

    assert added is False


def test_a_substring_match_does_NOT_count(repo):
    """The reason coverage is matched per line rather than by `in`.

    `foo.mcp.json` is a different file and a comment mentioning the pattern
    ignores nothing at all. Either would silently suppress a needed entry.
    """
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("# remember to think about .mcp.json one day\nfoo.mcp.json\n")

    added, _ = ensure_pattern(repo, ".mcp.json")

    assert added is True


def test_existing_content_is_preserved(repo):
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("__pycache__/\n*.log\n")

    ensure_pattern(repo, ".mcp.json")

    text = _gitignore(repo)
    assert "__pycache__/" in text and "*.log" in text and ".mcp.json" in text


def test_a_non_repository_is_left_alone(tmp_path):
    """No `.git`, nothing to ignore — and no stray .gitignore invented in a
    directory that never asked for one."""
    added, detail = ensure_pattern(str(tmp_path), ".mcp.json")

    assert added is False
    assert "Not a git repository" in detail
    assert not os.path.exists(os.path.join(str(tmp_path), ".gitignore"))


def test_an_empty_pattern_is_refused(repo):
    assert ensure_pattern(repo, "")[0] is False
    assert ensure_pattern(repo, "   ")[0] is False
    assert not os.path.exists(os.path.join(repo, ".gitignore"))


def test_no_blank_line_is_prepended_to_an_empty_file(repo):
    """A .gitignore that starts with a blank line is just untidy."""
    ensure_pattern(repo, ".mcp.json")
    assert not _gitignore(repo).startswith("\n")
