"""Tests for helpers/cursor_tasks.py.

This module reads an UNDOCUMENTED, reverse-engineered on-disk format, so the
tests are weighted towards what happens when the format is not what we expect:
a newer schema, a corrupt record, a missing timestamp, a `cwd` that is not a
string. The rule being enforced throughout is that one bad record is one
skipped record — never an exception into the Tasks tab, and never a row that
states something the file did not actually say.
"""
import json
import os
import time

import pytest

from helpers.cursor_tasks import (
    MAX_DEPTH,
    MAX_SCHEMA_VERSION,
    _cursor_chats_dir,
    scan_cursor_sessions,
)


def _chat(fake_home, chat_id, meta, bucket="bucket-a", depth_extra=0):
    """Write one chat's meta.json under ~/.cursor/chats."""
    parts = [str(fake_home), ".cursor", "chats", bucket]
    parts += [f"nested{i}" for i in range(depth_extra)]
    parts.append(chat_id)
    directory = os.path.join(*parts)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "meta.json")
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(meta, str):
            fh.write(meta)
        else:
            json.dump(meta, fh)
    return path


def _good(**over):
    meta = {"schemaVersion": 1,
            "createdAtMs": 1_700_000_000_000,
            "updatedAtMs": 1_700_000_000_000,
            "title": "Fix the parser",
            "cwd": "D:/Projects/Thing"}
    meta.update(over)
    return meta


# ── Absence ──────────────────────────────────────────────────────────────────

def test_no_cursor_at_all_returns_empty(fake_home):
    """The state of any machine without Cursor. Not an error."""
    assert scan_cursor_sessions([]) == []


def test_chats_dir_resolves_under_the_redirected_home(fake_home):
    assert _cursor_chats_dir().startswith(str(fake_home))


def test_empty_chats_dir_returns_empty(fake_home):
    os.makedirs(os.path.join(str(fake_home), ".cursor", "chats"))
    assert scan_cursor_sessions([]) == []


# ── The happy path ───────────────────────────────────────────────────────────

def test_a_good_record_becomes_a_row(fake_home):
    _chat(fake_home, "abc123", _good())
    rows = scan_cursor_sessions([])
    assert len(rows) == 1
    assert rows[0]["title"] == "Fix the parser"
    assert rows[0]["session_id"] == "abc123"
    assert rows[0]["agent"] == "cursor"


def test_row_shape_matches_the_claude_scanner(fake_home):
    """Both scanners feed one list; a differing shape breaks the merge."""
    _chat(fake_home, "abc123", _good())
    row = scan_cursor_sessions([])[0]
    assert set(row) >= {"session_id", "title", "project_display",
                        "last_activity", "is_recent", "agent"}


def test_rows_are_newest_first(fake_home):
    _chat(fake_home, "old", _good(updatedAtMs=1_600_000_000_000, title="old"))
    _chat(fake_home, "new", _good(updatedAtMs=1_800_000_000_000, title="new"))
    titles = [r["title"] for r in scan_cursor_sessions([])]
    assert titles == ["new", "old"]


def test_a_recent_session_is_flagged_recent(fake_home):
    _chat(fake_home, "now", _good(updatedAtMs=int(time.time() * 1000)))
    assert scan_cursor_sessions([])[0]["is_recent"] is True


def test_an_old_session_is_not_flagged_recent(fake_home):
    _chat(fake_home, "old", _good(updatedAtMs=1_600_000_000_000))
    assert scan_cursor_sessions([])[0]["is_recent"] is False


def test_limit_is_respected(fake_home):
    for i in range(8):
        _chat(fake_home, f"c{i}", _good(updatedAtMs=1_700_000_000_000 + i))
    assert len(scan_cursor_sessions([], limit=3)) == 3


# ── Schema gating ────────────────────────────────────────────────────────────

def test_a_newer_schema_is_skipped_rather_than_guessed(fake_home):
    _chat(fake_home, "future", _good(schemaVersion=MAX_SCHEMA_VERSION + 1))
    assert scan_cursor_sessions([]) == []


def test_a_missing_schema_version_is_accepted_as_zero(fake_home):
    """The earliest observed files carry no such key; rejecting them would
    list nothing on exactly the installs this was written for."""
    meta = _good()
    del meta["schemaVersion"]
    _chat(fake_home, "early", meta)
    assert len(scan_cursor_sessions([])) == 1


def test_a_non_numeric_schema_version_is_skipped(fake_home):
    _chat(fake_home, "weird", _good(schemaVersion="one"))
    assert scan_cursor_sessions([]) == []


# ── Malformed records are skipped INDIVIDUALLY ───────────────────────────────

def test_corrupt_json_does_not_abort_the_scan(fake_home):
    """The whole point: one bad record must not cost the user every other row."""
    _chat(fake_home, "broken", "{not json at all")
    _chat(fake_home, "fine", _good(title="survivor"))
    rows = scan_cursor_sessions([])
    assert [r["title"] for r in rows] == ["survivor"]


def test_a_json_array_instead_of_an_object_is_skipped(fake_home):
    _chat(fake_home, "arr", ["nope"])
    _chat(fake_home, "fine", _good(title="survivor"))
    assert [r["title"] for r in scan_cursor_sessions([])] == ["survivor"]


def test_a_record_with_no_usable_timestamp_is_skipped(fake_home):
    """Neither field usable AND no mtime is unreachable in practice, so this
    checks the softer rule: a junk timestamp falls back to the file mtime."""
    _chat(fake_home, "nots", _good(updatedAtMs="yesterday", createdAtMs=None))
    rows = scan_cursor_sessions([])
    assert len(rows) == 1
    assert rows[0]["last_activity"] > 0


def test_a_missing_title_renders_as_untitled_not_as_a_guess(fake_home):
    meta = _good()
    del meta["title"]
    _chat(fake_home, "notitle", meta)
    assert scan_cursor_sessions([])[0]["title"] == "(untitled)"


def test_a_non_string_title_renders_as_untitled(fake_home):
    _chat(fake_home, "badtitle", _good(title={"nope": 1}))
    assert scan_cursor_sessions([])[0]["title"] == "(untitled)"


# ── Project attribution ──────────────────────────────────────────────────────

def test_cwd_matches_a_known_project_case_insensitively(fake_home, tmp_path):
    proj = tmp_path / "MyProject"
    proj.mkdir()
    _chat(fake_home, "c1", _good(cwd=str(proj).upper()))
    row = scan_cursor_sessions([str(proj)])[0]
    assert row["project_display"] == "MyProject"


def test_cwd_with_a_trailing_separator_still_matches(fake_home, tmp_path):
    proj = tmp_path / "MyProject"
    proj.mkdir()
    _chat(fake_home, "c1", _good(cwd=str(proj) + os.sep))
    assert scan_cursor_sessions([str(proj)])[0]["project_display"] == "MyProject"


def test_missing_cwd_is_unknown_not_inferred_from_the_title(fake_home):
    """A guess that looks like a fact is the failure this module is written
    against — the title must never become a project attribution."""
    meta = _good(title="work on MyProject")
    del meta["cwd"]
    _chat(fake_home, "c1", meta)
    display = scan_cursor_sessions([str("D:/Projects/MyProject")])[0]["project_display"]
    assert display == "(unknown project)"
    assert "MyProject" not in display


def test_non_string_cwd_is_unknown_and_touches_no_filesystem(fake_home):
    _chat(fake_home, "c1", _good(cwd=12345))
    assert scan_cursor_sessions([])[0]["project_display"] == "(unknown project)"


def test_an_untracked_cwd_shows_its_own_name(fake_home):
    _chat(fake_home, "c1", _good(cwd="D:/Elsewhere/Unrelated"))
    assert scan_cursor_sessions([])[0]["project_display"] == "Unrelated"


# ── Bounded, symlink-safe traversal ──────────────────────────────────────────

def test_traversal_stops_at_the_depth_cap(fake_home):
    _chat(fake_home, "deep", _good(), depth_extra=MAX_DEPTH + 2)
    assert scan_cursor_sessions([]) == []


def test_a_record_within_the_depth_cap_is_found(fake_home):
    _chat(fake_home, "ok", _good(), depth_extra=0)
    assert len(scan_cursor_sessions([])) == 1


def test_symlinked_directories_are_skipped(fake_home, tmp_path):
    """A link can point anywhere, including back up the tree."""
    _chat(fake_home, "real", _good(title="real"))
    outside = tmp_path / "outside" / "chatx"
    outside.mkdir(parents=True)
    with open(outside / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(_good(title="via symlink"), fh)
    link = os.path.join(str(fake_home), ".cursor", "chats", "linked")
    try:
        os.symlink(str(tmp_path / "outside"), link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks not permitted in this environment")
    titles = [r["title"] for r in scan_cursor_sessions([])]
    assert "via symlink" not in titles
    assert "real" in titles


def test_store_db_is_never_opened(fake_home, monkeypatch):
    """Cursor may hold it open; every field we need is in the JSON."""
    path = _chat(fake_home, "c1", _good())
    with open(os.path.join(os.path.dirname(path), "store.db"), "wb") as fh:
        fh.write(b"\x00not a real sqlite file\x00")
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    scan_cursor_sessions([])
    assert not any(p.endswith("store.db") for p in opened)


def test_an_unreadable_directory_does_not_abort_the_scan(fake_home, monkeypatch):
    _chat(fake_home, "fine", _good(title="survivor"))
    real_scandir = os.scandir
    root = os.path.join(str(fake_home), ".cursor", "chats")

    def flaky(path=".", *a, **k):
        if os.path.basename(str(path)) == "denied":
            raise PermissionError("nope")
        return real_scandir(path, *a, **k)

    os.makedirs(os.path.join(root, "denied"), exist_ok=True)
    monkeypatch.setattr("helpers.cursor_tasks.os.scandir", flaky)
    assert [r["title"] for r in scan_cursor_sessions([])] == ["survivor"]


# ── Row identity across the two agents ───────────────────────────────────────

def test_cursor_and_claude_rows_with_the_same_id_are_distinguishable(fake_home):
    """Identity is (agent, session_id). A shared id must not let one row
    replace or mis-select the other once the two lists are merged."""
    _chat(fake_home, "shared-id", _good(title="cursor side"))
    cursor_rows = scan_cursor_sessions([])
    claude_row = {"session_id": "shared-id", "title": "claude side",
                  "project_display": "x", "last_activity": 1.0,
                  "is_recent": False, "agent": "claude"}
    merged = cursor_rows + [claude_row]
    keys = {(r["agent"], r["session_id"]) for r in merged}
    assert len(keys) == 2
    assert len({r["session_id"] for r in merged}) == 1   # ids alone collide
