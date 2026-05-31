"""tests/test_smoke_runner_single.py — run_single_test_file.

Backs the generate-then-verify loop: a freshly AI-generated test is run in
isolation and kept only if it passes. `passed` must be True ONLY on pytest
exit 0. Popen is mocked at the import site (G-E) — no real pytest.
"""
from __future__ import annotations

import subprocess

from helpers import smoke_runner
from helpers.smoke_runner import run_single_test_file


class _FakeProc:
    """Minimal subprocess.Popen stand-in for run_single_test_file."""

    def __init__(self, returncode=0, out="", timeout=False):
        self.returncode = returncode
        self.pid = 4242
        self._out = out
        self._timeout = timeout
        self._timed_out_once = False

    def communicate(self, timeout=None):
        if self._timeout and not self._timed_out_once:
            self._timed_out_once = True
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=timeout)
        return self._out, ""


def _patch_popen(monkeypatch, proc):
    monkeypatch.setattr("helpers.smoke_runner.subprocess.Popen",
                        lambda *a, **k: proc)


def test_passed_when_returncode_zero(monkeypatch):
    _patch_popen(monkeypatch, _FakeProc(0, "2 passed in 0.1s\n"))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is True
    assert "passed" in out


def test_failed_when_returncode_one(monkeypatch):
    _patch_popen(monkeypatch, _FakeProc(1, "E assert False\n1 failed\n"))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is False
    assert "failed" in out


def test_no_tests_collected_counts_as_fail(monkeypatch):
    _patch_popen(monkeypatch, _FakeProc(5, "no tests ran\n"))
    passed, _ = run_single_test_file(".", "tests/test_x.py")
    assert passed is False


def test_timeout_tree_kills_and_returns_false(monkeypatch):
    killed = {}
    _patch_popen(monkeypatch, _FakeProc(timeout=True))
    monkeypatch.setattr(smoke_runner, "_kill_tree",
                        lambda p: killed.setdefault("pid", p.pid))
    passed, out = run_single_test_file(".", "tests/test_x.py", timeout=20)
    assert passed is False
    assert "TIMEOUT" in out
    assert killed.get("pid") == 4242        # the whole tree was killed


def test_launch_oserror_returns_false(monkeypatch):
    monkeypatch.setattr("helpers.smoke_runner.subprocess.Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no python")))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is False
    assert "Failed to launch" in out


def test_passes_short_traceback_flags(monkeypatch):
    captured = {}

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(0)
    monkeypatch.setattr("helpers.smoke_runner.subprocess.Popen", fake_popen)
    run_single_test_file(".", "tests/test_x.py")
    assert "--tb=short" in captured["cmd"]
    assert "--no-header" in captured["cmd"]
    assert "tests/test_x.py" in captured["cmd"]


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    monkeypatch.setattr(smoke_runner.sys, "platform", "win32")
    calls = {}
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run",
                        lambda cmd, **k: calls.setdefault("cmd", cmd))
    smoke_runner._kill_tree(_FakeProc())
    assert calls["cmd"][:3] == ["taskkill", "/F", "/T"]
    assert "4242" in calls["cmd"]
