"""codegraph_daemon — list + stop running CodeGraph MCP daemons.

CodeGraph's own daemon lifecycle is opaque to the Manager: daemons are spawned
by whatever MCP client (Claude Code, VS Code) has codegraph wired in, and the
CLI's only listing/stop surface (`codegraph daemon`) is an interactive TTY
picker — "pick one and press enter to stop it". There is no scriptable
`--stop`/`--pid` flag.

Confirmed live (v1.5.0) that `codegraph daemon` with stdin closed/empty is
SAFE to call non-interactively for LISTING — it prints the table and exits,
it does not hang waiting for input. Example output::

    pid 46424  v1.5.0  up 5m 7s  D:\\Random Projects\\OpenChem Studio
    pid 51768  v1.5.0  up 5m 14s  D:\\Claude Co worker\\Token Save Manager Source

For STOPPING, this module deliberately does NOT attempt to drive the
interactive picker's stdin protocol (undocumented — index vs PID is unknown,
and getting it wrong risks stopping the wrong daemon). Instead it kills by
PID directly at the OS level, the same pattern already used twice elsewhere
in this codebase:
  * ``helpers/smoke_runner.py:_kill_tree`` (taskkill /F /T /PID, or
    os.killpg + SIGKILL on POSIX)
  * the now-removed tokensave daemon workaround documented in
    ``docs/upstream-issues/tokensave-daemon-stop-windows.md`` (taskkill /F
    /PID, or SIGTERM then SIGKILL on POSIX)

All functions are fail-open / pure-ish: no Tk, plain-data returns, so the
dialog layer can stay a thin presentation wrapper.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import Tuple

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0

# `pid <n>  v<version>  up <uptime>  <path>` — uptime is one-or-more
# space-separated tokens (e.g. "5m 7s", "1h 2m 3s"); the double-space run
# before the path is what terminates the non-greedy uptime capture, since
# uptime's own internal spaces are always single.
_DAEMON_LINE_RE = re.compile(
    r"^\s*pid\s+(\d+)\s+v(\S+)\s+up\s+(.+?)\s{2,}(.+?)\s*$")

_KILL_TIMEOUT = 10


def list_codegraph_daemons(codegraph_exe: str) -> list:
    """Return every running CodeGraph daemon as a list of dicts.

    Each entry: ``{"pid": int, "version": str, "uptime": str, "path": str}``.
    Fail-open: any subprocess or parse problem yields ``[]`` rather than
    raising, matching ``codegraph_freshness.py``'s convention.

    ``stdin=subprocess.DEVNULL`` is load-bearing — ``codegraph daemon`` is
    normally an interactive picker; closing stdin is what makes it print
    and exit instead of waiting on a TTY (confirmed live).
    """
    if not codegraph_exe or not os.path.isfile(codegraph_exe):
        return []
    try:
        proc = subprocess.run(
            [codegraph_exe, "daemon"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = proc.stdout or ""
    daemons: list = []
    for line in out.splitlines():
        m = _DAEMON_LINE_RE.match(line)
        if not m:
            continue
        pid_s, version, uptime, path = m.groups()
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        daemons.append({
            "pid": pid, "version": version,
            "uptime": uptime.strip(), "path": path.strip(),
        })
    return daemons


def kill_codegraph_daemon(pid: int) -> Tuple[bool, str]:
    """Terminate a daemon by PID. Returns (ok, detail).

    Windows: ``taskkill /F /PID <pid>``. POSIX: SIGTERM, briefly wait, then
    SIGKILL if the process is still alive — mirrors the documented (now-moot)
    tokensave-daemon-stop workaround exactly.
    """
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=_KILL_TIMEOUT,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"could not run taskkill: {exc}"
        if proc.returncode == 0:
            return True, (proc.stdout or "").strip()
        return False, ((proc.stderr or proc.stdout or "").strip()
                       or f"taskkill exited {proc.returncode}")
    return _kill_posix(pid)


def _kill_posix(pid: int) -> Tuple[bool, str]:
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "already gone"
    except OSError as exc:
        return False, f"SIGTERM failed: {exc}"
    for _ in range(20):   # ~2s at 0.1s steps
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True, "terminated (SIGTERM)"
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "terminated (SIGTERM, raced SIGKILL)"
    except OSError as exc:
        return False, f"SIGKILL failed: {exc}"
    return True, "terminated (SIGKILL)"


def unlock_codegraph_project(codegraph_exe: str, path: str) -> Tuple[bool, str]:
    """Run ``codegraph unlock <path>`` — removes a STALE lock file.

    Distinct from killing a live daemon: this is for the orphaned-lock case
    (a crashed process left a lock with nothing actually holding it). Safe
    to expose as a secondary fallback alongside the daemon list, less
    destructive than the tool's own suggested "delete .codegraph/ and
    re-init" nuclear option.
    """
    if not codegraph_exe or not os.path.isfile(codegraph_exe):
        return False, "codegraph is not installed"
    try:
        proc = subprocess.run(
            [codegraph_exe, "unlock", path],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run codegraph unlock: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out
