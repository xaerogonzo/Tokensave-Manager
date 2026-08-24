"""tests/test_path_setup.py — PATH readiness for project-scoped binding.

A project `.mcp.json` says `"command": "tokensave"` so the file stays portable,
which makes PATH resolution a prerequisite rather than a detail. The states this
module distinguishes are the point: "installed but not on PATH" is a fixable
setup step, "not installed" is a different problem with a different remedy, and
conflating them sends a user to reinstall a binary they already have.

Nothing here writes to the real registry. The repair function is exercised only
through its refusal paths and with the registry reads mocked; a test suite that
edits the developer's PATH would be a considerably worse bug than the one it was
checking for.
"""
from __future__ import annotations

import os

import pytest

from helpers import path_setup as ps


# ── read_state ────────────────────────────────────────────────────────────

def test_a_resolvable_command_is_ready(monkeypatch, tmp_path):
    exe = tmp_path / "tokensave.exe"
    exe.write_text("x")
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: True)

    state = ps.read_state({"tokensave_exe": str(exe)})

    assert state.verdict == ps.RESOLVES
    assert state.is_ready and not state.is_fixable


def test_an_installed_but_unreachable_binary_is_fixable(monkeypatch, tmp_path):
    """The state the whole module exists for."""
    exe = tmp_path / "bin" / "tokensave.exe"
    exe.parent.mkdir()
    exe.write_text("x")
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: False)

    state = ps.read_state({"tokensave_exe": str(exe)})

    assert state.verdict == ps.NOT_ON_PATH
    assert state.is_fixable and not state.is_ready
    assert state.exe_dir == str(exe.parent)


def test_no_configured_executable_is_not_a_path_problem(monkeypatch):
    """Reporting this as "not on PATH" would offer a repair that cannot work."""
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: False)

    state = ps.read_state({})

    assert state.verdict == ps.NOT_INSTALLED
    assert not state.is_fixable
    assert "not a PATH problem" in state.detail


def test_a_configured_path_that_does_not_exist_is_not_installed(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: False)

    state = ps.read_state({"tokensave_exe": str(tmp_path / "gone.exe")})

    assert state.verdict == ps.NOT_INSTALLED


def test_the_directory_comes_from_the_configured_exe(monkeypatch, tmp_path):
    """Never hard-coded: a Settings change must move the repair target with it."""
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: False)
    for name in ("aaa", "bbb"):
        exe = tmp_path / name / "tokensave.exe"
        exe.parent.mkdir()
        exe.write_text("x")
        assert ps.read_state({"tokensave_exe": str(exe)}).exe_dir == str(exe.parent)


def test_read_state_never_writes(monkeypatch, tmp_path):
    """It runs on every dialog render."""
    called = []
    monkeypatch.setattr(ps, "add_to_user_path",
                        lambda d: called.append(d) or (True, ""))
    monkeypatch.setattr(ps, "resolves_in_a_new_process", lambda *a, **k: False)
    ps.read_state({"tokensave_exe": str(tmp_path / "nope.exe")})
    assert called == []


# ── resolution uses a rebuilt environment, not this process's ─────────────

def test_resolution_reads_the_composed_path_not_os_environ(monkeypatch, tmp_path):
    """The trap this module exists to avoid.

    A child process inherits the manager's environment, so checking
    `os.environ["PATH"]` (or shelling out) would answer with the PATH the
    manager started with and report failure right after a successful edit.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tokensave.exe").write_text("x")
    (bindir / "tokensave").write_text("x")          # POSIX name too

    monkeypatch.setenv("PATH", "")                  # this process sees nothing
    monkeypatch.setattr(ps, "composed_path", lambda: str(bindir))

    assert ps.resolves_in_a_new_process("tokensave") is True


def test_composed_path_falls_back_when_the_registry_is_unavailable(monkeypatch):
    """On non-Windows there is no registry; the module must still answer."""
    monkeypatch.setattr(ps, "system_path", lambda: "")
    monkeypatch.setattr(ps, "user_path", lambda: "")
    monkeypatch.setenv("PATH", "/some/where")

    assert ps.composed_path() == "/some/where"


def test_composed_path_prefers_the_registry_when_present(monkeypatch):
    monkeypatch.setattr(ps, "system_path", lambda: "SYS")
    monkeypatch.setattr(ps, "user_path", lambda: "USR")
    monkeypatch.setenv("PATH", "IGNORED")

    assert ps.composed_path() == os.pathsep.join(["SYS", "USR"])


# ── the repair, refusal paths only ────────────────────────────────────────

def test_adding_nothing_is_refused():
    assert ps.add_to_user_path("")[0] is False
    assert ps.add_to_user_path("   ")[0] is False


def test_adding_a_non_directory_is_refused(tmp_path):
    ghost = str(tmp_path / "does-not-exist")
    ok, detail = ps.add_to_user_path(ghost)
    assert ok is False
    assert "Not a directory" in detail


def test_a_directory_already_present_is_reported_not_duplicated(
        monkeypatch, tmp_path):
    """Applying twice must not grow the user's PATH each time."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(ps, "user_path",
                        lambda: os.pathsep.join(["C:\\other", str(bindir)]))

    ok, detail = ps.add_to_user_path(str(bindir))

    assert ok is False
    assert "Already on the user PATH" in detail


def test_presence_check_ignores_case_and_trailing_separators(
        monkeypatch, tmp_path):
    """`D:\\Tools`, `d:\\tools` and `D:\\Tools\\` are one directory; adding a
    variant would leave two entries pointing at the same place."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(ps, "user_path", lambda: str(bindir).upper() + os.sep)

    ok, detail = ps.add_to_user_path(str(bindir))

    if os.name == "nt":
        assert ok is False and "Already" in detail
    else:                      # normcase is identity on POSIX, so case differs
        assert ok is False or "Already" in detail


# ── state object ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict,ready,fixable", [
    (ps.RESOLVES, True, False),
    (ps.NOT_ON_PATH, False, True),
    (ps.NOT_INSTALLED, False, False),
    (ps.UNKNOWN, False, False),
])
def test_only_not_on_path_advertises_itself_as_fixable(verdict, ready, fixable):
    state = ps.PathState(verdict)
    assert state.is_ready is ready
    assert state.is_fixable is fixable
