"""tests/test_geometry_predicates.py — level 1 of the visual oracle.

Pure predicates over CONSTRUCTED rectangles. No Tk, no real widgets, no font
metrics — deliberately, because a geometry test that measures a font is a
claim about the machine it runs on rather than about the application.

Every predicate gets two tests: one that it reports the defect, and one that
it stays SILENT on a clean surface. The second is the one that matters. A
predicate hard-wired to ``return True`` passes the first test perfectly, and
"it found the bug" is not evidence that it can tell the difference.

The fixtures are built from the four defects this project actually shipped —
see ``src/helpers/geometry.py`` for the list — rather than from invented
cases, so a predicate that cannot see its own motivating bug fails here.
"""
from __future__ import annotations

import pytest

from helpers.geometry import (
    Rect,
    escapes_container,
    exceeds_width_budget,
    is_collapsed,
    overlaps,
    still_elided,
)


# ── Rect basics ───────────────────────────────────────────────────────────

def test_rect_edges_are_derived_not_stored():
    r = Rect(x=10, y=20, w=100, h=50)
    assert r.right == 110
    assert r.bottom == 70


# ── escapes_container ─────────────────────────────────────────────────────
# The shipped defect: the Draft PR buttons squeezed off-screen on tall
# windows, and the gitignore Save/Cancel bar pushed past the dialog bottom.

_DIALOG = Rect(0, 0, 720, 600)


def test_escapes_container_reports_a_button_bar_pushed_below_the_dialog():
    button_bar = Rect(10, 585, 700, 40)          # bottom 625 > dialog 600
    assert escapes_container(button_bar, _DIALOG) is True


def test_escapes_container_reports_a_control_past_the_right_edge():
    wide = Rect(40, 100, 700, 30)                # right 740 > dialog 720
    assert escapes_container(wide, _DIALOG) is True


def test_escapes_container_is_silent_on_a_child_fully_inside():
    ok = Rect(10, 500, 700, 40)                  # bottom 540, right 710
    assert escapes_container(ok, _DIALOG) is False


def test_escapes_container_is_silent_on_an_exactly_flush_child():
    """A child filling its container exactly is correct, not a finding."""
    flush = Rect(0, 0, 720, 600)
    assert escapes_container(flush, _DIALOG) is False


def test_escapes_container_tolerance_absorbs_a_one_pixel_border():
    off_by_one = Rect(0, 0, 721, 600)
    assert escapes_container(off_by_one, _DIALOG) is True
    assert escapes_container(off_by_one, _DIALOG, tolerance=1) is False


# ── is_collapsed ──────────────────────────────────────────────────────────

def test_is_collapsed_reports_a_zero_width_label():
    assert is_collapsed(Rect(10, 10, 0, 18)) is True


def test_is_collapsed_reports_a_zero_height_row():
    assert is_collapsed(Rect(10, 10, 200, 0)) is True


def test_is_collapsed_is_silent_on_a_normal_label():
    assert is_collapsed(Rect(10, 10, 120, 18)) is False


# ── overlaps ──────────────────────────────────────────────────────────────
# Only ever applied to pairs the caller knows must not overlap.

def test_overlaps_reports_a_value_painted_over_its_caption():
    caption = Rect(10, 40, 120, 18)
    value = Rect(100, 40, 200, 18)               # starts inside the caption
    assert overlaps(caption, value) is True


def test_overlaps_is_silent_on_adjacent_widgets():
    """Touching edges are not an overlap — a shared boundary is normal."""
    caption = Rect(10, 40, 120, 18)
    value = Rect(130, 40, 200, 18)               # caption.right == 130
    assert overlaps(caption, value) is False


def test_overlaps_is_silent_on_widgets_in_different_rows():
    assert overlaps(Rect(10, 40, 120, 18), Rect(10, 70, 120, 18)) is False


# ── exceeds_width_budget ──────────────────────────────────────────────────
# The shipped defect: the doc-drafter tab strip needed ~760px against the
# dialog's own 720px minsize, and Tk does not scroll tab strips.

def test_exceeds_width_budget_reports_the_doc_drafter_tab_strip():
    assert exceeds_width_budget(required_px=760, declared_min_px=720) is True


def test_exceeds_width_budget_is_silent_when_content_fits():
    assert exceeds_width_budget(required_px=700, declared_min_px=720) is False


def test_exceeds_width_budget_is_silent_on_an_exact_fit():
    """Needing precisely the declared minimum is within contract."""
    assert exceeds_width_budget(required_px=720, declared_min_px=720) is False


# ── still_elided ──────────────────────────────────────────────────────────

def test_still_elided_reports_text_truncated_though_the_room_came_back():
    assert still_elided(displayed="C:/very/long/pa…", full="C:/very/long/path/x",
                        available_px=400, required_px=320) is True


def test_still_elided_is_silent_when_there_genuinely_is_no_room():
    """A narrow window is not a defect; this must not fire on every label."""
    assert still_elided(displayed="C:/very/long/pa…", full="C:/very/long/path/x",
                        available_px=200, required_px=320) is False


def test_still_elided_is_silent_when_the_text_is_complete():
    assert still_elided(displayed="ready", full="ready",
                        available_px=400, required_px=40) is False


def test_still_elided_does_not_key_on_the_ellipsis_character():
    """A value may legitimately contain an ellipsis.

    Guards the trap named in the module docstring: an oracle that greps the
    displayed text for "…" reports this correct label as elided.
    """
    assert still_elided(displayed="Loading…", full="Loading…",
                        available_px=400, required_px=60) is False


def test_still_elided_is_silent_on_empty_text():
    assert still_elided(displayed="", full="", available_px=0,
                        required_px=0) is False


# ── The predicates must be able to say NO ─────────────────────────────────

@pytest.mark.parametrize("predicate,clean_args", [
    (escapes_container, (Rect(10, 10, 100, 20), Rect(0, 0, 720, 600))),
    (is_collapsed,      (Rect(10, 10, 100, 20),)),
    (overlaps,          (Rect(0, 0, 10, 10), Rect(20, 20, 10, 10))),
    (exceeds_width_budget, (100, 720)),
    (still_elided,      ("x", "x", 100, 10)),
])
def test_every_predicate_stays_silent_on_a_clean_surface(predicate, clean_args):
    """The whole-suite version of the silence requirement.

    A predicate that returns everything passes each defect test above. This
    parametrized sweep is what stops that from going unnoticed when a new
    predicate is added and only its positive test is written.
    """
    assert predicate(*clean_args) is False
