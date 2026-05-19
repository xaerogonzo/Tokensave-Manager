"""
tokensave-wrapper.py
Run with pythonw.exe so Claude Desktop gets no console window.

Project selection order:
  1. %USERPROFILE%/.tokensave/desktop-project.txt  (if it exists and is valid)
  2. Most recently modified .tokensave/tokensave.db found under SEARCH_ROOTS
  3. No -p flag (tokensave will error with a helpful message)

To pin a project: create desktop-project.txt containing the full folder path.
To auto-switch: delete or update that file, then restart Claude Desktop.
"""

import json
import os
import subprocess
import sys

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
    # 1. Check override file
    override_path = os.path.join(os.environ.get("USERPROFILE", ""), ".tokensave", "desktop-project.txt")
    if os.path.isfile(override_path):
        pinned = open(override_path, encoding="utf-8").read().strip()
        if pinned and os.path.isfile(os.path.join(pinned, ".tokensave", "tokensave.db")):
            return pinned

    # 2. Scan for the most recently touched tokensave.db
    best_mtime = -1
    best_project = None

    for root in SEARCH_ROOTS:
        if not os.path.isdir(root):
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


project = find_project()
args = [TOKENSAVE, "serve"]
if project:
    args += ["-p", project]

# Inherit stdin/stdout/stderr directly — no buffering proxy needed.
# CREATE_NO_WINDOW ensures tokensave itself also gets no console window.
proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
sys.exit(proc.wait())
