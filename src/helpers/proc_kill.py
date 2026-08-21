"""proc_kill — terminate a process, with the semantics the caller needs.

This module exists because the codebase grew two kill implementations that
LOOK interchangeable and are not:

  * ``smoke_runner._kill_tree`` — ``taskkill /F /T`` on Windows, ``killpg``
    with SIGKILL on POSIX. Kills the whole child TREE, immediately. A test
    runner that leaves an orphaned child pytest burning CPU is the bug it
    was written for.
  * ``codegraph_daemon.kill_codegraph_daemon`` — ``taskkill /F`` on Windows,
    SIGTERM then SIGKILL on POSIX. Kills ONE process, giving it a moment to
    exit cleanly first.

Folding those into a single "kill it" function would have silently stopped
the smoke runner reaping its children AND turned the daemon's graceful stop
into an immediate one. So the two axes that actually differ are parameters:
``tree`` and ``graceful``.

## PID reuse, and what "verified" costs

A PID is not an identity. Between reading a process list and acting on it,
the process can exit and the OS can hand its number to something else —
Windows recycles aggressively. Every ``taskkill /PID``-shaped API resolves
the number at the moment it runs, so a stale PID silently retargets.

``kill_process(..., expect=identity)`` closes that window on the paths where
it can:

  * **Windows, single process** — ``OpenProcess`` once, verify creation time
    and image name THROUGH THAT HANDLE, then ``TerminateProcess`` on the same
    handle. A handle refers to the process object, not the number, so reuse
    cannot redirect it. This is the only fully race-free path here.
  * **POSIX, single process** — ``os.pidfd_open`` + ``signal.pidfd_send_signal``
    where the kernel provides them (Linux 5.3+), which give the same
    guarantee. Where they do not exist the fallback verifies and then signals
    by PID, which is a MITIGATION with a residual race, and says so.
  * **Tree kill, either platform** — ``taskkill /T`` and ``killpg`` both
    resolve the target themselves and offer no handle to hold. Verification
    narrows the window; it does not close it. Callers needing a guarantee
    should not use ``tree=True``.

The rule this module refuses to break: never report safety it does not have.
Each result's detail string names which path ran.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    from constants import CREATE_NO_WINDOW
except ImportError:                                    # standalone / test use
    CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_KILL_TIMEOUT = 10
_GRACEFUL_WAIT_STEPS = 20            # 20 x 0.1s = ~2s before escalating
_GRACEFUL_WAIT_INTERVAL = 0.1

_ERROR_INVALID_PARAMETER = 87        # Windows: no such process


@dataclass(frozen=True)
class ProcessIdentity:
    """Enough to tell a process from a later one wearing its PID.

    ``created_at`` is the discriminator that matters: two processes can share
    a PID over time, but not a PID *and* a start time. ``image`` is a second
    signal, and a readable one for error messages.
    """
    pid: int
    created_at: int          # platform-specific ticks; compare, don't interpret
    image: str = ""

    def matches(self, other: "ProcessIdentity | None") -> bool:
        if other is None:
            return False
        if self.pid != other.pid or self.created_at != other.created_at:
            return False
        # An empty image on either side means "could not read it", which is
        # not evidence of a mismatch — the start time already carried the
        # decision.
        if self.image and other.image:
            return self.image.casefold() == other.image.casefold()
        return True


# ── identity ──────────────────────────────────────────────────────────────

def process_identity(pid: int) -> "ProcessIdentity | None":
    """Read *pid*'s identity now, or None if it is gone / unreadable."""
    if sys.platform == "win32":
        return _identity_windows(pid)
    return _identity_posix(pid)


def _identity_windows(pid: int) -> "ProcessIdentity | None":
    import ctypes

    handle = _open_process_windows(pid)
    if not handle:
        return None
    try:
        created = _creation_time_windows(handle)
        if created is None:
            return None
        return ProcessIdentity(pid=pid, created_at=created,
                               image=_image_name_windows(handle))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _identity_posix(pid: int) -> "ProcessIdentity | None":
    """Start time from /proc where available, else the best signal there is."""
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8",
                  errors="replace") as fh:
            # The comm field can contain spaces and parens; everything after
            # the last ") " is positional, so split there rather than on the
            # first token.
            fields = fh.read().rsplit(") ", 1)[-1].split()
        starttime = int(fields[19])          # field 22 overall, 1-based
    except (OSError, ValueError, IndexError):
        if not _alive(pid):
            return None
        return ProcessIdentity(pid=pid, created_at=-1, image="")
    image = ""
    try:
        image = os.path.basename(os.readlink("/proc/%d/exe" % pid))
    except OSError:
        pass
    return ProcessIdentity(pid=pid, created_at=starttime, image=image)


# ── killing ───────────────────────────────────────────────────────────────

def kill_process(pid: int, *, tree: bool = False, graceful: bool = True,
                 expect: "ProcessIdentity | None" = None) -> "tuple[bool, str]":
    """Terminate *pid*. Returns ``(ok, detail)``; never raises.

    ``tree``     — also kill descendants (see the module docstring's caveat).
    ``graceful`` — ask politely first (POSIX SIGTERM, then SIGKILL). Windows
                   has no portable equivalent, so it is always immediate there.
    ``expect``   — an identity captured earlier. When given, the process is
                   killed only if it still matches.
    """
    if tree:
        return _kill_tree_by_pid(pid, expect=expect)
    if sys.platform == "win32":
        return _kill_one_windows(pid, expect=expect)
    return _kill_one_posix(pid, graceful=graceful, expect=expect)


def kill_popen_tree(proc) -> None:
    """Kill a ``Popen`` and its descendants, best-effort. Never raises.

    ``smoke_runner``'s shape, preserved exactly: it holds a real ``Popen``, so
    ``proc.kill()`` remains a genuine last resort that does not depend on the
    PID still resolving.
    """
    try:
        ok, _detail = _kill_tree_by_pid(proc.pid, expect=None)
        if ok:
            return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


# ── platform paths ────────────────────────────────────────────────────────

def _kill_one_windows(pid: int,
                      expect: "ProcessIdentity | None") -> "tuple[bool, str]":
    """OpenProcess → verify → TerminateProcess, all on one handle."""
    import ctypes

    handle = _open_process_windows(pid, terminate=True)
    if not handle:
        return _already_gone_or_denied()
    try:
        if expect is not None:
            created = _creation_time_windows(handle)
            if created is None:
                return False, "could not read the start time to verify identity"
            actual = ProcessIdentity(pid=pid, created_at=created,
                                     image=_image_name_windows(handle))
            if not actual.matches(expect):
                return False, (
                    "PID now belongs to a different process "
                    "(%s) — refusing to terminate it"
                    % (actual.image or "unknown"))
        if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
            err = ctypes.windll.kernel32.GetLastError()
            return False, "TerminateProcess failed (error %d)" % err
        return True, ("terminated via verified handle" if expect is not None
                      else "terminated via handle")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _kill_one_posix(pid: int, *, graceful: bool,
                    expect: "ProcessIdentity | None") -> "tuple[bool, str]":
    if expect is not None and not _identity_still_matches(pid, expect):
        return False, "PID now belongs to a different process — refusing"

    fd = _pidfd(pid)
    try:
        if not graceful:
            return _send(fd, pid, signal.SIGKILL, "SIGKILL")
        ok, detail = _send(fd, pid, signal.SIGTERM, "SIGTERM")
        if not ok:
            return (True, "already gone") if detail == "already gone" else (ok, detail)
        for _ in range(_GRACEFUL_WAIT_STEPS):
            if not _alive(pid):
                return True, _with_race_note("terminated (SIGTERM)", fd)
            time.sleep(_GRACEFUL_WAIT_INTERVAL)
        ok, detail = _send(fd, pid, signal.SIGKILL, "SIGKILL")
        if not ok and detail == "already gone":
            return True, "terminated (SIGTERM, raced SIGKILL)"
        return ok, detail
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _kill_tree_by_pid(pid: int,
                      expect: "ProcessIdentity | None") -> "tuple[bool, str]":
    """Whole-tree kill. Necessarily PID-based — see the module docstring."""
    if expect is not None and not _identity_still_matches(pid, expect):
        return False, "PID now belongs to a different process — refusing"
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=_KILL_TIMEOUT,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, "could not run taskkill: %s" % exc
        if proc.returncode == 0:
            return True, "tree terminated (taskkill /T; PID-resolved)"
        return False, ((proc.stderr or proc.stdout or "").strip()
                       or "taskkill exited %d" % proc.returncode)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return True, "already gone"
    except OSError as exc:
        return False, "killpg failed: %s" % exc
    return True, "tree terminated (killpg; PID-resolved)"


# ── small platform helpers ────────────────────────────────────────────────

def _identity_still_matches(pid: int, expect: "ProcessIdentity") -> bool:
    actual = process_identity(pid)
    return actual is not None and actual.matches(expect)


def _open_process_windows(pid: int, terminate: bool = False):
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    access = (PROCESS_QUERY_LIMITED_INFORMATION
              | (PROCESS_TERMINATE if terminate else 0))
    return ctypes.windll.kernel32.OpenProcess(access, False, pid)


def _creation_time_windows(handle) -> "int | None":
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    creation, exited = FILETIME(), FILETIME()
    kernel, user = FILETIME(), FILETIME()
    ok = ctypes.windll.kernel32.GetProcessTimes(
        handle, ctypes.byref(creation), ctypes.byref(exited),
        ctypes.byref(kernel), ctypes.byref(user))
    if not ok:
        return None
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def _image_name_windows(handle) -> str:
    import ctypes
    from ctypes import wintypes
    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
        handle, 0, buf, ctypes.byref(size))
    return os.path.basename(buf.value) if ok else ""


def _already_gone_or_denied() -> "tuple[bool, str]":
    """OpenProcess failed: the process is gone, or we may not touch it."""
    import ctypes
    err = ctypes.windll.kernel32.GetLastError()
    if err == _ERROR_INVALID_PARAMETER:
        return True, "already gone"
    return False, "could not open the process (error %d)" % err


def _pidfd(pid: int):
    """A pidfd where the kernel offers one, else None."""
    if not (hasattr(os, "pidfd_open")
            and hasattr(signal, "pidfd_send_signal")):
        return None
    try:
        return os.pidfd_open(pid)
    except (OSError, AttributeError):
        return None


def _send(fd, pid: int, sig, name: str) -> "tuple[bool, str]":
    try:
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return False, "already gone"
    except OSError as exc:
        return False, "%s failed: %s" % (name, exc)
    return True, _with_race_note("terminated (%s)" % name, fd)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _with_race_note(detail: str, fd) -> str:
    """Say plainly when the path taken was a mitigation, not a guarantee."""
    return detail if fd is not None else "%s; PID-resolved (residual race)" % detail
