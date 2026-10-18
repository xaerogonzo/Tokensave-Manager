"""The shared theme styles Combobox and Menubutton, not just buttons and trees.

Found on the Tasks tab's View dropdown: clam's light defaults showed through
(a pale grey read-only box with near-invisible text, white editable and disabled
boxes, a Menubutton whose arrow floated away from its label). Every Combobox in
the application had it; none had a style to fall back on.

A module-scoped interpreter of its own, not the shared `tk_root`: `_style` switches
the theme, and doing that on the session-wide root would leak into every other Tk
test. One per module, because a fourth fresh `tk.Tk()` in the same process fails to
initialise Tk here.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

pytestmark = pytest.mark.tk


@pytest.fixture(scope="module")
def themed():
    from app import App
    from constants import C
    root = tk.Tk()
    root.withdraw()
    App._style(root)
    yield root, ttk.Style(root), C
    root.destroy()


def test_the_readonly_combobox_is_dark_with_light_text(themed):
    _root, style, C = themed
    assert style.lookup("TCombobox", "fieldbackground", ("readonly",)) == C["surface0"]
    assert style.lookup("TCombobox", "foreground", ("readonly",)) == C["text"]


def test_an_editable_and_a_disabled_combobox_are_not_white(themed):
    _root, style, C = themed
    assert style.lookup("TCombobox", "fieldbackground") == C["surface0"]
    assert style.lookup("TCombobox", "fieldbackground", ("disabled",)) == C["mantle"]
    assert style.lookup("TCombobox", "foreground", ("disabled",)) == C["overlay0"]


def test_the_dropdown_list_is_themed_too(themed):
    """The popdown is a plain Tk Listbox, which a ttk style never reaches."""
    root, _style, C = themed
    combo = ttk.Combobox(root, values=("a", "b"), state="readonly")
    combo.pack()
    root.update_idletasks()
    popdown = root.tk.eval("ttk::combobox::PopdownWindow %s" % combo)
    # A Tcl-side window Python does not track, so ask Tcl directly.
    listbox = popdown + ".f.l"
    assert str(root.tk.call(listbox, "cget", "-background")) == C["mantle"]
    assert str(root.tk.call(listbox, "cget", "-foreground")) == C["text"]


def test_the_menubutton_has_a_background_to_hold_its_arrow(themed):
    _root, style, C = themed
    assert style.lookup("TMenubutton", "background") == C["surface0"]
    assert style.lookup("TMenubutton", "arrowcolor") == C["text"]
    assert style.lookup("TMenubutton", "background", ("active",)) == C["surface1"]
