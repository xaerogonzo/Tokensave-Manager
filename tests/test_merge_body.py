"""tests/test_merge_body.py — CHANGELOG [Unreleased] -> merge-commit body.

The risk this feature carries is not a crash, it is a plausible-looking wrong
commit message: [Unreleased] accumulates since the last RELEASE, not since the
last merge, so it can describe work that is not in the PR being merged. The
UI keeps it off by default; these tests pin the conversion itself and the
signal the UI warns with.

The content cases matter because CHANGELOG bullets here routinely contain
backticks, `#`, `&` and `=` — the same characters that break naive shell
quoting and URL-encoded gh calls.
"""
from __future__ import annotations

import pytest

from helpers.merge_body import (
    build_merge_body,
    count_bullets,
    looks_broader_than_pr,
)

_HEAD = "# Changelog\n\n"


def _cl(body: str, released: bool = True) -> str:
    tail = "\n## [2.2.1] — 2026-07-28\n\n### Added\n- older thing\n" if released else ""
    return f"{_HEAD}## [Unreleased]\n\n{body}\n{tail}"


# ── empty / absent ────────────────────────────────────────────────────────────

def test_empty_unreleased_yields_nothing():
    """Empty must be falsy so the caller can fall back to GitHub's default
    rather than writing a blank body over it."""
    assert build_merge_body(_cl("")) == ""


def test_missing_unreleased_anchor_yields_nothing():
    assert build_merge_body("# Changelog\n\n## [2.2.1]\n\n- thing\n") == ""


def test_no_changelog_at_all_yields_nothing():
    assert build_merge_body("") == ""
    assert build_merge_body(None) == ""


# ── section shapes ────────────────────────────────────────────────────────────

def test_single_section():
    out = build_merge_body(_cl("### Added\n- a new thing"))
    assert out == "Added:\n- a new thing"


def test_two_sections_keep_their_order_and_labels():
    out = build_merge_body(
        _cl("### Added\n- new thing\n\n### Fixed\n- broken thing"))
    assert out == "Added:\n- new thing\n\nFixed:\n- broken thing"


def test_headings_lose_their_hashes():
    """A literal '### Added' in a commit body is noise, and a leading '#' is
    read as a comment by some tooling — which would delete the label."""
    out = build_merge_body(_cl("### Added\n- x"))
    assert "#" not in out
    assert out.startswith("Added:")


def test_nested_subheadings_are_indented_not_dropped():
    out = build_merge_body(
        _cl("### Added\n#### Sub-area\n- deep thing"))
    lines = out.splitlines()
    assert lines[0] == "Added:"
    assert lines[1] == "  Sub-area:"
    assert lines[2] == "- deep thing"


def test_nested_bullets_keep_their_indentation():
    out = build_merge_body(_cl("### Added\n- parent\n  - child"))
    assert "\n  - child" in out


def test_bullet_markers_are_normalised():
    out = build_merge_body(_cl("### Added\n* star\n+ plus\n- dash"))
    assert out == "Added:\n- star\n- plus\n- dash"


# ── content preservation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    "uses `--cov-fail-under=14` now",
    "handles & and = and # in one line",
    "matches `a_b` not `axb`",
    "sets `strict_tree: true` in .tokensave/config.json",
    'quotes "double" and \'single\'',
])
def test_shell_hostile_characters_survive_verbatim(payload):
    """These are exactly the characters that break naive quoting, and the
    detail they carry is the reason the feature exists."""
    out = build_merge_body(_cl(f"### Fixed\n- {payload}"))
    assert payload in out


def test_blank_line_runs_are_collapsed_not_multiplied():
    out = build_merge_body(_cl("### Added\n- a\n\n\n\n### Fixed\n- b"))
    assert "\n\n\n" not in out


def test_trailing_blank_lines_are_trimmed():
    out = build_merge_body(_cl("### Added\n- a\n\n\n"))
    assert out == "Added:\n- a"


def test_prose_lines_between_bullets_are_kept():
    out = build_merge_body(
        _cl("### Changed\nSome context sentence.\n- the bullet"))
    assert "Some context sentence." in out


# ── the over-claim guard ──────────────────────────────────────────────────────

def test_count_bullets_counts_nested_ones_too():
    assert count_bullets(_cl("### Added\n- a\n  - a1\n- b")) == 3


def test_warns_when_the_block_dwarfs_the_pr():
    """Nine bullets against a one-commit PR almost certainly includes work
    merged earlier — the misattribution this feature must not do silently."""
    body = "### Added\n" + "\n".join(f"- item {i}" for i in range(9))
    assert looks_broader_than_pr(_cl(body), pr_commit_count=1) is True


def test_does_not_warn_when_sizes_are_comparable():
    body = "### Added\n- one\n- two\n- three"
    assert looks_broader_than_pr(_cl(body), pr_commit_count=3) is False


def test_does_not_warn_on_a_small_overhang():
    """Biased toward silence: a warning on every merge would be ignored."""
    body = "### Added\n- one\n- two\n- three\n- four"
    assert looks_broader_than_pr(_cl(body), pr_commit_count=3) is False


def test_unknown_commit_count_never_warns():
    assert looks_broader_than_pr(_cl("### Added\n- a"), 0) is False
