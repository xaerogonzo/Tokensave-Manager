"""tests/test_codegraph_daemon.py — codegraph daemon list/kill/unlock helpers.

Pure-function tests (no Tk, no real subprocess). `codegraph daemon` is
normally an interactive TTY picker; these helpers exist specifically to
avoid driving that protocol — listing via closed stdin (confirmed safe live)
and stopping via direct OS-level PID termination instead.
"""
from __future__ import annotations

from types import SimpleNamespace

from helpers.proc_kill import ProcessIdentity
from helpers.codegraph_daemon import (
    kill_codegraph_daemon,
    list_codegraph_daemons,
    unlock_codegraph_project,
)


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── list_codegraph_daemons ────────────────────────────────────────────────

# Exact live output captured during investigation (v1.5.0, Windows).
_REAL_OUTPUT = (
    r"pid 46424  v1.5.0  up 5m 7s  D:\Random Projects\OpenChem Studio" "\n"
    r"pid 51768  v1.5.0  up 5m 14s  D:\Claude Co worker\Token Save Manager Source"
)


def test_parses_real_live_output_format(mocker, tmp_path):
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    mocker.patch("helpers.codegraph_daemon.subprocess.run",
                return_value=_proc(0, stdout=_REAL_OUTPUT))
    daemons = list_codegraph_daemons(str(exe))
    assert daemons == [
        {"pid": 46424, "version": "1.5.0", "uptime": "5m 7s",
         "path": r"D:\Random Projects\OpenChem Studio"},
        {"pid": 51768, "version": "1.5.0", "uptime": "5m 14s",
         "path": r"D:\Claude Co worker\Token Save Manager Source"},
    ]


def test_uses_closed_stdin_not_interactive_input(mocker, tmp_path):
    """Regression guard: must never pipe input into the interactive picker."""
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    run = mocker.patch("helpers.codegraph_daemon.subprocess.run",
                       return_value=_proc(0, stdout=""))
    list_codegraph_daemons(str(exe))
    assert run.call_args.kwargs["stdin"] is not None
    import subprocess
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_empty_output_returns_empty_list(mocker, tmp_path):
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    mocker.patch("helpers.codegraph_daemon.subprocess.run",
                return_value=_proc(0, stdout=""))
    assert list_codegraph_daemons(str(exe)) == []


def test_missing_binary_returns_empty_list_no_subprocess(mocker):
    run = mocker.patch("helpers.codegraph_daemon.subprocess.run")
    assert list_codegraph_daemons("") == []
    assert list_codegraph_daemons(r"C:\does\not\exist.cmd") == []
    run.assert_not_called()


def test_subprocess_failure_is_fail_open(mocker, tmp_path):
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    mocker.patch("helpers.codegraph_daemon.subprocess.run",
                side_effect=OSError("boom"))
    assert list_codegraph_daemons(str(exe)) == []


def test_malformed_lines_are_skipped(mocker, tmp_path):
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    mocker.patch(
        "helpers.codegraph_daemon.subprocess.run",
        return_value=_proc(0, stdout=(
            "No daemons running.\n"
            "pid abc  v1.0.0  up 1s  /some/path\n"       # non-numeric pid
            r"pid 999  v1.5.0  up 1h 2m 3s  D:\ok\path"
        )))
    daemons = list_codegraph_daemons(str(exe))
    assert daemons == [
        {"pid": 999, "version": "1.5.0", "uptime": "1h 2m 3s",
         "path": r"D:\ok\path"},
    ]


# ── kill_codegraph_daemon ─────────────────────────────────────────────────
#
# The kill mechanics moved to `helpers/proc_kill.py` in Roadmap-10, so the
# argv- and signal-level assertions moved with them (see tests/test_proc_kill.py).
# What belongs HERE is the part that is this caller's own decision: which
# semantics it asks for. Getting that wrong is the regression that folding two
# different kill implementations together invites, and it would be invisible
# at the argv level once the shared helper is doing the work.

def test_kill_asks_for_single_process_graceful_semantics(mocker):
    """A daemon stop must not become a tree kill, nor lose its grace period.

    The two pre-existing implementations differed on exactly these axes —
    tree/single and graceful/immediate — so they are asserted explicitly
    rather than left to whatever the shared helper defaults to.
    """
    kp = mocker.patch("helpers.codegraph_daemon.kill_process",
                      return_value=(True, "terminated via handle"))
    ok, detail = kill_codegraph_daemon(4321)
    assert ok is True
    assert kp.call_args.args[0] == 4321
    assert kp.call_args.kwargs["tree"] is False
    assert kp.call_args.kwargs["graceful"] is True


def test_kill_passes_the_scanned_identity_through_for_verification(mocker):
    """The PID-reuse guard is only real if the caller forwards the identity."""
    ident = ProcessIdentity(pid=4321, created_at=123456, image="codegraph.exe")
    kp = mocker.patch("helpers.codegraph_daemon.kill_process",
                      return_value=(True, "terminated via verified handle"))
    kill_codegraph_daemon(4321, expect=ident)
    assert kp.call_args.kwargs["expect"] is ident


def test_kill_failure_detail_reaches_the_caller(mocker):
    """The dialog shows this string, so it must not be swallowed or reworded."""
    mocker.patch("helpers.codegraph_daemon.kill_process",
                 return_value=(False, "ERROR: no such process"))
    ok, detail = kill_codegraph_daemon(4321)
    assert ok is False
    assert "no such process" in detail


# ── unlock_codegraph_project ──────────────────────────────────────────────

def test_unlock_runs_codegraph_unlock_with_path(mocker, tmp_path):
    exe = tmp_path / "codegraph.cmd"
    exe.write_bytes(b"")
    run = mocker.patch("helpers.codegraph_daemon.subprocess.run",
                       return_value=_proc(0, stdout="Lock removed."))
    ok, detail = unlock_codegraph_project(str(exe), r"D:\some\project")
    assert ok is True
    assert run.call_args.args[0] == [str(exe), "unlock", r"D:\some\project"]


def test_unlock_missing_binary_no_subprocess(mocker):
    run = mocker.patch("helpers.codegraph_daemon.subprocess.run")
    ok, detail = unlock_codegraph_project("", r"D:\x")
    assert ok is False
    run.assert_not_called()
