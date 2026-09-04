"""Project keys in ~/.claude.json — normalising, matching, deduplicating.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import json
import os
import posixpath
from helpers.mcp_paths import (
    _DRIVE_RE,
    _claude_json_path,
)




def normalize_project_key(path: str) -> str:
    """The comparison form for a `~/.claude.json` `projects` key.

    Separators are unified before `normpath` so the answer is the same whether
    this runs on Windows or on Linux CI: these keys are Windows paths either
    way, and `posixpath` is the only module that collapses `a/b` and `a\\b`
    identically on both. The drive letter is folded for the same reason —
    `os.path.normcase` would fold it on Windows and leave it on Linux, which is
    exactly the kind of platform-dependent verdict that cannot be tested.

    Deliberately does NOT touch the filesystem. This runs over every key in a
    file with dozens of them, some naming directories that no longer exist; a
    `realpath` per key would turn a cheap read into a pile of stat calls and
    would silently re-point any key that happens to be a symlink.
    """
    if not path:
        return ""
    unified = posixpath.normpath(path.replace("\\", "/"))
    unified = _DRIVE_RE.sub(lambda m: m.group(1).lower() + ":", unified)
    return os.path.normcase(unified).rstrip("/\\") or unified



def read_claude_projects(claude_json_path: str = "") -> dict:
    """The `projects` map from `~/.claude.json`, or `{}` if unreadable.

    Unreadable degrades to empty rather than raising: every caller is
    decorating a status row, and a missing Claude config must read as "nothing
    known" instead of taking the dialog down.
    """
    path = claude_json_path or _claude_json_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}



def matching_project_keys(project_root: str, projects: dict) -> list:
    """Every raw key in `projects` naming `project_root`, matched normalised.

    A list rather than one key because duplicates are the normal case, not the
    exceptional one, and which duplicate applies depends on how the session was
    launched — a caller that collapses them to a single key is guessing.
    """
    want = normalize_project_key(project_root)
    if not want or not isinstance(projects, dict):
        return []
    return [k for k in projects if normalize_project_key(k) == want]



#: Fields Claude Code writes only after a session has actually run. Their
#: presence is what separates a spelling the user really works in from one some
#: tool created by invoking `claude` in that directory once.
_SESSION_FIELDS = ("lastSessionId", "lastCost", "lastStartTime", "lastDuration")



def _has_session(entry: dict) -> bool:
    """Has a real Claude Code session run under this key?"""
    return isinstance(entry, dict) and any(f in entry for f in _SESSION_FIELDS)



def canonical_launch_dir(project_root: str, projects: "dict | None" = None,
                         claude_json_path: str = "") -> str:
    """The spelling to launch `claude` with so no NEW duplicate key is minted.

    Prefers a spelling Claude Code has already recorded: reusing a key that
    exists is strictly better than adding a fourth way to spell one directory.
    Falls back to the OS-canonical form when the project is unknown to Claude
    Code, which is also what a human typing the path would produce.
    """
    if projects is None:
        projects = read_claude_projects(claude_json_path)
    existing = matching_project_keys(project_root, projects)

    # Prefer the spelling the user's REAL sessions use, identified by session
    # history. Measured 2026-08-25: this manager's own status checks had
    # created a bare entry under the backslash spelling for eight projects,
    # while every actual session lived under the forward-slash one — so
    # picking alphabetically read one record and wrote another. Matching the
    # session's spelling is what makes a status check describe the thing the
    # user is actually running.
    for key in sorted(existing, key=lambda k: (not _has_session(projects[k]), k)):
        if os.path.isdir(key):
            return key
    try:
        return os.path.normpath(os.path.abspath(project_root))
    except (OSError, ValueError):
        return project_root



def duplicate_project_keys(claude_json_path: str = "",
                           projects: "dict | None" = None) -> dict:
    """Normalised path -> the two-or-more raw keys that share it.

    Reported rather than repaired. Merging entries in `~/.claude.json` means
    choosing which side's approvals, trust flag and allowed-tools list survive,
    and that is a decision to put in front of the user behind the show-diff
    protocol — not something to do as a side effect of rendering a status row.
    """
    if projects is None:
        projects = read_claude_projects(claude_json_path)
    groups: dict = {}
    for key in projects:
        groups.setdefault(normalize_project_key(key), []).append(key)
    return {norm: sorted(keys) for norm, keys in groups.items()
            if len(keys) > 1}
