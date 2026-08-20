"""tests/test_source_watch.py — "manager source changed, restart" detection.

The banner this feeds is only worth having if it is both sensitive and quiet:
a missed change means the user keeps debugging stale code (the footgun that
motivated it), while a false alarm over an editor swap file gets the banner
dismissed permanently and costs more than never shipping it.

So the tests split along exactly that line — what MUST be detected, and what
must never raise it.
"""
from __future__ import annotations

import os

import pytest

from helpers.source_watch import (
    changed_files,
    describe_changes,
    is_source_change_candidate,
    snapshot_sources,
)


def _write(root, rel, text="x = 1"):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ── what counts as a source change ───────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "app.py", "helpers/git.py", "controllers/git_tab.py",
])
def test_python_files_are_candidates(name):
    assert is_source_change_candidate(name)


@pytest.mark.parametrize("name", [
    ".app.py.swp",          # vim
    ".#app.py",             # emacs lock
    "app.py~",              # emacs backup
    "app.pyc", "app.pyo",   # bytecode
    "app.py.tmp", "app.py.orig", "app.py.rej", "app.py.bak",
    "config.json.backup.1779511978257",   # this project's own helpers
    "README.md", "notes.txt",             # not source
])
def test_editor_noise_and_non_source_are_not_candidates(name):
    """Each of these would otherwise fire the banner spuriously."""
    assert not is_source_change_candidate(name)


def test_empty_path_is_not_a_candidate():
    assert not is_source_change_candidate("")


# ── snapshotting ──────────────────────────────────────────────────────────────

def test_snapshot_records_python_files(tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "pkg/b.py")
    snap = snapshot_sources(str(tmp_path))
    assert len(snap) == 2
    assert all(isinstance(v, tuple) and len(v) == 2 for v in snap.values())


def test_snapshot_skips_cache_and_build_directories(tmp_path):
    _write(tmp_path, "real.py")
    _write(tmp_path, "__pycache__/cached.py")
    _write(tmp_path, "dist/shipped.py")
    _write(tmp_path, ".git/hook.py")
    snap = snapshot_sources(str(tmp_path))
    assert [os.path.basename(p) for p in snap] == ["real.py"]


def test_snapshot_of_a_missing_root_is_empty_not_an_error(tmp_path):
    assert snapshot_sources(str(tmp_path / "nope")) == {}


# ── change detection ──────────────────────────────────────────────────────────

def test_no_changes_when_nothing_moved(tmp_path):
    _write(tmp_path, "a.py")
    snap = snapshot_sources(str(tmp_path))
    assert changed_files(snap, snapshot_sources(str(tmp_path))) == []


def test_content_change_is_detected_even_at_identical_mtime(tmp_path):
    """The reason the fingerprint carries size as well as mtime_ns.

    Filesystem timestamp granularity, and editors that restore timestamps,
    both defeat a bare mtime compare. Size catches the rewrite anyway.
    """
    path = _write(tmp_path, "a.py", "x = 1")
    before = snapshot_sources(str(tmp_path))
    st = os.stat(path)
    _write(tmp_path, "a.py", "x = 1  # a longer line now")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))   # restore the mtime
    assert os.stat(path).st_mtime_ns == st.st_mtime_ns, "mtime not restored"
    assert changed_files(before, snapshot_sources(str(tmp_path))) == [path]


def test_added_file_is_a_change(tmp_path):
    _write(tmp_path, "a.py")
    before = snapshot_sources(str(tmp_path))
    _write(tmp_path, "b.py")
    changed = changed_files(before, snapshot_sources(str(tmp_path)))
    assert [os.path.basename(p) for p in changed] == ["b.py"]


def test_deleted_file_is_a_change(tmp_path):
    """A removed module changes what the process would import."""
    a = _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    before = snapshot_sources(str(tmp_path))
    os.remove(a)
    changed = changed_files(before, snapshot_sources(str(tmp_path)))
    assert changed == [a]


def test_editor_swap_file_does_not_register_as_a_change(tmp_path):
    """The false-alarm case that would get the banner dismissed forever."""
    _write(tmp_path, "a.py")
    before = snapshot_sources(str(tmp_path))
    _write(tmp_path, ".a.py.swp", "vim noise")
    _write(tmp_path, "a.py.bak", "backup noise")
    assert changed_files(before, snapshot_sources(str(tmp_path))) == []


# ── the summary line ──────────────────────────────────────────────────────────

def test_describe_names_files_rather_than_counting(tmp_path):
    """"3 files changed" invites "which?" — name them."""
    paths = [os.path.join(str(tmp_path), "helpers", "git.py")]
    out = describe_changes(paths, str(tmp_path))
    assert out == "helpers/git.py"


def test_describe_caps_the_list_and_says_how_many_more(tmp_path):
    paths = [os.path.join(str(tmp_path), f"f{i}.py") for i in range(6)]
    out = describe_changes(paths, str(tmp_path), limit=2)
    assert out.startswith("f0.py, f1.py")
    assert "4 more" in out


def test_describe_of_nothing_is_empty(tmp_path):
    assert describe_changes([], str(tmp_path)) == ""
