"""tests/test_geometry_scan.py — level 2 of the visual oracle.

Exercises the extraction layer against a REAL laid-out Tk window. The
predicates are unit-tested on constructed rectangles in
``tests/test_geometry_predicates.py``; nothing here re-tests the arithmetic.
What is tested here is the part that only a running toolkit can answer: that
the walk finds the widgets, that it measures them after layout, and that it
reports its population.

The load-bearing test is ``test_the_scan_can_still_say_no``. Every other test
here would keep passing if ``scan_window`` were changed to return no findings
unconditionally — "we ran it and it was clean" is not evidence that it can
report. Forcing it to fire against the same geometry is.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from helpers.geometry_scan import format_result, scan_window

pytestmark = pytest.mark.tk


@pytest.fixture()
def laid_out_window(tk_root):
    """A small, realistic window: a body area and a bottom button bar.

    Mirrors the shape of the dialogs whose button bars have gone off-screen
    in this project, so the fixture is not degenerate with respect to the
    defect the scan exists to catch.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("480x320+0+0")
    body = ttk.Frame(win)
    body.pack(fill=tk.BOTH, expand=True)
    for i in range(4):
        ttk.Label(body, text=f"row {i}").pack(anchor="w", padx=8, pady=2)
    bar = ttk.Frame(win)
    bar.pack(fill=tk.X, side=tk.BOTTOM)
    ttk.Button(bar, text="Cancel").pack(side=tk.RIGHT, padx=6, pady=6)
    ttk.Button(bar, text="Save").pack(side=tk.RIGHT, pady=6)
    win.update_idletasks()
    win.update()
    yield win
    win.destroy()


def test_the_scan_measures_a_real_population(laid_out_window):
    """The population is part of the result, not diagnostics.

    A walk that silently returned nothing would report zero findings and read
    as a clean surface, so the count is asserted before anything else.
    """
    result = scan_window(laid_out_window)
    assert result.measured >= 8, (
        f"expected the walk to reach the frames, labels and buttons in the "
        f"fixture, but it measured only {result.measured} widget(s) — a "
        f"clean result over a population that small means nothing"
    )


def test_a_correctly_laid_out_window_is_clean(laid_out_window):
    """The silence half of the requirement, at the integration level."""
    result = scan_window(laid_out_window)
    assert result.clean, format_result(result)


def test_the_scan_can_still_say_no(laid_out_window):
    """Force a finding from geometry that is genuinely clean.

    Without this, every other test in this file passes just as well against
    a scan that cannot report anything at all. An impossible tolerance turns
    the same widgets into findings, which proves the pipeline — walk,
    measure, predicate, Finding, formatter — is connected end to end.
    """
    clean = scan_window(laid_out_window)
    assert clean.clean

    forced = scan_window(laid_out_window, tolerance=-10_000)
    assert forced.findings, (
        "an impossible tolerance produced no findings — the scan cannot "
        "report, so its clean results are worthless"
    )
    assert forced.measured == clean.measured, (
        "the forced run measured a different population than the clean run; "
        "the two results are not comparable"
    )
    assert all(f.kind == "escapes-window" for f in forced.findings)


def test_findings_name_the_widget_and_its_geometry(laid_out_window):
    """A finding has to be actionable without re-running the app."""
    forced = scan_window(laid_out_window, tolerance=-10_000)
    first = forced.findings[0]
    assert first.subject, "a finding with no subject cannot be acted on"
    assert "x" in first.detail and "+" in first.detail, (
        f"expected the rectangle in the detail, got {first.detail!r}"
    )


def test_a_collapsed_widget_is_reported(tk_root):
    """The squeezed-to-nothing case: in the tree, invisible to the user.

    Note the fixture asks for ``width=0`` and Tk gives back 1 — it clamps.
    That is why the scan's collapse threshold is 2 and not 1: a predicate
    written to fire on ``w < 1`` is correct in the abstract and can never
    fire on a real Tk widget. This test is what caught that.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("300x200+0+0")
    holder = ttk.Frame(win)
    holder.pack(fill=tk.BOTH, expand=True)
    squeezed = ttk.Label(holder, text="invisible")
    squeezed.place(x=5, y=5, width=0, height=18)
    win.update_idletasks()
    win.update()
    try:
        assert squeezed.winfo_width() <= 1, (
            "the fixture is meant to construct a collapsed widget; Tk gave "
            f"it {squeezed.winfo_width()}px, so it is not degenerate in the "
            "way this test needs"
        )
        result = scan_window(win)
        kinds = {f.kind for f in result.findings}
        assert "collapsed" in kinds, format_result(result)
    finally:
        win.destroy()


def test_a_separator_is_not_reported_as_collapsed(tk_root):
    """Thin-by-design widgets must not fire the collapse check.

    A check that flags every separator in the application is one that gets
    switched off, taking the real signal with it.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("300x200+0+0")
    holder = ttk.Frame(win)
    holder.pack(fill=tk.BOTH, expand=True)
    ttk.Separator(holder, orient="horizontal").pack(fill=tk.X, pady=4)
    ttk.Label(holder, text="body").pack(anchor="w")
    win.update_idletasks()
    win.update()
    try:
        result = scan_window(win)
        assert result.clean, format_result(result)
    finally:
        win.destroy()


def test_an_empty_container_is_not_reported_as_collapsed(tk_root):
    """Regression: the first real drive-and-scan run reported exactly this.

    The Help tab's footer holds three buttons that `_help_show()` packs per
    section, so on a freshly-opened tab it is genuinely empty and Tk lays it
    out 1px tall. That is correct behaviour. Reporting it is how a check
    earns a reputation for crying wolf and stops being read.

    A container squeezing real content is still caught, because its mapped
    children are measured in their own right — see the test below.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("300x200+0+0")
    body = ttk.Frame(win)
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text="content").pack(anchor="w")
    # An empty footer, packed but with nothing in it — the real shape.
    footer = tk.Frame(win)
    footer.pack(side=tk.BOTTOM, fill=tk.X)
    win.update_idletasks()
    win.update()
    try:
        assert footer.winfo_height() <= 2, (
            f"the fixture needs an actually-collapsed empty container; Tk "
            f"gave it {footer.winfo_height()}px"
        )
        result = scan_window(win)
        assert result.clean, format_result(result)
    finally:
        win.destroy()


def test_findings_are_locatable_by_widget_path(laid_out_window):
    """A finding must say WHICH widget, not just its class.

    The first real run reported `Frame:!frame`, which appears dozens of times
    in one window and cost a second run to place. Subjects carry the full Tk
    path so a finding can be acted on without re-driving the app.
    """
    forced = scan_window(laid_out_window, tolerance=-10_000)
    assert any("." in f.subject for f in forced.findings), (
        f"expected a Tk widget path in the subject, got "
        f"{[f.subject for f in forced.findings][:3]}"
    )


def test_format_result_always_states_the_population(laid_out_window):
    """A log line must never be able to say 'clean' without saying over what."""
    text = format_result(scan_window(laid_out_window))
    assert "mapped widget(s)" in text
    assert "0 finding(s)" in text
