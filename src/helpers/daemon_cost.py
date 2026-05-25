"""Helpers for managing the tokensave background daemon and reading cost metrics.

Pure-function module — no Tkinter imports. All functions call tokensave.exe as a
subprocess and parse its stdout. CREATE_NO_WINDOW suppresses console flicker on Windows.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

from constants import CREATE_NO_WINDOW

# Windows-only creationflag to spawn a fully detached process group. Combined
# with stdin/stdout/stderr = DEVNULL, this lets us launch the daemon without
# our parent process holding any file handles or waiting for it to exit.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def get_daemon_status(tokensave_exe: str) -> dict:
    """Invoke `tokensave daemon --status` and parse the result.

    Note: tokensave writes the daemon status line to STDERR (not stdout),
    even on success (rc=0). We combine stdout + stderr before parsing.

    Returns:
        {"running": bool, "autostart": bool, "pid": int | None, "error": str | None}
    """
    if not tokensave_exe or not os.path.exists(tokensave_exe):
        return {"running": False, "autostart": False, "pid": None,
                "error": "tokensave.exe not found"}
    try:
        proc = subprocess.Popen(
            [tokensave_exe, "daemon", "--status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = proc.communicate(timeout=5)
        combined = ((stdout or "") + "\n" + (stderr or "")).strip()

        if proc.returncode != 0 and not combined:
            return {"running": False, "autostart": False, "pid": None,
                    "error": "tokensave daemon --status returned no output"}

        lower = combined.lower()
        # "is running" matches the actual stderr line "tokensave daemon is running (PID: X)";
        # "not running" / "not started" / "no daemon" mark a stopped state.
        is_stopped = ("not running" in lower
                      or "not started" in lower
                      or "no daemon" in lower)
        is_running = ("is running" in lower or "daemon running" in lower) and not is_stopped
        autostart_enabled = "autostart: enabled" in lower or "autostart enabled" in lower
        pid_match = re.search(r"PID:\s*(\d+)", combined, re.IGNORECASE)
        pid = int(pid_match.group(1)) if pid_match else None

        # If neither running nor stopped patterns matched but returncode was
        # non-zero, surface the raw output so the user sees what went wrong.
        if not is_running and not is_stopped and proc.returncode != 0:
            return {"running": False, "autostart": False, "pid": None,
                    "error": combined[:200]}

        return {"running": is_running, "autostart": autostart_enabled,
                "pid": pid,
                "error": combined if is_stopped and not is_running else None}
    except Exception as e:
        return {"running": False, "autostart": False, "pid": None, "error": str(e)}


def _kill_pid(pid: int) -> tuple[bool, str]:
    """Force-terminate a PID. Windows: taskkill /F. POSIX: SIGTERM then SIGKILL."""
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            if proc.returncode == 0:
                return True, f"Killed PID {pid} (taskkill)."
            return False, (proc.stderr or proc.stdout or "taskkill failed").strip()
        except Exception as e:
            return False, f"taskkill error: {e}"
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
        try:
            os.kill(pid, 0)  # still alive?
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True, f"Killed PID {pid}."
    except ProcessLookupError:
        return True, f"PID {pid} already gone."
    except Exception as e:
        return False, f"kill({pid}) failed: {e}"


def _stop_daemon(tokensave_exe: str) -> tuple[bool, str]:
    """Stop the daemon, trying the official `--stop` first and falling back to
    a PID-based force-kill when that fails or leaves the process alive.

    `tokensave daemon --stop` is broken on Windows (tokensave 5.x): it tries to
    open a Windows service named "tokensave-daemon" which doesn't exist (the
    daemon is started as a detached process, not a registered service), and
    errors out with "failed to open service". When that happens we look up the
    PID via `--status` and terminate the process directly. Upstream issue:
    docs/upstream-issues/tokensave-daemon-stop-windows.md.
    """
    cli_msg = ""
    try:
        proc = subprocess.run(
            [tokensave_exe, "daemon", "--stop"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
        cli_msg = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0:
            # Verify — tokensave 5.x has been observed to return rc=0 while
            # leaving the daemon running. Trust --status, not the exit code.
            time.sleep(0.4)
            if not get_daemon_status(tokensave_exe).get("running"):
                return True, cli_msg or "Daemon stopped."
    except Exception as e:
        cli_msg = str(e)

    # --stop failed or didn't actually stop the process. Fall back to PID kill.
    status = get_daemon_status(tokensave_exe)
    pid = status.get("pid")
    if not pid:
        return False, (cli_msg or "Daemon stop failed and no PID available.")
    ok, kill_msg = _kill_pid(pid)
    if not ok:
        return False, f"--stop failed ({cli_msg}); PID kill also failed: {kill_msg}"
    # Confirm
    time.sleep(0.3)
    if get_daemon_status(tokensave_exe).get("running"):
        return False, f"Killed PID {pid} but daemon still reports running."
    return True, f"Daemon stopped (force-killed PID {pid} — `--stop` is broken on Windows)."


def toggle_daemon(tokensave_exe: str, enable: bool) -> tuple[bool, str]:
    """Start or stop the tokensave daemon.

    Start path: spawn `tokensave daemon` fully detached. On Windows, tokensave
    does NOT fork itself into the background even though --foreground is the
    documented OPT-IN flag — the bare `daemon` command stays in foreground
    until something closes its stdin/stdout. We work around this by spawning
    with DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP + DEVNULL on all standard
    streams, so the daemon survives our parent and we never block waiting on it.
    After spawn, we poll `daemon --status` for up to 3 seconds to confirm.

    Stop path: delegated to `_stop_daemon`, which tries `tokensave daemon --stop`
    first and falls back to a PID-based force-kill when it fails (on Windows it
    always fails with a service-handle error in tokensave 5.x — see upstream
    issue docs/upstream-issues/tokensave-daemon-stop-windows.md).

    "Already running" is treated as success when enable=True — the daemon
    is in the desired state regardless of who started it.

    Returns:
        (success: bool, message: str)
    """
    if not enable:
        return _stop_daemon(tokensave_exe)

    # Start path — check first if already running so we return success
    # immediately instead of trying to spawn a duplicate that tokensave will
    # reject with "already running".
    status = get_daemon_status(tokensave_exe)
    if status.get("running"):
        pid = status.get("pid")
        msg = f"Daemon already running (PID: {pid})." if pid else "Daemon already running."
        return True, msg

    # Spawn fully detached. DEVNULL on all streams + DETACHED_PROCESS +
    # CREATE_NEW_PROCESS_GROUP means our Popen handle goes out of scope
    # immediately and the daemon survives our parent process.
    try:
        subprocess.Popen(
            [tokensave_exe, "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(CREATE_NO_WINDOW
                           | _DETACHED_PROCESS
                           | _CREATE_NEW_PROCESS_GROUP),
            close_fds=True,
        )
    except Exception as e:
        return False, f"Failed to spawn daemon: {e}"

    # Poll for up to 3 seconds to confirm the daemon actually came up.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        time.sleep(0.3)
        status = get_daemon_status(tokensave_exe)
        if status.get("running"):
            pid = status.get("pid")
            msg = f"Daemon started (PID: {pid})." if pid else "Daemon started."
            return True, msg

    return False, ("Daemon spawned but did not report running within 3s. "
                   "Check tokensave logs.")


def toggle_autostart(tokensave_exe: str, enable: bool) -> tuple[bool, str]:
    """Install or remove the daemon autostart service.

    Calls `tokensave daemon --enable-autostart` / `--disable-autostart`
    (both confirmed via `tokensave daemon --help`). On Windows this
    typically registers a scheduled task; on macOS a launchd plist; on
    Linux a systemd user unit.

    Returns:
        (success: bool, message: str)
    """
    flag = "--enable-autostart" if enable else "--disable-autostart"
    try:
        proc = subprocess.Popen(
            [tokensave_exe, "daemon", flag],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = proc.communicate(timeout=15)
        if proc.returncode == 0:
            return True, stdout.strip() or stderr.strip()
        return False, stderr.strip() or stdout.strip()
    except Exception as e:
        return False, str(e)


def parse_tokensave_cost(tokensave_exe: str, scope: str = "7day") -> dict:
    """Invoke `tokensave cost` and parse the table output.

    The CLI output (verified Roadmap-3 Phase 1 dogfooding) looks like:

        Period           Cost      Input     Output  Cache-hit
        Today         $31.29       2.4k      93.9k       100%
        7d           $933.91      13.5k       4.5M       100%

        Savings  9.7M tokens (68% efficiency)

    scope ∈ {"today", "7day"} picks which row to read for cost/input/output.
    "saved" is read from the trailing "Savings X tokens" line which is
    period-independent (lifetime).

    Returns:
        {"saved": str, "value": str, "input": str, "output": str, "raw": str}
    All values are strings (already formatted with k/M suffixes by tokensave);
    "raw" is the full stdout for debugging.
    """
    cmd = [tokensave_exe, "cost"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, _ = proc.communicate(timeout=5)

        row_label = "Today" if scope == "today" else "7d"
        # Match the row: label, then cost ($N.NN), then input, then output.
        # Cache-hit column is ignored.
        row_re = re.compile(
            rf"^\s*{re.escape(row_label)}\s+\$([\d.,]+)\s+(\S+)\s+(\S+)",
            re.MULTILINE,
        )
        row_match = row_re.search(stdout)

        # Savings line — lifetime cumulative tokens saved.
        savings_match = re.search(
            r"Savings\s+(\S+)\s+tokens",
            stdout,
        )

        return {
            "saved":  savings_match.group(1) if savings_match else "0",
            "value":  row_match.group(1)     if row_match     else "0.00",
            "input":  row_match.group(2)     if row_match     else "0",
            "output": row_match.group(3)     if row_match     else "0",
            "raw":    stdout,
        }
    except Exception as e:
        return {"saved": "0", "value": "0.00", "input": "0", "output": "0",
                "raw": f"Error: {e}"}
