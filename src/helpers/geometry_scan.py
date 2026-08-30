"""helpers/geometry_scan.py — level 2 of the visual oracle.

Reads ACTUAL laid-out geometry off a real Tk widget tree and hands it to the
pure predicates in ``helpers/geometry.py``. All font and pixel measurement
lives here, on purpose: the predicates stay machine-independent and this
layer is only ever exercised against a running application.

**Neither layer alone is the check.** A passing predicate suite proves
rectangle arithmetic and nothing about the app. And a walk that silently
returned no widgets would make every predicate vacuously happy — zero
widgets means zero findings, which reads exactly like a clean surface. So
``scan_window`` reports ``measured``: how many widgets it actually looked at.
Callers are expected to assert that number, not merely the absence of
findings.

Two deliberate scope limits, both from traps that produce checks which
cannot fail:

  * **Scrollable content is exempt from the containment check.** A canvas's
    content frame extends past its viewport by definition, so comparing them
    reports a finding forever. Any widget with a Canvas ancestor is skipped
    for containment (it is still checked for collapse).
  * **Overlap is not wired up here yet.** Detecting it properly needs the
    caption/value association from the layout manager rather than raw
    rectangle collision, since Tk composites children legitimately all the
    time. The predicate exists and is tested; this walk does not call it, and
    says so rather than implying coverage it does not have.

Coordinates are taken in ROOT space (``winfo_rootx``/``winfo_rooty``) for
every widget, so a child can be compared against an ancestor that is not its
immediate parent without mixing coordinate spaces.
"""
from __future__ import annotations

from typing import NamedTuple

from helpers.geometry import Finding, Rect, escapes_container, is_collapsed

# Tk never reports a widget as zero-wide: it clamps to 1px, so a label that
# has been squeezed out of existence measures 1, not 0. A collapse threshold
# of 1 ("flag when w < 1") therefore cannot fire on any real widget — the
# predicate looks correct in isolation and is dead in practice. This is the
# toolkit fact, so it lives in the toolkit layer rather than in the pure
# predicate's default.
_COLLAPSE_MIN_PX = 2

# Widgets that are legitimately a couple of pixels thin in one dimension.
# Without this the collapse check reports every separator in the app, which
# is how a real signal becomes noise that gets switched off.
_THIN_BY_DESIGN = frozenset({
    "TSeparator", "Separator", "TSizegrip", "Sizegrip", "TProgressbar",
})

# Classes that carry content even with no `text` option set.
_CONTENT_CLASSES = frozenset({
    "Entry", "TEntry", "Text", "Listbox", "Treeview", "TCombobox",
    "Spinbox", "TSpinbox", "Canvas", "Scale", "TScale",
})


def _carries_content(widget, cls: str) -> bool:
    """True when collapsing this widget would actually hide something.

    An empty *container* at 1px is Tk working correctly, not a defect: the
    Help tab's footer holds three buttons that are packed per section, so on
    a freshly-opened tab it is legitimately empty and legitimately 1px tall.
    Reporting that is how a check earns a reputation for crying wolf and
    stops being read.

    A container squeezing real content is still caught — its mapped children
    are measured in their own right and report as collapsed or escaping.
    """
    if cls in _CONTENT_CLASSES:
        return True
    try:
        return bool(str(widget.cget("text")).strip())
    except Exception:
        return False


class ScanResult(NamedTuple):
    """Findings plus the population they were drawn from.

    ``measured`` is not diagnostics — it is half the result. "No findings
    across 214 widgets" and "no findings across 0 widgets" are the same
    empty list and completely different claims.
    """

    findings: "list[Finding]"
    measured: int
    skipped_unmapped: int
    skipped_scrollable: int

    @property
    def clean(self) -> bool:
        return not self.findings


def _rect_of(widget) -> Rect:
    """Laid-out rectangle of *widget* in root coordinates."""
    return Rect(x=widget.winfo_rootx(), y=widget.winfo_rooty(),
                w=widget.winfo_width(), h=widget.winfo_height())


def _has_scrollable_ancestor(widget, top) -> bool:
    """True when *widget* sits inside a Canvas (i.e. a scrolling surface)."""
    import tkinter as tk                       # lazy: keeps import-time clean

    node = widget
    while node is not None and node is not top:
        if isinstance(node, tk.Canvas):
            return True
        node = getattr(node, "master", None)
    return False


def _iter_descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _iter_descendants(child)


def scan_window(top, *, tolerance: int = 2) -> ScanResult:
    """Measure every mapped widget under *top* and report geometric defects.

    ``top`` must already be laid out — call ``update_idletasks()`` first, or
    every widget reports 1x1 and the scan is measuring Tk's placeholder
    geometry rather than the application's.

    ``tolerance`` absorbs borders and sub-pixel rounding on the containment
    check. It is also the handle that proves this scan can still say NO: pass
    a negative value and every child becomes a finding against the same
    geometry, which is how you confirm the pipeline reports at all rather
    than trusting an empty list.
    """
    findings: list[Finding] = []
    measured = 0
    skipped_unmapped = 0
    skipped_scrollable = 0

    top.update_idletasks()
    bounds = _rect_of(top)

    for widget in _iter_descendants(top):
        try:
            if not widget.winfo_ismapped():
                skipped_unmapped += 1
                continue
            rect = _rect_of(widget)
        except Exception:
            # A widget destroyed mid-walk is not a geometric defect.
            skipped_unmapped += 1
            continue

        measured += 1
        cls = widget.winfo_class()
        # The Tk path (".!frame.!frame2.!label") rather than the bare name:
        # a finding has to be locatable without re-running the app, and
        # "Frame:!frame" appears dozens of times in one window.
        subject = f"{cls} {widget}"

        if (cls not in _THIN_BY_DESIGN
                and _carries_content(widget, cls)
                and is_collapsed(rect, min_w=_COLLAPSE_MIN_PX,
                                 min_h=_COLLAPSE_MIN_PX)):
            findings.append(Finding(
                kind="collapsed",
                subject=subject,
                detail=f"laid out at {rect} — present in the tree, invisible "
                       f"to the user",
            ))

        if _has_scrollable_ancestor(widget, top):
            skipped_scrollable += 1
            continue

        if escapes_container(rect, bounds, tolerance=tolerance):
            findings.append(Finding(
                kind="escapes-window",
                subject=subject,
                detail=f"{rect} is not contained by the window {bounds} — "
                       f"this is how a button bar ends up off-screen",
            ))

    return ScanResult(findings=findings, measured=measured,
                      skipped_unmapped=skipped_unmapped,
                      skipped_scrollable=skipped_scrollable)


def format_result(result: ScanResult) -> str:
    """Human-readable summary that always states the population.

    Written so a log line can never say "clean" without saying over what.
    """
    head = (f"geometry: {len(result.findings)} finding(s) across "
            f"{result.measured} mapped widget(s) "
            f"({result.skipped_unmapped} unmapped, "
            f"{result.skipped_scrollable} inside scrollable regions)")
    if not result.findings:
        return head
    return head + "\n" + "\n".join(f"  {f}" for f in result.findings)
