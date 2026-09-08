"""Cursor Agent CLI chat sessions, for the Tasks tab.

Mirrors `helpers/claude_tasks.scan_sessions` and returns the same dict shape,
so the Tasks tab concatenates two lists instead of growing a second code path.

Where the data comes from, and how much to trust it
----------------------------------------------------
The CLI keeps each chat under::

    ~/.cursor/chats/<bucket>/<chat-id>/store.db     the transcript (SQLite)
    ~/.cursor/chats/<bucket>/<chat-id>/meta.json    schemaVersion, createdAtMs,
                                                    updatedAtMs, cwd, title

**Cursor does not document any of this.** It is community reverse-engineering,
which changes what the code is allowed to assume. Two consequences run through
this module:

* Only ``meta.json`` is read. ``store.db`` is never opened — every field the
  Tasks tab needs is in the JSON, and touching a SQLite file that a running
  Cursor may hold open buys a lock risk for nothing.
* A record must EARN its way into the list: known ``schemaVersion``, a real
  object, a usable identity and a usable timestamp. A newer schema lists
  nothing rather than guessing, because a wrong title is worse than a missing
  row — the user acts on what the row says.

Failure is open, and logged
---------------------------
Nothing here raises into the UI. But "fail silently" would mean Cursor sessions
quietly vanishing with no way to find out why, so every skip emits a debug-level
line naming the path and the reason. One bad record is one skipped record, never
an aborted scan.
"""

from __future__ import annotations

import json
import os
import time

from helpers.mcp_paths import _same_project
from helpers.runtime import log

#: Highest `meta.json` schemaVersion this code has been written against.
#: A higher one means the format moved under us: list nothing for that record
#: rather than read new semantics with old assumptions.
MAX_SCHEMA_VERSION = 1

#: How deep below ~/.cursor/chats to look. The observed layout is
#: <bucket>/<chat-id>/meta.json, i.e. depth 2; 3 leaves room for one extra
#: grouping level without turning this into an unbounded walk of the home dir.
MAX_DEPTH = 3

#: A session touched within this many seconds is rendered as "recent".
#: Same window claude_tasks uses, so the two agents' rows mean the same thing.
_RECENT_SECONDS = 300


def _cursor_chats_dir() -> str:
    """``~/.cursor/chats``, resolved at call time.

    Never at import: tests redirect ``$USERPROFILE`` via the ``fake_home``
    fixture, and a module-level constant would capture the developer's real
    home before the fixture runs (G-L).
    """
    return os.path.join(os.path.expanduser("~"), ".cursor", "chats")


def _iter_meta_files(root: str, limit: int) -> list:
    """Collect ``(meta_path, mtime, chat_id)`` WITHOUT opening any file.

    Bounded on every axis that could otherwise run away on a machine with
    thousands of chats:

    * depth is capped at ``MAX_DEPTH``;
    * symlinked directories are skipped, so a loop cannot walk forever and a
      link pointing outside ``~/.cursor`` cannot drag unrelated files in;
    * a directory that cannot be read is skipped, not fatal;
    * ordering is deterministic (sorted), so two runs agree.

    Reading is deferred until after the sort, so only the newest ``limit``
    files are ever opened.
    """
    found: list = []

    def walk(directory: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            log.debug("cursor_tasks: cannot scan %s: %s", directory, exc)
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    # A link can point anywhere, including back up the tree.
                    continue
                if entry.is_dir():
                    walk(entry.path, depth + 1)
                elif entry.name == "meta.json":
                    stat = entry.stat()
                    found.append((entry.path, stat.st_mtime,
                                  os.path.basename(directory)))
            except OSError as exc:
                log.debug("cursor_tasks: skipping %s: %s", entry.path, exc)

    walk(root, 0)
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:limit]


def _load_meta(path: str) -> "dict | None":
    """Parse one ``meta.json``, or None with a logged reason."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.debug("cursor_tasks: unreadable meta %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.debug("cursor_tasks: meta is not an object: %s", path)
        return None
    return data


def _schema_ok(meta: dict, path: str) -> bool:
    """True when this code understands the record's format.

    A MISSING schemaVersion is accepted as version 0: the earliest observed
    files carry no such key, and rejecting them would list nothing at all on
    exactly the installs this was written for.
    """
    version = meta.get("schemaVersion", 0)
    if not isinstance(version, (int, float)) or isinstance(version, bool):
        log.debug("cursor_tasks: non-numeric schemaVersion in %s", path)
        return False
    if version > MAX_SCHEMA_VERSION:
        log.debug("cursor_tasks: schemaVersion %s newer than %s: %s",
                  version, MAX_SCHEMA_VERSION, path)
        return False
    return True


def _valid_timestamp(meta: dict, fallback_mtime: float) -> "float | None":
    """`updatedAtMs` in seconds, falling back to the file's own mtime.

    The fallback matters: a record whose timestamp is missing or junk is still
    a real session, and the file's mtime is a perfectly good answer to "when
    was this last touched". Only a record with neither is dropped.
    """
    raw = meta.get("updatedAtMs", meta.get("createdAtMs"))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw) / 1000.0
    if fallback_mtime > 0:
        return fallback_mtime
    return None


def _project_display(meta: dict, known_paths: list) -> str:
    """Map a record's `cwd` onto one of the manager's known projects.

    `cwd` is validated as a string before it reaches any filesystem call, then
    compared with `_same_project` rather than by string equality — on Windows
    `D:\\P\\Foo`, `D:/P/foo/` and a junction alias are one directory, and raw
    comparison would report a known project as unknown.

    A missing or unmatched `cwd` renders as unknown. It is NEVER inferred from
    the title or the chat id: a guess that looks like a fact is the failure
    mode this whole module is written against.
    """
    cwd = meta.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return "(unknown project)"
    for candidate in known_paths or []:
        if isinstance(candidate, str) and _same_project(cwd, candidate):
            return os.path.basename(candidate.rstrip("/\\")) or candidate
    # A real directory we simply do not track — show its name, not a guess
    # about which known project it might be.
    return os.path.basename(cwd.rstrip("/\\")) or cwd


def _row_from_meta(meta_path: str, mtime: float, chat_id: str,
                   known_paths: list, now: float) -> "dict | None":
    """Validate one record and render it as a row, or None with a logged reason.

    Split out of `scan_cursor_sessions` so the scan reads as "walk, validate,
    collect" rather than as one function holding both the traversal and every
    per-record rule. It is also the natural unit: everything here is a reason
    ONE record is dropped, and dropping one is never allowed to end the scan.
    """
    meta = _load_meta(meta_path)
    if meta is None or not _schema_ok(meta, meta_path):
        return None

    session_id = meta.get("id") or meta.get("chatId") or chat_id
    if not isinstance(session_id, str) or not session_id.strip():
        log.debug("cursor_tasks: no usable session id in %s", meta_path)
        return None

    last_activity = _valid_timestamp(meta, mtime)
    if last_activity is None:
        log.debug("cursor_tasks: no usable timestamp in %s", meta_path)
        return None

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "(untitled)"

    return {
        "session_id": session_id,
        "title": title,
        "project_display": _project_display(meta, known_paths),
        "last_activity": last_activity,
        "is_recent": (now - last_activity) < _RECENT_SECONDS,
        "agent": "cursor",
    }


def scan_cursor_sessions(known_paths: list, limit: int = 50) -> list:
    """Recent Cursor CLI chats, newest first.

    Returns the same dict shape as `claude_tasks.scan_sessions`, plus
    ``agent: "cursor"``:

        session_id, title, project_display, last_activity (float),
        is_recent (bool), agent

    Returns ``[]`` when Cursor has never been run, which is the state of any
    machine without it — not an error.
    """
    root = _cursor_chats_dir()
    if not os.path.isdir(root):
        return []

    now = time.time()
    sessions = [
        row for row in (
            _row_from_meta(meta_path, mtime, chat_id, known_paths, now)
            for meta_path, mtime, chat_id in _iter_meta_files(root, limit)
        )
        if row is not None
    ]
    sessions.sort(key=lambda s: s["last_activity"], reverse=True)
    return sessions
