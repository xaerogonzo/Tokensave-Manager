"""docs/RELEASE_CONTENTS.md is a guard, not a description written once.

It fails when a tracked top-level entry has no row (a new folder either ships by accident or
stays out by accident), when a "No" entry is staged by `build.ps1`, when a "Yes" entry is not,
and when a file the zip check requires is missing from the page.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import frozen_smoke as fs
from helpers import release_zip as rz

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "RELEASE_CONTENTS.md").read_text(encoding="utf-8")
BUILD = (ROOT / "build.ps1").read_text(encoding="utf-8")
# Only this part of the script decides what lands in dist\: the rest builds, judges and packages. The
# whole file also names `scripts\...` (commands it RUNS), which must not read as "staged".
_STAGE_FROM = BUILD.index("=== Staging distribution files ===")
STAGING = BUILD[_STAGE_FROM:BUILD.index("if ($Release) {", _STAGE_FROM)]


def _section(title: str) -> str:
    start = DOC.index("## " + title)
    rest = DOC[start + 3:]
    nxt = re.search(r"(?m)^## ", rest)
    return rest[: nxt.start()] if nxt else rest


def _rows(section: str) -> "list[list[str]]":
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|\s*-", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]  # header


def _names(cell: str) -> "list[str]":
    return re.findall(r"`([^`]+)`", cell)


def _tracked_top_level() -> "set[str]":
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return {line.split("/")[0] for line in out.splitlines() if line}


needs_git = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")


@needs_git
def test_every_tracked_top_level_entry_has_a_row_and_every_row_is_tracked():
    rows = _rows(_section("The repository's top level"))
    named = {_names(r[0])[0] for r in rows}
    assert named == _tracked_top_level(), (
        "rows without an entry: %s; entries without a row: %s"
        % (sorted(named - _tracked_top_level()), sorted(_tracked_top_level() - named)))


def test_a_no_row_is_never_staged_and_a_yes_row_always_is():
    for row in _rows(_section("The repository's top level")):
        name, ships = _names(row[0])[0], row[1]
        named = ('$ROOT\\%s' % name) in STAGING or ('"%s"' % name) in STAGING
        if ships == "No":
            assert not named, "%s is marked No but the staging section of build.ps1 names it" % name
        elif ships in ("Yes", "Partial"):
            assert named, "%s is marked %s but the staging section of build.ps1 never names it" % (name, ships)


def test_every_file_the_zip_requires_is_on_the_page():
    inside = _section("Inside the zip")
    for rel in rz.REQUIRED + ("LICENSE",):
        assert rel.split("/")[0] in inside or rel in inside, "%s is required by the zip but not on the page" % rel


def test_the_licence_is_required_staged_and_listed():
    assert "LICENSE" in rz.REQUIRED
    assert '"LICENSE"' in BUILD
    assert (ROOT / "LICENSE").is_file()


def test_the_three_executables_are_on_the_page_and_agree_with_the_smoke_harness():
    inside = _section("Inside the zip")
    for exe in fs.ALL_EXES:
        assert "`%s`" % exe in inside


def test_the_version_resource_is_stamped_from_the_one_version_source():
    assert 'release_info.py" version' in BUILD
    for flag in ("--product-name=", "--product-version=", "--file-version=", "--company-name=", "--file-description="):
        assert flag in BUILD, "build.ps1 no longer passes %s" % flag
    assert "$VERSION.0" in BUILD


def test_release_refuses_a_dirty_tree_with_no_override_and_an_ordinary_build_only_warns():
    assert "-Release refuses a working tree" in BUILD
    guard = BUILD[BUILD.index("function Assert-CleanTree"):]
    guard = guard[: guard.index("\n}\n")]
    assert guard.index("if ($Release)") < guard.index("-not $AllowDirty")  # Release decides before AllowDirty is read
    assert "throw" in guard[: guard.index("-not $AllowDirty")]
    assert "throw" not in guard[guard.index("-not $AllowDirty"):]


def test_the_release_path_runs_the_smoke_before_it_packages():
    body = BUILD[BUILD.index("if ($Release) {\n    Write-Host"):]
    assert body.index("frozen_smoke.py") < body.index("make_release_zip.py")
    assert body.count("throw") >= 2


def test_release_checks_dist_for_user_files_before_it_compiles_and_deletes_nothing():
    fn = BUILD[BUILD.index("function Assert-DistHoldsNoUserFiles"):]
    fn = fn[: fn.index("\n}\n")]
    for name in ("manager-config.json", "config-migration.json", "logs"):
        assert name in fn
    assert "if (-not $Release) { return }" in fn
    assert "throw" in fn and "Nothing was deleted" in fn
    assert "Remove-Item" not in fn
    call = BUILD.index("\nAssert-DistHoldsNoUserFiles\n")
    assert call < BUILD.index('\nBuild-Exe "')   # asked before the first 25-minute compile

