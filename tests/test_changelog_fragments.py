"""`helpers/changelog_fragments.py`: fragments exist so two PRs cannot conflict on CHANGELOG.md.

Everything runs in a temporary root except the last two tests, which check the real
repository and, in a disposable git repo, the one property this exists for.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import changelog_fragments as tool

ROOT = Path(__file__).resolve().parent.parent

CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\n### Added\n- (old) **Old add.** legacy\n\n"
    "### Fixed\n- (old) **Old fix.** legacy\n\n"
    "## [0.1.0] — 2026-01-01\n\n### Added\n- shipped\n"
)
GOOD = "### Added\n- ({area}) **{title}.** did a thing\n"


def _root(tmp_path: Path, text: str = CHANGELOG) -> Path:
    (tmp_path / "CHANGELOG.md").write_bytes(text.encode("utf-8"))
    (tmp_path / "changelog.d").mkdir()
    return tmp_path


def _frag(root: Path, name: str, body: str) -> Path:
    path = root / "changelog.d" / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def _good(root: Path, name: str, title: str, area: str = "x") -> Path:
    return _frag(root, name, GOOD.format(title=title, area=area))


def _text(root: Path) -> str:
    return (root / "CHANGELOG.md").read_bytes().decode("utf-8")


def test_same_slug_twice_gives_two_paths_and_never_overwrites(tmp_path):
    root = _root(tmp_path)
    when = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    paths = {tool.new_fragment(root, "Fix Docs", when) for _ in range(20)}
    assert len(paths) == 20
    assert all(tool.FRAGMENT_NAME.fullmatch(p.name) and p.name.startswith("202610221200-fix-docs-") for p in paths)


def test_a_name_collision_retries_and_never_truncates_the_existing_file(tmp_path, monkeypatch):
    root = _root(tmp_path)
    when = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    taken = _frag(root, "202610221200-x-abcd.md", "precious")
    draws = iter(["abcd", "ef01"])
    monkeypatch.setattr(tool.secrets, "token_hex", lambda n: next(draws))
    made = tool.new_fragment(root, "x", when)
    assert made.name == "202610221200-x-ef01.md" and taken.read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "..", "", "!!!"])
def test_an_unsafe_slug_is_refused(tmp_path, bad):
    with pytest.raises(tool.ChangelogError):
        tool.new_fragment(_root(tmp_path), bad)


def test_the_untouched_template_fails_check(tmp_path):
    root = _root(tmp_path)
    tool.new_fragment(root, "x")
    assert any("placeholder" in e for e in tool.validate(root, CHANGELOG))


def test_readme_is_not_a_fragment(tmp_path):
    root = _root(tmp_path)
    (root / "changelog.d" / "README.md").write_text("not a fragment", encoding="utf-8")
    assert tool.fragment_paths(root) == [] and tool.validate(root, CHANGELOG) == []


@pytest.mark.parametrize(
    "body, expect",
    [
        ("", "empty"),
        ("preamble\n### Added\n- b\n", "first line"),
        ("### Added\n", "no '- ' bullet"),
        ("### Added\n```\n- fake bullet\n```\n", "inside a bullet"),
        ("### Added\n- b\n```\n", "never closed"),
        ("### Added\n- b\n### Added\n- c\n", "appears twice"),
        ("### Features\n- b\n", "unknown category"),
        ("## Added\n- b\n", "heading"),
        ("- b\n", "first line"),
        ("### Added\nstray prose\n- b\n", "outside a bullet"),
    ],
)
def test_each_validation_rule_fails_its_case(tmp_path, body, expect):
    root = _root(tmp_path)
    _frag(root, "202610221200-x-aaaa.md", body)
    assert any(expect in e for e in tool.validate(root, CHANGELOG)), tool.validate(root, CHANGELOG)


def test_a_fence_and_continuation_lines_inside_a_bullet_are_fine(tmp_path):
    root = _root(tmp_path)
    _frag(root, "202610221200-x-aaaa.md", "### Fixed\n- real\n  more words\n```\n### not a title\n```\n")
    assert tool.validate(root, CHANGELOG) == []


def test_duplicate_bullets_across_fragments_are_reported(tmp_path):
    root = _root(tmp_path)
    _good(root, "202610221200-a-aaaa.md", "Same  Title")
    _good(root, "202610221201-b-bbbb.md", "same title")
    errors = tool.validate(root, CHANGELOG)
    assert len(errors) == 1 and "duplicates" in errors[0]


def test_a_bullet_used_in_a_released_section_is_allowed(tmp_path):
    root = _root(tmp_path)
    _frag(root, "202610221200-a-aaaa.md", "### Added\n- shipped\n")
    assert tool.validate(root, CHANGELOG) == []


def test_render_merges_into_existing_groups_newest_first_and_keeps_everything_else(tmp_path):
    root = _root(tmp_path)
    _good(root, "202610221200-a-aaaa.md", "First")
    _good(root, "202610221300-b-bbbb.md", "Second")
    result = tool.build(root, write=False)
    assert [p.name[:12] for p in result.folded] == ["202610221300", "202610221200"]
    text = result.text
    assert text.index("**Second.**") < text.index("**First.**") < text.index("**Old add.**") < text.index("### Fixed")
    assert text.count("### Added") == 2  # the Unreleased group was extended, not duplicated
    assert text.endswith(CHANGELOG.split("## [0.1.0]", 1)[1])
    assert tool.build(root, write=False).text == text  # deterministic


def test_a_missing_category_is_created_in_canonical_order(tmp_path):
    root = _root(tmp_path)
    _frag(root, "202610221200-a-aaaa.md", "### Changed\n- c\n")
    _frag(root, "202610221201-b-bbbb.md", "### Removed\n- r\n")
    text = tool.build(root, write=False).text
    unreleased = text.split("## [0.1.0]")[0]
    assert unreleased.index("### Added") < unreleased.index("### Changed") < unreleased.index("### Fixed")
    assert unreleased.index("### Fixed") < unreleased.index("### Removed")
    assert unreleased.endswith("- r\n\n")  # blank line kept before the next release heading


def test_build_writes_consumes_and_a_second_build_is_a_noop(tmp_path):
    root = _root(tmp_path)
    frag = _good(root, "202610221200-a-aaaa.md", "First")
    preview = tool.build(root, write=False).text
    assert _text(root) == CHANGELOG  # preview wrote nothing
    assert tool.main(["--root", str(root), "build"]) == 0
    assert _text(root) == preview and not frag.exists()
    assert tool.main(["--root", str(root), "build"]) == 0
    assert _text(root) == preview


def test_crlf_changelog_stays_crlf(tmp_path):
    root = _root(tmp_path, CHANGELOG.replace("\n", "\r\n"))
    _good(root, "202610221200-a-aaaa.md", "First")
    text = tool.build(root, write=False).text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_an_invalid_fragment_stops_build_and_consumes_nothing(tmp_path):
    root = _root(tmp_path)
    good = _good(root, "202610221200-a-aaaa.md", "First")
    _frag(root, "202610221201-b-bbbb.md", "### Added\n")
    assert tool.main(["--root", str(root), "build"]) == 1
    assert good.exists() and _text(root) == CHANGELOG


def test_a_failed_write_consumes_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path)
    frag = _good(root, "202610221200-a-aaaa.md", "First")
    monkeypatch.setattr(tool.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        tool.build(root, write=True)
    assert frag.exists() and _text(root) == CHANGELOG
    assert not list(root.glob(".CHANGELOG.*.tmp"))


def test_a_deletion_that_fails_midway_is_recovered_by_a_rerun_without_duplicating(tmp_path, monkeypatch):
    root = _root(tmp_path)
    a = _good(root, "202610221200-a-aaaa.md", "First")
    b = _good(root, "202610221300-b-bbbb.md", "Second")
    real_unlink = Path.unlink

    def flaky(self, *args, **kwargs):
        if self.name == b.name:
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(tool.ChangelogError) as caught:
        tool.build(root, write=True)
    assert b.name in str(caught.value) and "Run build again" in str(caught.value)
    assert not a.exists() and b.exists()
    after_first = _text(root)
    assert after_first.count("**Second.**") == 1

    monkeypatch.undo()
    assert tool.validate(root, after_first) == []  # a stale fragment is not an error
    assert tool.main(["--root", str(root), "build"]) == 0
    assert _text(root) == after_first and not b.exists()  # removed, not inserted twice


def test_a_partly_folded_fragment_is_an_error_not_a_guess(tmp_path):
    root = _root(tmp_path)
    _frag(root, "202610221200-a-aaaa.md", "### Added\n- (old) **Old add.** legacy\n- brand new\n")
    errors = tool.validate(root, CHANGELOG)
    assert len(errors) == 1 and "some bullets are already" in errors[0]


def test_no_pending_gate(tmp_path):
    root = _root(tmp_path)
    assert tool.main(["--root", str(root), "check", "--no-pending"]) == 0
    _good(root, "202610221200-a-aaaa.md", "First")
    assert tool.main(["--root", str(root), "check"]) == 0
    assert tool.main(["--root", str(root), "check", "--no-pending"]) == 1


def test_a_missing_or_doubled_unreleased_heading_is_refused(tmp_path):
    assert tool.main(["--root", str(_root(tmp_path, "# C\n## [0.1.0]\n- x\n")), "check"]) == 1


def test_the_launcher_does_not_depend_on_the_working_directory(tmp_path):
    (tmp_path / "repo").mkdir()
    root = _root(tmp_path / "repo")
    _good(root, "202610221200-a-aaaa.md", "First")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "changelog.py"), "--root", str(root), "check"],
        cwd=elsewhere, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr


def test_the_real_repository_has_only_valid_fragments():
    assert tool.validate(ROOT, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")) == []


# --- the property this exists for ------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=cwd, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_two_branches_adding_fragments_merge_cleanly_and_two_editing_unreleased_do_not(tmp_path):
    (tmp_path / "r").mkdir()
    repo = _root(tmp_path / "r")
    _git(repo, "init", "-q", "-b", "main")
    (repo / "changelog.d" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-qm", "base").returncode == 0

    def branch(name: str, edit):
        _git(repo, "checkout", "-q", "-b", name, "main")
        edit()
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-qm", name).returncode == 0

    def old_style(title):
        def go():
            path = repo / "CHANGELOG.md"
            path.write_bytes(path.read_bytes().replace(b"### Added\n", f"### Added\n- {title}\n".encode(), 1))
        return go

    branch("frag-a", lambda: tool.new_fragment(repo, "same-slug"))
    branch("frag-b", lambda: tool.new_fragment(repo, "same-slug"))
    branch("old-a", old_style("A"))
    branch("old-b", old_style("B"))

    _git(repo, "checkout", "-q", "frag-a")
    assert _git(repo, "merge", "-q", "--no-edit", "frag-b").returncode == 0
    assert len(tool.fragment_paths(repo)) == 2
    _git(repo, "checkout", "-q", "old-a")
    assert _git(repo, "merge", "-q", "--no-edit", "old-b").returncode != 0  # the conflict this removes
