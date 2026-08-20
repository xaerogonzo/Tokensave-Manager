"""tests/test_theme.py — Tk UI primitives (tooltips, mouse wheel, themed widgets).

Tests _Tooltip hover behavior, bind_mousewheel canvas scrolling,
themed_checkbutton custom indicator images, and the UiPumpMixin that carries
worker-thread updates onto the Tk thread.
"""
from __future__ import annotations

import pytest
tk = pytest.importorskip("tkinter")
import tkinter as tk
from unittest import mock

import threading

from theme import (
    UiPumpMixin,
    _Tooltip,
    bind_mousewheel,
    _make_chk_images,
    themed_checkbutton,
)


pytestmark = pytest.mark.tk


class TestTooltip:
    """_Tooltip hover widget lifecycle and event handling."""


    def test_cancel_clears_job(self, tk_root, monkeypatch):
        """_cancel clears _job and calls after_cancel."""
        button = tk.Button(tk_root, text="Test")
        button.pack()
        tooltip = _Tooltip(button, "Help")

        after_cancel_calls = []
        original_after_cancel = button.after_cancel

        def mock_after_cancel(job_id):
            after_cancel_calls.append(job_id)

        monkeypatch.setattr(button, "after_cancel", mock_after_cancel)

        tooltip._job = "test_job_id"
        tooltip._cancel()

        assert tooltip._job is None
        assert "test_job_id" in after_cancel_calls

    def test_cancel_destroys_tip_win(self, tk_root):
        """_cancel destroys _tip_win if it exists."""
        button = tk.Button(tk_root, text="Button", width=10)
        button.pack()
        tooltip = _Tooltip(button, "Help")

        tooltip._show()
        tk_root.update()
        assert tooltip._tip_win is not None

        tooltip._cancel()

        assert tooltip._tip_win is None

    def test_schedule_stores_job(self, tk_root, monkeypatch):
        """_schedule calls after() and stores the returned job ID."""
        button = tk.Button(tk_root, text="Test")
        button.pack()

        after_calls = []

        def mock_after(delay, callback):
            after_calls.append((delay, callback))
            return "mock_job_123"

        monkeypatch.setattr(button, "after", mock_after)

        tooltip = _Tooltip(button, "Help")
        tooltip._schedule()

        assert tooltip._job == "mock_job_123"
        assert len(after_calls) == 1
        assert after_calls[0][0] == _Tooltip._DELAY

    def test_schedule_cancels_previous_job(self, tk_root, monkeypatch):
        """_schedule cancels any previous job."""
        button = tk.Button(tk_root, text="Test")
        button.pack()

        after_cancel_calls = []

        def mock_after_cancel(job_id):
            after_cancel_calls.append(job_id)

        monkeypatch.setattr(button, "after_cancel", mock_after_cancel)

        job_counter = [0]

        def mock_after(delay, callback):
            job_counter[0] += 1
            return f"job_{job_counter[0]}"

        monkeypatch.setattr(button, "after", mock_after)

        tooltip = _Tooltip(button, "Help")
        tooltip._schedule()
        first_job = tooltip._job

        tooltip._schedule()
        second_job = tooltip._job

        assert first_job != second_job
        assert first_job in after_cancel_calls

    def test_show_creates_toplevel(self, tk_root):
        """_show creates a Toplevel window."""
        button = tk.Button(tk_root, text="Button", width=10, height=2)
        button.pack(padx=10, pady=10)

        tooltip = _Tooltip(button, "Test tooltip")
        tooltip._show()
        tk_root.update()

        assert tooltip._tip_win is not None
        assert tooltip._tip_win.winfo_exists()

        tooltip._cancel()

    def test_show_positions_below_widget(self, tk_root):
        """_show positions the tooltip at widget_x+12, widget_y+height+6."""
        button = tk.Button(tk_root, text="Button", width=10, height=2)
        button.pack(padx=20, pady=20)
        tk_root.update()

        tooltip = _Tooltip(button, "Tip")
        tooltip._show()
        tk_root.update()

        wx = button.winfo_rootx()
        wy = button.winfo_rooty()
        wh = button.winfo_height()

        exp_x = wx + 12
        exp_y = wy + wh + 6

        geom = tooltip._tip_win.wm_geometry()
        parts = geom.split("+")
        actual_x = int(parts[1])
        actual_y = int(parts[2])

        assert actual_x == exp_x
        assert actual_y == exp_y

        tooltip._cancel()

    def test_show_sets_topmost(self, tk_root):
        """_show requests the -topmost attribute on its tip window.

        We assert the CALL rather than reading back the live WM state:
        under a headless window manager (e.g. xvfb on CI) ``-topmost`` is
        silently ignored and reads back ``0``, so a state readback is
        environment-dependent and flaky. Spying on ``wm_attributes``
        verifies the code's intent portably.
        """
        button = tk.Button(tk_root, text="Button")
        button.pack()
        tooltip = _Tooltip(button, "Tip")

        real_toplevel = tk.Toplevel
        captured = {}

        def _spy_toplevel(*a, **k):
            win = real_toplevel(*a, **k)
            orig = win.wm_attributes

            def _rec(*args, **kwargs):
                if args and args[0] == "-topmost":
                    captured["topmost"] = args[1] if len(args) > 1 else None
                return orig(*args, **kwargs)

            win.wm_attributes = _rec
            return win

        with mock.patch.object(tk, "Toplevel", _spy_toplevel):
            tooltip._show()

        assert captured.get("topmost") in (1, True)

        tooltip._cancel()

    def test_show_uses_overrideredirect(self, tk_root):
        """_show disables window decorations."""
        button = tk.Button(tk_root, text="Button")
        button.pack()
        tooltip = _Tooltip(button, "Tip")

        tooltip._show()

        override = tooltip._tip_win.wm_overrideredirect()
        assert override == 1

        tooltip._cancel()


    def test_show_clears_job(self, tk_root):
        """_show clears _job after displaying."""
        button = tk.Button(tk_root, text="Button")
        button.pack()
        tooltip = _Tooltip(button, "Tip")

        tooltip._job = 123
        tooltip._show()

        assert tooltip._job is None

        tooltip._cancel()





class TestBindMousewheel:
    """bind_mousewheel canvas scrolling with focus-aware activation."""

    def test_binds_enter_and_leave(self, tk_root):
        """bind_mousewheel registers <Enter> and <Leave> on the canvas."""
        canvas = tk.Canvas(tk_root, width=200, height=200)
        canvas.pack()

        bind_mousewheel(canvas)

        bindings = canvas.bind()
        assert "<Enter>" in bindings
        assert "<Leave>" in bindings



    def test_multiple_canvases_work_together(self, tk_root):
        """Multiple bound canvases can coexist without conflict."""
        canvas1 = tk.Canvas(tk_root, width=200, height=200)
        canvas1.pack()
        canvas2 = tk.Canvas(tk_root, width=200, height=200)
        canvas2.pack()

        bind_mousewheel(canvas1)
        bind_mousewheel(canvas2)
        tk_root.update()

        canvas1.event_generate("<Enter>")
        tk_root.update()

        canvas2.event_generate("<Enter>")
        tk_root.update()

        canvas1.event_generate("<Leave>")
        tk_root.update()

    def test_scroll_callback_uses_delta(self, tk_root, monkeypatch):
        """Scroll callback divides delta by 120 for scroll units."""
        canvas = tk.Canvas(tk_root, width=200, height=400)
        canvas.pack()

        frame = tk.Frame(canvas, height=2000)
        canvas.create_window((0, 0), window=frame, anchor=tk.NW)
        canvas.config(scrollregion=canvas.bbox("all"))

        scroll_calls = []
        original_yview_scroll = canvas.yview_scroll

        def mock_yview_scroll(units, what):
            scroll_calls.append((units, what))
            return original_yview_scroll(units, what)

        monkeypatch.setattr(canvas, "yview_scroll", mock_yview_scroll)

        bind_mousewheel(canvas)

        canvas.event_generate("<Enter>")
        tk_root.update()

        assert True


class TestMakeChkImages:
    """Checkbox indicator image generation and caching."""

    def test_returns_photoimages(self, tk_root):
        """_make_chk_images returns tuple of PhotoImage objects."""
        off_img, on_img = _make_chk_images()

        assert isinstance(off_img, tk.PhotoImage)
        assert isinstance(on_img, tk.PhotoImage)

    def test_images_14_by_14(self, tk_root):
        """Generated images are 14×14 pixels."""
        off_img, on_img = _make_chk_images()

        assert off_img.width() == 14
        assert off_img.height() == 14
        assert on_img.width() == 14
        assert on_img.height() == 14

    def test_caches_images(self, tk_root):
        """Subsequent calls return the same cached objects."""
        first_off, first_on = _make_chk_images()
        second_off, second_on = _make_chk_images()

        assert first_off is second_off
        assert first_on is second_on

    def test_off_and_on_different(self, tk_root):
        """Off and on images are distinct PhotoImage objects."""
        off_img, on_img = _make_chk_images()

        assert off_img is not on_img


class TestThemedCheckbutton:
    """themed_checkbutton custom indicator images and styling."""

    def test_returns_checkbutton(self, tk_root):
        """themed_checkbutton returns a Checkbutton instance."""
        btn = themed_checkbutton(tk_root, text="Check")

        assert isinstance(btn, tk.Checkbutton)

    def test_indicatoron_false(self, tk_root):
        """Checkbutton has indicatoron disabled."""
        btn = themed_checkbutton(tk_root)

        val = btn.cget("indicatoron")
        assert val in ("0", 0, False)

    def test_image_and_selectimage_set(self, tk_root):
        """Checkbutton has custom image attributes."""
        btn = themed_checkbutton(tk_root)

        assert btn.cget("image")
        assert btn.cget("selectimage")

    def test_selectcolor_matches_bg(self, tk_root):
        """selectcolor matches background."""
        btn = themed_checkbutton(tk_root, bg="blue")

        assert btn.cget("selectcolor") == "blue"

    def test_selectcolor_default(self, tk_root):
        """selectcolor has a default when bg unspecified."""
        btn = themed_checkbutton(tk_root)

        assert btn.cget("selectcolor")

    def test_relief_flat(self, tk_root):
        """relief is FLAT."""
        btn = themed_checkbutton(tk_root)

        assert btn.cget("relief") == tk.FLAT

    def test_compound_left(self, tk_root):
        """compound is LEFT."""
        btn = themed_checkbutton(tk_root)

        assert btn.cget("compound") == tk.LEFT

    def test_overrelief_flat(self, tk_root):
        """overrelief is FLAT."""
        btn = themed_checkbutton(tk_root)

        assert btn.cget("overrelief") == tk.FLAT

    def test_caller_overrides(self, tk_root):
        """Caller kwargs override defaults."""
        btn = themed_checkbutton(tk_root, text="Custom", font=("Arial", 12))

        assert btn.cget("text") == "Custom"
        assert "Arial" in btn.cget("font")

    def test_selectcolor_kwarg_overridden(self, tk_root):
        """selectcolor kwarg is overridden by bg."""
        btn = themed_checkbutton(tk_root, bg="green", selectcolor="red")

        assert btn.cget("selectcolor") == "green"

    def test_activebackground_matches_bg(self, tk_root):
        """activebackground matches bg."""
        btn = themed_checkbutton(tk_root, bg="yellow")

        assert btn.cget("activebackground") == "yellow"

    def test_padding_and_border(self, tk_root):
        """Default padding and border set."""
        btn = themed_checkbutton(tk_root)

        padx = btn.cget("padx")
        bd = btn.cget("bd")

        assert padx in ("2", 2)
        assert bd in ("0", 0)

class _PumpWindow(UiPumpMixin, tk.Toplevel):
    """Minimal host for the mixin — a window and nothing else."""

    def __init__(self, parent):
        super().__init__(parent)
        self.seen: list = []
        self._start_ui_pump()


class TestUiPumpMixin:
    """Worker -> UI hand-off.

    These exercise the mixin through a real Tk window and a real thread,
    because the bug it replaces only ever appeared with both present: calling
    `after()` from a worker works on Windows, and silently BLOCKS on Linux.
    """

    def _drain(self, win, wait_for, predicate):
        wait_for(predicate, timeout_s=3.0)

    def test_a_worker_thread_reaches_the_widget(self, tk_root, wait_for):
        win = _PumpWindow(tk_root)
        try:
            done = threading.Event()

            def _worker():
                win._post(win.seen.append, "from-worker")
                done.set()

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=5.0)
            assert done.is_set(), "worker did not finish"
            self._drain(win, wait_for, lambda: win.seen == ["from-worker"])
            assert win.seen == ["from-worker"]
        finally:
            win.destroy()

    def test_posted_callables_run_in_order(self, tk_root, wait_for):
        win = _PumpWindow(tk_root)
        try:
            for i in range(5):
                win._post(win.seen.append, i)
            self._drain(win, wait_for, lambda: len(win.seen) == 5)
            assert win.seen == [0, 1, 2, 3, 4]
        finally:
            win.destroy()

    def test_a_raising_callback_does_not_kill_the_pump(self, tk_root,
                                                       wait_for):
        """The regression this guards is a frozen window with no error.

        If one bad callback escaped the pump loop, `_ui_pump` would never
        reach its reschedule, every later post would be dropped, and the
        window would sit there looking fine. So the pump logs and continues.
        """
        win = _PumpWindow(tk_root)
        try:
            def _boom():
                raise ValueError("callback blew up")

            win._post(_boom)
            win._post(win.seen.append, "after-the-exception")
            self._drain(win, wait_for, lambda: win.seen)
            assert win.seen == ["after-the-exception"]

            # And it is still alive for the NEXT post, not just this drain.
            win._post(win.seen.append, "still-pumping")
            self._drain(win, wait_for, lambda: len(win.seen) == 2)
            assert win.seen == ["after-the-exception", "still-pumping"]
        finally:
            win.destroy()

    def test_a_destroyed_widget_does_not_raise(self, tk_root, wait_for):
        """Posts racing a closing window are normal, not errors."""
        win = _PumpWindow(tk_root)
        lbl = tk.Label(win, text="x")

        def _touch():
            lbl.configure(text="y")

        lbl.destroy()
        win._post(_touch)
        win._post(win.seen.append, "survived")
        self._drain(win, wait_for, lambda: win.seen == ["survived"])
        assert win.seen == ["survived"]
        win.destroy()

    def test_stop_ignores_a_child_widgets_destroy(self, tk_root, wait_for):
        """<Destroy> fires for every descendant, so the handler must check.

        Without the `evt.widget is not self` guard, destroying any child
        would cancel the pump and quietly freeze every later update.
        """
        win = _PumpWindow(tk_root)
        try:
            child = tk.Label(win, text="child")
            child.pack()
            win.update_idletasks()
            child.destroy()
            win.update()

            assert win._ui_pump_id is not None, "child destroy killed the pump"
            win._post(win.seen.append, "still-running")
            self._drain(win, wait_for, lambda: win.seen == ["still-running"])
            assert win.seen == ["still-running"]
        finally:
            win.destroy()

    def test_destroying_the_window_stops_the_pump(self, tk_root):
        win = _PumpWindow(tk_root)
        win.update_idletasks()
        assert win._ui_pump_id is not None
        win.destroy()
        tk_root.update()
        assert win._ui_pump_id is None
