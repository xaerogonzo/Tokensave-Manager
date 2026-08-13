"""Tests for the Housekeeping surface — detection, sourcing, and backup safety.

Everything here exercises pure functions against a temp tree or a synthesized
SQLite file. Nothing runs tokensave, and nothing depends on the developer's real
home (the `fake_home` fixture redirects it) — see
``tests/test_no_import_time_path_resolution.py`` for the invariant that makes
that possible.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from helpers import housekeeping as hk


# ── Transcript parsing ────────────────────────────────────────────────────────

DIRTY = r"""
Global database
  ! 3 stale project(s) in global DB (registered but `.tokensave/` is gone):
      • D:\A\one
      • D:\B\two
      • D:\C\three
        Re-run `tokensave doctor` interactively to purge them.
User config
  • unrelated bullet that must not be captured
"""

CLEAN = r"""
Global database
  * Global DB: C:\Users\x\.tokensave\global.db
  * No stale projects in global DB
  • unrelated bullet that must not be captured
User config
"""


def test_parses_stale_block():
    entries = hk.parse_stale_entries(DIRTY)
    assert [e.path for e in entries] == [r"D:\A\one", r"D:\B\two", r"D:\C\three"]


def test_clean_output_does_not_enter_the_block():
    """A healthy run says 'No stale projects in global DB'.

    That sentence contains both 'stale project' and 'global DB', so a naive
    substring test opens the block on a clean system and then treats any later
    bulleted line as a stale path. Requiring a count is what separates them.
    """
    assert hk.parse_stale_entries(CLEAN) == []


def test_parse_accepts_a_line_list_as_well_as_text():
    assert len(hk.parse_stale_entries(DIRTY.splitlines())) == 3


# ── Classification ────────────────────────────────────────────────────────────

def test_classify_dir_missing_vs_not_indexed(tmp_path, fake_home):
    present = tmp_path / "still-here"
    present.mkdir()
    entries = [hk.StaleEntry(path=str(present)),
               hk.StaleEntry(path=str(tmp_path / "long-gone"))]
    out = hk.classify_stale_entries(entries, home=str(fake_home))
    assert out[0].reason == hk.REASON_NOT_INDEXED
    assert out[1].reason == hk.REASON_DIR_MISSING


def test_may_regenerate_only_when_dir_gone_and_logs_remain(tmp_path, fake_home):
    """Session logs matter only for a directory that no longer exists.

    A directory that is still there can simply be re-indexed; one that is gone
    cannot, so its surviving logs are what can resurrect the records.
    """
    gone = tmp_path / "deleted-project"
    encoded = hk.encode_project_path(os.path.normpath(str(gone)))
    logdir = fake_home / ".claude" / "projects" / encoded
    logdir.mkdir(parents=True)
    (logdir / "session.jsonl").write_text("{}", encoding="utf-8")

    out = hk.classify_stale_entries([hk.StaleEntry(path=str(gone))],
                                    home=str(fake_home))
    assert out[0].session_logs_present is True
    assert out[0].may_regenerate is True

    alive = tmp_path / "alive"
    alive.mkdir()
    enc2 = hk.encode_project_path(os.path.normpath(str(alive)))
    (fake_home / ".claude" / "projects" / enc2).mkdir(parents=True)
    (fake_home / ".claude" / "projects" / enc2 / "s.jsonl").write_text(
        "{}", encoding="utf-8")
    out2 = hk.classify_stale_entries([hk.StaleEntry(path=str(alive))],
                                     home=str(fake_home))
    assert out2[0].session_logs_present is True
    assert out2[0].may_regenerate is False        # directory still exists


def test_encode_replaces_dots_and_underscores():
    """Claude Code maps every non-alphanumeric to '-', including . and _.

    Verified against real ~/.claude/projects directories: a path containing
    `.claude` lands on `--claude`, and `KicomAI_Project` on `KicomAI-Project`.
    An earlier `[^\\w.]` pattern preserved both and produced directory names
    that never exist.
    """
    assert hk.encode_project_path(r"D:\P\.claude\worktrees\x") == \
        "D--P--claude-worktrees-x"
    assert hk.encode_project_path(r"D:\P\Kicom_AI") == "D--P-Kicom-AI"


# ── Source resolution ─────────────────────────────────────────────────────────

def _make_global_db(path, projects=(), hashes=(), offsets=()):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE projects (path TEXT, tokens_saved INT)")
    con.execute("CREATE TABLE turns (project_hash TEXT)")
    con.execute("CREATE TABLE parse_offsets (file_path TEXT)")
    con.executemany("INSERT INTO projects VALUES (?, 0)", [(p,) for p in projects])
    con.executemany("INSERT INTO turns VALUES (?)", [(h,) for h in hashes])
    con.executemany("INSERT INTO parse_offsets VALUES (?)", [(o,) for o in offsets])
    con.commit()
    con.close()


def test_source_project_row(tmp_path):
    p = r"D:\Work\Thing"
    db = tmp_path / "global.db"
    _make_global_db(db, projects=[p])
    out = hk.resolve_entry_source([hk.StaleEntry(path=p)], str(db))
    assert out[0].source == hk.SOURCE_PROJECT_ROW


def test_source_cost_history(tmp_path):
    p = r"D:\Work\Thing"
    db = tmp_path / "global.db"
    _make_global_db(db, hashes=[hk.encode_project_path(p)])
    out = hk.resolve_entry_source([hk.StaleEntry(path=p)], str(db))
    assert out[0].source == hk.SOURCE_COST_HISTORY


def test_source_unknown_when_in_neither(tmp_path):
    """Absence is never evidence.

    'Not in projects' must not be read as 'therefore cost history' — the
    history tables are checked positively, and anything unmatched stays
    unknown so it never receives source-specific semantics.
    """
    db = tmp_path / "global.db"
    _make_global_db(db, projects=[r"D:\Someone\Else"])
    out = hk.resolve_entry_source([hk.StaleEntry(path=r"D:\Work\Thing")], str(db))
    assert out[0].source == hk.SOURCE_UNKNOWN


def test_source_unknown_when_db_missing_and_scan_still_succeeds(tmp_path):
    entries = [hk.StaleEntry(path=r"D:\Work\Thing")]
    out = hk.resolve_entry_source(entries, str(tmp_path / "nope.db"))
    assert len(out) == 1 and out[0].source == hk.SOURCE_UNKNOWN


def test_source_unknown_when_schema_is_not_what_we_expect(tmp_path):
    """A future tokensave release may move these tables. Degrade, don't crash."""
    db = tmp_path / "global.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE something_else (x TEXT)")
    con.commit()
    con.close()
    out = hk.resolve_entry_source([hk.StaleEntry(path=r"D:\W\T")], str(db))
    assert out[0].source == hk.SOURCE_UNKNOWN


def test_source_unknown_when_file_is_not_a_database(tmp_path):
    db = tmp_path / "global.db"
    db.write_text("definitely not sqlite", encoding="utf-8")
    out = hk.resolve_entry_source([hk.StaleEntry(path=r"D:\W\T")], str(db))
    assert out[0].source == hk.SOURCE_UNKNOWN


# ── Backup matching ───────────────────────────────────────────────────────────

def _pair(root, live_name, backup_name, same=True):
    live = root / live_name
    live.write_text("hello world", encoding="utf-8")
    (root / backup_name).write_text(
        "hello world" if same else "something else entirely", encoding="utf-8")
    return live


@pytest.mark.parametrize("backup_name", [
    "rules.md.bak", "rules.md.backup", "rules.md.backup.1786595846864",
])
def test_supported_backup_patterns(tmp_path, backup_name):
    _pair(tmp_path, "rules.md", backup_name)
    scan = hk.find_redundant_backups([str(tmp_path)])
    assert [os.path.basename(d.path) for d in scan.duplicates] == [backup_name]
    assert scan.kept == 0


def test_ambiguous_name_is_ignored_entirely(tmp_path):
    """`foo.backup.tar.gz` has no single obvious counterpart, so it is skipped.

    Not counted as 'kept' either — it was never a backup candidate.
    """
    _pair(tmp_path, "foo", "foo.backup.tar.gz")
    scan = hk.find_redundant_backups([str(tmp_path)])
    assert scan.duplicates == [] and scan.kept == 0


def test_differing_backup_is_kept_not_offered(tmp_path):
    _pair(tmp_path, "rules.md", "rules.md.bak", same=False)
    scan = hk.find_redundant_backups([str(tmp_path)])
    assert scan.duplicates == []
    assert scan.kept == 1
    assert "different contents" in scan.kept_label


def test_orphaned_backup_is_kept_not_offered(tmp_path):
    (tmp_path / "gone.md.bak").write_text("x", encoding="utf-8")
    scan = hk.find_redundant_backups([str(tmp_path)])
    assert scan.duplicates == [] and scan.kept == 1


def test_size_mismatch_short_circuits_before_hashing(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("short", encoding="utf-8")
    (tmp_path / "a.md.bak").write_text("a much longer body", encoding="utf-8")
    called = []
    monkeypatch.setattr(hk, "_sha256_file",
                        lambda p: called.append(p) or "deadbeef")
    scan = hk.find_redundant_backups([str(tmp_path)])
    assert scan.duplicates == [] and scan.kept == 1
    assert called == []          # never hashed — sizes already proved difference


def test_missing_root_is_skipped_quietly(tmp_path):
    scan = hk.find_redundant_backups([str(tmp_path / "not-there")])
    assert scan.duplicates == [] and scan.kept == 0


# ── Pre-deletion revalidation ─────────────────────────────────────────────────

def test_revalidate_passes_for_an_untouched_pair(tmp_path):
    _pair(tmp_path, "r.md", "r.md.bak")
    cand = hk.find_redundant_backups([str(tmp_path)]).duplicates[0]
    assert hk.revalidate_backup(cand) is True


def test_revalidate_rejects_a_file_changed_since_the_scan(tmp_path):
    """Closes the scan → user waits → file changes → delete race."""
    _pair(tmp_path, "r.md", "r.md.bak")
    cand = hk.find_redundant_backups([str(tmp_path)]).duplicates[0]
    (tmp_path / "r.md.bak").write_text("edited after the scan", encoding="utf-8")
    assert hk.revalidate_backup(cand) is False


def test_revalidate_rejects_when_the_live_file_changed(tmp_path):
    _pair(tmp_path, "r.md", "r.md.bak")
    cand = hk.find_redundant_backups([str(tmp_path)]).duplicates[0]
    (tmp_path / "r.md").write_text("live file edited, no longer a duplicate",
                                   encoding="utf-8")
    assert hk.revalidate_backup(cand) is False


def test_revalidate_rejects_when_the_file_vanished(tmp_path):
    _pair(tmp_path, "r.md", "r.md.bak")
    cand = hk.find_redundant_backups([str(tmp_path)]).duplicates[0]
    os.remove(cand.path)
    assert hk.revalidate_backup(cand) is False


def test_default_roots_are_explicit_and_non_overlapping(fake_home):
    roots = hk.default_backup_roots(str(fake_home))
    assert len(roots) == len(set(roots))
    # NOT a recursive sweep of ~/.claude — that tree holds session state and
    # legitimate backups that are none of our business.
    assert str(fake_home / ".claude") not in roots
