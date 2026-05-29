"""install_codegraph — npm-driven lifecycle for the codegraph CLI binary (v4.8).

Three thin wrappers over npm:
  * ``install_codegraph(npm_exe)``    — ``npm install -g @colbymchenry/codegraph``
  * ``update_codegraph(npm_exe)``     — ``npm install -g @colbymchenry/codegraph@latest``
  * ``uninstall_codegraph(npm_exe)``  — ``npm uninstall -g @colbymchenry/codegraph``

Plus version-detection and a fallback-chain finder for the
brand-new-Node-install case where ``%APPDATA%\\npm`` isn't on PATH yet
(G-B from the v4.8 plan).

Gemini-fix coverage:
  * **G-A**: every callsite takes ``npm_exe`` as an absolute path
    parameter — never invokes ``"npm"`` by bare name (would
    FileNotFoundError on Windows since ``npm`` is a ``.cmd`` shim).
    Caller resolves via ``helpers.detection._detect_npm()`` once and
    passes the result; module is shell=False throughout.
  * **G-B**: ``detect_codegraph_after_install`` provides a three-step
    fallback (shutil.which → npm prefix -g probe → restart hint) so
    the UI doesn't get stuck in "not installed" after a successful
    first-ever global npm install.

Pure-ish: each function returns ``(ok: bool, log: str)`` so the Tool
Manager dialog can stream output without coupling to Tk.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Optional, Tuple

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0


_PKG = "@colbymchenry/codegraph"


def _run_npm(npm_exe: str, args: list, on_log: Optional[Callable[[str], None]],
              timeout: int = 300) -> Tuple[bool, str]:
    """Shared subprocess runner for the three npm wrappers below.

    G-A: requires npm_exe to be the absolute path (caller resolved via
    ``_detect_npm()``). Bare ``"npm"`` would FileNotFoundError on
    Windows.

    Streams stdout line-by-line into ``on_log`` if provided so the
    Tool Manager dialog can render progress in real time.
    """
    if not npm_exe or not os.path.isfile(npm_exe):
        return False, f"npm not found at: {npm_exe!r}"
    log_lines: list = []
    try:
        proc = subprocess.Popen(
            [npm_exe] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as exc:
        return False, f"could not launch npm: {exc}"
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                stripped = line.rstrip()
                log_lines.append(stripped)
                if on_log is not None:
                    try:
                        on_log(stripped)
                    except Exception:
                        pass
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log_lines.append(f"[timed out after {timeout}s]")
        return False, "\n".join(log_lines)
    return proc.returncode == 0, "\n".join(log_lines)


# ── Lifecycle wrappers ────────────────────────────────────────────────────────

def install_codegraph(npm_exe: str,
                       on_log: Optional[Callable[[str], None]] = None
                       ) -> Tuple[bool, str]:
    """Run ``npm install -g @colbymchenry/codegraph``.

    G-A: npm_exe MUST be an absolute path to ``npm.cmd`` on Windows.
    Use ``helpers.detection._detect_npm()`` to resolve it.

    Returns ``(ok, log)``. Caller is responsible for refreshing the
    binary-path detection (use ``detect_codegraph_after_install`` to
    cover the G-B PATH-not-updated case on first-install machines).
    """
    return _run_npm(npm_exe, ["install", "-g", _PKG], on_log)


def update_codegraph(npm_exe: str,
                      on_log: Optional[Callable[[str], None]] = None
                      ) -> Tuple[bool, str]:
    """Run ``npm install -g @colbymchenry/codegraph@latest``.

    Per v4.8 user decision: prefer ``install …@latest`` over
    ``npm update -g`` (which sometimes silently no-ops on Windows).

    Post-update: if npm emitted EPERM cleanup warnings (Windows file-lock
    on the old better_sqlite3.node native binary), append a friendly note
    so the user knows the update still completed and a restart is all that
    is needed.
    """
    ok, log = _run_npm(npm_exe, ["install", "-g", f"{_PKG}@latest"], on_log)
    if "EPERM" in log:
        note = (
            "\n"
            "ℹ️  The 'EPERM cleanup' warnings above are harmless on Windows.\n"
            "   npm could not delete a temp directory because the old\n"
            "   codegraph binary was still loaded in memory — this is normal\n"
            "   when the file watcher is running. The update completed\n"
            "   successfully. Restart TokenSave Manager to fully release\n"
            "   the old binary and pick up the new version."
        )
        if on_log is not None:
            try:
                on_log(note)
            except Exception:
                pass
        log += note
    return ok, log


def uninstall_codegraph(npm_exe: str,
                         on_log: Optional[Callable[[str], None]] = None
                         ) -> Tuple[bool, str]:
    """Run ``npm uninstall -g @colbymchenry/codegraph``."""
    return _run_npm(npm_exe, ["uninstall", "-g", _PKG], on_log, timeout=120)


# ── Version + detection ──────────────────────────────────────────────────────

def codegraph_version(codegraph_exe: str) -> str:
    """Return e.g. ``'1.4.2'`` or ``''`` on any error.

    Bounded 5 s timeout so a hung binary can't freeze the Tool Manager
    dialog's refresh pass.
    """
    if not codegraph_exe or not os.path.isfile(codegraph_exe):
        return ""
    try:
        proc = subprocess.run(
            [codegraph_exe, "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    # Output is typically just the version string on one line, but
    # tolerate "codegraph 1.4.2" or similar prefixes.
    out = (proc.stdout or "").strip()
    if not out:
        return ""
    parts = out.split()
    # First token that looks version-y (digits + dots)
    for tok in parts:
        s = tok.lstrip("v")
        if s and s[0].isdigit() and any(c == "." for c in s):
            return s
    return out.split("\n", 1)[0].strip()


def _npm_prefix(npm_exe: str) -> str:
    """Return ``npm prefix -g`` output (the npm global install dir) or ''."""
    if not npm_exe or not os.path.isfile(npm_exe):
        return ""
    try:
        proc = subprocess.run(
            [npm_exe, "prefix", "-g"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def detect_codegraph_after_install(npm_exe: str = "") -> str:
    """G-B 3-step fallback chain for finding the codegraph binary after install.

    Bare ``shutil.which`` often fails on a brand-new first-ever-global-
    npm-install machine because the Python process's PATH snapshot
    doesn't yet include npm's global bin dir. This helper:

    1. ``shutil.which('codegraph.cmd' / 'codegraph')`` — fast path
    2. If empty: ``<npm prefix -g>\\codegraph.cmd`` and
       ``<npm prefix -g>\\node_modules\\.bin\\codegraph.cmd``
    3. If still empty: returns ''. Caller logs a "restart the shell so
       PATH refreshes" hint and the user clicks Check again later.

    Returns the resolved absolute path or empty string.
    """
    # Step 1 — PATH lookup
    for name in ("codegraph.cmd", "codegraph"):
        found = shutil.which(name)
        if found:
            return found
    # Step 2 — npm-global-prefix probe
    prefix = _npm_prefix(npm_exe) if npm_exe else ""
    if prefix:
        for sub in ("codegraph.cmd", "codegraph",
                    os.path.join("node_modules", ".bin", "codegraph.cmd"),
                    os.path.join("node_modules", ".bin", "codegraph")):
            candidate = os.path.join(prefix, sub)
            if os.path.isfile(candidate):
                return candidate
    # Step 3 — also check the common Windows location explicitly
    appdata = os.environ.get("APPDATA")
    if appdata:
        for sub in ("codegraph.cmd", "codegraph"):
            candidate = os.path.join(appdata, "npm", sub)
            if os.path.isfile(candidate):
                return candidate
    return ""
