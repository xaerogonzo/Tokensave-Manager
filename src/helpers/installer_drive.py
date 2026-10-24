"""installer_drive -- really install, upgrade and uninstall the installer, and say what happened.

A script that compiles is not an installer that works. This drives the real thing, per-user and silent,
into a folder with a space in its name, with a TEST build of the same script: its own AppId, so it can
never replace a real installation's uninstall entry, and a sandbox data folder, so removal can be
tested without touching real settings. The scenarios are the ones an installer gets wrong quietly:

  install          all three executables, templates, docs and an uninstall entry; the app smoke-tested
  upgrade          a newer version replaces the old; a stale program file is gone; settings and logs
                   in the data folder are untouched
  uninstall        silent default removes the program and keeps everything the person made
  remove settings  only the settings go; remove logs: only the logs; both: the folder goes if empty
  foreign files    a file a person kept in the data folder is never deleted, and the folder stays
  not our folder   a data folder not named for the application is never touched, even when asked

It refuses to run if the REAL AppId is registered (a test must never be mistaken for a real install),
and removes a leftover TEST registration first. Windows only; every result is a `Check`, and a driver
that could not run a scenario reports it as failed, never as passed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from helpers import installer as inst

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROGRAM_FILES = ("tokensave-manager.exe", "tokensave-wrapper.exe", "tokensave-manager-cli.exe",
                 "templates", "docs", "LICENSE")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{%s}_is1"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return "%-4s %s%s" % ("PASS" if self.ok else "FAIL", self.name, (" -- " + self.detail) if self.detail else "")


def app_ids(script: Path = inst.SCRIPT) -> "tuple[str, str]":
    """(real, test) AppId GUIDs, read from the script so there is one place they are written."""
    text = script.read_text(encoding="utf-8")
    real = re.search(r'#else\s+#define AppIdGuid "\{\{([0-9A-F-]{36})\}"', text)
    test = re.search(r'#ifdef TestBuild\s+#define AppIdGuid "\{\{([0-9A-F-]{36})\}"', text)
    if not real or not test or real.group(1) == test.group(1):
        raise inst.InstallerError("cannot read two distinct AppIds from %s" % script)
    return real.group(1), test.group(1)


def registered(guid: str) -> "dict | None":
    """The uninstall entry for an AppId (per-user, then all-users), or None."""
    import winreg
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY % guid, 0, winreg.KEY_READ | view) as key:
                values = {}
                for name in ("DisplayVersion", "UninstallString", "InstallLocation"):
                    try:
                        values[name] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        values[name] = ""
                return values
        except OSError:
            continue
    return None


def _run(argv: "list[str]", timeout: float = 300.0) -> int:
    return subprocess.run(argv, creationflags=CREATE_NO_WINDOW, timeout=timeout,
                          stdin=subprocess.DEVNULL, capture_output=True).returncode


def install(setup: Path, target: Path, data: Path, log: Path) -> int:
    return _run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                 "/DIR=%s" % target, "/LOG=%s" % log, "/TESTDATADIR=%s" % data])


def uninstall(target: Path, test_id: str, data: Path, *, settings: bool = False, logs: bool = False,
              wait: float = 120.0) -> bool:
    """Run the uninstaller and wait for the registration to go. The Inno uninstaller hands off to a
    temporary copy and returns at once, so its exit code says nothing about when it finished."""
    unins = sorted(target.glob("unins*.exe"))
    if not unins:
        return False
    log = target.parent / ("uninstall-%s.log" % uuid.uuid4().hex[:8])   # outside {app}, which is being removed
    argv = [str(unins[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TESTDATADIR=%s" % data,
            "/LOG=%s" % log]
    if settings:
        argv.append("/REMOVESETTINGS=1")
    if logs:
        argv.append("/REMOVEDATA=1")
    _run(argv)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        # The registration goes BEFORE the last step (the data removal), so it is not "finished". The
        # log's closing line is: Inno writes it when the uninstall process is done.
        if registered(test_id) is None and _log_closed(log):
            return True
        time.sleep(0.5)
    return False


def _log_closed(log: Path) -> bool:
    try:
        return "Log closed." in log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _program_present(target: Path) -> "list[str]":
    return [name for name in PROGRAM_FILES if not (target / name).exists()]


def _seed(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "manager-config.json").write_text('{"editor_cmd": "marker"}', encoding="utf-8")
    (data / "config-migration.json").write_text("{}", encoding="utf-8")
    (data / "logs").mkdir(exist_ok=True)
    (data / "logs" / "run.log").write_text("log", encoding="utf-8")


def bump(version: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    return "%d.%d.%d" % (major, minor, patch + 1)


def drive(source_dir: Path, version: str, *, iscc: str = "", smoke: "Callable[[Path], bool] | None" = None,
          scratch_root: "Path | None" = None) -> "list[Check]":
    """Every scenario. `source_dir` is a release tree (the zip's top folder, or a stand-in in tests)."""
    real_id, test_id = app_ids()
    if registered(real_id) is not None:
        return [Check("refuses to run", False,
                      "the real application (AppId %s) is installed here; a test must not share its registry key" % real_id)]
    checks: "list[Check]" = []
    root = Path(tempfile.mkdtemp(prefix="tokensave manager installer test ", dir=scratch_root))
    compiler = inst.find_iscc(explicit=iscc)
    try:
        leftover = registered(test_id)
        if leftover is not None:
            # A TEST registration from an aborted run: ours to remove, and only ours.
            location = Path(leftover.get("InstallLocation") or "")
            if location.is_dir():
                uninstall(location, test_id, root / "unused-data")
            checks.append(Check("removed a leftover test registration", registered(test_id) is None))

        old = inst.compile_installer(compiler, source_dir, version, root / "old", test=True)
        new = inst.compile_installer(compiler, source_dir, bump(version), root / "new", test=True)
        checks.append(Check("both test installers compile", True, "%s, %s" % (version, bump(version))))
        # The dialog-less install is per-user; one folder with a space is the path people have.
        app = root / "Program Files stand-in" / "TokenSave Manager"
        data = root / "TokenSaveManager-TEST"

        # --- install
        code = install(old, app, data, root / "install.log")
        missing = _program_present(app)
        entry = registered(test_id)
        checks.append(Check("install: exit 0", code == 0, "exit %s" % code))
        checks.append(Check("install: program files present", not missing, "missing: %s" % ", ".join(missing)))
        checks.append(Check("install: uninstall entry at the old version",
                            bool(entry) and entry["DisplayVersion"] == version, str(entry)))
        checks.append(Check("install: the real application is still not registered", registered(real_id) is None))
        if smoke is not None:
            checks.append(Check("install: the installed application passes its smoke test", bool(smoke(app))))

        # --- upgrade
        _seed(data)
        (app / "templates" / "stale-from-old-version.txt").write_text("x", encoding="utf-8")
        (app / "docs" / "stale-from-old-version.txt").write_text("x", encoding="utf-8")
        code = install(new, app, data, root / "upgrade.log")
        entry = registered(test_id)
        checks.append(Check("upgrade: exit 0", code == 0, "exit %s" % code))
        checks.append(Check("upgrade: the version changed", bool(entry) and entry["DisplayVersion"] == bump(version), str(entry)))
        checks.append(Check("upgrade: a stale program file is gone",
                            not (app / "templates" / "stale-from-old-version.txt").exists()
                            and not (app / "docs" / "stale-from-old-version.txt").exists()))
        checks.append(Check("upgrade: program files still present", not _program_present(app),
                            "missing: %s" % ", ".join(_program_present(app))))
        checks.append(Check("upgrade: settings and logs untouched",
                            (data / "manager-config.json").read_text(encoding="utf-8") == '{"editor_cmd": "marker"}'
                            and (data / "logs" / "run.log").exists()))

        # --- uninstall: the default keeps everything
        gone = uninstall(app, test_id, data)
        checks.append(Check("uninstall (default): the program and its entry are gone",
                            gone and not (app / "tokensave-manager.exe").exists()))
        checks.append(Check("uninstall (default): settings and logs kept",
                            (data / "manager-config.json").exists() and (data / "logs" / "run.log").exists()))

        # --- each removal option on its own
        for label, kwargs, keep_config, keep_logs in (
                ("remove settings", {"settings": True}, False, True),
                ("remove logs", {"logs": True}, True, False)):
            _reinstall(old, app, data, root, label)
            _seed(data)
            done = uninstall(app, test_id, data, **kwargs)
            checks.append(Check("uninstall (%s): finished" % label, done))
            checks.append(Check("uninstall (%s): only that part removed" % label,
                                (data / "manager-config.json").exists() == keep_config
                                and (data / "logs" / "run.log").exists() == keep_logs,
                                "config=%s logs=%s" % ((data / "manager-config.json").exists(), (data / "logs" / "run.log").exists())))

        # --- both, with a file the person kept: the folder must stay
        _reinstall(old, app, data, root, "both")
        _seed(data)
        (data / "my-notes.txt").write_text("keep me", encoding="utf-8")
        uninstall(app, test_id, data, settings=True, logs=True)
        checks.append(Check("uninstall (both): a person's own file is kept and so is the folder",
                            (data / "my-notes.txt").read_text(encoding="utf-8") == "keep me"))
        checks.append(Check("uninstall (both): settings and logs removed",
                            not (data / "manager-config.json").exists() and not (data / "logs").exists()))

        # --- both, nothing else there: the empty folder goes too
        shutil.rmtree(data, ignore_errors=True)
        _reinstall(old, app, data, root, "both-empty")
        _seed(data)
        uninstall(app, test_id, data, settings=True, logs=True)
        checks.append(Check("uninstall (both, nothing else there): the empty folder is removed", not data.exists()))

        # --- a folder that is not ours is never touched, even when asked
        foreign = root / "NotOurs"
        _seed(foreign)
        _reinstall(old, app, foreign, root, "foreign")
        uninstall(app, test_id, foreign, settings=True, logs=True)
        checks.append(Check("uninstall: a data folder not named for the application is never touched",
                            (foreign / "manager-config.json").exists() and (foreign / "logs" / "run.log").exists()))
    except Exception as exc:  # a driver that cannot run a scenario has not passed it
        checks.append(Check("driver ran to the end", False, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        # Leave nothing registered: the TEST entry is the only one this run may have made.
        entry = registered(test_id)
        if entry is not None:
            location = Path(entry.get("InstallLocation") or "")
            if location.is_dir():
                uninstall(location, test_id, root / "unused-data")
        shutil.rmtree(root, ignore_errors=True)
    return checks


def _reinstall(setup: Path, app: Path, data: Path, root: Path, tag: str) -> None:
    code = install(setup, app, data, root / ("reinstall-%s-%s.log" % (tag, uuid.uuid4().hex[:6])))
    if code != 0:
        raise inst.InstallerError("could not reinstall for the %r scenario (exit %s)" % (tag, code))
