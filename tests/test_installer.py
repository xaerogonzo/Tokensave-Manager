"""The installer script and the code that builds and drives it.

Three kinds of claim, none of them "the script looks right":

* **structure** -- what the script installs and removes is stated once and compared with the zip
  inventory, so a fourth executable or a shipped config cannot slip past either;
* **the build code** -- command lines, compiler discovery and the checksum file are pure functions;
* **the real thing** -- on a Windows machine with Inno Setup, the script is compiled and INSTALLED,
  upgraded and uninstalled for real (a stand-in tree, so no Nuitka build is needed), with the same
  scenarios `scripts/drive_installer.py` runs on the real build. Skipped, visibly, anywhere else.
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

from helpers import installer as inst
from helpers import installer_drive as drive
from helpers import release_zip as rz

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "packaging" / "tokensave-manager.iss").read_text(encoding="utf-8")


def _iscc_or_none():
    try:
        return inst.find_iscc()
    except inst.InstallerError:
        return None


# ── structure ──────────────────────────────────────────────────────────

def test_the_two_app_ids_are_distinct_and_read_from_the_script():
    real, test = drive.app_ids()
    assert real != test
    assert ISS.count(real) == 1 and ISS.count(test) == 1


def test_the_installer_carries_the_whole_zip_tree_and_names_no_config():
    assert 'Source: "{#SourceDir}\\*"' in ISS and "recursesubdirs" in ISS
    assert "manager-config.json" not in re.sub(r"\{.*?\}|;.*|\{ .*? \}", "", ISS.split("[Code]")[0])
    for executable in ("tokensave-manager.exe", "tokensave-wrapper.exe", "tokensave-manager-cli.exe"):
        assert executable in rz.REQUIRED
    assert set(drive.PROGRAM_FILES) >= {"tokensave-manager.exe", "tokensave-wrapper.exe", "tokensave-manager-cli.exe"}


def test_an_upgrade_clears_exactly_the_programs_own_data_folders():
    assert 'Type: filesandordirs; Name: "{app}\\templates"' in ISS
    assert 'Type: filesandordirs; Name: "{app}\\docs"' in ISS
    delete = ISS[ISS.index("[InstallDelete]"):ISS.index("[Icons]")]
    assert "manager-config" not in delete and "logs" not in delete


def test_the_default_is_per_user_and_the_uninstall_defaults_keep_everything():
    assert "PrivilegesRequired=lowest" in ISS and "PrivilegesRequiredOverridesAllowed=dialog" in ISS
    # both boxes are created without a Checked assignment, and a silent uninstall removes only what is asked
    assert "SettingsBox.Checked := True" not in ISS and "DataBox.Checked := True" not in ISS
    assert "RemoveSettings := ParamIsOne('REMOVESETTINGS')" in ISS and "RemoveData := ParamIsOne('REMOVEDATA')" in ISS


def test_removal_is_confined_to_a_folder_named_for_the_application_and_never_a_whole_tree():
    assert "Pos('TokenSaveManager', ExtractFileName(Folder)) <> 1" in ISS
    assert "DelTree(Folder," not in ISS                     # the logs folder, never the data folder
    assert "RemoveDir(Folder)" in ISS                        # only succeeds when empty


def test_the_shipped_data_folder_matches_the_one_the_application_uses():
    import config_home
    assert "{localappdata}\\" + config_home.APP_DIR in ISS
    assert config_home.CONFIG_NAME == "manager-config.json" and "manager-config.json" in ISS


def test_the_test_build_cannot_stall_on_a_running_manager_but_the_shipped_one_waits_for_it():
    block = ISS[ISS.index("#ifndef TestBuild\n; The running"):]
    assert block.startswith("#ifndef TestBuild") and "AppMutex=TokenSaveManager_SingleInstance" in block.split("#endif")[0]
    import helpers.runtime as runtime
    assert runtime._MUTEX_NAME == "TokenSaveManager_SingleInstance"


# ── the build code ─────────────────────────────────────────────────────

def test_the_installer_name_follows_the_zip_name():
    assert inst.installer_name("2.7.0") == rz.zip_name("2.7.0").replace(".zip", "-setup.exe")
    with pytest.raises(inst.InstallerError):
        inst.installer_name("2.7")


def test_a_path_with_a_space_reaches_the_compiler_quoted_as_one_define():
    line = inst.command_line(r"C:\Inno Setup 6\ISCC.exe", Path(r"D:\a b\x.iss"),
                             {"AppVersion": "2.7.0", "SourceDir": r"C:\with space\tree", "TestBuild": ""})
    assert line == ('"C:\\Inno Setup 6\\ISCC.exe" /Q /DAppVersion="2.7.0" /DSourceDir="C:\\with space\\tree" '
                    '/DTestBuild "D:\\a b\\x.iss"')
    with pytest.raises(inst.InstallerError):
        inst.command_line("i", Path("x"), {"bad name": "1"})


def test_the_compiler_is_found_by_path_never_by_a_bare_name(tmp_path):
    fake = tmp_path / "ISCC.exe"
    fake.write_text("x")
    assert inst.find_iscc({"ISCC": str(fake)}) == str(fake)
    assert inst.find_iscc({}, explicit=str(fake)) == str(fake)
    nothing = {"LOCALAPPDATA": str(tmp_path / "none"), "ProgramFiles": str(tmp_path / "none")}
    if not _iscc_or_none():                       # on a machine without Inno the failure is explicit
        with pytest.raises(inst.InstallerError, match="ISCC.exe was not found"):
            inst.find_iscc(nothing)


def _zip(tmp_path, version="2.7.0", extra=()):
    path = tmp_path / rz.zip_name(version)
    top = rz.top_folder(version)
    with zipfile.ZipFile(path, "w") as z:
        for name in rz.REQUIRED:
            z.writestr("%s/%s" % (top, name), "x")
        for name in extra:
            z.writestr("%s/%s" % (top, name), "x")
    return path


def test_the_installer_is_never_built_from_a_zip_that_fails_its_inventory(tmp_path):
    bad = _zip(tmp_path, extra=("manager-config.json",))
    with pytest.raises(inst.InstallerError, match="inventory"):
        inst.extract_release(bad, "2.7.0", tmp_path / "x")
    good = _zip(tmp_path / "ok" if (tmp_path / "ok").mkdir() is None else tmp_path)
    assert (inst.extract_release(good, "2.7.0", tmp_path / "y") / "tokensave-manager.exe").is_file()


def test_the_installer_joins_the_checksums_and_keeps_the_zip_line(tmp_path, monkeypatch):
    zip_path = _zip(tmp_path)
    rz.write_checksums(tmp_path / "SHA256SUMS.txt", zip_path, zip_path.name)
    setup = tmp_path / "built" / "x-setup.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"MZ-setup")
    monkeypatch.setattr(inst, "find_iscc", lambda **kw: "iscc")
    monkeypatch.setattr(inst, "compile_installer", lambda *a, **k: setup)
    final = inst.build_installer(zip_path, "2.7.0", tmp_path)
    lines = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].endswith(" *" + final.name) and lines[1].endswith(" *" + zip_path.name)
    assert lines[0].split()[0] == rz.sha256_of(final)
    assert not list(tmp_path.glob("*.partial"))
    with pytest.raises(inst.InstallerError, match="already exists"):
        inst.build_installer(zip_path, "2.7.0", tmp_path)


def test_a_compiler_that_fails_leaves_no_installer_and_says_why(tmp_path, monkeypatch):
    zip_path = _zip(tmp_path)
    monkeypatch.setattr(inst, "find_iscc", lambda **kw: "iscc")

    def boom(*a, **k):
        raise inst.InstallerError("ISCC failed (exit 1):\nsyntax")
    monkeypatch.setattr(inst, "compile_installer", boom)
    with pytest.raises(inst.InstallerError, match="syntax"):
        inst.build_installer(zip_path, "2.7.0", tmp_path)
    assert not (tmp_path / inst.installer_name("2.7.0")).exists() and not (tmp_path / "SHA256SUMS.txt").exists()


def test_bump_changes_only_the_patch_part():
    assert drive.bump("2.7.9") == "2.7.10"


# ── the real thing ─────────────────────────────────────────────────────

needs_inno = pytest.mark.skipif(sys.platform != "win32" or _iscc_or_none() is None,
                                reason="needs Windows and Inno Setup 6 (ISCC.exe)")


def _stand_in_tree(tmp_path):
    tree = tmp_path / "tree"
    for name in rz.REQUIRED:
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("stand-in", encoding="utf-8")
    return tree


@needs_inno
def test_the_real_script_installs_upgrades_and_uninstalls_as_documented(tmp_path):
    if drive.registered(drive.app_ids()[0]) is not None:
        pytest.skip("the real application is installed on this machine; the driver refuses to run")
    checks = drive.drive(_stand_in_tree(tmp_path), "2.7.0", scratch_root=tmp_path)
    failed = [c.line() for c in checks if not c.ok]
    assert not failed, failed
    assert len(checks) >= 18
    assert drive.registered(drive.app_ids()[1]) is None, "the test left its registration behind"
    assert os.listdir(tmp_path) is not None


@needs_inno
def test_the_driver_refuses_to_run_when_the_real_application_is_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "registered", lambda guid: {"DisplayVersion": "1"} if guid == drive.app_ids()[0] else None)
    checks = drive.drive(_stand_in_tree(tmp_path), "2.7.0", scratch_root=tmp_path)
    assert len(checks) == 1 and not checks[0].ok and "real application" in checks[0].detail
