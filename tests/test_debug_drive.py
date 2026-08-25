"""tests/test_debug_drive.py — the in-process driver's dispatch and guards.

The driver exists so a live check does not need the mouse. That means its
failure modes are all "the run stopped and looked like a hang": a bad step
name, a step handler raising, a script that is not a list. Each of those has
to degrade to a printed line and the NEXT step, because the next step is
usually the `quit` that ends the run.

The steps themselves are thin wrappers over Tk calls; what is worth pinning is
that one bad step cannot strand the chain, and that `click` reports the button
it actually pressed even when pressing it destroys that button.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.tk

import tkinter as tk

import debug_drive
from debug_drive import _Driver, _walk, _widget_text, start_if_requested


class _FakeApp:
    """Stands in for `App`: records `after` instead of scheduling it."""

    def __init__(self):
        self.scheduled = []
        self._cfg = None

    def after(self, delay, fn=None):
        self.scheduled.append((delay, fn))
        return "timer"

    def destroy(self):
        self.destroyed = True


def _drive(steps):
    app = _FakeApp()
    return app, _Driver(app, steps)


def test_unknown_step_does_not_stop_the_chain(capsys):
    """A typo in a step name must not strand the run before `quit`."""
    app, driver = _drive([{"do": "nonsense"}, {"do": "wait"}])
    driver._run_next()

    assert "unknown step" in capsys.readouterr().out
    assert app.scheduled, "the next step was never scheduled"


def test_a_raising_handler_does_not_stop_the_chain(capsys, monkeypatch):
    app, driver = _drive([{"do": "boom"}, {"do": "wait"}])
    monkeypatch.setattr(
        _Driver, "_do_boom",
        lambda self, step: (_ for _ in ()).throw(RuntimeError("nope")),
        raising=False)

    driver._run_next()

    out = capsys.readouterr().out
    assert "failed" in out and "nope" in out
    assert app.scheduled, "the next step was never scheduled"


def test_after_ms_controls_the_gap_between_steps():
    app, driver = _drive([{"do": "wait", "after_ms": 2500}])
    driver._run_next()
    assert app.scheduled[0][0] == 2500


def test_default_gap_is_used_when_a_step_is_silent():
    app, driver = _drive([{"do": "wait"}])
    driver._run_next()
    assert app.scheduled[0][0] == debug_drive._DEFAULT_AFTER_MS


def test_running_past_the_last_step_stops_cleanly(capsys):
    app, driver = _drive([])
    driver._run_next()
    assert "done" in capsys.readouterr().out
    assert not app.scheduled


def test_start_if_requested_is_inert_without_the_env_var(monkeypatch):
    """The driver must be invisible in a normal launch."""
    monkeypatch.setattr(debug_drive, "_DRIVE_SCRIPT", None)
    assert start_if_requested(_FakeApp()) is None


def test_start_if_requested_reports_a_missing_script(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(debug_drive, "_DRIVE_SCRIPT", str(tmp_path / "nope.json"))
    assert start_if_requested(_FakeApp()) is None
    assert "cannot read" in capsys.readouterr().out


def test_start_if_requested_rejects_a_non_list_script(monkeypatch, capsys, tmp_path):
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"do": "wait"}), encoding="utf-8")
    monkeypatch.setattr(debug_drive, "_DRIVE_SCRIPT", str(script))

    assert start_if_requested(_FakeApp()) is None
    assert "not a JSON list" in capsys.readouterr().out


# ── widget helpers ────────────────────────────────────────────────────────

def test_walk_visits_every_descendant(tk_root):
    outer = tk.Frame(tk_root)
    inner = tk.Frame(outer)
    leaf = tk.Label(inner, text="deep")
    found = list(_walk(outer))
    assert outer in found and inner in found and leaf in found


def test_widget_text_is_empty_for_widgets_without_it(tk_root):
    """`cget("text")` raises for a Frame, and this runs over every widget."""
    assert _widget_text(tk.Frame(tk_root)) == ""
    assert _widget_text(tk.Label(tk_root, text=" Hi ")) == " Hi "


def test_widget_text_survives_a_destroyed_widget(tk_root):
    label = tk.Label(tk_root, text="gone")
    label.destroy()
    assert _widget_text(label) == ""


def test_click_reports_the_button_even_when_invoking_destroys_it(tk_root, capsys):
    """The `show` button re-renders its pane, destroying itself.

    Reading the label after `invoke()` returned "", which reads as "matched
    the wrong widget" rather than "worked, then went away".
    """
    frame = tk.Frame(tk_root)
    pressed = []

    def _rerender():
        pressed.append(True)
        button.destroy()

    button = tk.Button(frame, text="show", command=_rerender)
    button.pack()

    app = _FakeApp()
    driver = _Driver(app, [])
    driver._dialog = frame
    driver._do_click({"do": "click", "text": "show"})

    assert pressed, "the command never ran"
    assert "'show'" in capsys.readouterr().out


def test_click_reports_when_nothing_matches(tk_root, capsys):
    driver = _Driver(_FakeApp(), [])
    driver._dialog = tk.Frame(tk_root)
    driver._do_click({"do": "click", "text": "absent"})
    assert "no button matching" in capsys.readouterr().out


def test_shot_without_a_path_is_reported_not_raised(capsys):
    driver = _Driver(_FakeApp(), [])
    driver._do_shot({"do": "shot"})
    assert "no path" in capsys.readouterr().out
