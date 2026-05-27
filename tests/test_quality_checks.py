"""tests/test_quality_checks.py — run_syntax_check + run_pyflakes_check.

Both checks shell out via ``subprocess.run``. Tests mock at the IMPORT
SITE (``helpers.quality_checks.subprocess.run``) — never globally — to
avoid blast radius (G-E).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from helpers.quality_checks import run_pyflakes_check, run_syntax_check


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Build a fake CompletedProcess for mocker.patch return values."""
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── run_syntax_check ──────────────────────────────────────────────────────

def test_syntax_passes_on_rc_zero(tmp_path, mocker):
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(rc=0))
    passed, summary = run_syntax_check(str(tmp_path))
    assert passed is True
    assert "passed" in summary
    assert "0 errors" in summary


def test_syntax_fails_on_rc_nonzero(tmp_path, mocker):
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(
                     rc=1,
                     stderr="*** SyntaxError: invalid syntax (foo.py, line 42)\n"
                            "Some additional noise\n",
                 ))
    passed, summary = run_syntax_check(str(tmp_path))
    assert passed is False
    assert "SyntaxError" in summary
    assert len(summary) <= 200       # truncated to first 200 chars


def test_syntax_summary_truncated_to_200_chars(tmp_path, mocker):
    long_err = "*** SyntaxError: " + ("x" * 500)
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(rc=1, stderr=long_err))
    _passed, summary = run_syntax_check(str(tmp_path))
    assert len(summary) == 200


def test_syntax_fallback_message_on_empty_error(tmp_path, mocker):
    """If subprocess fails with empty stdout/stderr, give a generic
    summary rather than an IndexError."""
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(rc=1))
    passed, summary = run_syntax_check(str(tmp_path))
    assert passed is False
    assert summary  # not empty


def test_syntax_uses_src_subdirectory(tmp_path, mocker):
    """The check targets ``<project>/src/`` — record the actual argv."""
    mock_run = mocker.patch("helpers.quality_checks.subprocess.run",
                            return_value=_proc(rc=0))
    run_syntax_check(str(tmp_path))
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[1] == "-m"
    assert cmd[2] == "compileall"
    assert cmd[3].endswith("src")
    assert "-q" in cmd


# ── run_pyflakes_check ────────────────────────────────────────────────────

def test_pyflakes_passes_on_rc_zero(tmp_path, mocker):
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(rc=0))
    passed, summary = run_pyflakes_check(str(tmp_path))
    assert passed is True
    assert "passed" in summary
    assert "0 warnings" in summary


def test_pyflakes_fails_on_rc_nonzero_single_warning(tmp_path, mocker):
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(
                     rc=1,
                     stdout="src/app.py:42: 'os' imported but unused\n",
                 ))
    passed, summary = run_pyflakes_check(str(tmp_path))
    assert passed is False
    assert "imported but unused" in summary
    assert "more" not in summary    # single warning — no "+N more" suffix


def test_pyflakes_fails_on_multi_warning_with_more_suffix(tmp_path, mocker):
    """Multi-warning output gets the ``(+N more)`` suffix on the first line."""
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(
                     rc=1,
                     stdout=(
                         "src/app.py:42: 'os' imported but unused\n"
                         "src/foo.py:10: undefined name 'bar'\n"
                         "src/baz.py:99: 'json' imported but unused\n"
                     ),
                 ))
    passed, summary = run_pyflakes_check(str(tmp_path))
    assert passed is False
    assert "imported but unused" in summary
    assert "+2 more" in summary


def test_pyflakes_summary_first_line_truncated_to_200_chars(tmp_path, mocker):
    long_warning = "src/x.py:1: " + ("x" * 500)
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(rc=1, stdout=long_warning))
    _passed, summary = run_pyflakes_check(str(tmp_path))
    # First line truncated to 200, no "+N more" since it's 1 line.
    assert len(summary) <= 200


def test_pyflakes_strips_empty_lines_from_count(tmp_path, mocker):
    """Empty lines in the warning stream must not inflate the (+N more) count."""
    mocker.patch("helpers.quality_checks.subprocess.run",
                 return_value=_proc(
                     rc=1,
                     stdout="warning 1\n\n  \nwarning 2\n",
                 ))
    _passed, summary = run_pyflakes_check(str(tmp_path))
    # 2 warnings → "+1 more"
    assert "+1 more" in summary


def test_pyflakes_uses_src_subdirectory(tmp_path, mocker):
    mock_run = mocker.patch("helpers.quality_checks.subprocess.run",
                            return_value=_proc(rc=0))
    run_pyflakes_check(str(tmp_path))
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[1] == "-m"
    assert cmd[2] == "pyflakes"
    assert cmd[3].endswith("src")


# ── Cwd verification ──────────────────────────────────────────────────────

def test_both_checks_run_in_project_cwd(tmp_path, mocker):
    """Both subprocess calls must use cwd=<project_path>."""
    mock_run = mocker.patch("helpers.quality_checks.subprocess.run",
                            return_value=_proc(rc=0))
    run_syntax_check(str(tmp_path))
    _args, kwargs = mock_run.call_args
    assert kwargs["cwd"] == str(tmp_path)

    run_pyflakes_check(str(tmp_path))
    _args, kwargs = mock_run.call_args
    assert kwargs["cwd"] == str(tmp_path)
