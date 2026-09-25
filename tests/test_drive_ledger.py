"""tests/test_drive_ledger.py — the ledger keeps what a driven run saw.

The ledger listens on process-wide channels, so the failure that matters most is
not "it missed something" but "it changed what the application does": an
exception hook that no longer chains, a logging handler that duplicates output,
a restore that deletes somebody else's newer hook. Those are pinned first.

Every test that installs restores the hooks in a `finally`, because a leaked
`sys.excepthook` wrapper would make every later failure in the suite report
itself twice.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import warnings

import pytest

from helpers import drive_ledger as dl


@pytest.fixture
def ledger():
    """A ledger that is always uninstalled, whatever the test did."""
    led = dl.Ledger()
    try:
        yield led
    finally:
        led.uninstall()


# ── hooks: restored exactly, chained, and fail-open ───────────────────────

def test_install_and_uninstall_restore_every_process_hook(ledger):
    before = (sys.excepthook, threading.excepthook, warnings.showwarning)
    ledger.install()
    assert (sys.excepthook, threading.excepthook,
            warnings.showwarning) != before, "install changed nothing"
    ledger.uninstall()
    assert (sys.excepthook, threading.excepthook,
            warnings.showwarning) == before


def test_the_exception_hook_records_and_still_chains(ledger, monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a))
    ledger.install()
    ledger.start_drive()
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    assert len(seen) == 1, "the original hook was not called exactly once"
    [entry] = ledger.in_phase(dl.DRIVE)
    assert entry.kind == dl.UNCAUGHT_EXCEPTION
    assert "ValueError: boom" in entry.messages[0]
    assert "Traceback" in entry.traceback
    assert ledger.failed


def test_a_recording_fault_never_stops_the_original_hook(ledger, monkeypatch):
    """The ledger is observability. It must not become a new failure source."""
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a))
    ledger.install()
    monkeypatch.setattr(ledger, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        raise KeyError("k")
    except KeyError:
        sys.excepthook(*sys.exc_info())          # must not raise
    assert len(seen) == 1


def test_a_worker_thread_exception_is_recorded_and_chained(ledger, monkeypatch):
    seen = []
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append(a))
    ledger.install()
    ledger.start_drive()

    def work():
        raise RuntimeError("worker died")

    t = threading.Thread(target=work, name="doctor-worker")
    t.start()
    t.join(2)

    assert len(seen) == 1
    [entry] = ledger.in_phase(dl.DRIVE)
    assert entry.kind == dl.WORKER_FAILURE
    assert ledger.contains("worker died")


def test_warnings_are_recorded_and_still_shown(ledger, monkeypatch):
    shown = []
    monkeypatch.setattr(warnings, "showwarning",
                        lambda *a, **k: shown.append(a))
    ledger.install()
    ledger.start_drive()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        # catch_warnings resets showwarning on exit, so call our hook directly:
        warnings.showwarning("odd thing", UserWarning, "f.py", 3)
    assert len(shown) == 1
    assert ledger.contains("UserWarning: odd thing")


def test_logging_leaves_existing_handlers_alone(ledger):
    """One warning, one existing handler: that handler sees exactly one record."""
    class Collect(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    collector = Collect()
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    root.addHandler(collector)
    try:
        ledger.install()
        ledger.start_drive()
        logging.getLogger("tsm").warning("careful now")
        assert len(collector.records) == 1, "the existing handler was duplicated"
        assert ledger.contains("careful now")
        ledger.uninstall()
        assert root.handlers == handlers_before + [collector], \
            "uninstall removed or left something other than its own handler"
    finally:
        root.removeHandler(collector)


def test_logging_below_warning_is_not_kept(ledger):
    ledger.install()
    ledger.start_drive()
    logging.getLogger("tsm").info("chatter")
    assert not ledger.contains("chatter")


# ── conditional restore ───────────────────────────────────────────────────

def test_a_hook_replaced_meanwhile_is_left_alone_and_drift_is_recorded(ledger):
    ledger.install()
    ledger.start_drive()

    def theirs(*a):
        pass

    sys.excepthook = theirs
    try:
        ledger.uninstall()
        assert sys.excepthook is theirs, "clobbered somebody else's hook"
    finally:
        sys.excepthook = sys.__excepthook__
    kinds = [e.kind for e in ledger.in_phase(dl.DRIVE)]
    assert dl.HOOK_DRIFT in kinds
    assert not ledger.failed, "drift is about the harness, not the application"


class _InheritingApp:
    """report_callback_exception comes from the CLASS, as it does on tk.Tk."""

    def __init__(self):
        self.calls = []

    def report_callback_exception(self, exc, val, tb):
        self.calls.append(val)


def test_tk_callback_exceptions_are_recorded_and_chained(ledger):
    app = _InheritingApp()
    ledger.install_tk(app)
    ledger.start_drive()
    app.report_callback_exception(ValueError, ValueError("in a button"), None)
    assert len(app.calls) == 1, "the original handler (manager.log) was skipped"
    assert ledger.contains("in a button")
    assert ledger.failed


def test_restoring_an_inherited_tk_hook_deletes_the_instance_attribute(ledger):
    """Restoring the same STATE, not pinning a bound copy onto the instance."""
    app = _InheritingApp()
    assert "report_callback_exception" not in vars(app)
    ledger.install_tk(app)
    assert "report_callback_exception" in vars(app)
    ledger.uninstall()
    assert "report_callback_exception" not in vars(app)
    app.report_callback_exception(ValueError, ValueError("after"), None)
    assert app.calls and str(app.calls[-1]) == "after"


def test_restoring_an_instance_tk_hook_restores_that_object(ledger):
    app = _InheritingApp()

    def mine(exc, val, tb):
        pass

    app.report_callback_exception = mine
    ledger.install_tk(app)
    ledger.uninstall()
    assert app.report_callback_exception is mine


def test_a_tk_hook_replaced_meanwhile_is_not_clobbered(ledger):
    app = _InheritingApp()
    ledger.install_tk(app)
    ledger.start_drive()

    def theirs(exc, val, tb):
        pass

    app.report_callback_exception = theirs
    ledger.uninstall()
    assert app.report_callback_exception is theirs
    assert any(e.kind == dl.HOOK_DRIFT for e in ledger.in_phase(dl.DRIVE))


# ── phases and what fails a run ───────────────────────────────────────────

def test_a_startup_warning_is_kept_but_is_not_what_expect_clean_asks(ledger):
    ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING", "fp", "launch noise")
    ledger.start_drive()
    assert ledger.drive_diagnostics() == []
    assert [e.messages[0] for e in ledger.in_phase(dl.STARTUP)] == ["launch noise"]
    assert not ledger.failed


def test_a_startup_exception_still_fails_the_run(ledger):
    """Phase must not become a way to excuse a catastrophic launch."""
    ledger.record(dl.UNCAUGHT_EXCEPTION, "sys.excepthook", "CRITICAL", "fp", "crash")
    ledger.start_drive()
    assert ledger.failed and not ledger.passed


def test_a_warning_alone_does_not_fail_a_run(ledger):
    ledger.start_drive()
    ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING", "fp", "hmm")
    assert not ledger.failed
    assert len(ledger.drive_diagnostics()) == 1


def test_a_failed_expectation_is_permanent(ledger):
    ledger.start_drive()
    ledger.expect("a", False, expected=1, actual=2, elapsed_ms=5, reason="nope")
    ledger.expect("a", True, expected=1, actual=1, elapsed_ms=9)
    assert ledger.failed and not ledger.passed
    assert [e["passed"] for e in ledger.expectations] == [False, True]


def test_a_step_failure_keeps_its_evidence(ledger):
    ledger.start_drive()
    ledger.step_failed(3, "click", "no such button", KeyError("k"))
    [entry] = ledger.in_phase(dl.DRIVE)
    assert entry.kind == dl.STEP_FAILURE
    assert entry.detail["step_index"] == 3
    assert entry.detail["step"] == "click"
    assert entry.detail["exception"] == "KeyError"
    assert ledger.failed


def test_aggregation_counts_but_keeps_distinct_messages(ledger):
    ledger.start_drive()
    for _ in range(3):
        ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING", "same", "one")
    ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING", "same", "two")
    [entry] = ledger.in_phase(dl.DRIVE)
    assert entry.count == 4
    assert entry.messages == ["one", "two"]
    assert entry.first <= entry.last


def test_the_ledger_is_safe_to_hit_from_many_threads(ledger):
    ledger.start_drive()

    def hammer():
        for i in range(200):
            ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING",
                          "fp", "m%d" % (i % 3))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    [entry] = ledger.in_phase(dl.DRIVE)
    assert entry.count == 1600
    assert ledger.sequence == 1600


# ── finalizing and the report ─────────────────────────────────────────────

def test_finalize_builds_once_and_returns_the_same_report(ledger):
    calls = []

    def build():
        calls.append(1)
        return {"n": len(calls)}

    first = ledger.finalize(build)
    second = ledger.finalize(build)
    assert first is second and calls == [1]
    assert ledger.finalized


def test_finalize_uninstalls_the_hooks_before_snapshotting(ledger):
    before = sys.excepthook
    ledger.install()
    ledger.finalize(lambda: {})
    assert sys.excepthook is before


def test_the_report_is_evidence_not_a_flag(ledger, tmp_path):
    ledger.record(dl.APPLICATION_WARNING, "logging", "WARNING", "s", "launch")
    ledger.start_drive()
    ledger.step_failed(2, "click", "gone")
    ledger.expect("e", True, expected=1, actual=1, elapsed_ms=1)

    report = dl.build_report(ledger, script=None, run_id="r1", started=1.0,
                             reason="quit", shots=["a.png"])

    assert report["report_schema"] == dl.REPORT_SCHEMA
    assert report["passed"] is False
    assert report["run_id"] == "r1" and report["pid"]
    assert report["finished_because"] == "quit"
    assert report["commit_sha"] is None
    assert report["commit_sha_state"] == dl.COMMIT_UNKNOWN
    assert [e["messages"] for e in report["startup_diagnostics"]] == [["launch"]]
    assert report["steps_failed"][0]["detail"]["step"] == "click"
    assert report["shots"] == ["a.png"]
    json.dumps(report)                                   # plain data


def test_a_clean_run_reports_passed(ledger):
    ledger.start_drive()
    ledger.expect("e", True, expected=1, actual=1, elapsed_ms=1)
    report = dl.build_report(ledger, script=None, run_id="r", started=0.0,
                             reason="quit", shots=[])
    assert report["passed"] is True


def test_the_report_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "run.report.json"
    dl.write_report_atomically(str(target), {"a": "✓"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": "✓"}
    assert [p.name for p in tmp_path.iterdir()] == ["run.report.json"]


def test_an_unreadable_commit_is_unknown_not_a_failure():
    assert dl.commit_info("", ".") == (None, dl.COMMIT_UNKNOWN)
    assert dl.commit_info("definitely-not-a-git-exe-xyz", ".") == \
        (None, dl.COMMIT_UNKNOWN)


def test_script_hash_of_a_missing_file_is_none(tmp_path):
    assert dl.script_hash(None) is None
    assert dl.script_hash(str(tmp_path / "nope.json")) is None
