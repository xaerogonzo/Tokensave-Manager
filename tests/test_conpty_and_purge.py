"""Tests for the pseudoconsole transport and the purge/verify flow.

Deliberately never stands up a real pseudoconsole. Whether ConPTY works is a
property of the host OS, not of this code, and asserting on it in CI would make
the suite fail for environmental reasons — the same lesson as the headless-Tk
``-topmost`` readback, where the fix was to assert the call was made rather than
the window-manager's answer. So the prompt logic is tested directly and the
transport is stubbed at the seam.
"""
from __future__ import annotations

import sys

import pytest

from controllers import doctor_ctrl as dc
from controllers.doctor_ctrl import DoctorController, PurgeResult
from helpers import conpty, housekeeping


POLICY = conpty.PromptPolicy(prompt_id="stale_purge",
                             prompt_regex=r"purge|stale",
                             answer="y\r", max_answers=3)


def _scan(chunks, policies=(POLICY,)):
    s = conpty._PromptScanner(list(policies))
    found = []
    for c in chunks:
        found += s.feed(c)
    return found


# ── Prompt detection ──────────────────────────────────────────────────────────

def test_prompt_split_across_reads_matches_once():
    """A prompt can arrive in pieces; matching runs on the accumulated buffer."""
    found = _scan(["Pur", "ge ", "stale entries? [y/N]"])
    assert len(found) == 1
    assert found[0][1] is POLICY


def test_multiple_prompts_in_one_read_each_match():
    found = _scan(["Purge stale entries? [y/N] ok\nPurge stale rows? [y/N]"])
    assert len(found) == 2
    assert all(p is POLICY for _, p in found)


def test_ordinary_output_with_question_marks_is_not_a_prompt():
    """Punctuation is not a prompt.

    Every line here ends with a newline, meaning the program moved on rather
    than stopping to wait — that is the distinction that keeps prose from
    tripping the unexpected-prompt safety path.
    """
    found = _scan(["Checking config: ok\nIs it stale? yes it is.\nDone.\n"])
    assert found == []


def test_unrecognised_prompt_is_reported_with_no_policy():
    """Shaped like a prompt, claimed by nothing → caller must refuse to answer."""
    found = _scan(["Enter your name: "])
    assert len(found) == 1
    assert found[0][1] is None


def test_bare_output_produces_nothing():
    assert _scan(["Files 241\nNodes 7214\n"]) == []


# ── Answer budget ─────────────────────────────────────────────────────────────

def test_budget_allows_up_to_the_caller_ceiling():
    answers: dict = {}
    for i in range(POLICY.max_answers):
        assert conpty.budget_left(answers, POLICY) is True
        answers[POLICY.prompt_id] = i + 1
    assert conpty.budget_left(answers, POLICY) is False


def test_budget_is_per_policy():
    other = conpty.PromptPolicy("other", r"x", "y\r", 1)
    answers = {"stale_purge": 99}
    assert conpty.budget_left(answers, other) is True


# ── Capability probing ────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only assertion")
def test_unavailable_off_windows():
    assert conpty.is_available() is False
    assert conpty.can_attach(force=True) is False


def test_can_attach_is_false_when_api_missing(monkeypatch):
    monkeypatch.setattr(conpty, "is_available", lambda: False)
    assert conpty.can_attach(force=True) is False


# ── Purge flow ────────────────────────────────────────────────────────────────

class _Cfg:
    tokensave_exe = "tokensave.exe"


def _controller():
    d = DoctorController.__new__(DoctorController)
    d._cfg = _Cfg()
    return d


def _entries(n):
    return [housekeeping.StaleEntry(path=f"D:/gone/{i}") for i in range(n)]


def test_handed_off_when_conpty_cannot_attach(monkeypatch):
    """The handoff is a first-class outcome, not an error."""
    monkeypatch.setattr(conpty, "can_attach", lambda: False)
    called = []
    monkeypatch.setattr(conpty, "run_interactive",
                        lambda *a, **k: called.append(a))
    r = _controller().purge_stale("D:/proj", baseline=_entries(2))
    assert r.status == PurgeResult.HANDED_OFF
    assert len(r.stale_before) == 2
    assert called == []          # never even attempted the transport


def test_baseline_prevents_a_second_doctor_run(monkeypatch):
    """Passing a baseline must not trigger another `tokensave doctor`."""
    monkeypatch.setattr(conpty, "can_attach", lambda: False)
    ctrl = _controller()
    scans = []
    monkeypatch.setattr(ctrl, "scan_stale",
                        lambda *a, **k: scans.append(a) or dc.DoctorScanResult(True))
    ctrl.purge_stale("D:/proj", baseline=_entries(1))
    assert scans == []


def test_nothing_to_purge_reports_verified(monkeypatch):
    monkeypatch.setattr(conpty, "can_attach", lambda: True)
    r = _controller().purge_stale("D:/proj", baseline=[])
    assert r.status == PurgeResult.SUCCESS
    assert r.verification_status == dc.VERIFY_VERIFIED


@pytest.mark.parametrize("cstatus,expected", [
    (conpty.ConPtyStatus.UNEXPECTED_PROMPT, PurgeResult.UNEXPECTED_PROMPT),
    (conpty.ConPtyStatus.TIMEOUT, PurgeResult.TIMEOUT),
    (conpty.ConPtyStatus.PROCESS_ERROR, PurgeResult.PROCESS_ERROR),
    (conpty.ConPtyStatus.UNAVAILABLE, PurgeResult.UNAVAILABLE),
])
def test_transport_failures_map_to_their_own_status(monkeypatch, cstatus, expected):
    monkeypatch.setattr(conpty, "can_attach", lambda: True)
    monkeypatch.setattr(
        conpty, "run_interactive",
        lambda *a, **k: conpty.ConPtyResult(status=cstatus, error="boom"))
    r = _controller().purge_stale("D:/proj", baseline=_entries(1))
    assert r.status == expected


def test_unexpected_prompt_sends_no_answers(monkeypatch):
    monkeypatch.setattr(conpty, "can_attach", lambda: True)
    monkeypatch.setattr(
        conpty, "run_interactive",
        lambda *a, **k: conpty.ConPtyResult(
            status=conpty.ConPtyStatus.UNEXPECTED_PROMPT, answers_sent={}))
    r = _controller().purge_stale("D:/proj", baseline=_entries(4))
    assert r.status == PurgeResult.UNEXPECTED_PROMPT
    assert r.answers_sent == 0


def test_answer_ceiling_comes_from_the_baseline(monkeypatch):
    """The scan sets an upper bound; it does not predict the prompt count."""
    monkeypatch.setattr(conpty, "can_attach", lambda: True)
    seen = {}

    def fake_run(argv, cwd, policies, **kw):
        seen["max"] = policies[0].max_answers
        return conpty.ConPtyResult(status=conpty.ConPtyStatus.COMPLETED)

    monkeypatch.setattr(conpty, "run_interactive", fake_run)
    ctrl = _controller()
    monkeypatch.setattr(ctrl, "scan_stale",
                        lambda *a, **k: dc.DoctorScanResult(True, transcript=""))
    ctrl.purge_stale("D:/proj", baseline=_entries(7))
    assert seen["max"] == 7


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
