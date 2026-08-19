"""Module-level constants shared across the TokenSave Manager codebase.

NOTHING IN THIS FILE IS RUNTIME-MUTABLE. Anything that can change after
the user opens Settings -> Save lives in `state.ManagerConfig`. Constants
here are loaded once at module import and never reassigned.

Add new constants here only if:
  - They are immutable for the life of the process
  - They are referenced from more than one module (so deduping helps)
  - They do not depend on anything in `state.py`, `helpers/`, `dialogs/`,
    or `controllers/` (constants live at the bottom of the import graph)
"""

from __future__ import annotations

import os
import re
import sys


# ── Repo base dir (works under both python.exe and Nuitka --onefile) ─────────
# Under Nuitka --onefile, NUITKA_ONEFILE_PARENT is the actual .exe path.
# In dev mode, constants.py lives in src/ so go up one level to repo root.
if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_CONFIG_PATH = os.path.join(_BASE_DIR, "manager-config.json")
LOG_DIR      = os.path.join(_BASE_DIR, "logs")
LOG_FILE     = os.path.join(LOG_DIR, "manager.log")


# ── ANSI escape stripper ──────────────────────────────────────────────────────
# Used by App._run when streaming subprocess output to the OUTPUT pane so
# tokensave's coloured output doesn't render as raw escape sequences.
_ANSI = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])')


# ── Tokensave self-update detection ───────────────────────────────────────────
# tokensave emits this line at the end of any sync when a newer release is
# available on GitHub. Capture both versions so the manager can display
# "Upgrade v5.1.1 -> v5.1.2" in Settings and decide when the button should
# show up. Accepts an arrow rendered as either Unicode arrow or ASCII -> / =>,
# and tolerates either the bare "5.1.2" or "v5.1.2" form.
_TOKENSAVE_UPDATE_RE = re.compile(
    r'Update available:\s*v?(\d+\.\d+\.\d+(?:\.\d+)?)\s*'
    r'(?:→|->|=>)\s*v?(\d+\.\d+\.\d+(?:\.\d+)?)')


# ── Project-discovery limits ──────────────────────────────────────────────────
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "target", "build", "dist", "out", ".gradle", "bin", "obj",
}
MAX_DEPTH = 4


# ── Windows subprocess flag ───────────────────────────────────────────────────
# Hide the cmd.exe window when spawning subprocesses from a Tk app under
# pythonw.exe — without this every git/tokensave invocation flashes a
# black console. MUST be 0 off-Windows: a non-zero `creationflags` makes
# subprocess raise "creationflags is only supported on Windows platforms"
# (breaks the Linux CI test runner, which exercises the git/test-gap helpers).
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# The opposite flag: spawn a REAL console window, used where the point is to
# give a child process an interactive TTY (tokensave's purge prompt, the
# Claude CLI hand-off). Same rule and the same reason as above — referencing
# `subprocess.CREATE_NEW_CONSOLE` directly raises AttributeError off-Windows,
# because the constant does not exist there at all. That is not hypothetical:
# it took the Linux CI test-gate red for five commits on master.
CREATE_NEW_CONSOLE = 0x00000010 if sys.platform == "win32" else 0


# ── App refresh cadence ──────────────────────────────────────────────────────
AUTO_REFRESH_MS = 60_000   # auto-refresh project list every 60 s


# ── Git network-op environment ───────────────────────────────────────────────
# Prevents infinite hang when credentials aren't cached. GIT_TERMINAL_PROMPT=0
# tells git to fail immediately instead of waiting for stdin. Compatible with
# Git Credential Manager (GCM authenticates via browser, not stdin, so this
# env var doesn't interfere with it).
_GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


# ── Pin file (Claude Desktop active-project) ─────────────────────────────────
def desktop_project_file() -> str:
    """Path to the ``.tokensave/desktop-project.txt`` pin file.

    Lazy resolution (re-evaluates per call) so that tests can redirect
    ``$USERPROFILE`` via the ``fake_home`` fixture in
    ``tests/conftest.py`` (G-F + G-J). See
    ``tests/test_no_import_time_path_resolution.py`` (G-L) for the
    pre-flight test that enforces this invariant.
    """
    return os.path.join(
        os.environ.get("USERPROFILE", os.path.expanduser("~")),
        ".tokensave", "desktop-project.txt",
    )


# ── Catppuccin Mocha palette ─────────────────────────────────────────────────
# Imported by every UI module (dialogs, controllers, theme). Hex codes
# match the upstream Catppuccin spec exactly.
C = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "overlay0": "#6c7086",
    "text":     "#cdd6f4",
    "subtext":  "#bac2de",
    "blue":     "#89b4fa",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "red":      "#f38ba8",
    "lavender": "#b4befe",
    "sky":      "#89dceb",
    "peach":    "#fab387",
    "mauve":    "#cba6f7",
}
