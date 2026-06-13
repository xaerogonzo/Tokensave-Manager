"""tests/test_shadow_links.py — helpers/shadow_links.py (pure logic).

Covers the R9-SL1 persistence helpers (load/save_shadow_map), the
R9-SL4 hardlink probe (supports_hardlinks), the R9-SL5 scanner
(indexed_extensions / suggest_shadow_candidates / ai_suggest_suffixes),
and the R9-SL6 default-map expansion. The pre-existing generation
functions get a hardlink round-trip test as a bonus baseline.
"""
from __future__ import annotations

import json
import os
import sqlite3
import types

from helpers.shadow_links import (
    DEFAULT_SHADOW_EXT_MAP,
    ai_suggest_suffixes,
    generate_shadow_links,
    indexed_extensions,
    load_shadow_map,
    save_shadow_map,
    shadow_map_path,
    suggest_shadow_candidates,
    supports_hardlinks,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_index(root, entries):
    """Create a stub .tokensave/tokensave.db with a files table.

    entries: list of (path, node_count).
    """
    db_dir = os.path.join(root, ".tokensave")
    os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(os.path.join(db_dir, "tokensave.db"))
    con.execute(
        "CREATE TABLE files (path TEXT, content_hash TEXT, size INT, "
        "modified_at INT, indexed_at INT, node_count INT)")
    con.executemany(
        "INSERT INTO files (path, node_count) VALUES (?, ?)", entries)
    con.commit()
    con.close()


def _cfg(provider="ollama", **extra):
    raw = {"ask_tab_llm": {"provider": provider, **extra}}
    return types.SimpleNamespace(raw=raw, claude_cli_exe="")


# ── supports_hardlinks (R9-SL4) ──────────────────────────────────────────

def test_supports_hardlinks_true_on_real_fs(tmp_path):
    """NTFS (Windows dev) and ext4 (Linux CI) both support hardlinks."""
    assert supports_hardlinks(str(tmp_path)) is True


def test_supports_hardlinks_cleans_up_probe_files(tmp_path):
    supports_hardlinks(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_supports_hardlinks_false_when_link_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("helpers.shadow_links.os.link",
                        lambda *a: (_ for _ in ()).throw(OSError("nope")))
    assert supports_hardlinks(str(tmp_path)) is False
    assert list(tmp_path.iterdir()) == []      # probe still cleaned up


def test_supports_hardlinks_false_on_unwritable_path():
    assert supports_hardlinks("Z:/definitely/not/here") is False


# ── load/save_shadow_map (R9-SL1) ────────────────────────────────────────

def test_save_load_round_trip(tmp_path):
    root = str(tmp_path)
    ext_map = {".zsc": ".cpp", "DECORATE": ".cpp", ".gd": ".py"}
    p = save_shadow_map(root, ext_map)
    assert p == shadow_map_path(root)
    assert load_shadow_map(root) == ext_map


def test_load_missing_returns_none(tmp_path):
    assert load_shadow_map(str(tmp_path)) is None


def test_load_invalid_json_returns_none(tmp_path):
    p = shadow_map_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert load_shadow_map(str(tmp_path)) is None


def test_load_non_dict_ext_map_returns_none(tmp_path):
    p = shadow_map_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"ext_map": [".zsc", ".cpp"]}, fh)
    assert load_shadow_map(str(tmp_path)) is None


def test_load_filters_entries_without_dot_suffix(tmp_path):
    p = shadow_map_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"ext_map": {".zsc": ".cpp", ".bad": "cpp", "": ".c"}}, fh)
    assert load_shadow_map(str(tmp_path)) == {".zsc": ".cpp"}


def test_load_all_entries_invalid_returns_none(tmp_path):
    p = shadow_map_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"ext_map": {".zsc": "no-dot"}}, fh)
    assert load_shadow_map(str(tmp_path)) is None


def test_save_failure_returns_empty_string(tmp_path, monkeypatch):
    monkeypatch.setattr("helpers.shadow_links.os.makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert save_shadow_map(str(tmp_path), {".zsc": ".cpp"}) == ""


# ── generation baseline (hardlink round trip) ────────────────────────────

def test_generate_creates_hardlink_for_mapped_extension(tmp_path):
    src = tmp_path / "Blood.zsc"
    src.write_text("class Foo {}", encoding="utf-8")
    created, skipped, failed = generate_shadow_links(
        str(tmp_path), {".zsc": ".cpp"})
    assert (created, skipped, failed) == (1, 0, 0)
    shadow = tmp_path / "Blood.zsc.cpp"
    assert shadow.is_file()
    assert shadow.read_text(encoding="utf-8") == "class Foo {}"
    # Second run: idempotent.
    created2, skipped2, _ = generate_shadow_links(
        str(tmp_path), {".zsc": ".cpp"})
    assert (created2, skipped2) == (0, 1)


# ── R9-SL6: default-map expansion ────────────────────────────────────────

def test_gzdoom_lumps_in_default_map():
    for lump in ("MAPINFO", "ZMAPINFO", "SNDINFO", "SBARINFO", "LANGUAGE",
                 "GLDEFS", "ANIMDEFS", "MENUDEF", "CVARINFO", "KEYCONF"):
        assert DEFAULT_SHADOW_EXT_MAP.get(lump) == ".cpp", lump
    # Originals untouched.
    assert DEFAULT_SHADOW_EXT_MAP[".zsc"] == ".cpp"
    assert DEFAULT_SHADOW_EXT_MAP["DECORATE"] == ".cpp"


def test_lump_generates_shadow_by_exact_name(tmp_path):
    (tmp_path / "MAPINFO").write_text("map E1M1 {}", encoding="utf-8")
    (tmp_path / "mapinfo_lower").write_text("noise", encoding="utf-8")
    created, _, failed = generate_shadow_links(
        str(tmp_path), {"MAPINFO": ".cpp"})
    assert (created, failed) == (1, 0)
    assert (tmp_path / "MAPINFO.cpp").is_file()


# ── R9-SL5: indexed_extensions ───────────────────────────────────────────

def test_indexed_extensions_none_without_db(tmp_path):
    assert indexed_extensions(str(tmp_path)) is None


def test_indexed_extensions_parses_files_table(tmp_path):
    _make_index(str(tmp_path), [
        ("src/app.py", 12), ("README.md", 3),
        ("weird.xyz", 0),            # tracked but no nodes → NOT indexed
    ])
    exts = indexed_extensions(str(tmp_path))
    assert exts == {".py", ".md"}    # .xyz excluded (node_count 0)


def test_indexed_extensions_corrupt_db_returns_none(tmp_path):
    db_dir = tmp_path / ".tokensave"
    db_dir.mkdir()
    (db_dir / "tokensave.db").write_text("not a database", encoding="utf-8")
    assert indexed_extensions(str(tmp_path)) is None


# ── R9-SL5: suggest_shadow_candidates ────────────────────────────────────

def test_suggest_candidates_surfaces_unindexed(tmp_path):
    _make_index(str(tmp_path), [("a.py", 5)])
    for i in range(3):
        (tmp_path / f"f{i}.gd").write_text("x", encoding="utf-8")
    (tmp_path / "one.uc").write_text("x", encoding="utf-8")
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")     # indexed
    (tmp_path / "Blood.zsc.cpp").write_text("x", encoding="utf-8")  # a suffix
    out = suggest_shadow_candidates(str(tmp_path), {".zsc": ".cpp"})
    d = dict(out)
    assert d.get(".gd") == 3
    assert d.get(".uc") == 1
    assert ".py" not in d        # indexed
    assert ".cpp" not in d       # shadow suffix
    assert ".zsc" not in d       # already mapped
    # Sorted by count desc.
    assert out[0][0] == ".gd"


def test_suggest_candidates_empty_without_index(tmp_path):
    (tmp_path / "f.gd").write_text("x", encoding="utf-8")
    assert suggest_shadow_candidates(str(tmp_path), {}) == []


def test_suggest_candidates_skips_walk_dirs(tmp_path):
    _make_index(str(tmp_path), [("a.py", 5)])
    vendored = tmp_path / "node_modules"
    vendored.mkdir()
    (vendored / "dep.gd").write_text("x", encoding="utf-8")
    assert suggest_shadow_candidates(str(tmp_path), {}) == []


# ── R9-SL5: ai_suggest_suffixes ──────────────────────────────────────────

def test_ai_suggest_empty_when_no_provider(tmp_path):
    cfg = types.SimpleNamespace(raw={"ask_tab_llm": {}}, claude_cli_exe="")
    assert ai_suggest_suffixes(cfg, [".gd"]) == {}


def test_ai_suggest_empty_for_no_exts():
    assert ai_suggest_suffixes(_cfg(), []) == {}


def test_ai_suggest_parses_whitelisted_reply(monkeypatch):
    monkeypatch.setattr("helpers.llm._call_llm",
                        lambda *a, **k: ".gd = .py\n.uc = .cpp\n")
    out = ai_suggest_suffixes(_cfg(), [".gd", ".uc"])
    assert out == {".gd": ".py", ".uc": ".cpp"}


def test_ai_suggest_drops_off_whitelist_suffix(monkeypatch):
    monkeypatch.setattr("helpers.llm._call_llm",
                        lambda *a, **k: ".gd = .gdscript\n.uc = .cpp\n")
    out = ai_suggest_suffixes(_cfg(), [".gd", ".uc"])
    assert ".gd" not in out          # .gdscript not a known target
    assert out == {".uc": ".cpp"}


def test_ai_suggest_ignores_unrequested_exts(monkeypatch):
    monkeypatch.setattr("helpers.llm._call_llm",
                        lambda *a, **k: ".gd = .py\n.foo = .cpp\n")
    out = ai_suggest_suffixes(_cfg(), [".gd"])
    assert out == {".gd": ".py"}     # .foo wasn't asked for


def test_ai_suggest_empty_on_llm_failure(monkeypatch):
    monkeypatch.setattr("helpers.llm._call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert ai_suggest_suffixes(_cfg(), [".gd"]) == {}
