"""tests/test_install_codegraph.py — npm-driven codegraph lifecycle.

Tests the three npm wrappers (install/update/uninstall) for correct argv
construction (G-A: absolute npm path, never bare ``"npm"``) and the
``detect_codegraph_after_install`` 3-step fallback chain (G-B).

All ``subprocess.Popen`` / ``subprocess.run`` calls are mocked at the
import site (``helpers.install_codegraph.subprocess.*``) per G-E.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from helpers import install_codegraph
from helpers.install_codegraph import (
    _PKG,
    codegraph_version,
    detect_codegraph_after_install,
    install_codegraph as install_cg,
    uninstall_codegraph,
    update_codegraph,
)


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _popen_mock(stdout_lines=(), rc=0):
    """Build a fake Popen that yields the given lines + returns rc."""
    return SimpleNamespace(
        stdout=iter(list(stdout_lines)),
        wait=lambda timeout=None: rc,
        returncode=rc,
        kill=lambda: None,
    )


@pytest.fixture
def npm_exe(tmp_path):
    """A bogus but existing npm.cmd file so isfile() checks pass."""
    p = tmp_path / "npm.cmd"
    p.write_bytes(b"")
    return str(p)


# ── G-A: absolute path discipline ────────────────────────────────────────

def test_install_refuses_when_npm_exe_empty():
    """G-A: bare 'npm' would FileNotFoundError on Windows. Refuse early."""
    ok, log = install_cg("")
    assert ok is False
    assert "npm not found" in log


def test_install_refuses_when_npm_exe_missing(tmp_path):
    """A path that doesn't point at an actual file is also rejected."""
    ok, log = install_cg(str(tmp_path / "nope-not-here.cmd"))
    assert ok is False
    assert "npm not found" in log


# ── install_codegraph argv ───────────────────────────────────────────────

def test_install_constructs_argv_with_absolute_npm_path(npm_exe, mocker):
    fake = _popen_mock()
    mock_popen = mocker.patch("helpers.install_codegraph.subprocess.Popen",
                              return_value=fake)
    ok, _log = install_cg(npm_exe)
    assert ok is True
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == npm_exe       # absolute path — G-A
    assert cmd[1] == "install"
    assert cmd[2] == "-g"
    assert cmd[3] == _PKG          # @colbymchenry/codegraph


def test_install_streams_to_on_log(npm_exe, mocker):
    fake = _popen_mock(stdout_lines=["added 1 package\n", "audited 2 packages\n"])
    mocker.patch("helpers.install_codegraph.subprocess.Popen", return_value=fake)
    captured = []
    ok, log = install_cg(npm_exe, on_log=captured.append)
    assert ok is True
    assert any("added 1 package" in line for line in captured)
    assert "added 1 package" in log


def test_install_returns_false_on_nonzero_rc(npm_exe, mocker):
    fake = _popen_mock(stdout_lines=["error: permission denied\n"], rc=1)
    mocker.patch("helpers.install_codegraph.subprocess.Popen", return_value=fake)
    ok, log = install_cg(npm_exe)
    assert ok is False
    assert "permission denied" in log


# ── update_codegraph argv ────────────────────────────────────────────────

def test_update_uses_at_latest_suffix(npm_exe, mocker):
    """Per v4.8 design: prefer ``install …@latest`` over ``npm update -g``."""
    fake = _popen_mock()
    mock_popen = mocker.patch("helpers.install_codegraph.subprocess.Popen",
                              return_value=fake)
    ok, _log = update_codegraph(npm_exe)
    assert ok is True
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == npm_exe
    assert cmd[1] == "install"
    assert cmd[2] == "-g"
    assert cmd[3] == f"{_PKG}@latest"


# ── uninstall_codegraph argv ─────────────────────────────────────────────

def test_uninstall_argv(npm_exe, mocker):
    fake = _popen_mock()
    mock_popen = mocker.patch("helpers.install_codegraph.subprocess.Popen",
                              return_value=fake)
    ok, _log = uninstall_codegraph(npm_exe)
    assert ok is True
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == npm_exe
    assert cmd[1] == "uninstall"
    assert cmd[2] == "-g"
    assert cmd[3] == _PKG


# ── codegraph_version ────────────────────────────────────────────────────

def test_version_empty_when_exe_missing():
    assert codegraph_version("") == ""
    assert codegraph_version("/nonexistent/codegraph") == ""


def test_version_parses_clean_version_string(tmp_path, mocker):
    cg = tmp_path / "codegraph"; cg.write_bytes(b"")
    mocker.patch("helpers.install_codegraph.subprocess.run",
                 return_value=_proc(rc=0, stdout="1.4.2\n"))
    assert codegraph_version(str(cg)) == "1.4.2"


def test_version_extracts_from_prefixed_output(tmp_path, mocker):
    cg = tmp_path / "codegraph"; cg.write_bytes(b"")
    mocker.patch("helpers.install_codegraph.subprocess.run",
                 return_value=_proc(rc=0, stdout="codegraph 1.4.2\n"))
    assert codegraph_version(str(cg)) == "1.4.2"


def test_version_strips_v_prefix(tmp_path, mocker):
    cg = tmp_path / "codegraph"; cg.write_bytes(b"")
    mocker.patch("helpers.install_codegraph.subprocess.run",
                 return_value=_proc(rc=0, stdout="v1.4.2\n"))
    assert codegraph_version(str(cg)) == "1.4.2"


def test_version_empty_on_nonzero_rc(tmp_path, mocker):
    cg = tmp_path / "codegraph"; cg.write_bytes(b"")
    mocker.patch("helpers.install_codegraph.subprocess.run",
                 return_value=_proc(rc=1, stderr="error"))
    assert codegraph_version(str(cg)) == ""


def test_version_empty_on_timeout(tmp_path, mocker):
    import subprocess as _sp
    cg = tmp_path / "codegraph"; cg.write_bytes(b"")
    mocker.patch("helpers.install_codegraph.subprocess.run",
                 side_effect=_sp.TimeoutExpired("cg", 5))
    assert codegraph_version(str(cg)) == ""


# ── detect_codegraph_after_install (G-B 3-step fallback) ─────────────────

def test_detect_step1_shutil_which_wins(tmp_path, mocker):
    """First-step: shutil.which finds it on PATH — return immediately."""
    found_path = str(tmp_path / "codegraph.cmd")
    # Patch shutil.which to return our path.
    mocker.patch("helpers.install_codegraph.shutil.which",
                 side_effect=lambda name: found_path if "codegraph" in name else None)
    assert detect_codegraph_after_install("") == found_path


def test_detect_step2_npm_prefix_probe(tmp_path, mocker, monkeypatch):
    """When shutil.which returns nothing, fall back to npm prefix -g."""
    mocker.patch("helpers.install_codegraph.shutil.which", return_value=None)
    # Strip APPDATA so step 3 doesn't accidentally succeed.
    monkeypatch.delenv("APPDATA", raising=False)
    prefix = tmp_path / "npm-prefix"
    prefix.mkdir()
    candidate = prefix / "codegraph.cmd"
    candidate.write_bytes(b"")
    npm = tmp_path / "npm.cmd"; npm.write_bytes(b"")
    mocker.patch("helpers.install_codegraph._npm_prefix",
                 return_value=str(prefix))
    assert detect_codegraph_after_install(str(npm)) == str(candidate)


def test_detect_step2_node_modules_bin_subpath(tmp_path, mocker, monkeypatch):
    """The node_modules/.bin/codegraph.cmd fallback inside npm prefix."""
    mocker.patch("helpers.install_codegraph.shutil.which", return_value=None)
    monkeypatch.delenv("APPDATA", raising=False)
    prefix = tmp_path / "npm-prefix"
    bin_dir = prefix / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    candidate = bin_dir / "codegraph.cmd"
    candidate.write_bytes(b"")
    npm = tmp_path / "npm.cmd"; npm.write_bytes(b"")
    mocker.patch("helpers.install_codegraph._npm_prefix",
                 return_value=str(prefix))
    assert detect_codegraph_after_install(str(npm)) == str(candidate)


def test_detect_step3_appdata_fallback(tmp_path, mocker, monkeypatch):
    """Last-resort: check %APPDATA%\\npm\\codegraph.cmd on Windows."""
    mocker.patch("helpers.install_codegraph.shutil.which", return_value=None)
    mocker.patch("helpers.install_codegraph._npm_prefix", return_value="")
    appdata = tmp_path / "Roaming"
    (appdata / "npm").mkdir(parents=True)
    candidate = appdata / "npm" / "codegraph.cmd"
    candidate.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    npm = tmp_path / "npm.cmd"; npm.write_bytes(b"")
    assert detect_codegraph_after_install(str(npm)) == str(candidate)


def test_detect_returns_empty_when_all_fallbacks_fail(tmp_path, mocker, monkeypatch):
    mocker.patch("helpers.install_codegraph.shutil.which", return_value=None)
    mocker.patch("helpers.install_codegraph._npm_prefix", return_value="")
    monkeypatch.delenv("APPDATA", raising=False)
    npm = tmp_path / "npm.cmd"; npm.write_bytes(b"")
    assert detect_codegraph_after_install(str(npm)) == ""


def test_detect_works_with_empty_npm_exe(mocker, monkeypatch):
    """Detection should not crash when no npm path is provided."""
    mocker.patch("helpers.install_codegraph.shutil.which", return_value=None)
    monkeypatch.delenv("APPDATA", raising=False)
    # No call to _npm_prefix because npm_exe is empty.
    npm_prefix_mock = mocker.patch("helpers.install_codegraph._npm_prefix")
    result = detect_codegraph_after_install("")
    assert result == ""
    npm_prefix_mock.assert_not_called()
