"""tests/test_git_staged_deletions.py — staged-deletion git helpers.

Exercises `_staged_deletions` and the staged-deletion exclusion in
`_find_tracked_but_ignored` against REAL temporary git repositories (not
mocks) — these helpers are thin wrappers over git plumbing whose whole
value is matching git's actual behaviour, so a real repo is the honest test.

Skips entirely if git is not on PATH (keeps the suite green on minimal envs).
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from helpers.git import _find_tracked_but_ignored, _staged_deletions

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git not available on PATH")


def _run(repo, *args):
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(
        [_GIT, "-C", str(repo)] + list(args),
        check=True, capture_output=True, text=True,
    )


def _init_repo(repo):
    """Init a repo with a deterministic identity (commit needs user.*)."""
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    _run(repo, "config", "commit.gpgsign", "false")


def _commit_file(repo, name, content="x"):
    (repo / name).write_text(content, encoding="utf-8")
    _run(repo, "add", name)
    _run(repo, "commit", "-m", f"add {name}")


# ── _staged_deletions ──────────────────────────────────────────────────────

def test_staged_deletions_empty_clean_repo(tmp_path):
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.txt")
    assert _staged_deletions(str(tmp_path), _GIT) == []


def test_staged_deletions_after_rm_cached(tmp_path):
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.txt")
    _run(tmp_path, "rm", "--cached", "a.txt")
    assert _staged_deletions(str(tmp_path), _GIT) == ["a.txt"]


def test_staged_deletions_bad_path_returns_empty(tmp_path):
    # Not a git repo → git errors → helper returns [] (never raises).
    assert _staged_deletions(str(tmp_path / "nope"), _GIT) == []


# ── _find_tracked_but_ignored excludes in-progress untracks ─────────────────

def test_find_tracked_but_ignored_flags_then_excludes_after_rm(tmp_path):
    _init_repo(tmp_path)
    # Commit a file, THEN add it to .gitignore — the classic stale-tracking case.
    _commit_file(tmp_path, "data.log")
    (tmp_path / ".gitignore").write_text("data.log\n", encoding="utf-8")
    _run(tmp_path, "add", ".gitignore")
    _run(tmp_path, "commit", "-m", "ignore data.log")

    # Before untracking: flagged as tracked-but-ignored.
    assert "data.log" in _find_tracked_but_ignored(str(tmp_path), _GIT)

    # After `git rm --cached`: the deletion is staged → excluded so the
    # stale-ignore warning doesn't loop on a fix already in progress.
    _run(tmp_path, "rm", "--cached", "data.log")
    assert "data.log" not in _find_tracked_but_ignored(str(tmp_path), _GIT)
