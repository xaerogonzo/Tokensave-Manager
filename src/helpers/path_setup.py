"""helpers/path_setup.py — is `tokensave` resolvable as a bare command?

A project-scoped `.mcp.json` is shared through version control, so it must not
contain this machine's paths. That means the entry says `"command": "tokensave"`
and resolution is PATH's job — which makes "is tokensave on PATH" a
**prerequisite** of project binding rather than a detail.

Three states, and the distinction between the last two is the whole point:

    resolves       a new process would find `tokensave`; binding is ready
    not_on_path    the binary exists where the manager configured it, but its
                   directory is not on PATH. A fixable setup state.
    not_installed  no configured executable at all. An installation problem,
                   handled by the Tool Manager, not by this module.

Reporting "not installed" for a working binary that merely is not on PATH would
send the user to reinstall something they already have.

## Why the registry rather than `setx`

`setx PATH "%PATH%;new"` is the obvious one-liner and is a footgun: it expands
the *current process's* PATH — system entries and all — and writes the whole
flattened result into the USER variable, duplicating every system entry into
user scope, and it silently truncates at 1024 characters. Reading and writing
`HKCU\\Environment\\Path` touches only the user's own value and cannot truncate.

## Why verification cannot use a plain subprocess

A child process inherits its parent's environment block, so a `where tokensave`
spawned from the running manager sees the PATH the manager started with — not
the one just written. It would report failure immediately after a successful
edit, which looks exactly like the edit not working. Verification therefore
rebuilds the environment a *new* process would receive (system PATH + user PATH,
read back from the registry) and resolves against that.

Even so, already-running processes keep their old environment: Claude Code must
be restarted before a binding that relies on PATH will work. Callers are
expected to say so.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess

from constants import CREATE_NO_WINDOW

RESOLVES = "resolves"
NOT_ON_PATH = "not_on_path"
NOT_INSTALLED = "not_installed"
UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class PathState:
    """What the manager knows about `tokensave` as a bare command."""

    verdict: str
    exe_dir: str = ""
    detail: str = ""

    @property
    def is_ready(self) -> bool:
        return self.verdict == RESOLVES

    @property
    def is_fixable(self) -> bool:
        """True only for the state this module can actually repair."""
        return self.verdict == NOT_ON_PATH


# ── reading the environment a NEW process would get ───────────────────────

def _reg_path(hive_name: str, subkey: str) -> str:
    """One `Path` value from the registry, or "" if unreadable.

    winreg is Windows-only and imported here rather than at module scope: this
    repo's CI runs Linux and guards against platform imports at import time.
    """
    try:
        import winreg
    except ImportError:
        return ""
    hive = (winreg.HKEY_CURRENT_USER if hive_name == "user"
            else winreg.HKEY_LOCAL_MACHINE)
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, "Path")
            return value or ""
    except OSError:
        return ""


def user_path() -> str:
    return _reg_path("user", r"Environment")


def system_path() -> str:
    return _reg_path(
        "machine",
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")


def composed_path() -> str:
    """PATH as a freshly launched process would see it.

    Not `os.environ["PATH"]` — that is this process's snapshot, taken before
    any edit made here.
    """
    parts = [p for p in (system_path(), user_path()) if p]
    return os.pathsep.join(parts) if parts else os.environ.get("PATH", "")


def resolves_in_a_new_process(command: str = "tokensave") -> bool:
    """Would a process started now find *command*?

    Uses `shutil.which` against the rebuilt PATH rather than shelling out,
    because a spawned `where` would inherit this process's stale environment
    and answer the wrong question.
    """
    import shutil
    path = composed_path()
    if not path:
        return False
    return bool(shutil.which(command, path=path))


# ── the state ─────────────────────────────────────────────────────────────

def read_state(cfg: dict, command: str = "tokensave") -> PathState:
    """Classify PATH readiness for project binding. Pure read, no side effects."""
    exe = ((cfg.get("tokensave_exe") if isinstance(cfg, dict) else "") or "").strip()
    if resolves_in_a_new_process(command):
        return PathState(RESOLVES, os.path.dirname(exe) if exe else "",
                         "`%s` resolves on PATH." % command)
    if not exe or not os.path.isfile(exe):
        return PathState(
            NOT_INSTALLED, "",
            "No tokensave executable is configured, or the configured path "
            "does not exist. Install or locate it first (Settings → Tool "
            "Manager); this is not a PATH problem.")
    directory = os.path.dirname(exe)
    return PathState(
        NOT_ON_PATH, directory,
        "tokensave is installed at %s, but that folder is not on PATH, so "
        "`tokensave` cannot be run as a bare command." % exe)


# ── the repair ────────────────────────────────────────────────────────────

def add_to_user_path(directory: str) -> "tuple[bool, str]":
    """Append *directory* to the USER PATH. Returns (changed, detail).

    Deliberately narrow:

    * user scope only — no admin rights, and nothing another account can see;
    * idempotent — an entry already present is reported, not duplicated;
    * the previous value is returned in *detail* so a caller can show it and a
      user can restore it by hand. There is no file to back up here, which is
      why the old value has to travel in the result.

    A broadcast tells Explorer to re-read the environment so newly launched
    programs inherit it; processes already running never will.
    """
    directory = (directory or "").strip().rstrip("\\/")
    if not directory:
        return False, "No directory to add."
    if not os.path.isdir(directory):
        return False, "Not a directory: %s" % directory

    try:
        import winreg
    except ImportError:
        return False, "Editing PATH is only supported on Windows."

    current = user_path()
    existing = [p for p in current.split(os.pathsep) if p.strip()]
    if any(os.path.normcase(p.rstrip("\\/")) == os.path.normcase(directory)
           for p in existing):
        return False, "Already on the user PATH: %s" % directory

    new_value = os.pathsep.join(existing + [directory]) if existing else directory
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            # REG_EXPAND_SZ so entries containing %VARS% keep working; that is
            # what Windows itself uses for this value.
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_value)
    except OSError as exc:
        return False, "Could not write the user PATH: %s" % exc

    _broadcast_environment_change()
    return True, ("Added to your user PATH: %s\n\nPrevious value:\n%s"
                  % (directory, current or "(empty)"))


def _broadcast_environment_change() -> None:
    """Ask running shells to re-read the environment. Best effort by design.

    Failing to broadcast only means new programs pick the change up later; it
    is never a reason to report the PATH edit itself as failed.
    """
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"), SMTO_ABORTIFHUNG, 5000, None)
    except Exception:                                        # noqa: BLE001
        pass


def where_report(command: str = "tokensave") -> str:
    """`where` output, for showing the user what a shell would find.

    Diagnostic only — the authoritative answer is
    :func:`resolves_in_a_new_process`, because this call inherits the manager's
    own (possibly stale) environment.
    """
    try:
        proc = subprocess.run(
            ["where", command], capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "could not run `where`: %s" % exc
    out = (proc.stdout or proc.stderr or "").strip()
    return out or "not found"
