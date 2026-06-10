"""tests/test_shadow_links.py — helpers/shadow_links.py (pure logic).

Covers the R9-SL1 persistence helpers (load/save_shadow_map) and the
R9-SL4 hardlink probe (supports_hardlinks). The pre-existing generation
functions get a hardlink round-trip test as a bonus baseline.
"""
from __future__ import annotations

import json
import os

from helpers.shadow_links import (
    generate_shadow_links,
    load_shadow_map,
    save_shadow_map,
    shadow_map_path,
    supports_hardlinks,
)


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
