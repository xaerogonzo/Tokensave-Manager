"""Cost metrics helper — reads `tokensave cost` output.

The daemon was removed in tokensave v6.0.0 (file-watching now lives inside the
MCP server itself). This module retains only the cost-parsing helper used by
CostViewerDialog; all daemon start/stop/autostart functions have been deleted.
"""

from __future__ import annotations

import re
import subprocess

from constants import CREATE_NO_WINDOW


def parse_tokensave_cost(tokensave_exe: str, scope: str = "7day") -> dict:
    """Invoke `tokensave cost` and parse the table output.

    The CLI output looks like:

        Period           Cost      Input     Output  Cache-hit
        Today         $31.29       2.4k      93.9k       100%
        7d           $933.91      13.5k       4.5M       100%

        Savings  9.7M tokens (68% efficiency)

    scope ∈ {"today", "7day"} picks which row to read for cost/input/output.
    "saved" is read from the trailing "Savings X tokens" line (lifetime total).

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
        row_re = re.compile(
            rf"^\s*{re.escape(row_label)}\s+\$([\d.,]+)\s+(\S+)\s+(\S+)",
            re.MULTILINE,
        )
        row_match = row_re.search(stdout)
        savings_match = re.search(r"Savings\s+(\S+)\s+tokens", stdout)

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
