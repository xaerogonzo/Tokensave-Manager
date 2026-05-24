"""Helpers for managing the tokensave background daemon and reading cost metrics.

Pure-function module — no Tkinter imports. All functions call tokensave.exe as a
subprocess and parse its stdout. CREATE_NO_WINDOW suppresses console flicker on Windows.
"""

from __future__ import annotations

import os
import re
import subprocess

from constants import CREATE_NO_WINDOW


def get_daemon_status(tokensave_exe: str) -> dict:
    """Invoke `tokensave daemon --status` and parse the result.

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
        if proc.returncode != 0:
            return {"running": False, "autostart": False, "pid": None,
                    "error": stderr.strip()}
        lower = stdout.lower()
        is_running       = "running" in lower
        autostart_enabled = "autostart: enabled" in lower
        pid_match = re.search(r"PID:\s*(\d+)", stdout, re.IGNORECASE)
        pid = int(pid_match.group(1)) if pid_match else None
        return {"running": is_running, "autostart": autostart_enabled,
                "pid": pid, "error": None}
    except Exception as e:
        return {"running": False, "autostart": False, "pid": None, "error": str(e)}


def toggle_daemon(tokensave_exe: str, enable: bool) -> tuple[bool, str]:
    """Start or stop the tokensave daemon.

    Returns:
        (success: bool, message: str)
    """
    action = "start" if enable else "stop"
    try:
        proc = subprocess.Popen(
            [tokensave_exe, "daemon", f"--{action}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, stderr = proc.communicate(timeout=10)
        if proc.returncode == 0:
            return True, stdout.strip()
        return False, stderr.strip()
    except Exception as e:
        return False, str(e)


def parse_tokensave_cost(tokensave_exe: str, scope: str = "7day") -> dict:
    """Invoke `tokensave cost [today]` and parse the metrics table.

    Returns:
        {"saved": str, "value": str, "input": str, "output": str, "raw": str}
    All values are strings (formatted numbers); "raw" is the full stdout for debugging.
    """
    cmd = [tokensave_exe, "cost"]
    if scope == "today":
        cmd.append("today")
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
        saved_match = re.search(r"Saved Tokens:\s*([\d,]+)", stdout)
        value_match = re.search(r"Estimated Value Saved:\s*\$([\d.]+)", stdout)
        input_match = re.search(r"Input Tokens:\s*([\d,]+)", stdout)
        output_match = re.search(r"Output Tokens:\s*([\d,]+)", stdout)
        return {
            "saved":  saved_match.group(1)  if saved_match  else "0",
            "value":  value_match.group(1)  if value_match  else "0.00",
            "input":  input_match.group(1)  if input_match  else "0",
            "output": output_match.group(1) if output_match else "0",
            "raw":    stdout,
        }
    except Exception as e:
        return {"saved": "0", "value": "0.00", "input": "0", "output": "0",
                "raw": f"Error: {e}"}
