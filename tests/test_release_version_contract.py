"""One version number, and everything that restates it agrees.

`constants.APP_VERSION` is the single source (it cannot come from `git describe`: the Nuitka
onefile build ships without a repository). Several things restate it and each has drifted or
could: the extension's `package.json`, the CHANGELOG section a release's notes come from, the
tag, and the version resource of the built executables. The last is checked against the real
build by `scripts/frozen_smoke.py`; the rest are held here.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from constants import APP_VERSION

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_info_script", ROOT / "scripts" / "release_info.py")
info = importlib.util.module_from_spec(_spec)
sys.modules["release_info_script"] = info
_spec.loader.exec_module(info)


def test_app_version_is_a_plain_dotted_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)


def test_the_release_script_reads_the_same_number_the_app_does():
    assert info.project_version() == APP_VERSION


def test_the_extensions_package_json_carries_the_same_version():
    package = json.loads((ROOT / "vscode-extension" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == APP_VERSION, (
        "vscode-extension/package.json says %s but constants.APP_VERSION is %s; the release "
        "workflow's gate fails on this at publish time" % (package["version"], APP_VERSION))


def test_the_current_version_has_release_notes_in_the_changelog():
    """A version number with no section is a release whose notes say nothing about it."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert info.release_notes(text, APP_VERSION).strip(), "CHANGELOG.md has no notes for %s" % APP_VERSION


def test_only_the_matching_tag_is_a_release_tag():
    assert info.check_tag("v" + APP_VERSION, APP_VERSION) == APP_VERSION
    other = "v%d.0.0" % (int(APP_VERSION.split(".")[0]) + 1)
    for bad in (other, APP_VERSION, "v" + APP_VERSION + "-rc1", "latest"):
        with pytest.raises(info.ReleaseInfoError):
            info.check_tag(bad, APP_VERSION)


def test_unreleased_is_not_a_version_with_notes():
    with pytest.raises(info.ReleaseInfoError):
        info.release_notes("# C\n\n## [Unreleased]\n- x\n", "Unreleased")


def test_the_version_source_is_where_the_script_and_the_runbook_say_it_is():
    assert info.VERSION_FILE == "src/constants.py"
    assert re.search(r'(?m)^APP_VERSION = "\d+\.\d+\.\d+"', (ROOT / "src" / "constants.py").read_text(encoding="utf-8"))


# ── the release body cap ──────────────────────────────────────────────────

def test_the_helper_and_the_standalone_script_agree_on_githubs_release_body_cap():
    from helpers.release import RELEASE_NOTES_LIMIT
    assert info.NOTES_LIMIT == RELEASE_NOTES_LIMIT == 125_000


def test_notes_over_the_cap_are_refused_and_exactly_the_cap_is_not():
    head = "# C\n\n## [1.2.3] — d\n\n"
    ok = head + "x" * (info.NOTES_LIMIT - 1) + "\n\n## [1.2.2]\n- old\n"      # the returned text adds one newline
    assert len(info.release_notes(ok, "1.2.3")) == info.NOTES_LIMIT
    with pytest.raises(info.ReleaseInfoError, match="125,000"):
        info.release_notes(ok.replace("x" * (info.NOTES_LIMIT - 1), "x" * info.NOTES_LIMIT), "1.2.3")


def test_a_detail_section_under_its_own_heading_is_outside_the_release_notes():
    big = "x" * (info.NOTES_LIMIT * 2)
    text = "# C\n\n## [Unreleased]\n\n## [1.2.3] — d\n- summary\n\n## [Full detail of this release]\n%s\n\n## [1.2.2]\n- old\n" % big
    assert info.release_notes(text, "1.2.3") == "- summary\n"
