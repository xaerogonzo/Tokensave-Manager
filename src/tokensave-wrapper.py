# anti-monolith: exempt — MCP wrapper, must stay single-threaded; see docs/MCP_INTEGRATION_GOTCHAS.md
"""
tokensave-wrapper.py
Run with pythonw.exe so Claude Desktop gets no console window.

Project selection order:
  1. %USERPROFILE%/.tokensave/desktop-project.txt  (if it exists and is valid)
  2. Most recently modified .tokensave/tokensave.db found under SEARCH_ROOTS
  3. No -p flag (tokensave will error with a helpful message)

To pin a project: create desktop-project.txt containing the full folder path.
To auto-switch: delete or update that file, then restart Claude Desktop.

Run record (Roadmap-10 phase B):
  Immediately after spawning, this writes
  %USERPROFILE%/.tokensave/wrapper-runs/<child-pid>.json noting the project
  and WHICH of the three rules above chose it. Selection rule 2 is a moving
  target when several projects are active -- whichever was indexed most
  recently wins -- and without a record that choice is unrecoverable after
  the fact. The manager reads these to explain a running server rather than
  infer it. Removed on exit; stale ones are pruned after a week.

stdio note (2026-05-23):
  The Popen call below passes sys.stdin/stdout/stderr EXPLICITLY. Without
  that, Python's default inheritance produces a child with broken standard
  handles when the wrapper runs under pythonw.exe — the child tokensave
  binary then never sees MCP messages from Claude Desktop and the attach
  times out at 30 s. This may have always been the case in the wrapper;
  Claude Desktop installs that ran tokensave directly (via the DXT
  extension shape) wouldn't have hit it. The explicit pass-through is
  defensive — it makes the wrapper work under both console (python.exe)
  and windowless (pythonw.exe) launches.

Live-reload note (deferred):
  An earlier revision added a background daemon thread that watched the
  pin file. Removed for now — not strictly needed for correctness, and
  the threading interaction with subprocess + pythonw is fiddly. Pin
  changes currently require a Claude Desktop restart. Future live-reload
  will be implemented as an OUT-OF-PROCESS watcher (sibling process or a
  manager-managed daemon) that signals the running tokensave PID
  directly via taskkill, leaving this wrapper single-threaded.
"""

import json
import os
import subprocess
import sys
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


def find_project():
    """Return (project_path_or_None, reason).

    The reason is the point of the return-tuple change: "Desktop is serving
    project X" and "Desktop is serving project X *because nothing was pinned
    and X happened to be indexed most recently*" are very different facts when
    several projects are active, and only the first was ever recoverable.
    """
    # 1. Check override file
    override_path = os.path.join(os.environ.get("USERPROFILE", ""), ".tokensave", "desktop-project.txt")
    if os.path.isfile(override_path):
        try:
            pinned = open(override_path, encoding="utf-8").read().strip()
        except OSError:
            pinned = ""
        if pinned and os.path.isfile(os.path.join(pinned, ".tokensave", "tokensave.db")):
            return pinned, "pin"

    # 2. Scan for the most recently touched tokensave.db
    best_mtime = -1
    best_project = None

    for root in SEARCH_ROOTS:
        # Search root may be a bare string OR {"path": ..., "label": ...}.
        # The bare-string case is what the original wrapper supported; the
        # dict case was added later in the manager's config schema.
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

    if best_project:
        return best_project, "most-recent-index"
    return None, "none"


def _record_dir():
    return os.path.join(os.environ.get("USERPROFILE", ""),
                        ".tokensave", "wrapper-runs")


def _prune_old_records(max_age_s=7 * 24 * 3600):
    """Drop records left by wrappers that exited without cleaning up.

    Age-based rather than liveness-based on purpose: checking whether a PID is
    alive would put process introspection into a file that has to stay as
    small and predictable as possible.
    """
    directory = _record_dir()
    try:
        now = time.time()
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            full = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(full) > max_age_s:
                    os.remove(full)
            except OSError:
                pass
    except OSError:
        pass


def _write_record(child_pid, project, reason):
    """Note which project this server was started for, and why.

    Written after Popen because the child PID is the key the manager looks it
    up by. `written_at` is a staleness guard: PIDs are reused, so the reader
    only trusts a record whose timestamp is close to the process's own start
    time.

    Every failure is swallowed. A server that runs unrecorded is a small loss;
    a server that fails to start because bookkeeping raised is a large one.
    """
    try:
        directory = _record_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%d.json" % child_pid)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": child_pid,
                       "project": project,
                       "reason": reason,
                       "wrapper_pid": os.getpid(),
                       "written_at": time.time()}, fh)
        return path
    except (OSError, TypeError, ValueError):
        return ""


def _remove_record(path):
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


_prune_old_records()
project, reason = find_project()
args = [TOKENSAVE, "serve"]
if project:
    args += ["-p", project]

# CRITICAL: pass sys.stdin/stdout/stderr EXPLICITLY. Without this, Python's
# default "inherit" behavior on Windows fails when the parent is pythonw.exe
# (windowless) — the child tokensave.exe doesn't get usable standard handles,
# never sees MCP messages, and Claude Desktop times out with
# "MCP server tokensave connection timed out after 30000ms" after 30 s.
# Diagnosed 2026-05-23 by running this wrapper directly with `subprocess.PIPE`
# stdio, sending a real MCP `initialize` request: with default-inheritance
# Popen the child produced zero bytes; with explicit `stdin=sys.stdin, ...`
# the child responded with a full valid initialize result in <100 ms.
# CREATE_NO_WINDOW still suppresses tokensave's console window.
proc = subprocess.Popen(args,
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    creationflags=CREATE_NO_WINDOW)

_record_path = _write_record(proc.pid, project, reason)
try:
    _rc = proc.wait()
finally:
    _remove_record(_record_path)
sys.exit(_rc)
