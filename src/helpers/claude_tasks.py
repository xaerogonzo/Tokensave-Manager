"""Pure functions for scanning Claude Code sessions and git worktrees."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess

from constants import CREATE_NO_WINDOW

#: Lines of a transcript searched for the working directory.
_CWD_SEARCH_LINES = 200


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


def worktree_label(cwd: str) -> str:
    """A readable name for a folder a session ran in.

    A path under `<repo>/.claude/worktrees/<name>` reads as
    `<repo> (worktree <name>)`; anything else is its last segment. The encoded
    directory name is never shown: `D--Claude-Co-worker-...-claude-worktrees-x`
    is lossy (every separator is a dash), so it cannot be turned back into
    either part, which is why a session's own recorded `cwd` is used instead.
    """
    norm = (cwd or "").replace("\\", "/").rstrip("/")
    marker = "/.claude/worktrees/"
    at = norm.lower().find(marker)
    if at != -1:
        repo = os.path.basename(norm[:at])
        name = norm[at + len(marker):].split("/")[0]
        if repo and name:
            return f"{repo} (worktree {name})"
    return norm.rsplit("/", 1)[-1] or (cwd or "")


#: `<repo>/.claude/worktrees/<name>`: where Claude Code makes a session's own checkout.
_WORKTREE_PATH = re.compile(
    r"^(?P<repo>.+?)[\\/]\.claude[\\/]worktrees[\\/][^\\/]+", re.IGNORECASE)

#: The folder a session ran in is there and usable.
FOLDER_OK = "ok"
#: A Claude worktree that is no longer a checkout (finished and removed, so the
#: path is gone or an empty leftover) while its repository is still there.
FOLDER_REMOVED = "removed"
#: Nothing usable, and nothing to say it moved: never guessed at.
FOLDER_MISSING = "missing"


@dataclasses.dataclass(frozen=True)
class SessionFolder:
    state: str
    #: The folder to open: the session's own when OK, its repository when REMOVED,
    #: else "".
    path: str
    recorded: str


def _is_git_root(path: str) -> bool:
    """`.git` is a directory in a clone and a file in a worktree, so it is existence
    that counts (the project's rule for `_is_local_git_repo`)."""
    return os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))


def resolve_session_folder(cwd: str) -> SessionFolder:
    """Where a session can be continued, from the folder it recorded.

    `os.path.isdir` alone calls a removed worktree present: Claude leaves the
    empty directory behind, git no longer lists it, and a handoff would open an
    empty folder (with a "repository state now" read from whatever repository
    sits above it). Inside `.claude/worktrees/<name>` a folder counts only while
    it holds a `.git`. When it does not and the repository itself is still a git
    checkout, the repository is the fallback, and the caller is told it is one.
    Elsewhere nothing is inferred: a folder is there or it is MISSING.
    """
    if not cwd:
        return SessionFolder(FOLDER_MISSING, "", "")
    found = _WORKTREE_PATH.match(cwd)
    if not found:
        return (SessionFolder(FOLDER_OK, cwd, cwd) if os.path.isdir(cwd)
                else SessionFolder(FOLDER_MISSING, "", cwd))
    if _is_git_root(found.group(0)):
        return (SessionFolder(FOLDER_OK, cwd, cwd) if os.path.isdir(cwd)
                else SessionFolder(FOLDER_MISSING, "", cwd))
    repo = found.group("repo")
    if _is_git_root(repo):
        return SessionFolder(FOLDER_REMOVED, repo, cwd)
    return SessionFolder(FOLDER_MISSING, "", cwd)


def transcript_cwd(path: str) -> str:
    """The working directory a transcript was recorded in, or `""`.

    It sits on the first user record, a handful of lines in, so only the head
    is read. Never guessed from the directory name (see `worktree_label`).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _CWD_SEARCH_LINES:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


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
      session_id, title, project_encoded, project_display, branch, last_activity
      (float), is_recent (bool: mtime within last 5 minutes), path (the transcript file --
      a session id is not unique on disk, so a caller that needs THE file must
      not rebuild its name from the id).
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

    # Lazy: live_sessions imports this module.
    from helpers.live_sessions import read_tail_meta

    for file_path, mtime, encoded in candidates:
        session_id, head_title = _extract_session_meta(file_path)
        # The tail first: the name the desktop app shows, the folder the session
        # is in NOW and its branch. The head is the fallback, not the answer.
        meta = read_tail_meta(file_path)
        cwd = meta["cwd"] or transcript_cwd(file_path)
        if session_id is None:
            session_id = os.path.splitext(os.path.basename(file_path))[0]
        title = meta["title"] or head_title or "(untitled)"
        sessions.append(
            {
                "session_id": session_id,
                "title": title,
                "project_encoded": encoded,
                "project_display": _with_branch(
                    _display_for(encoded, cwd, known_paths), meta["branch"]),
                "branch": meta["branch"],
                "cwd": cwd,
                "last_activity": mtime,
                "is_recent": (now - mtime) < 300,
                "path": file_path,
            }
        )
    return sessions


def _with_branch(display: str, branch: str) -> str:
    """`<project> @ <branch>`: the branch is what the desktop app's environment
    panel shows, so it is the one thing that lets a row be matched to a session."""
    return f"{display} @ {branch}" if branch else display


def _display_for(encoded: str, cwd: str, known_paths: list[str]) -> str:
    """A known project's name, else a readable name from the session's `cwd`.

    `decode_project_path` returns the encoded name unchanged on a miss, and a
    worktree is exactly the folder that is never a known project, so those rows
    used to read as `D--Claude-Co-worker-Token-Save-Manager-Source--claude-...`.
    """
    shown = decode_project_path(encoded, known_paths)
    if shown == encoded and cwd:
        label = worktree_label(cwd)
        if resolve_session_folder(cwd).state == FOLDER_REMOVED:
            label += " - checkout removed"
        return label
    return shown


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
