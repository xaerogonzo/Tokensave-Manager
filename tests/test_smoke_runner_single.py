"""tests/test_smoke_runner_single.py — run_single_test_file.

Backs the generate-then-verify loop: a freshly AI-generated test is run in
isolation and kept only if it passes. `passed` must be True ONLY on pytest
exit 0. subprocess.run is mocked at the import site (G-E) — no real pytest.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from helpers.smoke_runner import run_single_test_file


def _proc(rc, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def test_passed_when_returncode_zero(monkeypatch):
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run",
                        lambda *a, **k: _proc(0, "2 passed in 0.1s\n"))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is True
    assert "passed" in out


def test_failed_when_returncode_one(monkeypatch):
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run",
                        lambda *a, **k: _proc(1, "E   assert False\n1 failed\n"))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is False
    assert "failed" in out


def test_no_tests_collected_counts_as_fail(monkeypatch):
    # pytest exit 5 = no tests collected — must NOT count as passing.
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run",
                        lambda *a, **k: _proc(5, "no tests ran\n"))
    passed, _ = run_single_test_file(".", "tests/test_x.py")
    assert passed is False


def test_timeout_returns_false_with_message(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=20)
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run", boom)
    passed, out = run_single_test_file(".", "tests/test_x.py", timeout=20)
    assert passed is False
    assert "TIMEOUT" in out


def test_launch_oserror_returns_false(monkeypatch):
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no python")))
    passed, out = run_single_test_file(".", "tests/test_x.py")
    assert passed is False
    assert "Failed to launch" in out


def test_passes_short_traceback_flags(monkeypatch):
    captured = {}
    def fake(cmd, **k):
        captured["cmd"] = cmd
        return _proc(0)
    monkeypatch.setattr("helpers.smoke_runner.subprocess.run", fake)
    run_single_test_file(".", "tests/test_x.py")
    assert "--tb=short" in captured["cmd"]
    assert "--no-header" in captured["cmd"]
    assert "tests/test_x.py" in captured["cmd"]
