"""helpers/release_zip.py: a zip that cannot be mistaken for a finished one.

The archive layer is tested with fake `dist/` folders; the part that needs the real build is
`scripts/frozen_smoke.py`'s job. The last two tests tie this module to the other places that
know the same facts (`build.ps1`'s clean config and the files it stages), so a change in one
cannot quietly leave the other behind.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from helpers import release_zip as rz

ROOT = Path(__file__).resolve().parent.parent
VERSION = "2.6.0"


def _dist(tmp_path: Path, *, drop=(), extra=()) -> Path:
    dist = tmp_path / "dist"
    for rel in rz.REQUIRED:
        if rel in drop:
            continue
        path = dist / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" + rel.encode())
    for rel in extra:
        (dist / rel).parent.mkdir(parents=True, exist_ok=True)
        (dist / rel).write_bytes(b"y")
    return dist


def _build(tmp_path, dist=None, **kw):
    return rz.build_release(dist or _dist(tmp_path), VERSION, tmp_path / "out", **kw)


def test_one_versioned_folder_checksums_of_the_final_bytes_and_no_partial(tmp_path):
    seen = []
    final = _build(tmp_path, smoke=lambda extracted: seen.append((str(extracted), (extracted / "tokensave-manager.exe").is_file())) or True)
    assert final.name == "Tokensave-Manager-v2.6.0-windows.zip"
    with zipfile.ZipFile(final) as archive:
        assert {n.split("/")[0] for n in archive.namelist()} == {"Tokensave-Manager-v2.6.0-windows"}
    sums = (tmp_path / "out" / "SHA256SUMS.txt").read_bytes().decode("utf-8")
    assert sums == "%s *%s\n" % (hashlib.sha256(final.read_bytes()).hexdigest(), final.name)
    assert "\r" not in sums and not sums.startswith("﻿")
    assert not list((tmp_path / "out").glob("*.partial"))
    assert seen and " " in seen[0][0] and seen[0][1]  # extracted to a path with a space, with the exe inside


@pytest.mark.parametrize("case", ["missing_exe", "missing_data", "logs", "a_config", "a_migration_record", "smoke"])
def test_a_bad_archive_leaves_nothing_that_looks_final(tmp_path, case):
    dist = {
        "missing_exe": lambda: _dist(tmp_path, drop=("tokensave-wrapper.exe",)),
        "missing_data": lambda: _dist(tmp_path, drop=("templates/project-baseline.md",)),
        "logs": lambda: _dist(tmp_path, extra=("logs/manager.log",)),
        "a_config": lambda: _dist(tmp_path, extra=("manager-config.json",)),
        "a_migration_record": lambda: _dist(tmp_path, extra=("config-migration.json",)),
        "smoke": lambda: _dist(tmp_path),
    }[case]()
    with pytest.raises(rz.ReleaseError):
        _build(tmp_path, dist, smoke=lambda e: case != "smoke")
    out = tmp_path / "out"
    assert not list(out.glob("*.zip")) and not (out / "SHA256SUMS.txt").exists() and not list(out.glob("*.partial"))


def test_build_debris_is_left_out_of_the_archive_rather_than_shipped(tmp_path):
    final = _build(tmp_path, _dist(tmp_path, extra=("templates/__pycache__/x.pyc",)))
    with zipfile.ZipFile(final) as archive:
        assert not any("__pycache__" in n for n in archive.namelist())


@pytest.mark.parametrize("content", ['{"tokensave_exe": "", "mcp_skip_warnings": []}', "{}", '{"tokensave_exe": "D:/mine/tokensave.exe"}'])
def test_a_manager_config_is_refused_whether_it_is_clean_or_someones_real_settings(tmp_path, content):
    """A clean one overwrites real settings on an unzip-over and reads as an old config to import;
    a real one is a leak. Either way it is not in a release."""
    dist = _dist(tmp_path)
    (dist / "manager-config.json").write_text(content, encoding="utf-8")
    with pytest.raises(rz.ReleaseError, match="residue is in the archive: .*manager-config.json"):
        _build(tmp_path, dist)
    assert not list((tmp_path / "out").glob("*.zip"))


def test_manager_config_is_no_longer_a_required_file():
    assert "manager-config.json" not in rz.REQUIRED and "manager-config.json" in rz.FORBIDDEN_PATHS


def test_it_never_replaces_without_force_and_a_failed_rebuild_keeps_the_previous_release(tmp_path):
    first = _build(tmp_path)
    before = first.read_bytes()
    with pytest.raises(rz.ReleaseError, match="force"):
        _build(tmp_path)
    broken = _dist(tmp_path)
    (broken / "README.md").unlink()
    with pytest.raises(rz.ReleaseError, match="missing from the archive"):
        _build(tmp_path, broken, force=True)
    assert first.read_bytes() == before


def test_force_replaces_and_keeps_other_checksum_lines(tmp_path):
    first = _build(tmp_path)
    sums = tmp_path / "out" / "SHA256SUMS.txt"
    sums.write_text(sums.read_text(encoding="utf-8") + "ab" * 32 + " *other-file.exe\n", encoding="utf-8", newline="\n")
    _build(tmp_path, force=True)
    lines = sums.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[1].endswith("*other-file.exe") and lines[0].endswith("*" + first.name)


@pytest.mark.parametrize("version", ["", "unknown", "1.0 0", "1/2", 'a"b'])
def test_an_unusable_version_is_refused(tmp_path, version):
    with pytest.raises(rz.ReleaseError):
        rz.build_release(_dist(tmp_path), version, tmp_path / "out")


def test_a_folder_without_the_gui_exe_is_not_a_build(tmp_path):
    with pytest.raises(rz.ReleaseError, match="no tokensave-manager.exe"):
        rz.build_release(tmp_path, VERSION, tmp_path / "out")


def test_an_entry_outside_the_top_folder_or_escaping_it_is_reported(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        top = rz.top_folder(VERSION)
        for rel in rz.REQUIRED:
            z.writestr("%s/%s" % (top, rel), "x" if rel != "manager-config.json" else "{}")
        z.writestr("stray.txt", "x")
        z.writestr("%s/../escape.txt" % top, "x")
    problems = rz.validate_inventory(bad, VERSION)
    assert any("outside" in p for p in problems) and any("escapes" in p for p in problems)


# ── ties to the other places that know the same facts ───────────────────

def test_the_build_no_longer_writes_or_copies_a_manager_config():
    text = (ROOT / "build.ps1").read_text(encoding="utf-8")
    assert "$cleanConfig" not in text
    stage = text[text.index("=== Staging distribution files ==="):text.index("if ($Release) {", text.index("=== Staging distribution files ==="))]
    assert 'manager-config.json"' not in stage.replace("manager-config.example.json", "")
    assert "manager-config.example.json" in stage  # the annotated template still ships


def test_every_file_the_zip_requires_is_something_the_build_stages():
    text = (ROOT / "build.ps1").read_text(encoding="utf-8")
    for rel in rz.REQUIRED:
        leaf = rel.split("/")[-1]
        assert leaf in text or rel.split("/")[0] in text, "%s is required by the zip but build.ps1 never stages it" % rel
    assert (ROOT / "templates" / "project-baseline.md").is_file() and (ROOT / "docs" / "USER_GUIDE.md").is_file()
