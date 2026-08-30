"""helpers/geometry.py — pure geometric predicates over laid-out UI.

Level 1 of the visual oracle. Everything here is a pure function over
rectangles and strings: no Tk, no widget tree, and **no font measurement**.
That separation is the point, not tidiness.

A predicate that measures a font is a claim about the machine running the
test. A headless runner's default font can be twice the width of the one a
user sees, so a geometry assertion that resolves its own text widths will
fail by tens of pixels on a surface that is measurably clean in the real
application. Every width in this module arrives as an argument; the caller
(``helpers/geometry_scan.py``) does the measuring against real widgets.

Scope, deliberately narrow:

    this module owns geometric invariants that are mechanically measurable
    a screenshot owns human judgment about whether something looks right

The predicates exist because this project shipped each of these defects, all
of them with a fully green suite:

  * the Draft PR buttons were squeezed off-screen on tall windows;
  * the gitignore Save/Cancel bar was pushed off-screen when the content
    exceeded the screen height;
  * the doc-drafter notebook needed ~760px of tab strip against the dialog's
    own 720px minimum width, and Tk does not scroll tab strips, so the last
    tab was unreachable at the smallest supported size;
  * the Test Gaps list was clipped with no scrollbar.

Three traps worth stating, because each is a way to build a check that
cannot fail:

  * **Do not treat this as collision detection.** Tk composites children
    constantly and legitimately; a frame overlapping its own child is normal.
    ``overlaps`` is only meaningful on pairs the caller knows are
    semantically distinct — a caption and its value, two sibling buttons.
    The association has to come from the layout, not from proximity.
  * **Do not judge a scrolling surface against its own content.** Content
    extends past a viewport by definition, so that comparison reports a
    finding forever. ``escapes_container`` is for a child against the region
    that is supposed to bound it.
  * **Do not detect elision by looking for an ellipsis in the text.** A value
    may legitimately contain one. ``still_elided`` compares the displayed
    string against the widget's own stored full string, and additionally
    requires that the room has actually come back — otherwise it fires on
    every genuinely-too-narrow label, which is not a defect, just a small
    window.
"""
from __future__ import annotations

from typing import NamedTuple


class Rect(NamedTuple):
    """A laid-out rectangle in pixels, in whatever coordinate space the
    caller is consistent about (this module never mixes spaces itself)."""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def __str__(self) -> str:                      # pragma: no cover - display
        return f"{self.w}x{self.h}+{self.x}+{self.y}"


class Finding(NamedTuple):
    """One geometric defect, with enough detail to act on without a rerun."""

    kind: str
    subject: str
    detail: str

    def __str__(self) -> str:                      # pragma: no cover - display
        return f"[{self.kind}] {self.subject}: {self.detail}"


def escapes_container(child: Rect, container: Rect, tolerance: int = 0) -> bool:
    """True when *child* is not fully inside *container*.

    The off-screen-control case: a button bar pushed below the bottom of the
    dialog that owns it, or a control pushed past its right edge.

    ``tolerance`` absorbs sub-pixel and border rounding; it is not a licence
    to widen the check until it stops reporting. Pass the container that is
    genuinely supposed to bound the child — never a scrollable canvas's
    content frame, whose children exceed the viewport by design.
    """
    return (child.x < container.x - tolerance
            or child.y < container.y - tolerance
            or child.right > container.right + tolerance
            or child.bottom > container.bottom + tolerance)


def is_collapsed(rect: Rect, min_w: int = 1, min_h: int = 1) -> bool:
    """True when a widget has been squeezed to (effectively) nothing.

    A label at zero width still exists, still passes every test that asks
    what its text is, and is invisible to the user.
    """
    return rect.w < min_w or rect.h < min_h


def overlaps(a: Rect, b: Rect) -> bool:
    """True when two rectangles share any area.

    Only meaningful for pairs that must not overlap — a caption and its
    value, two sibling buttons. Feeding it a parent and its child reports a
    finding that is not a defect. See the module docstring.
    """
    return not (a.right <= b.x or b.right <= a.x
                or a.bottom <= b.y or b.bottom <= a.y)


def exceeds_width_budget(required_px: int, declared_min_px: int) -> bool:
    """True when content cannot fit the window's own declared minimum width.

    The doc-drafter case. A notebook tab strip needing 760px inside a dialog
    whose ``minsize`` is 720px means the last tab is unreachable at the
    smallest size the dialog itself says it supports — and Tk does not scroll
    tab strips, so there is no recovery. This compares a *declared* contract
    against a *measured* requirement, which is why it can be checked without
    resizing anything.
    """
    return required_px > declared_min_px


def still_elided(displayed: str, full: str, available_px: int,
                 required_px: int) -> bool:
    """True when text is shown truncated even though there is room for it.

    Both halves matter. ``displayed != full`` alone fires on every label in a
    legitimately narrow window, which is not a defect. Requiring
    ``available_px >= required_px`` restricts it to the real bug: the room
    came back and the widget never re-rendered.

    Note this never inspects the text for an ellipsis — a value may contain
    one of its own.
    """
    if not full or displayed == full:
        return False
    return available_px >= required_px


def fitting_columns(widths: "list[int]", available_px: int,
                    gap_px: int = 0) -> int:
    """Most grid columns whose measured total still fits *available_px*.

    For a row-major grid (``row, col = divmod(index, cols)``), a widget's own
    width is not what it costs — a grid column is as wide as its widest
    member, so the cost of a column is a maximum over every row. Summing those
    maxima is what makes this agree with what the toolkit will actually do; a
    greedy row-fill over individual widths does not, and produces a layout
    that overflows anyway.

    Returns at least 1: one column per row is always "fitting" in the sense
    that there is no narrower arrangement to fall back to.
    """
    if not widths:
        return 1
    for cols in range(len(widths), 0, -1):
        col_w = [0] * cols
        for i, w in enumerate(widths):
            col_w[i % cols] = max(col_w[i % cols], w)
        if sum(col_w) + gap_px * (cols - 1) <= available_px:
            return cols
    return 1
