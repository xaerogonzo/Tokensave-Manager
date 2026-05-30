"""tests/test_branch_diff_encoding.py — _branch_diff UTF-8 decoding.

Regression guard for the "Draft PR — empty diff" bug: `_branch_diff` ran
`git diff` with text=True but no explicit encoding, so on Windows it decoded
with cp1252 and raised UnicodeDecodeError the moment a diff contained a byte
cp1252 can't map. The exception was swallowed and surfaced as a bogus
"empty diff" message.

We commit a file containing Cyrillic "я" (U+044F → UTF-8 bytes D1 8F). The
0x8F byte is undefined in cp1252, so the un-fixed code path raised; with
encoding="utf-8", errors="replace" the diff decodes cleanly.

Skips if git is not on PATH.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from helpers.pr_draft import _branch_diff

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git not available on PATH")

# Cyrillic small letter ya — its UTF-8 encoding contains 0x8F (cp1252-undefined).
_CYRILLIC = "я"


def _run(repo, *args):
    subprocess.run([_GIT, "-C", str(repo)] + list(args),
                   check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def _init_repo(repo):
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    _run(repo, "config", "commit.gpgsign", "false")
    _run(repo, "branch", "-M", "main")


def test_branch_diff_decodes_non_cp1252_bytes(tmp_path):
    _init_repo(tmp_path)
    # Base commit on main.
    (tmp_path / "base.txt").write_text("hello\n", encoding="utf-8")
    _run(tmp_path, "add", "base.txt")
    _run(tmp_path, "commit", "-m", "base")

    # Feature branch with a Unicode-laden change.
    _run(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "note.txt").write_text(
        f"greeting {_CYRILLIC} done\n", encoding="utf-8")
    _run(tmp_path, "add", "note.txt")
    _run(tmp_path, "commit", "-m", "add unicode note")

    diff = _branch_diff(str(tmp_path), "main", git_exe=_GIT)

    # Must be non-empty (the bug returned "") and must contain the Unicode char.
    assert diff.strip(), "diff came back empty — UnicodeDecodeError regression"
    assert "note.txt" in diff
    assert _CYRILLIC in diff


def test_branch_diff_empty_when_no_commits_ahead(tmp_path):
    """No commits past base → genuinely empty diff (not an error)."""
    _init_repo(tmp_path)
    (tmp_path / "base.txt").write_text("hello\n", encoding="utf-8")
    _run(tmp_path, "add", "base.txt")
    _run(tmp_path, "commit", "-m", "base")
    # HEAD == main → diff against main is empty.
    assert _branch_diff(str(tmp_path), "main", git_exe=_GIT).strip() == ""
