"""tests/test_codegraph_daemon.py — codegraph daemon list/kill/unlock helpers.

Pure-function tests (no Tk, no real subprocess). `codegraph daemon` is
normally an interactive TTY picker; these helpers exist specifically to
avoid driving that protocol — listing via closed stdin (confirmed safe live)
and stopping via direct OS-level PID termination instead.
"""
from __future__ import annotations

from types import SimpleNamespace

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

def test_kill_dispatches_taskkill_on_windows(mocker):
    mocker.patch("helpers.codegraph_daemon.sys.platform", "win32")
    run = mocker.patch("helpers.codegraph_daemon.subprocess.run",
                       return_value=_proc(0, stdout="SUCCESS"))
    ok, detail = kill_codegraph_daemon(4321)
    assert ok is True
    assert run.call_args.args[0] == ["taskkill", "/F", "/PID", "4321"]


def test_kill_taskkill_failure_reports_detail(mocker):
    mocker.patch("helpers.codegraph_daemon.sys.platform", "win32")
    mocker.patch("helpers.codegraph_daemon.subprocess.run",
                return_value=_proc(1, stderr="ERROR: no such process"))
    ok, detail = kill_codegraph_daemon(4321)
    assert ok is False
    assert "no such process" in detail


def test_kill_posix_sigterm_success(mocker):
    mocker.patch("helpers.codegraph_daemon.sys.platform", "linux")
    calls = []
    def _fake_kill(pid, sig):
        calls.append(sig)
        if len(calls) >= 2:   # process gone after the poll checks it once
            raise ProcessLookupError
    mocker.patch("helpers.codegraph_daemon.os.kill", side_effect=_fake_kill)
    mocker.patch("helpers.codegraph_daemon.time.sleep")
    ok, detail = kill_codegraph_daemon(999)
    assert ok is True
    assert "SIGTERM" in detail


def test_kill_posix_escalates_to_sigkill(mocker):
    """Process survives SIGTERM through the whole poll window → SIGKILL.

    `signal.SIGKILL` doesn't exist on the real Windows `signal` module even
    when `sys.platform` is patched to "linux" for the code path under test
    (`_kill_posix` does a real `import signal`) — `create=True` lets the test
    run on any dev platform.
    """
    mocker.patch("helpers.codegraph_daemon.sys.platform", "linux")
    mocker.patch("signal.SIGKILL", 9, create=True)
    calls = []
    def _fake_kill(pid, sig):
        calls.append(sig)
        # Never raises ProcessLookupError -> poll loop exhausts -> SIGKILL sent.
    mocker.patch("helpers.codegraph_daemon.os.kill", side_effect=_fake_kill)
    mocker.patch("helpers.codegraph_daemon.time.sleep")
    ok, detail = kill_codegraph_daemon(999)
    assert ok is True
    assert 9 in calls
    assert "SIGKILL" in detail


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
