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
import os

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


# -- select / doctor / log: driving the REAL Doctor without a mouse ----------

import threading
from tkinter import ttk


def _tree_app(tk_root, paths, *, cmd_doctor=None):
    """An app whose Projects tab has a nested tree of `proj:` rows."""
    tree = ttk.Treeview(tk_root)
    tree.insert("", "end", iid="cat:Random", text="Random", open=False)
    for path in paths:
        tree.insert("cat:Random", "end", iid="proj:" + path, text=path)
    app = _FakeApp()
    calls = []
    bar = type("Bar", (), {"cmd_doctor": lambda self: calls.append("doctor")})()
    app._projects = type("P", (), {"_tree": tree, "_cmd_bar": bar})()
    return app, tree, calls


def test_select_finds_a_row_by_normalised_path_and_opens_its_parent(
        tk_root, capsys):
    row = os.path.join("root", "Random Projects", "OpenChem Studio")
    spelled = os.path.join("root", "Random Projects", "x", "..",
                           "OpenChem Studio")            # differs on every OS
    app, tree, _ = _tree_app(tk_root, [row])
    driver = _Driver(app, [])

    ok = driver._do_select({"project": spelled})

    assert ok is True
    assert tree.selection() == ("proj:" + row,)
    assert tree.item("cat:Random", "open") in (1, True)     # a collapsed parent
    assert "select ->" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="backslash separators are Windows")
def test_select_matches_backslash_and_forward_slash_spellings(tk_root):
    """The case a real script hits: the tree holds `D:/x`, a user types `D:\\x`."""
    app, tree, _ = _tree_app(tk_root, ["D:/Random Projects/OpenChem Studio"])
    ok = _Driver(app, [])._do_select(
        {"project": "D:\\Random Projects\\OpenChem Studio"})
    assert ok is True


def test_select_says_so_when_no_row_matches(tk_root, capsys):
    app, tree, _ = _tree_app(tk_root, ["D:/a"])
    assert _Driver(app, [])._do_select({"project": "D:/nope"}) is False
    assert tree.selection() == ()
    assert "no project row" in capsys.readouterr().out


def test_select_without_a_tree_is_reported_not_raised(capsys):
    assert _Driver(_FakeApp(), [])._do_select({"project": "D:/a"}) is False
    assert "no project tree" in capsys.readouterr().out


def test_doctor_is_not_started_when_the_row_cannot_be_selected(tk_root):
    """A missing row must never reach `_selected_path`, which raises a modal."""
    app, _, calls = _tree_app(tk_root, ["D:/a"])
    driver = _Driver(app, [])
    driver._do_doctor({"project": "D:/nope"})
    assert calls == []
    assert driver._hold is None


def test_doctor_holds_the_chain_until_the_worker_thread_ends(tk_root):
    app, _, calls = _tree_app(tk_root, ["D:/a"])
    driver = _Driver(app, [{"do": "wait"}])
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="doctor-worker",
                              daemon=True)
    worker.start()

    driver._do_doctor({"project": "D:/a"})

    assert calls == ["doctor"]
    driver._run_next()
    assert driver._index == 0                    # held: the step did not run
    assert app.scheduled[-1][0] == 500           # and it will look again
    release.set()
    worker.join(2)
    driver._run_next()
    assert driver._index == 1                    # released: the chain moved on
    assert driver._hold is None


def test_doctor_gives_up_at_the_timeout_instead_of_hanging_the_run(
        tk_root, capsys):
    app, _, _ = _tree_app(tk_root, ["D:/a"])
    driver = _Driver(app, [])
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="doctor-worker",
                              daemon=True)
    worker.start()
    try:
        driver._do_doctor({"project": "D:/a", "timeout_ms": 0})
        assert driver._hold() is False
        assert "still running at the timeout" in capsys.readouterr().out
    finally:
        release.set()
        worker.join(2)


def _log_app(tk_root, text):
    widget = tk.Text(tk_root)
    widget.insert("1.0", text)
    app = _FakeApp()
    app._output = type("Out", (), {"text": widget})()
    return app


def test_log_report_starts_at_the_last_line_naming_the_marker(tk_root, capsys):
    lines = ["old", "=== Extra checkouts ===", "first",
             "=== Extra checkouts ===", "second", ""]
    text = os.linesep.join(lines)
    _Driver(_log_app(tk_root, text), [])._report_log({"from": "Extra checkouts"})
    out = capsys.readouterr().out
    assert "found=True" in out and "second" in out and "first" not in out


def test_log_report_says_when_the_marker_is_absent(tk_root, capsys):
    """Absent and never-looked-for must not read alike."""
    _Driver(_log_app(tk_root, "nothing to see"), [])._report_log(
        {"from": "Extra checkouts"})
    out = capsys.readouterr().out
    assert "found=False" in out and "0 line(s)" in out


# -- the ledger: what a run proves, not only that it reached the end ---------

from helpers import drive_ledger


def _evidence(steps, **kw):
    app = _FakeApp()
    ledger = drive_ledger.Ledger()
    ledger.start_drive()
    return app, ledger, _Driver(app, steps, ledger=ledger, **kw)


def test_a_raising_step_is_a_failure_of_the_run_not_a_remark(capsys, monkeypatch):
    app, ledger, driver = _evidence([{"do": "boom"}])
    monkeypatch.setattr(
        _Driver, "_do_boom",
        lambda self, step: (_ for _ in ()).throw(RuntimeError("nope")),
        raising=False)
    driver.step()
    [entry] = ledger.in_phase(drive_ledger.DRIVE)
    assert entry.kind == drive_ledger.STEP_FAILURE
    assert entry.detail["step"] == "boom" and entry.detail["exception"] == "RuntimeError"
    assert ledger.failed


def test_an_unknown_step_is_a_failure_of_the_run(capsys):
    _, ledger, driver = _evidence([{"do": "nonsense"}])
    driver.step()
    assert ledger.failed


def test_step_arms_no_timer_but_run_next_does():
    """The half a hand-driven test wants. A per-step timer leaked into a later
    test is what corrupted Fortuna's suite with a heap error."""
    app, _, driver = _evidence([{"do": "wait"}, {"do": "wait"}])
    assert driver.step() is True
    assert app.scheduled == []
    driver._run_next()
    assert len(app.scheduled) == 1


def test_expect_passes_at_once_when_the_state_already_holds():
    app, ledger, driver = _evidence(
        [{"do": "expect", "id": "seen", "check": "diagnostic_contains",
          "text": "hello"}])
    ledger.record(drive_ledger.APPLICATION_WARNING, "logging", "WARNING", "f", "hello")
    driver.step()
    assert driver._hold is None
    assert [e["passed"] for e in ledger.expectations] == [True]
    assert not ledger.failed


def test_expect_fails_permanently_when_the_state_never_arrives():
    _, ledger, driver = _evidence(
        [{"do": "expect", "id": "never", "check": "diagnostic_contains",
          "text": "absent", "within_ms": 0}])
    driver.step()
    assert ledger.failed
    assert ledger.expectations[0]["failure_reason"]


def test_expect_waits_for_state_that_settles_later():
    app, ledger, driver = _evidence(
        [{"do": "expect", "id": "later", "check": "diagnostic_contains",
          "text": "eventually", "within_ms": 60000}])
    driver.step()
    assert driver._hold is not None and driver._hold_ms == 100
    assert driver._hold() is True                       # still waiting
    ledger.record(drive_ledger.APPLICATION_WARNING, "logging", "WARNING", "f",
                  "eventually")
    assert driver._hold() is False                      # arrived
    assert ledger.expectations[-1]["passed"] and not ledger.failed


def test_an_unknown_check_is_a_recorded_failure_not_a_dead_chain():
    _, ledger, driver = _evidence([{"do": "expect", "check": "bogus"}])
    driver.step()
    assert ledger.failed
    assert "unknown check" in ledger.expectations[0]["failure_reason"]


def test_expect_clean_passes_over_a_quiet_window():
    _, ledger, driver = _evidence([{"do": "expect_clean", "settle_ms": 0}])
    driver.step()
    assert not ledger.failed and ledger.expectations[0]["passed"]


def test_expect_clean_fails_on_a_drive_warning():
    _, ledger, driver = _evidence([{"do": "expect_clean", "settle_ms": 0}])
    ledger.record(drive_ledger.APPLICATION_WARNING, "logging", "WARNING", "f", "bad")
    driver.step()
    assert ledger.failed


def test_expect_clean_is_not_failed_by_launch_noise():
    app = _FakeApp()
    ledger = drive_ledger.Ledger()
    ledger.record(drive_ledger.APPLICATION_WARNING, "logging", "WARNING", "f", "launch")
    ledger.start_drive()
    _Driver(app, [{"do": "expect_clean", "settle_ms": 0}], ledger=ledger).step()
    assert not ledger.failed


def test_expect_clean_catches_a_callback_that_throws_after_the_check_began():
    """The false green the observation window exists to prevent."""
    _, ledger, driver = _evidence([{"do": "expect_clean", "settle_ms": 60000}])
    driver.step()
    assert driver._hold() is True                       # quiet so far
    ledger.record(drive_ledger.UNCAUGHT_EXCEPTION, "tk.callback", "CRITICAL", "f",
                  "late")
    assert driver._hold() is False
    assert ledger.failed and not ledger.expectations[-1]["passed"]


def test_a_failed_expect_does_not_make_expect_clean_report_itself_twice():
    _, ledger, driver = _evidence([{"do": "expect_clean", "settle_ms": 0}])
    ledger.expect("x", False, expected=1, actual=2, elapsed_ms=1, reason="r")
    driver.step()
    assert ledger.expectations[-1]["id"] == "expect_clean"
    assert ledger.expectations[-1]["passed"], \
        "the script's own failure was blamed on the application"


def test_finalize_writes_the_report_once_beside_the_script(tmp_path):
    script = tmp_path / "s.json"
    script.write_text("[]", encoding="utf-8")
    _, ledger, driver = _evidence([], script=str(script))
    ledger.expect("e", False, expected=1, actual=2, elapsed_ms=1, reason="r")

    report = driver._finalize("quit")
    written = tmp_path / "s.report.json"
    first = written.read_text(encoding="utf-8")
    driver._finalize("exit")                            # atexit after quit

    assert report["passed"] is False
    assert json.loads(first)["finished_because"] == "quit"
    assert written.read_text(encoding="utf-8") == first, "second finalize rewrote"


def test_quit_on_a_hand_built_driver_never_exits_the_process(capsys):
    """No script means a test, and `os._exit` would end the test runner."""
    app, _, driver = _evidence([])
    driver._do_quit({"do": "quit"})
    assert getattr(app, "destroyed", False)
    assert "exit 0" in capsys.readouterr().out


def test_quit_reports_a_failed_run_with_a_nonzero_code(capsys):
    _, ledger, driver = _evidence([])
    ledger.expect("e", False, expected=1, actual=2, elapsed_ms=1, reason="r")
    driver._do_quit({"do": "quit"})
    assert "exit 1" in capsys.readouterr().out


def test_log_report_prints_the_ledger(capsys):
    _, ledger, driver = _evidence([])
    driver._do_log_report({"do": "log_report"})
    out = capsys.readouterr().out
    assert "startup" in out and "drive" in out and "expectation(s) passed" in out


def test_a_real_tk_callback_exception_reaches_the_ledger(tk_root, capsys):
    """Through real Tk dispatch, not a hand-called hook: this is the channel
    that used to vanish into manager.log with the run still 'finishing'."""
    ledger = drive_ledger.Ledger()
    ledger.install_tk(tk_root)
    ledger.start_drive()
    try:
        tk_root.after(0, lambda: 1 / 0)
        tk_root.update()
    finally:
        ledger.uninstall()
    assert ledger.contains("ZeroDivisionError")
    assert ledger.failed
