"""Filesystem / PATH detection for external tools and project formats.

Pure functions — no globals, no state. Each function takes no arguments
(or only the input it inspects) and returns a path string or an empty
string when the tool isn't found.

`state.ManagerConfig.refresh_derived()` calls into this module lazily
(inside the function body) to avoid a circular import at module load.
"""

from __future__ import annotations

import os
import shutil


# ── Semver helper (used by version comparisons across the manager) ───────────

def _version_lt(a: str, b: str) -> bool:
    """Return True if version string `a` is strictly less than `b`.

    Compares dotted numeric versions tuple-wise after coercing missing
    components to 0. Non-numeric tags (alpha/beta/rc) are not handled —
    pure semver-style "1.2.3" comparisons only, which is what tokensave
    uses. Falls back to string compare on parse failure.
    """
    def _parts(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, AttributeError):
            return None
    pa, pb = _parts(a), _parts(b)
    if pa is None or pb is None:
        return a < b
    # Pad to equal length for fair tuple compare.
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return pa < pb


# ── External-tool detection ──────────────────────────────────────────────────

def _detect_git() -> str:
    """Return the best available path to git.exe.

    Priority:
      1. Explicit path in manager-config.json  (caller checks that first)
      2. shutil.which("git")  — works if Git is on PATH
      3. Common Git-for-Windows install locations
      4. Bare "git" fallback (will fail with a clear error if not found)
    """
    found = shutil.which("git")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "git"


def _detect_gh() -> str:
    """Return the path to gh.exe (GitHub CLI) if installed, else empty string.

    Checks PATH first, then common winget/scoop install locations.
    Returns "" when not found so callers can easily test with `if _detect_gh()`.
    """
    found = shutil.which("gh")
    if found:
        return found
    for candidate in [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\gh.exe"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_npm() -> str:
    """Return the path to npm, else empty string.

    On Windows npm is a `.cmd` shim, not a `.exe` — `subprocess.run` with
    a bare `.cmd` raises FileNotFoundError unless the absolute path
    (including the .cmd extension) is supplied. So we probe `.cmd` first.
    """
    for name in ("npm.cmd", "npm"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\npm.cmd"),
        os.path.expandvars(r"%ProgramFiles%\nodejs\npm.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_codegraph() -> str:
    """Return the path to the codegraph CLI, else empty string.

    Same Windows-.cmd-first priority as _detect_npm because codegraph is
    installed by npm as a .cmd shim. Returns "" (not the bare command
    name) so callers can test `if CODEGRAPH_EXE:` cleanly without
    accidentally invoking a bare command via subprocess.
    """
    for name in ("codegraph.cmd", "codegraph"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\codegraph.cmd"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\codegraph.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_claude_cli() -> str:
    """Return the path to the Claude Code CLI, else empty string.

    npm installs `claude` as a .cmd shim on Windows — probe that first.
    Returns "" (not a bare command name) so callers can test `if cfg.claude_cli_exe:` cleanly.
    If auto-detect fails (e.g. npm global bin not on PATH in this launch context), the user
    should set the full path manually in Settings (e.g. %APPDATA%\\npm\\claude.cmd).
    """
    for name in ("claude.cmd", "claude"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\claude.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _is_codegraph_project(path: str) -> bool:
    """True iff `path` has been initialised by CodeGraph (the .codegraph/
    SQLite database exists)."""
    return os.path.isfile(os.path.join(path, ".codegraph", "codegraph.db"))


# ── Search-root format normalisers (str OR {"path":..,"label":..} dict) ──────

def _root_path(r):
    """Return the directory path from a search-root entry (str or dict)."""
    return r if isinstance(r, str) else r["path"]


def _root_label(r):
    """Return the display label for a search-root entry."""
    p = _root_path(r)
    if isinstance(r, str):
        return os.path.basename(p.rstrip("/\\"))
    return r.get("label", os.path.basename(p.rstrip("/\\"))) or os.path.basename(p.rstrip("/\\"))


def _detect_glab() -> str:
    """Path to glab.exe (GitLab CLI) if installed, else "".

    Same shape as ``_detect_gh``: PATH first, then the winget install
    location. Detection rather than a configured path, because that is how
    every other forge CLI is found here — adding a config key for this one
    would leave two ways to answer the same question.
    """
    found = shutil.which("glab")
    if found:
        return found
    for candidate in [
        r"C:\Program Files\glab\bin\glab.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Links\glab.exe"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""
