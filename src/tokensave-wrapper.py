"""
tokensave-wrapper.py
Run with pythonw.exe so Claude Desktop gets no console window.

Project selection order:
  1. %USERPROFILE%/.tokensave/desktop-project.txt  (if it exists and is valid)
  2. Most recently modified .tokensave/tokensave.db found under SEARCH_ROOTS
  3. No -p flag (tokensave will error with a helpful message)

To pin a project: create desktop-project.txt containing the full folder path.

Live reload (added 2026-05-23):
  A background thread polls desktop-project.txt every 2 seconds. When the pin
  changes to a different valid project, the wrapper terminates the current
  `tokensave serve` child and respawns it pointing at the new project. MCP
  clients reconnect transparently — no Claude Code / Claude Desktop restart
  is required.
"""

import json
import os
import subprocess
import sys
import threading
import time

if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_CONFIG_PATH = os.path.join(_BASE_DIR, "manager-config.json")

def _load_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

_cfg = _load_config()

TOKENSAVE    = _cfg.get("tokensave_exe", "")
SEARCH_ROOTS = _cfg.get("search_roots", [])

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "target", "build", "dist", "out", ".gradle"}

MAX_DEPTH = 4
CREATE_NO_WINDOW = 0x08000000

OVERRIDE_PATH = os.path.join(
    os.environ.get("USERPROFILE", ""), ".tokensave", "desktop-project.txt")

# Poll cadence for the pin watcher. 2s is responsive enough that a manual ★
# click in the manager feels nearly instant, but slow enough that we're not
# stat()-ing the pin file at a silly rate.
_PIN_POLL_INTERVAL = 2.0

# How long to wait for the OS to release the port after terminating a child
# tokensave server before retrying a spawn that hit address-in-use.
_PORT_RELEASE_BACKOFF = 0.5
_PORT_RELEASE_MAX_RETRIES = 10  # ~5 seconds total


def _read_pin() -> str:
    """Read the pin file with tolerant error handling.

    Returns "" on any failure (missing, locked mid-write by the manager,
    transient permission error, empty contents). The caller treats "" as
    "no pin, fall back to scan / leave current project alone".
    """
    if not os.path.isfile(OVERRIDE_PATH):
        return ""
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        # The manager might be in the middle of writing it on Windows.
        # The next 2s tick will catch the real value.
        return ""


def _pin_is_valid(path: str) -> bool:
    return bool(path) and os.path.isfile(
        os.path.join(path, ".tokensave", "tokensave.db"))


def find_project():
    """Choose the project to serve. Called once at startup AND once per
    respawn after the watcher signals a pin change."""
    # 1. Check override file
    pinned = _read_pin()
    if _pin_is_valid(pinned):
        return pinned

    # 2. Scan for the most recently touched tokensave.db
    best_mtime = -1
    best_project = None

    for root in SEARCH_ROOTS:
        # Search root may be a bare string OR {"path": ..., "label": ...}
        if isinstance(root, dict):
            root = root.get("path", "")
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Enforce depth limit
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= MAX_DEPTH:
                dirnames.clear()
                continue

            # Check before pruning (pruning removes dot-dirs including .tokensave)
            has_tokensave = ".tokensave" in dirnames

            # Prune noise dirs in-place so os.walk skips them
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

            if has_tokensave:
                db = os.path.join(dirpath, ".tokensave", "tokensave.db")
                if os.path.isfile(db):
                    mtime = os.path.getmtime(db)
                    if mtime > best_mtime:
                        best_mtime = mtime
                        best_project = dirpath

    return best_project


# ───────────────────────────────────────────────────────────────────────
# Shared state between the main thread (spawns / awaits the tokensave child)
# and the pin-watcher thread (terminates the child when the pin changes).
# Wrapped in single-element lists so we can mutate from either thread without
# the `nonlocal` keyword (these are module-globals anyway).
# ───────────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_current_project = [find_project()]
_current_child   = [None]   # type: list[subprocess.Popen | None]
_swap_requested  = [False]  # set True by watcher when it kills the child
                            # for a project swap; main loop clears it after
                            # respawning so an unrelated child crash still
                            # exits the wrapper cleanly.


def _watch_pin():
    """Background thread: poll the pin file; when it changes to a different
    valid project, signal a child-swap to the main thread."""
    try:
        last_mtime = os.path.getmtime(OVERRIDE_PATH)
    except OSError:
        last_mtime = 0.0

    while True:
        time.sleep(_PIN_POLL_INTERVAL)

        try:
            new_mtime = os.path.getmtime(OVERRIDE_PATH)
        except OSError:
            # File got deleted between ticks (or never existed). Keep polling
            # — it might come back. Don't kill the running child either way.
            continue

        if new_mtime == last_mtime:
            continue
        last_mtime = new_mtime

        pinned = _read_pin()
        if not _pin_is_valid(pinned):
            # The user emptied the file or pointed at a folder without a DB.
            # Leave the current child alone — silent ignore is the safe play.
            continue

        with _state_lock:
            if pinned == _current_project[0]:
                # Pin file was rewritten with the same value — no-op.
                continue
            sys.stderr.write(
                f"[tokensave-wrapper] pin changed: "
                f"{_current_project[0]!r} -> {pinned!r}; restarting child\n")
            _current_project[0] = pinned
            _swap_requested[0] = True
            child = _current_child[0]

        # Terminate outside the lock so the main thread can grab it
        # immediately when proc.wait() returns.
        if child is not None:
            try:
                child.terminate()
            except OSError:
                pass


# ───────────────────────────────────────────────────────────────────────
# Main: spawn the tokensave child in a loop. If the watcher killed it for
# a project swap, respawn with the new -p. If it died on its own (crash or
# clean shutdown by the MCP client), exit with the same code so Claude
# Code's MCP supervisor can take over.
# ───────────────────────────────────────────────────────────────────────

threading.Thread(target=_watch_pin, daemon=True, name="pin-watcher").start()

while True:
    with _state_lock:
        project = _current_project[0]
    args = [TOKENSAVE, "serve"]
    if project:
        args += ["-p", project]

    # Spawn with retry-on-port-busy. tokensave serve may hit address-in-use
    # for ~half a second after we terminate the previous child because the
    # OS hasn't released the listening socket yet.
    child = None
    last_err: BaseException | None = None
    for attempt in range(_PORT_RELEASE_MAX_RETRIES):
        try:
            child = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            last_err = e
            sys.stderr.write(
                f"[tokensave-wrapper] spawn failed ({e}); "
                f"retrying in {_PORT_RELEASE_BACKOFF}s "
                f"(attempt {attempt + 1}/{_PORT_RELEASE_MAX_RETRIES})\n")
            time.sleep(_PORT_RELEASE_BACKOFF)
            continue
        break

    if child is None:
        sys.stderr.write(
            f"[tokensave-wrapper] giving up after "
            f"{_PORT_RELEASE_MAX_RETRIES} spawn attempts; last error: {last_err}\n")
        sys.exit(1)

    with _state_lock:
        _current_child[0] = child

    # Inherit stdin/stdout/stderr from the child — MCP protocol goes through
    # stdio so we don't proxy anything.
    rc = child.wait()

    with _state_lock:
        swap = _swap_requested[0]
        _swap_requested[0] = False
        _current_child[0] = None

    if swap:
        # We killed it on purpose; loop around and respawn with the new -p.
        sys.stderr.write(
            f"[tokensave-wrapper] respawning with project {_current_project[0]!r}\n")
        continue

    # Child died on its own — exit with its code so the MCP supervisor sees
    # the same exit signal it would have seen pre-watcher.
    sys.exit(rc)
