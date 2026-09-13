"""tests/test_output_pane.py — the OUTPUT pane: selectable, read-only, resizable.

The defect this replaces: a ``state=DISABLED`` Text that could not take focus,
so its output could not be copied. The pane is now a normal Text whose widget
command refuses mutation. Two things are proven here, and each would fail the
other's way if broken:

* **every public mutation route is blocked** -- typed keys, BackSpace, Delete,
  Return, paste, cut, clear, middle-click paste, undo, and a direct
  ``text.insert()`` / Tcl-level ``insert``;
* **reading still works natively** -- selection, Ctrl+C, and the pane's own
  ``append`` / ``clear_view``.

Key routes run Tk's own ``Text`` class-binding scripts rather than synthesised
key events: the session root is withdrawn, and Tk drops key events for an
application with no focus, which would make a "blocked" test pass vacuously.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from controllers import output_pane as op                    # noqa: E402
from controllers.output_pane import OutputPaneController      # noqa: E402

pytestmark = pytest.mark.tk


@pytest.fixture
def pane(tk_root, mock_config):
    """A paned window with a stand-in notebook above the OUTPUT pane."""
    paned = tk.PanedWindow(tk_root, orient=tk.VERTICAL)
    paned.pack(fill=tk.BOTH, expand=True)
    above = tk.Frame(paned)
    paned.add(above, stretch="always")
    calls = {"stop": 0, "savings": 0, "log": 0}

    def bump(key):
        def fn():
            calls[key] += 1
        return fn

    ctrl = OutputPaneController(
        paned, mock_config, host=tk_root, above=above,
        on_stop=bump("stop"), on_open_savings=bump("savings"),
        on_open_log=bump("log"))
    ctrl.calls = calls
    yield ctrl
    paned.destroy()


def _content(ctrl):
    return ctrl.text.get("1.0", "end-1c")


def _run_class_binding(text, sequence, **subs):
    """Run the script Tk's `Text` class binds to *sequence*, as Tk would."""
    script = text.tk.call("bind", "Text", sequence)
    assert script, f"Tk has no Text binding for {sequence}"
    values = {"W": text._w, "A": "x", "K": "x", "s": "0", "x": "1", "y": "1"}
    values.update(subs)
    for key, value in values.items():
        script = script.replace("%" + key, str(value))
    text.tk.eval(script)


# ── read-only by construction ────────────────────────────────────────────

def test_the_text_is_not_disabled(pane):
    """A disabled Text never takes focus, so Ctrl+C never reached it."""
    assert str(pane.text.cget("state")) == "normal"


@pytest.mark.parametrize("sequence,subs", [
    ("<KeyPress>", {"A": "z"}),
    ("<BackSpace>", {}),
    ("<Delete>", {}),
    ("<Return>", {}),
])
def test_key_routes_cannot_mutate(pane, sequence, subs):
    pane.append("line one")
    pane.text.mark_set("insert", "1.4")
    before = _content(pane)
    _run_class_binding(pane.text, sequence, **subs)
    assert _content(pane) == before


@pytest.mark.parametrize("virtual", ["<<Paste>>", "<<Cut>>", "<<Clear>>"])
def test_virtual_event_routes_cannot_mutate(pane, virtual):
    pane.append("line one")
    pane.text.tag_add("sel", "1.0", "1.4")
    pane.text.clipboard_clear()
    pane.text.clipboard_append("PASTED")
    before = _content(pane)
    _run_class_binding(pane.text, virtual)
    assert _content(pane) == before


@pytest.mark.parametrize("virtual", ["<<Undo>>", "<<Redo>>"])
def test_undo_routes_cannot_mutate(pane, virtual):
    """Non-vacuous only with an undo stack: `-undo` is off by default, and an
    empty stack makes these no-ops whether or not the proxy exists."""
    pane.text.configure(undo=True)
    pane.append("first")
    pane.text.edit_separator()
    pane.append("second")
    if virtual == "<<Redo>>":
        pane.text.tk.call(pane._raw, "edit", "undo")    # something to redo
    before = _content(pane)
    _run_class_binding(pane.text, virtual)
    assert _content(pane) == before


def test_middle_click_paste_cannot_mutate(pane):
    """Owning PRIMARY first: with no selection there is nothing to paste and
    the route is a no-op whether or not the proxy exists."""
    pane.append("line one")
    pane.text.tag_add("sel", "1.0", "1.4")
    pane.text.selection_own()
    before = _content(pane)
    _run_class_binding(pane.text, "<<PasteSelection>>")
    assert _content(pane) == before


def test_direct_calls_cannot_mutate(pane):
    pane.append("line one")
    before = _content(pane)
    pane.text.insert("end", "sneaky")
    pane.text.delete("1.0", "end")
    pane.text.replace("1.0", "1.4", "LINE")
    pane.text.tk.call(pane.text._w, "insert", "end", "x")
    assert _content(pane) == before


def test_append_and_clear_view_are_the_writers(pane):
    pane.append("first")
    pane.append("second", "#ff0000")
    assert _content(pane) == "first\nsecond\n"
    assert pane.text.tag_cget("col_#ff0000", "foreground") == "#ff0000"
    pane.clear_view()
    assert _content(pane) == ""


def test_reading_errors_stay_in_tcl(pane):
    """`tk_textCopy` catches `get sel.first` with no selection.

    A Python proxy would set _tkinter's pending-error flag here and
    `mainloop()` would re-raise it later. The Tcl proxy must let the catch
    work: no exception, clipboard untouched.
    """
    pane.append("nothing selected")
    pane.text.clipboard_clear()
    pane.text.clipboard_append("kept")
    _run_class_binding(pane.text, "<<Copy>>")
    assert pane.text.clipboard_get() == "kept"


def test_copy_puts_the_selection_on_the_clipboard(pane):
    pane.append("alpha beta gamma")
    pane.text.tag_add("sel", "1.6", "1.10")
    pane.text.event_generate("<<Copy>>")
    assert pane.text.clipboard_get() == "beta"


def test_menu_copy_all_and_select_all(pane):
    pane.append("one")
    pane.append("two")
    pane._copy_all()
    assert pane.text.clipboard_get() == "one\ntwo\n"
    pane._select_all()
    assert pane.text.get("sel.first", "sel.last") == "one\ntwo\n"


def test_the_proxy_alias_goes_with_the_widget(tk_root, mock_config):
    paned = tk.PanedWindow(tk_root)
    above = tk.Frame(paned)
    paned.add(above)
    ctrl = OutputPaneController(paned, mock_config, host=tk_root, above=above,
                                on_stop=lambda: None,
                                on_open_savings=lambda: None,
                                on_open_log=lambda: None)
    path, raw = ctrl.text._w, ctrl._raw
    paned.destroy()
    assert not tk_root.tk.call("info", "commands", path)
    assert not tk_root.tk.call("info", "commands", raw)


# ── scrolling and selection ──────────────────────────────────────────────

@pytest.fixture
def mapped_pane(tk_root, mock_config):
    """A pane in a MAPPED off-screen window: scrolling needs real layout.

    The session root is withdrawn, and an unmapped Text has no view to scroll,
    so `yview` there says nothing about what a user would see.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("420x260+-3000+-3000")
    paned = tk.PanedWindow(win, orient=tk.VERTICAL)
    paned.pack(fill=tk.BOTH, expand=True)
    above = tk.Frame(paned, height=40)
    paned.add(above, stretch="always")
    ctrl = OutputPaneController(paned, mock_config, host=win, above=above,
                                on_stop=lambda: None,
                                on_open_savings=lambda: None,
                                on_open_log=lambda: None)
    for i in range(60):
        ctrl.append(f"line {i}")
    win.update()
    yield ctrl, win
    win.destroy()


def test_append_follows_only_a_view_at_the_bottom(mapped_pane):
    pane, win = mapped_pane
    pane.text.see("end")
    win.update()
    assert op.at_bottom(pane.text)
    pane.append("newest")
    win.update()
    assert op.at_bottom(pane.text)

    pane.text.yview_moveto(0.0)
    pane.text.tag_add("sel", "2.0", "2.4")
    win.update()
    assert not op.at_bottom(pane.text)
    pane.append("while reading")
    win.update()
    assert pane.text.yview()[0] == 0.0                   # not yanked away
    assert pane.text.get("sel.first", "sel.last") == "line"


# ── height preference ────────────────────────────────────────────────────

@pytest.mark.parametrize("stored,expected", [
    (0, op.MIN_H),
    (20, op.MIN_H),
    (100000, 900 - op._NB_MIN - op._SASH),
    ("garbage", op.DEFAULT_H),
    (None, op.DEFAULT_H),
    (True, op.DEFAULT_H),
    (180, 180),
])
def test_clamp_height(stored, expected):
    assert op.clamp_height(stored, 900) == expected


def test_a_clamped_restore_does_not_rewrite_the_preference(tk_root, mock_config):
    mock_config.raw["output_pane_height"] = 100000
    paned = tk.PanedWindow(tk_root)
    above = tk.Frame(paned)
    paned.add(above)
    OutputPaneController(paned, mock_config, host=tk_root, above=above,
                         on_stop=lambda: None, on_open_savings=lambda: None,
                         on_open_log=lambda: None)
    assert mock_config.raw["output_pane_height"] == 100000
    assert not getattr(mock_config, "_saved", False)
    paned.destroy()


def test_save_height_records_a_user_drag(pane, mock_config):
    pane.save_height(240)
    assert mock_config.raw["output_pane_height"] == 240
    assert mock_config._saved


def test_save_height_ignores_a_collapsed_measurement(pane, mock_config):
    pane.save_height(1)                 # an unmapped frame reports 1px
    assert "output_pane_height" not in mock_config.raw


def test_a_release_before_layout_saves_where_the_sash_ended(tk_root, mock_config):
    """The last committed sash position wins, even when layout has not caught up.

    Found in the live window: Tk's Panedwindow binding moves the sash on
    <B1-Motion>, but the pane's size only updates when layout runs at idle. A
    release arriving before then was saved as the height from BEFORE the last
    move (504 saved while the pane showed 414). Events are generated back to
    back here, with no idle between them, which is that exact order.
    """
    win = tk.Toplevel(tk_root)
    win.geometry("420x520+-3000+-3000")
    paned = tk.PanedWindow(win, orient=tk.VERTICAL, sashwidth=6)
    paned.pack(fill=tk.BOTH, expand=True)
    above = tk.Frame(paned, height=40)
    paned.add(above, stretch="always")
    mock_config.raw["output_pane_height"] = 120
    ctrl = OutputPaneController(paned, mock_config, host=win, above=above,
                                on_stop=lambda: None,
                                on_open_savings=lambda: None,
                                on_open_log=lambda: None)
    win.update()
    try:
        before = ctrl.frame.winfo_height()
        _x, y = paned.sash_coord(0)
        paned.event_generate("<Button-1>", x=100, y=y + 2)
        paned.event_generate("<B1-Motion>", x=100, y=y + 2 + 60)
        paned.event_generate("<ButtonRelease-1>", x=100, y=y + 2 + 60)
        win.update()
        after = ctrl.frame.winfo_height()
        assert after < before - 40, (before, after)   # the drag really moved it
        assert mock_config.raw["output_pane_height"] == after
    finally:
        win.destroy()


# ── running state, header callbacks, clear during a run ─────────────────

def test_set_running_paints_the_header(pane):
    stop, label = pane._headers[0]
    assert "disabled" in str(stop.cget("state"))
    pane.set_running(True, "Sync All")
    assert "disabled" not in str(stop.cget("state"))
    assert "Sync All" in label.cget("text")
    pane.set_running(False)
    assert "disabled" in str(stop.cget("state"))
    assert label.cget("text") == ""


def test_header_buttons_reach_their_callbacks_exactly_once(pane):
    pane.set_running(True, "x")
    buttons = {b.cget("text"): b for b in pane.frame.winfo_children()[0]
               .winfo_children() if isinstance(b, tk.ttk.Button)}
    buttons["View Log"].invoke()
    buttons["Savings"].invoke()
    buttons["■  Stop"].invoke()
    assert pane.calls == {"stop": 1, "savings": 1, "log": 1}


def test_clear_view_during_a_run_never_stops_it(pane):
    pane.set_running(True, "Sync All")
    pane.append("before")
    pane.clear_view()
    pane.append("after")
    assert _content(pane) == "after\n"
    assert pane.calls["stop"] == 0


# ── E2: text peers -- shared content, independent views ─────────────────
#
# The pop-out is built on this, so it is proven first and on its own: if any
# of these fail, the pop-out is not built and the docked pane ships alone.

@pytest.fixture
def peer(pane, tk_root):
    win = tk.Toplevel(tk_root)
    view = op.PeerText(win, pane._raw, height=4)
    raw = op.install_read_only(view)
    yield pane, view, raw, win
    win.destroy()


def test_peer_sees_appends_made_through_the_docked_pane(peer):
    pane, view, _, _ = peer
    pane.append("from the docked side", "#abcdef")
    assert view.get("1.0", "end-1c") == "from the docked side\n"
    assert "col_#abcdef" in view.tag_names("1.0")


def test_tag_configuration_is_shared_in_both_directions(peer):
    pane, view, _, _ = peer
    pane.append("x", "#111111")
    pane.text.tag_configure("col_#111111", foreground="#222222")
    assert view.tag_cget("col_#111111", "foreground") == "#222222"
    view.tag_configure("col_#111111", foreground="#333333")
    assert pane.text.tag_cget("col_#111111", "foreground") == "#333333"


def test_selection_is_per_view_and_copies_from_the_view(peer):
    pane, view, _, _ = peer
    pane.append("alpha beta gamma")
    view.tag_add("sel", "1.11", "1.16")
    assert pane.text.tag_ranges("sel") == ()
    view.event_generate("<<Copy>>")
    assert view.clipboard_get() == "gamma"


def test_mutation_routes_are_blocked_on_the_peer_too(peer):
    pane, view, _, _ = peer
    pane.append("line one")
    before = pane.text.get("1.0", "end-1c")
    view.insert("end", "sneaky")
    view.delete("1.0", "end")
    _run_class_binding(view, "<KeyPress>", A="z")
    _run_class_binding(view, "<<Paste>>")
    assert pane.text.get("1.0", "end-1c") == before


def test_destroying_the_peer_leaves_the_docked_content(peer):
    pane, view, raw, win = peer
    pane.append("survives")
    path = view._w
    win.destroy()
    assert pane.text.get("1.0", "end-1c") == "survives\n"
    assert not pane.text.tk.call("info", "commands", path)
    assert not pane.text.tk.call("info", "commands", raw)
    pane.append("still writable")
    assert pane.text.get("1.0", "end-1c") == "survives\nstill writable\n"


# ── E3: pop-out and dock ────────────────────────────────────────────────
#
# "Closing the pop-out puts it back exactly as it was" is the requirement, so
# the height asserted after docking is the one recorded BEFORE popping out --
# never anything the pop-out window could have influenced.

def _panes(ctrl):
    return [str(p) for p in ctrl._paned.panes()]


def _popout_buttons(ctrl):
    out = {}
    stack = [ctrl._popout]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, tk.ttk.Button):
            out[w.cget("text")] = w
    return out


def test_pop_out_leaves_the_paned_and_shares_content(pane):
    pane.append("before")
    pane.pop_out()
    assert pane.is_popped_out
    assert str(pane.frame) not in _panes(pane)
    assert pane._peer.get("1.0", "end-1c") == "before\n"
    pane.append("while popped out")
    assert pane._peer.get("1.0", "end-1c") == "before\nwhile popped out\n"
    pane.dock()
    assert pane.text.get("1.0", "end-1c") == "before\nwhile popped out\n"


def test_dock_restores_the_pre_pop_out_height_not_the_windows(pane):
    pane._paned.paneconfigure(pane.frame, height=173)
    pane.pop_out()
    assert pane._docked_h == 173
    pane._popout.geometry("900x700+40+40")        # user moves and resizes it
    pane._popout.update_idletasks()
    pane.dock()
    panes = _panes(pane)
    assert panes.index(str(pane.frame)) == panes.index(str(pane._above)) + 1
    assert int(pane._paned.panecget(pane.frame, "height")) == 173
    assert pane._popout is None


def test_pop_out_and_dock_are_idempotent(pane, tk_root):
    pane.pop_out()
    first = pane._popout
    pane.pop_out()
    assert pane._popout is first
    tops = [w for w in tk_root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops.count(first) == 1 and len([t for t in tops if t.title()
                                           == first.title()]) == 1
    pane.dock()
    pane.dock()                                   # close box after Dock
    assert _panes(pane).count(str(pane.frame)) == 1


def test_window_close_box_docks(pane):
    pane.pop_out()
    win = pane._popout
    win.tk.call(win.protocol("WM_DELETE_WINDOW"))
    assert not pane.is_popped_out
    assert str(pane.frame) in _panes(pane)


def test_header_buttons_survive_pop_out_and_dock(pane):
    pane.pop_out()
    buttons = _popout_buttons(pane)
    buttons["Savings"].invoke()
    buttons["View Log"].invoke()
    pane.set_running(True, "Sync All")
    assert "disabled" not in str(buttons["■  Stop"].cget("state"))
    buttons["■  Stop"].invoke()
    buttons["⇲  Dock"].invoke()
    assert not pane.is_popped_out
    pane.set_running(False)                       # the dead header raises nothing
    assert len(pane._headers) == 1
    docked = {b.cget("text"): b for b in pane.frame.winfo_children()[0]
              .winfo_children() if isinstance(b, tk.ttk.Button)}
    docked["■  Stop"].configure(state=tk.NORMAL)
    docked["■  Stop"].invoke()
    assert pane.calls == {"stop": 2, "savings": 1, "log": 1}


def test_hiding_the_main_window_hides_the_same_pop_out(pane, tk_root):
    pane.pop_out()
    win = pane._popout
    pane._on_host_unmap(type("E", (), {"widget": tk_root})())
    assert win.state() == "withdrawn"
    pane._on_host_map(type("E", (), {"widget": tk_root})())
    assert pane._popout is win and win.state() != "withdrawn"
    assert len([w for w in tk_root.winfo_children()
                if isinstance(w, op.PeerText) or
                (isinstance(w, tk.Toplevel) and w is not win and
                 w.title() == win.title())]) == 0


def test_a_child_unmap_is_not_the_main_window(pane, tk_root):
    """<Unmap> bound on a toplevel also fires for its children."""
    pane.pop_out()
    pane._on_host_unmap(type("E", (), {"widget": pane._peer})())
    assert pane._popout.state() != "withdrawn"


def test_remembered_geometry_is_validated_before_use(pane):
    """An off-screen memory (disconnected monitor) must be checked, not applied."""
    seen = []
    pane._geometry_ok = lambda geom: seen.append(geom) or False
    pane.pop_out()
    assert seen == []                          # nothing remembered yet
    pane.dock()
    remembered = pane._popout_geom
    assert remembered
    pane.pop_out()
    assert seen == [remembered]                # consulted with the memory
    pane.dock()


def _outer_bottom(geom, title_bar=40):
    size, x, y = geom.split("+")
    _w, h = size.split("x")
    return int(y) + int(h) + title_bar


def test_first_pop_out_fits_above_the_taskbar_at_a_tall_preference():
    """Measured live: a 384 px pane opened a 1152 px pop-out on a 1080 px screen.

    Windows shrank it to 1100 px outer, still past the 1032 px work area, so
    the newest line -- where output arrives -- sat under the taskbar.
    """
    geom = op.default_popout_geometry(
        host_x=-8, host_y=24, host_w=1920, host_h=1009,
        docked_h=384, screen_w=1920, screen_h=1080)
    _size, _x, _y = geom.split("+")
    assert int(_size.split("x")[1]) <= 1080 * 2 // 3
    assert _outer_bottom(geom) <= 1032, geom


@pytest.mark.parametrize("host,screen", [
    ((0, 0, 760, 600), (1366, 768)),          # small laptop, default window
    ((100, 400, 900, 650), (1920, 1080)),     # window low on the screen
    ((0, 0, 3840, 2100), (3840, 2160)),       # 4K, maximised
])
def test_first_pop_out_stays_on_screen(host, screen):
    hx, hy, hw, hh = host
    sw, sh = screen
    for docked_h in (60, 110, 384, 900):
        geom = op.default_popout_geometry(hx, hy, hw, hh, docked_h, sw, sh)
        size, x, y = geom.split("+")
        w, h = (int(v) for v in size.split("x"))
        assert 0 <= int(x) and int(x) + w <= sw, geom
        assert 0 <= int(y) and _outer_bottom(geom) <= sh - 48, geom
        assert h >= min(280, sh * 2 // 3)                 # still roomy


def test_first_pop_out_is_centred_over_the_main_window():
    geom = op.default_popout_geometry(200, 100, 1200, 900, 110, 1920, 1080)
    size, x, y = geom.split("+")
    w, h = (int(v) for v in size.split("x"))
    assert int(x) + w // 2 == 200 + 1200 // 2
    assert int(y) + h // 2 == 100 + 900 // 2


def test_popping_out_never_writes_the_height_preference(pane, mock_config):
    pane.pop_out()
    pane.dock()
    assert "output_pane_height" not in mock_config.raw


def test_a_following_view_stays_on_the_end_through_a_resize(mapped_pane):
    """Tk anchors the top line on resize; a log must keep its newest line.

    Reproduces the live measurement: every line fits (so the view is "at the
    bottom" with line 1 on top), then the window shrinks. The docked pane has
    a fixed pane height, so the pop-out -- whose Text fills its window -- is
    the view that actually resizes.
    """
    pane, win = mapped_pane
    pane.pop_out()
    top = pane._popout
    top.geometry("600x1000+-3000+-3000")
    top.update()
    assert op.at_bottom(pane._peer)
    top.geometry("600x260+-3000+-3000")
    top.update()
    top.update()
    assert op.at_bottom(pane._peer)
    pane.dock()


def test_a_scrolled_up_view_is_left_alone_on_resize(mapped_pane):
    pane, win = mapped_pane
    pane.pop_out()
    top = pane._popout
    top.geometry("600x300+-3000+-3000")
    top.update()
    pane._peer.yview_moveto(0.0)
    top.update()
    top.geometry("600x240+-3000+-3000")
    top.update()
    top.update()
    assert pane._peer.yview()[0] == 0.0
    pane.dock()
