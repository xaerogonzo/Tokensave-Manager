"""Pure functions for scanning Claude Code sessions and git worktrees."""
from __future__ import annotations

import json
import os
import re
import subprocess

from constants import CREATE_NO_WINDOW

def _claude_projects_dir() -> str:
    """Return ``~/.claude/projects/`` resolved at call time (NOT import time).

    Lazy resolution is important: tests redirect ``$HOME`` via the
    ``fake_home`` fixture in ``tests/conftest.py``, and a module-level
    constant would silently capture the developer's real home before the
    fixture runs. See ``tests/test_no_import_time_path_resolution.py``
    (G-L) for the pre-flight test that enforces this invariant project-
    wide.

    On Windows resolves to ``C:\\Users\\<name>\\.claude\\projects``.
    """
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def encode_project_path(path: str) -> str:
    """Encode a filesystem path the same way Claude Code does for ~/.claude/projects/ dirs.

    Every character outside ``[A-Za-z0-9]`` becomes ``-``, **including ``.`` and
    ``_``**. Verified against all 23 real directories in ``~/.claude/projects``:
    ``D:\\Random Projects\\KicomAI_Project`` lands on
    ``D--Random-Projects-KicomAI-Project`` (underscore replaced), and a worktree
    at ``…\\OpenChem Studio\\.claude\\worktrees\\<name>`` on
    ``…-OpenChem-Studio--claude-worktrees-<name>`` (dot replaced). Not one of
    those directories contains a ``.`` or a ``_``.

    The earlier ``[^\\w.]`` pattern preserved both, so any path containing a dot
    or underscore encoded to a directory name that does not exist. That made
    `decode_project_path` fall through to the raw encoded string for those
    projects, and would have mis-reported session-log presence for exactly the
    ``.claude/worktrees/…`` paths that matter most.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def decode_project_path(encoded: str, known_paths: list[str]) -> str:
    """Return a display name for a ~/.claude/projects/ directory name.

    Matches encode_project_path(p) against encoded for each known path and
    returns that path's basename on a hit. Falls back to the raw encoded string.
    """
    for p in known_paths:
        if encode_project_path(p) == encoded:
            return os.path.basename(p)
    return encoded


def scan_worktrees(project_path: str, git_exe: str) -> list[dict]:
    """Return non-main worktrees for project_path as a list of dicts.

    Keys: path, branch, head (8-char SHA). Returns [] on any error.
    """
    try:
        result = subprocess.run(
            [git_exe, "-C", project_path, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []
    return _parse_worktrees_porcelain(result.stdout)


def _parse_worktrees_porcelain(output: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output, skipping the main worktree."""
    blocks = output.strip().split("\n\n")
    worktrees: list[dict] = []
    for i, block in enumerate(blocks):
        if not block.strip():
            continue
        wt: dict = {"path": "", "branch": "", "head": ""}
        for line in block.splitlines():
            if line.startswith("worktree "):
                wt["path"] = line[9:].strip()
            elif line.startswith("HEAD "):
                wt["head"] = line[5:].strip()[:8]
            elif line.startswith("branch "):
                wt["branch"] = line[7:].strip().removeprefix("refs/heads/")
        if i == 0:
            continue  # skip the main worktree
        if wt["path"]:
            worktrees.append(wt)
    return worktrees


def scan_sessions(known_paths: list[str], limit: int = 50) -> list[dict]:
    """Scan ~/.claude/projects/*/*.jsonl for recent Claude Code sessions.

    Performance strategy: collect (file_path, mtime, encoded_proj_name) tuples
    via os.scandir WITHOUT opening files; sort by mtime descending; open only
    the top `limit` files (read first 30 lines each to find the ai-title event).

    Returned dicts have keys:
      session_id, title, project_encoded, project_display, last_activity (float),
      is_recent (bool: mtime within last 5 minutes).
    """
    projects_dir = _claude_projects_dir()
    if not os.path.isdir(projects_dir):
        return []

    candidates: list[tuple[str, float, str]] = []
    try:
        with os.scandir(projects_dir) as outer:
            for proj_entry in outer:
                if not proj_entry.is_dir():
                    continue
                try:
                    with os.scandir(proj_entry.path) as inner:
                        for f in inner:
                            if f.name.endswith(".jsonl"):
                                try:
                                    mtime = f.stat().st_mtime
                                    candidates.append((f.path, mtime, proj_entry.name))
                                except OSError:
                                    pass
                except OSError:
                    pass
    except OSError:
        return []

    candidates.sort(key=lambda x: x[1], reverse=True)
    candidates = candidates[:limit]

    import time as _time
    now = _time.time()
    sessions: list[dict] = []

    for file_path, mtime, encoded in candidates:
        session_id, title = _extract_session_meta(file_path)
        if session_id is None:
            session_id = os.path.splitext(os.path.basename(file_path))[0]
        if title is None:
            title = "(untitled)"
        sessions.append(
            {
                "session_id": session_id,
                "title": title,
                "project_encoded": encoded,
                "project_display": decode_project_path(encoded, known_paths),
                "last_activity": mtime,
                "is_recent": (now - mtime) < 300,
            }
        )
    return sessions


def _extract_session_meta(file_path: str) -> tuple[str | None, str | None]:
    """Read up to 30 lines of a JSONL file to find the ai-title event.

    Returns (session_id, title) or (None, None) on failure.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 30:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "ai-title":
                        return obj.get("sessionId"), obj.get("aiTitle")
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        pass
    return None, None
