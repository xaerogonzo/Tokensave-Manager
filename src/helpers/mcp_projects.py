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


#: Trust states. Claude Code will not load a project's `.mcp.json` servers in
#: a directory it has not been trusted in, so this gates MCP binding BEFORE
#: `enabledMcpjsonServers` does — and a project blocked here looks exactly
#: like one losing a precedence contest, with a completely different fix.
TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted"
TRUST_UNKNOWN = "unknown"


def project_trust_state(project_root: str, projects: "dict | None" = None,
                        claude_json_path: str = "") -> str:
    """Has Claude Code been trusted in `project_root`?

    Read off the **forward-slash** spelling specifically, not whatever
    `canonical_launch_dir` returns. Measured 2026-09-05 against a
    `~/.claude.json` holding 49 project keys: **all 8 keys carrying session
    history are forward-slash, and none of the 34 backslash keys carry any.**
    Claude Code writes forward slashes; the backslash keys are artifacts of
    tools running `claude` in a directory — this manager's own status checks
    included, as `canonical_launch_dir` says above.

    That distinction is the whole point. `canonical_launch_dir` falls back to
    `os.path.normpath`, which on Windows produces backslashes, so for a
    project with no session history it can return a spelling Claude Code does
    not key by — and the trust flag read from it describes a record nothing
    consults.

    Absence is **untrusted**, not unknown: trust is granted, never assumed,
    and a directory Claude Code has no record of is one it will ask about.
    Until it does, that project's `.mcp.json` is not loaded. `UNKNOWN` is
    reserved for not being able to read `~/.claude.json` at all, which is a
    fact about this tool rather than about the user's setup.

    Verified against the live dialog on six projects with byte-identical
    `.mcp.json` files and `enabledMcpjsonServers: ['tokensave']` in every
    one: the two reporting "verified serving" were exactly the two whose
    forward-slash key had `hasTrustDialogAccepted: true`, and the four
    reporting "shadowed" were the three with `false` plus one with no
    forward-slash key at all.
    """
    record = resolve_project_record(project_root, projects, claude_json_path)
    if record.status == RECORD_UNKNOWN:
        return TRUST_UNKNOWN
    if record.status == RECORD_FOUND and isinstance(record.record, dict) \
            and record.record.get("hasTrustDialogAccepted"):
        return TRUST_TRUSTED
    return TRUST_UNTRUSTED


#: Which `~/.claude.json` record Claude Code consults for a directory.
RECORD_FOUND = "found"
RECORD_ABSENT = "absent"
#: The config could not be read, or the root could not be spelled.
RECORD_UNKNOWN = "unknown"


class ProjectRecord:
    """The one record Claude Code keys a project by, or why there is none."""

    __slots__ = ("status", "key", "record")

    def __init__(self, status: str, key: str = "", record=None):
        self.status = status
        self.key = key
        self.record = record


def read_claude_projects_strict(claude_json_path: str = "") -> "dict | None":
    """The `projects` map, or None when `~/.claude.json` cannot be read.

    The strict sibling of `read_claude_projects`: "no record of this project"
    and "could not read the config" are different facts, and the second is
    about this tool rather than the user's setup.
    """
    path = claude_json_path or _claude_json_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    found = data.get("projects") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def resolve_project_record(project_root: str, projects: "dict | None" = None,
                           claude_json_path: str = "") -> ProjectRecord:
    """The record for *project_root* under the spelling Claude Code writes.

    ONE matching rule, shared by every flag read off a project record — trust,
    external-include approval, anything later — so two readers can never
    disagree about which record a directory is. See `project_trust_state` for
    the measurement behind the forward-slash spelling.
    """
    if projects is None:
        projects = read_claude_projects_strict(claude_json_path)
        if projects is None:
            return ProjectRecord(RECORD_UNKNOWN)
    try:
        forward = os.path.abspath(project_root).replace("\\", "/")
    except (OSError, ValueError):
        return ProjectRecord(RECORD_UNKNOWN)

    # Compared against the forward-slash spelling and NOT normalised, which
    # is the whole mechanism: a backslash-spelled key cannot equal `wanted`,
    # so a flag sitting on one of those leftovers is ignored without needing a
    # rule of its own. Normalising `key` here would quietly undo that and let
    # a leftover confer trust. (An explicit "skip backslash keys" guard stood
    # here briefly and was removed as dead — a mutation proved it could be
    # deleted with nothing failing.)
    wanted = forward.rstrip("/").lower()
    for key, value in projects.items():
        if key.rstrip("/").lower() == wanted:
            return ProjectRecord(RECORD_FOUND, key, value)
    return ProjectRecord(RECORD_ABSENT)


def external_includes_approved(record: ProjectRecord) -> "bool | None":
    """Has Claude Code been allowed to load CLAUDE.md includes from outside?

    An OBSERVED configuration field, measured 2026-09-13 with a canary: an
    include outside the project loaded only once
    `hasClaudeMdExternalIncludesApproved` was true for that project. Nothing
    promises the field keeps that meaning, which is why the Manager localizes
    the baseline instead of depending on it. None means the config was
    unreadable; an absent record is False, because approval is never assumed.
    """
    if record.status == RECORD_UNKNOWN:
        return None
    if record.status == RECORD_FOUND and isinstance(record.record, dict):
        return bool(record.record.get("hasClaudeMdExternalIncludesApproved"))
    return False


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
