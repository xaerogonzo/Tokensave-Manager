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

Desktop chats are a second source
---------------------------------
The Cursor DESKTOP app keeps no `meta.json`: its chats exist only as transcripts
under ``~/.cursor/projects/<workspace>/agent-transcripts/`` (`cursor_transcript`).
Measured on one machine: 74 transcripts, and the meta-only scan listed 49, missing
every recent desktop chat. Those rows are merged in by chat id. A transcript does
not record the folder it ran in, and Cursor's workspace folder name is lossy, so a
chat's folder is RESOLVED, never decoded (see `resolve_workspace`): exactly one
known folder, an inferred project, no match, or an ambiguity that is reported and
never broken by picking one.

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
import re
import time

from helpers import cursor_transcript as ct
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
    # about which known project it might be. A worktree reads as its repo.
    from helpers.claude_tasks import worktree_label
    return worktree_label(cwd)


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
        # The folder the chat ran in, as Cursor recorded it. A handoff needs
        # the real path, and the display name is a lossy summary of it.
        "cwd": meta["cwd"] if isinstance(meta.get("cwd"), str) else "",
        "last_activity": last_activity,
        "is_recent": (now - last_activity) < _RECENT_SECONDS,
        "agent": "cursor",
    }


# -- Desktop chats: transcripts, and the folder each one ran in ---------------

#: How a transcript's workspace folder was resolved.
RES_MATCHED = "matched"            # exactly one known folder has that name
RES_PROJECT_ONLY = "project_only"  # a `_scratch` checkout of one known project
RES_UNMATCHED = "unmatched"        # nothing known has that name
RES_AMBIGUOUS = "ambiguous"        # several do; none is chosen

#: The project's documented place for extra checkouts: `<drive>:\_scratch\<repo>-<sha>`.
#: The only layout a project is INFERRED from; elsewhere a name is matched or it is not.
_SCRATCH = re.compile(r"^[a-z]-scratch-(?P<rest>.+)$")

#: Newest `limit` Cursor chats; a desktop user has many more than 50.
DEFAULT_LIMIT = 200

_TITLE_CACHE: dict = {}
_TITLE_CACHE_MAX = 1000


def _known_folders(known_paths: list) -> dict:
    """`{normalised path: (path, project path)}`: every known project and each
    worktree git has registered for it. Plain file reads, no git process: a
    worktree's `.git/worktrees/<name>/gitdir` names its own `.git` file."""
    found: dict = {}
    for project in known_paths or []:
        if not isinstance(project, str) or not project.strip():
            continue
        norm = os.path.normpath(project)
        found.setdefault(os.path.normcase(norm), (norm, project))
        registry = os.path.join(project, ".git", "worktrees")
        try:
            names = os.listdir(registry)
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(registry, name, "gitdir"), encoding="utf-8") as fh:
                    dotgit = fh.read().strip()
            except OSError:
                continue
            folder = os.path.normpath(os.path.dirname(dotgit)) if dotgit else ""
            if folder and os.path.isdir(folder):
                found.setdefault(os.path.normcase(folder), (folder, project))
    return found


def resolve_workspace(project_dir: str, folders: dict, known_paths: list) -> dict:
    """Which folder a Cursor workspace directory name stands for.

    `{"state", "cwd", "project_path", "candidates"}`. `cwd` is set ONLY for an
    exact match: Cursor's name collapses every separator, so it is compared with
    the same collapse of KNOWN folders and never turned back into a path. Several
    matches are `ambiguous`; taking the first or the newest would make the answer
    depend on enumeration order and could send a handoff into the wrong checkout.
    """
    key = ct.encode_name(project_dir)
    hits = {norm: pair for norm, pair in folders.items()
            if ct.encode_name(pair[0]) == key}
    if len(hits) == 1:
        path, project = next(iter(hits.values()))
        return {"state": RES_MATCHED, "cwd": path, "project_path": project,
                "candidates": ()}
    if len(hits) > 1:
        return {"state": RES_AMBIGUOUS, "cwd": "", "project_path": "",
                "candidates": tuple(sorted(pair[0] for pair in hits.values()))}
    scratch = _SCRATCH.match(key)
    if scratch:
        rest = scratch.group("rest")
        named: dict = {}
        for project in known_paths or []:
            if not isinstance(project, str):
                continue
            base = ct.encode_name(os.path.basename(os.path.normpath(project)))
            if base and (rest == base or rest.startswith(base + "-")):
                named.setdefault(base, {})[os.path.normcase(os.path.normpath(project))] = project
        if named:
            projects = named[max(named, key=len)]      # the most specific name
            if len(projects) == 1:
                return {"state": RES_PROJECT_ONLY, "cwd": "",
                        "project_path": next(iter(projects.values())),
                        "candidates": ()}
            return {"state": RES_AMBIGUOUS, "cwd": "", "project_path": "",
                    "candidates": tuple(sorted(projects.values()))}
    return {"state": RES_UNMATCHED, "cwd": "", "project_path": "", "candidates": ()}


def _base(path: str) -> str:
    return os.path.basename(os.path.normpath(path)) or path


def _display_for_resolution(res: dict, project_dir: str) -> str:
    """What the Project column says. An inference is worded as one."""
    state, cwd, project = res["state"], res["cwd"], res["project_path"]
    if state == RES_MATCHED:
        if os.path.normcase(os.path.normpath(cwd)) == os.path.normcase(os.path.normpath(project)):
            return _base(project)
        return f"{_base(project)} (worktree {_base(cwd)})"
    if state == RES_PROJECT_ONLY:
        return f"{_base(project)} (checkout not identified)"
    if state == RES_AMBIGUOUS:
        return f"{project_dir} (several matching folders)"
    return project_dir


def _title_of(transcript) -> str:
    """The list title for one transcript, cached on what could change it."""
    key = (transcript.path, transcript.mtime_ns, transcript.size)
    if key not in _TITLE_CACHE:
        if len(_TITLE_CACHE) >= _TITLE_CACHE_MAX:
            _TITLE_CACHE.clear()
        _TITLE_CACHE[key] = ct.title_from_query(ct.first_user_query(transcript.path))
    return _TITLE_CACHE[key] or "(untitled)"


def scan_cursor_transcripts(known_paths: list, limit: int = DEFAULT_LIMIT) -> list:
    """Cursor desktop chats from their transcripts, newest first.

    The same row shape as `scan_cursor_sessions`, plus ``path`` (the transcript a
    handoff reads), ``resolution`` (how the folder was resolved) and
    ``project_path`` (the known project it belongs to, or ""). One unreadable
    transcript is one row less, never an aborted scan.
    """
    now = time.time()
    folders = _known_folders(known_paths)
    listed = ct.list_transcripts()[:limit]
    live_keys = {(t.path, t.mtime_ns, t.size) for t in listed}
    for stale in [k for k in _TITLE_CACHE if k not in live_keys]:
        del _TITLE_CACHE[stale]
    rows = []
    for transcript in listed:
        res = resolve_workspace(transcript.project_dir, folders, known_paths)
        rows.append({
            "session_id": transcript.session_id,
            "title": _title_of(transcript),
            "project_display": _display_for_resolution(res, transcript.project_dir),
            "cwd": res["cwd"],
            "last_activity": transcript.mtime,
            "is_recent": (now - transcript.mtime) < _RECENT_SECONDS,
            "agent": "cursor",
            "path": transcript.path,
            "resolution": res["state"],
            "project_path": res["project_path"],
        })
    return rows


def _usable_dir(path) -> bool:
    return isinstance(path, str) and os.path.isabs(path) and os.path.isdir(path)


def _merge(meta_row: dict, chat: dict, folders: dict, now: float) -> dict:
    """One chat seen by both sources. Field by field:

    title: the metadata's unless it has none. cwd: the metadata's while it names a
    real directory, else the transcript's exact match, else the metadata's stale
    value kept (a handoff re-checks it and names it in the error). last_activity:
    the metadata's, always: it is the chat's own recorded time, while a
    transcript's file time moves with a copy or a touch (the context window and
    the ordering of existing consumers read it). path: the transcript. agent:
    always cursor.
    """
    row = dict(meta_row)
    row["path"] = chat["path"]
    if row.get("title") in (None, "", "(untitled)"):
        row["title"] = chat["title"]
    if not _usable_dir(row.get("cwd")) and chat["resolution"] == RES_MATCHED:
        row["cwd"] = chat["cwd"]
        row["project_display"] = chat["project_display"]
    row["is_recent"] = (now - row["last_activity"]) < _RECENT_SECONDS
    if _usable_dir(row.get("cwd")):
        norm = os.path.normcase(os.path.normpath(row["cwd"]))
        row["resolution"] = RES_MATCHED
        row["project_path"] = (folders.get(norm) or ("", chat["project_path"]))[1]
    else:
        row["resolution"] = chat["resolution"]
        row["project_path"] = chat["project_path"]
    return row


def scan_cursor_sessions(known_paths: list, limit: int = DEFAULT_LIMIT) -> list:
    """Recent Cursor chats, newest first: the terminal agent's (`meta.json`) and
    the desktop app's (transcripts), merged by chat id.

    Returns the same dict shape as `claude_tasks.scan_sessions`, plus
    ``agent: "cursor"``:

        session_id, title, project_display, last_activity (float),
        is_recent (bool), agent

    and, for a chat with a transcript, ``path``, ``resolution`` and
    ``project_path`` (see `scan_cursor_transcripts`).

    Returns ``[]`` when Cursor has never been run, which is the state of any
    machine without it — not an error.
    """
    now = time.time()
    meta_rows = []
    root = _cursor_chats_dir()
    if os.path.isdir(root):
        meta_rows = [
            row for row in (
                _row_from_meta(meta_path, mtime, chat_id, known_paths, now)
                for meta_path, mtime, chat_id in _iter_meta_files(root, limit)
            )
            if row is not None
        ]
    chats = {row["session_id"]: row
             for row in scan_cursor_transcripts(known_paths, limit)}
    if not chats:
        meta_rows.sort(key=lambda s: s["last_activity"], reverse=True)
        return meta_rows

    folders = _known_folders(known_paths)
    merged = []
    for row in meta_rows:
        chat = chats.pop(row["session_id"], None)
        merged.append(_merge(row, chat, folders, now) if chat else row)
    merged.extend(chats.values())
    merged.sort(key=lambda s: s["last_activity"], reverse=True)
    return merged[:limit]
