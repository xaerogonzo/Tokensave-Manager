"""Tests for the purge / verify flow.

tokensave only offers its stale-entry purge prompt on a real TTY, so the
Manager hands off to a terminal and then *verifies* the result rather than
trusting the mechanism. These cover that contract: the handoff is a first-class
outcome, the baseline is reused instead of re-scanning, and every verification
verdict is reachable and distinct — in particular that a failed scan reports
"unverified" and never "no change".

(A pseudoconsole transport that would have answered the prompt in-process was
implemented and abandoned; see docs/WINDOWS_CONPTY_FINDINGS.md.)
"""
from __future__ import annotations

import pytest

from controllers import doctor_ctrl as dc
from controllers.doctor_ctrl import DoctorController, PurgeResult
from helpers import housekeeping


# ── Purge flow ────────────────────────────────────────────────────────────────

class _Cfg:
    tokensave_exe = "tokensave.exe"


def _controller():
    d = DoctorController.__new__(DoctorController)
    d._cfg = _Cfg()
    return d


def _entries(n):
    return [housekeeping.StaleEntry(path=f"D:/gone/{i}") for i in range(n)]


def test_purge_hands_off_and_claims_nothing(monkeypatch):
    """The handoff is a first-class outcome, not an error.

    It must also make no claim about the result: nothing is verified until
    `verify_purge` runs, so `stale_after` stays empty and the verdict blank.
    """
    r = _controller().purge_stale("D:/proj", baseline=_entries(2))
    assert r.status == PurgeResult.HANDED_OFF
    assert len(r.stale_before) == 2
    assert r.stale_after == []
    assert r.verification_status == ""


def test_baseline_prevents_a_second_doctor_run(monkeypatch):
    """Passing a baseline must not trigger another `tokensave doctor`."""
    ctrl = _controller()
    scans = []
    monkeypatch.setattr(ctrl, "scan_stale",
                        lambda *a, **k: scans.append(a) or dc.DoctorScanResult(True))
    ctrl.purge_stale("D:/proj", baseline=_entries(1))
    assert scans == []


def test_nothing_to_purge_reports_verified():
    r = _controller().purge_stale("D:/proj", baseline=[])
    assert r.status == PurgeResult.SUCCESS
    assert r.verification_status == dc.VERIFY_VERIFIED


def test_failed_baseline_scan_is_a_process_error(monkeypatch):
    """A scan we couldn't run must not be mistaken for 'nothing to purge'."""
    ctrl = _controller()
    monkeypatch.setattr(ctrl, "scan_stale",
                        lambda *a, **k: dc.DoctorScanResult(False, error="boom"))
    r = ctrl.purge_stale("D:/proj")
    assert r.status == PurgeResult.PROCESS_ERROR
    assert "boom" in r.error








# ── Verification ──────────────────────────────────────────────────────────────

_ONE_LEFT = """
  ! 1 stale project(s) in global DB (x):
      • D:\\gone\\0
"""


@pytest.mark.parametrize("before,after_text,expected", [
    (3, "", dc.VERIFY_VERIFIED),
    (3, _ONE_LEFT, dc.VERIFY_PARTIAL),
])
def test_verification_outcomes(monkeypatch, before, after_text, expected):
    ctrl = _controller()
    monkeypatch.setattr(
        ctrl, "scan_stale",
        lambda *a, **k: dc.DoctorScanResult(True, transcript=after_text))
    r = ctrl.verify_purge("D:/proj", _entries(before))
    assert r.verification_status == expected


def test_no_change_is_distinct_from_partial(monkeypatch):
    ctrl = _controller()
    same = """
  ! 2 stale project(s) in global DB (x):
      • D:\\gone\\0
      • D:\\gone\\1
"""
    monkeypatch.setattr(
        ctrl, "scan_stale",
        lambda *a, **k: dc.DoctorScanResult(True, transcript=same))
    r = ctrl.verify_purge("D:/proj", _entries(2))
    assert r.verification_status == dc.VERIFY_NO_CHANGE
    assert r.status == PurgeResult.VERIFICATION_FAILED


def test_failed_scan_is_unverified_not_no_change(monkeypatch):
    """'We couldn't find out' must never be reported as 'nothing happened'."""
    ctrl = _controller()
    monkeypatch.setattr(
        ctrl, "scan_stale",
        lambda *a, **k: dc.DoctorScanResult(False, error="timed out"))
    r = ctrl.verify_purge("D:/proj", _entries(2))
    assert r.verification_status == dc.VERIFY_UNVERIFIED
    assert "timed out" in r.error


def test_open_purge_terminal_launches_a_real_console(monkeypatch):
    """The handoff must actually open a terminal, not just say it did.

    Spawned with CREATE_NEW_CONSOLE because the whole point is giving tokensave
    a real TTY — the flag is the feature, so assert on it.
    """
    seen = {}

    def fake_popen(cmd, cwd=None, creationflags=0, **kw):
        seen.update(cmd=cmd, cwd=cwd, flags=creationflags)
        return object()

    monkeypatch.setattr(dc.subprocess, "Popen", fake_popen)
    _controller().open_purge_terminal("D:/proj")
    assert "doctor" in seen["cmd"]
    assert seen["cwd"] == "D:/proj"
    assert seen["flags"] == dc.subprocess.CREATE_NEW_CONSOLE


def test_verification_labels_render_a_count():
    r = PurgeResult(PurgeResult.VERIFICATION_FAILED,
                    stale_after=_entries(3),
                    verification_status=dc.VERIFY_PARTIAL)
    assert "3 remaining" in DoctorController.verification_label(r)
