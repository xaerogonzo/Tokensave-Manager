"""tests/test_proc_kill.py — the shared kill helper.

Two things are being protected here, and they fail in opposite directions.

**Semantics.** This module was created by folding together two kill
implementations that looked interchangeable and were not: one killed a whole
process TREE immediately, the other killed ONE process gracefully. A merge
that lost either axis would have been invisible in review — the smoke runner
would silently start orphaning child pytest processes, or a daemon stop would
stop being graceful. So `tree` and `graceful` are asserted at the argv and
signal level, not assumed.

**Identity.** A PID is not an identity: a process can exit and the OS can
reissue its number, so acting on a PID read moments ago can hit a stranger.
The real-subprocess test at the bottom is the one that matters — it proves a
stale identity is *refused* and the process survives, on whichever platform
is running.
"""
from __future__ import annotations

import subprocess
import sys

from helpers import proc_kill
from helpers.proc_kill import ProcessIdentity, kill_process


# ── ProcessIdentity.matches ───────────────────────────────────────────────

def test_identity_matches_itself():
    ident = ProcessIdentity(pid=10, created_at=555, image="tokensave.exe")
    assert ident.matches(ident)


def test_identity_rejects_a_different_start_time_on_the_same_pid():
    """The whole point: same number, different process."""
    a = ProcessIdentity(pid=10, created_at=555, image="tokensave.exe")
    b = ProcessIdentity(pid=10, created_at=556, image="tokensave.exe")
    assert not a.matches(b)


def test_identity_rejects_a_different_image():
    a = ProcessIdentity(pid=10, created_at=555, image="tokensave.exe")
    b = ProcessIdentity(pid=10, created_at=555, image="notepad.exe")
    assert not a.matches(b)


def test_identity_treats_an_unreadable_image_as_no_evidence():
    """An empty image means "could not read it", not "different process".

    Reading the image name can fail on a process we are otherwise allowed to
    query. Treating that as a mismatch would refuse legitimate stops; the
    start time has already carried the decision by this point.
    """
    a = ProcessIdentity(pid=10, created_at=555, image="")
    b = ProcessIdentity(pid=10, created_at=555, image="tokensave.exe")
    assert a.matches(b) and b.matches(a)


def test_identity_never_matches_none():
    assert not ProcessIdentity(pid=10, created_at=555).matches(None)


# ── tree vs single: the axis a careless merge would have lost ────────────

def test_tree_kill_on_windows_walks_the_child_tree(mocker):
    """`/T` is what stops an orphaned child pytest burning CPU until reboot."""
    mocker.patch.object(proc_kill.sys, "platform", "win32")
    run = mocker.patch.object(proc_kill.subprocess, "run",
                              return_value=subprocess.CompletedProcess(
                                  [], 0, stdout="SUCCESS", stderr=""))
    ok, detail = kill_process(4321, tree=True)
    assert ok is True
    assert run.call_args.args[0] == ["taskkill", "/F", "/T", "/PID", "4321"]


def test_tree_kill_reports_taskkill_failure_detail(mocker):
    mocker.patch.object(proc_kill.sys, "platform", "win32")
    mocker.patch.object(proc_kill.subprocess, "run",
                        return_value=subprocess.CompletedProcess(
                            [], 1, stdout="", stderr="ERROR: no such process"))
    ok, detail = kill_process(4321, tree=True)
    assert ok is False
    assert "no such process" in detail


def test_single_kill_on_windows_does_not_shell_out_to_taskkill(mocker):
    """The deliberate Roadmap-10 change, pinned so it cannot regress.

    `taskkill /PID` re-resolves the number when it runs, so a recycled PID
    would be killed instead. The handle path cannot be redirected that way.
    """
    mocker.patch.object(proc_kill.sys, "platform", "win32")
    run = mocker.patch.object(proc_kill.subprocess, "run")
    win = mocker.patch.object(proc_kill, "_kill_one_windows",
                              return_value=(True, "terminated via handle"))
    ok, _detail = kill_process(4321, tree=False)
    assert ok is True
    win.assert_called_once()
    run.assert_not_called()


# ── graceful vs immediate: the other lost-in-a-merge axis ────────────────

def _posix(mocker, alive_after_sigterm: bool):
    """Drive the POSIX path on any host, with no pidfd and no real signals."""
    mocker.patch.object(proc_kill.sys, "platform", "linux")
    mocker.patch.object(proc_kill, "_pidfd", return_value=None)
    mocker.patch.object(proc_kill.time, "sleep")
    mocker.patch("signal.SIGKILL", 9, create=True)
    mocker.patch("signal.SIGTERM", 15, create=True)
    sent = []

    def _fake_kill(pid, sig):
        sent.append(sig)
        # sig 0 is the liveness probe, not a signal being delivered.
        if sig == 0 and not alive_after_sigterm:
            raise ProcessLookupError

    mocker.patch.object(proc_kill.os, "kill", side_effect=_fake_kill)
    return sent


def test_graceful_posix_kill_tries_sigterm_first(mocker):
    sent = _posix(mocker, alive_after_sigterm=False)
    ok, detail = kill_process(999, graceful=True)
    assert ok is True
    assert sent[0] == 15
    assert 9 not in sent, "process died on SIGTERM; SIGKILL was unnecessary"
    assert "SIGTERM" in detail


def test_graceful_posix_kill_escalates_when_sigterm_is_ignored(mocker):
    sent = _posix(mocker, alive_after_sigterm=True)
    ok, detail = kill_process(999, graceful=True)
    assert ok is True
    assert sent[0] == 15 and 9 in sent
    assert "SIGKILL" in detail


def test_ungraceful_posix_kill_goes_straight_to_sigkill(mocker):
    sent = _posix(mocker, alive_after_sigterm=True)
    ok, _detail = kill_process(999, graceful=False)
    assert ok is True
    assert sent == [9], "graceful=False must not send SIGTERM at all"


def test_a_process_that_is_already_gone_counts_as_success(mocker):
    """Nothing to kill is the goal state, not an error to report."""
    mocker.patch.object(proc_kill.sys, "platform", "linux")
    mocker.patch.object(proc_kill, "_pidfd", return_value=None)
    mocker.patch("signal.SIGTERM", 15, create=True)
    mocker.patch.object(proc_kill.os, "kill", side_effect=ProcessLookupError)
    ok, detail = kill_process(999, graceful=True)
    assert ok is True
    assert "already gone" in detail


# ── honesty about which path ran ─────────────────────────────────────────

def test_the_pid_resolved_path_admits_its_residual_race(mocker):
    """Without a pidfd the signal is PID-addressed, so the race remains.

    Saying so in the detail string is the point: the module must never report
    a guarantee it did not provide.
    """
    _posix(mocker, alive_after_sigterm=False)
    _ok, detail = kill_process(999, graceful=True)
    assert "residual race" in detail


def test_the_pidfd_path_makes_no_such_admission(mocker):
    mocker.patch.object(proc_kill.sys, "platform", "linux")
    mocker.patch.object(proc_kill, "_pidfd", return_value=7)
    mocker.patch.object(proc_kill.os, "close")
    mocker.patch.object(proc_kill.time, "sleep")
    mocker.patch("signal.SIGTERM", 15, create=True)
    mocker.patch("signal.pidfd_send_signal", lambda *a: None, create=True)
    mocker.patch.object(proc_kill.os, "kill", side_effect=ProcessLookupError)
    _ok, detail = kill_process(999, graceful=True)
    assert "residual race" not in detail


# ── identity refusal ─────────────────────────────────────────────────────

def test_a_stale_identity_refuses_the_tree_kill_too(mocker):
    """Tree kill cannot be made race-free, but it can still check first."""
    mocker.patch.object(proc_kill.sys, "platform", "win32")
    run = mocker.patch.object(proc_kill.subprocess, "run")
    mocker.patch.object(proc_kill, "process_identity",
                        return_value=ProcessIdentity(1, 999, "other.exe"))
    ok, detail = kill_process(1, tree=True,
                              expect=ProcessIdentity(1, 111, "tokensave.exe"))
    assert ok is False
    assert "different process" in detail
    run.assert_not_called()      # nothing may be killed once identity fails


def test_a_vanished_process_refuses_rather_than_killing_its_successor(mocker):
    """`process_identity` returning None means the PID no longer resolves."""
    mocker.patch.object(proc_kill.sys, "platform", "linux")
    mocker.patch.object(proc_kill, "process_identity", return_value=None)
    killer = mocker.patch.object(proc_kill.os, "kill")
    ok, _detail = kill_process(1, expect=ProcessIdentity(1, 111, "x"))
    assert ok is False
    killer.assert_not_called()


# ── kill_popen_tree keeps smoke_runner's last resort ─────────────────────

def test_popen_tree_falls_back_to_proc_kill_when_the_pid_path_fails(mocker):
    """`proc.kill()` does not depend on the PID still resolving."""
    mocker.patch.object(proc_kill, "_kill_tree_by_pid",
                        return_value=(False, "taskkill exited 128"))

    class _FakeProc:
        pid = 4242
        killed = False

        def kill(self):
            type(self).killed = True

    proc_kill.kill_popen_tree(_FakeProc())
    assert _FakeProc.killed is True


def test_popen_tree_does_not_double_kill_on_success(mocker):
    mocker.patch.object(proc_kill, "_kill_tree_by_pid",
                        return_value=(True, "tree terminated"))

    class _FakeProc:
        pid = 4242
        killed = False

        def kill(self):
            type(self).killed = True

    proc_kill.kill_popen_tree(_FakeProc())
    assert _FakeProc.killed is False


# ── the guarantee, against a real process ────────────────────────────────

def test_a_stale_identity_cannot_kill_a_real_process():
    """The end-to-end proof, on whichever platform is running.

    Everything above mocks the syscall boundary, which means it verifies the
    logic and not the platform primitive. This spawns a real child, refuses a
    stale identity, and checks the child is *still alive* — the actual
    property the PID-reuse guard exists to provide.
    """
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(30)"])
    try:
        real = proc_kill.process_identity(child.pid)
        assert real is not None, "could not read the child's identity"

        stale = ProcessIdentity(pid=child.pid,
                                created_at=real.created_at - 1_000_000,
                                image=real.image)
        ok, detail = kill_process(child.pid, expect=stale)
        assert ok is False, detail
        assert child.poll() is None, "refused kill must leave the process alive"

        ok, detail = kill_process(child.pid, expect=real)
        assert ok is True, detail
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
