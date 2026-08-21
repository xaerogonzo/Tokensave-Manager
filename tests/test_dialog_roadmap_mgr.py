"""tests/test_dialog_roadmap_mgr.py — the similarity floor, and the skeleton.

The Ship tab proposes promoting 🟡 roadmap entries to ✅ by matching them
against `[Unreleased]` changelog bullets with a Jaccard score. Everything it
does goes through ProposalBridge, so a bad match cannot write on its own —
but it can pre-tick a checkbox, and a pre-ticked wrong answer is the kind a
user approves without reading.

So what is pinned here is the *floor*: near-identical wording matches, and
two unrelated sentences that happen to share ordinary English words do not.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.roadmap_mgr import _JACCARD_FLOOR, _jaccard, RoadmapManagerDialog


# ── the similarity measure ──────────────────────────────────────────────

def test_identical_strings_score_one():
    assert _jaccard("add a widget", "add a widget") == 1.0


def test_case_is_ignored():
    """Changelog bullets and roadmap titles rarely agree on capitalisation."""
    assert _jaccard("Add A Widget", "add a widget") == 1.0


def test_disjoint_strings_score_zero():
    assert _jaccard("alpha beta", "gamma delta") == 0.0


def test_two_empty_strings_are_treated_as_identical():
    """Avoids a divide-by-zero, and an empty title matches nothing real."""
    assert _jaccard("", "") == 1.0


def test_an_empty_string_never_matches_a_real_one():
    assert _jaccard("", "add a widget") == 0.0


def test_word_order_does_not_matter():
    assert _jaccard("widget add", "add widget") == 1.0


# ── the floor is where the pre-ticking decision happens ─────────────────

def test_a_reworded_bullet_still_clears_the_floor():
    """The case the feature exists for: same work, different phrasing."""
    entry = "multi-remote push support"
    bullet = "multi-remote push support for GitLab"
    assert _jaccard(entry, bullet) >= _JACCARD_FLOOR


def test_two_unrelated_items_sharing_filler_words_do_not_clear_it():
    """"the", "a", "for" are everywhere in a changelog.

    A floor low enough to match on those would pre-tick promotions the user
    never made, and pre-ticked wrong answers are the ones that get approved
    unread.
    """
    entry = "add the release wizard"
    bullet = "fix the login redirect"
    assert _jaccard(entry, bullet) < _JACCARD_FLOOR


def test_a_bullet_that_merely_mentions_the_same_area_does_not_clear_it():
    entry = "shadow links stale detection"
    bullet = "fix a typo in the shadow links help text"
    assert _jaccard(entry, bullet) < _JACCARD_FLOOR


def test_the_floor_is_a_half():
    """Pinned so a later tweak is a deliberate decision, not a drift.

    Lowering it pre-ticks more promotions; raising it makes the tab look
    broken on genuinely reworded entries.
    """
    assert _JACCARD_FLOOR == 0.5


# ── the plan skeleton ───────────────────────────────────────────────────

def _bare():
    return object.__new__(RoadmapManagerDialog)


def test_the_skeleton_carries_target_and_note_when_given():
    text = _bare()._build_skeleton(11, "Theme", "2026-09-01", "a note")
    assert "_Target: 2026-09-01_" in text
    assert "a note" in text


def test_the_skeleton_omits_empty_fields_rather_than_leaving_blanks():
    text = _bare()._build_skeleton(11, "Theme", "", "")
    assert "_Target:" not in text
    assert not text.startswith("\n")


def test_the_skeleton_is_visibly_a_placeholder():
    """It must not read as a real planned item once inserted.

    The whole section goes into ROADMAP.md through ProposalBridge; a
    skeleton that looked finished would quietly become a commitment.
    """
    text = _bare()._build_skeleton(11, "Theme", "", "")
    assert "Placeholder" in text
    assert "Replace this" in text
    assert "🔮" in text, "should carry the not-started status glyph"
