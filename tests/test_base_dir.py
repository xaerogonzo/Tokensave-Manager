"""tests/test_base_dir.py — where a packaged Manager thinks it lives.

`_BASE_DIR` decides where `manager-config.json` is read from and where logs are
written, so getting it wrong in a frozen build means the shipped product reads
somebody else's config — or none.

The bug this pins was live in every Nuitka build. `NUITKA_ONEFILE_PARENT` marks
a onefile run, but its **value is the parent process PID**, not a path — the old
code's comment asserted the opposite. Running a bare number like `"41108"`
through `dirname(abspath(...))` resolves it against the current directory, so
`_BASE_DIR` silently became **the cwd**. It went unnoticed because the normal
launch path happens to have the exe's own folder as the cwd; run the exe from
anywhere else and the Manager looked for its config in the wrong place.

That is the same silent-cwd-inference failure class as the MCP scope collision,
which is why it is pinned rather than merely fixed.
"""
from __future__ import annotations

import os

import pytest

from constants import _resolve_base_dir


#: What Nuitka 4.1 actually puts in the variable — measured with a probe build.
_PID_SHAPED_VALUE = "41108"


def test_frozen_build_resolves_beside_the_executable(tmp_path):
    exe = tmp_path / "install" / "tokensave-manager-cli.exe"
    exe.parent.mkdir(parents=True)
    got = _resolve_base_dir({"NUITKA_ONEFILE_PARENT": _PID_SHAPED_VALUE},
                            str(exe), "/irrelevant/constants.py")
    assert got == str(exe.parent)


def test_frozen_build_does_not_resolve_to_the_cwd(tmp_path, monkeypatch):
    """The regression itself: a PID must never be read as a relative path."""
    elsewhere = tmp_path / "some" / "unrelated" / "cwd"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    exe = tmp_path / "install" / "manager.exe"
    exe.parent.mkdir(parents=True)
    got = _resolve_base_dir({"NUITKA_ONEFILE_PARENT": _PID_SHAPED_VALUE},
                            str(exe), "/irrelevant/constants.py")

    assert got == str(exe.parent)
    assert got != str(elsewhere), "resolved to the cwd — the original bug"


def test_the_answer_is_stable_across_working_directories(tmp_path, monkeypatch):
    """Same exe, two cwds, one answer. Anything else is cwd inference."""
    exe = tmp_path / "install" / "manager.exe"
    exe.parent.mkdir(parents=True)
    env = {"NUITKA_ONEFILE_PARENT": _PID_SHAPED_VALUE}

    first_cwd = tmp_path / "a"
    second_cwd = tmp_path / "b"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = _resolve_base_dir(env, str(exe), "/irrelevant/constants.py")
    monkeypatch.chdir(second_cwd)
    second = _resolve_base_dir(env, str(exe), "/irrelevant/constants.py")

    assert first == second


def test_dev_mode_goes_one_level_up_from_src(tmp_path):
    """constants.py lives in src/, so the repo root is its parent."""
    module = tmp_path / "repo" / "src" / "constants.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    got = _resolve_base_dir({}, "/irrelevant/argv0", str(module))
    assert got == os.path.normpath(str(tmp_path / "repo"))


def test_dev_mode_ignores_argv0_entirely(tmp_path):
    """In source mode the interpreter's argv[0] says nothing about the repo."""
    module = tmp_path / "repo" / "src" / "constants.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    a = _resolve_base_dir({}, "/one/python.exe", str(module))
    b = _resolve_base_dir({}, "/completely/other/pytest.exe", str(module))
    assert a == b


@pytest.mark.parametrize("empty", ["", None])
def test_an_absent_marker_means_dev_mode(tmp_path, empty):
    """Only a truthy marker selects the frozen branch."""
    module = tmp_path / "repo" / "src" / "constants.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    env = {} if empty is None else {"NUITKA_ONEFILE_PARENT": empty}
    got = _resolve_base_dir(env, "/irrelevant", str(module))
    assert got == os.path.normpath(str(tmp_path / "repo"))


def test_the_live_constant_matches_the_helper():
    """The module-level constant must be the helper's answer, not a copy."""
    import sys

    import constants
    expected = _resolve_base_dir(os.environ, sys.argv[0], constants.__file__)
    assert constants._BASE_DIR == expected
