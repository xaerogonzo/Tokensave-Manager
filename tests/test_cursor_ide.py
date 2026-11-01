"""Tests for helpers/cursor_ide.py -- opening a folder in the Cursor APP.

The app's own executable is launched, never the `cursor.cmd` shim: cmd.exe
strips the outer quotes of a line whose exe path and argument both contain
spaces, and `D:\\Claude Co worker\\...` is exactly that.
"""
import os
import sys

import pytest

from helpers import cursor_ide as ide


def _make_exe(root):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "Cursor.exe")
    open(path, "w").close()
    return path


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Cursor's default install folder is a Windows location")
def test_the_default_install_location_is_found(fake_home, monkeypatch):
    monkeypatch.setattr(ide.shutil, "which", lambda _n: None)
    exe = _make_exe(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "cursor"))
    assert ide.find_cursor_exe() == exe


def test_the_exe_is_derived_from_the_cursor_shim_when_installed_elsewhere(
        fake_home, monkeypatch, tmp_path):
    root = str(tmp_path / "Apps" / "Cursor")
    shim_dir = os.path.join(root, "resources", "app", "bin")
    os.makedirs(shim_dir)
    shim = os.path.join(shim_dir, "cursor.cmd")
    open(shim, "w").close()
    exe = _make_exe(root)
    monkeypatch.setattr(ide.shutil, "which",
                        lambda name: shim if name == "cursor.cmd" else None)
    assert ide.find_cursor_exe() == exe


def test_no_install_is_an_empty_string_not_a_guess(fake_home, monkeypatch):
    monkeypatch.setattr(ide.shutil, "which", lambda _n: None)
    assert ide.find_cursor_exe() == ""


def test_open_folder_launches_the_exe_with_the_folder_as_a_separate_argument(
        monkeypatch, tmp_path):
    spaced = tmp_path / "Claude Co worker" / "Token Save Manager Source"
    spaced.mkdir(parents=True)
    seen = {}
    monkeypatch.setattr(ide.subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv, kw=kw))
    ok, err = ide.open_folder(str(spaced), exe="C:/x/Cursor.exe")
    assert ok, err
    # A list, never a shell string: nothing for cmd.exe to strip quotes from.
    assert seen["argv"] == ["C:/x/Cursor.exe", str(spaced)]
    assert seen["kw"]["creationflags"] == ide.CREATE_NO_WINDOW


def test_a_vanished_folder_is_refused_before_anything_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(ide.subprocess, "Popen", lambda *a, **k: pytest.fail("started"))
    ok, err = ide.open_folder(str(tmp_path / "gone"), exe="C:/x/Cursor.exe")
    assert not ok and "no longer exists" in err
    assert ide.open_folder("", exe="C:/x/Cursor.exe")[0] is False


def test_a_missing_cursor_is_said_plainly(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(ide.shutil, "which", lambda _n: None)
    ok, err = ide.open_folder(str(tmp_path))
    assert not ok and "not installed" in err


def test_a_failed_start_is_reported(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("denied")
    monkeypatch.setattr(ide.subprocess, "Popen", boom)
    ok, err = ide.open_folder(str(tmp_path), exe="C:/x/Cursor.exe")
    assert not ok and "denied" in err
