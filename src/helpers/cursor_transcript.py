"""Bounded reads of Cursor agent transcripts.

Cursor's DESKTOP chats are not in `~/.cursor/chats` (that is the terminal agent's
store, read by `cursor_tasks`). They exist only as transcripts::

    ~/.cursor/projects/<project-dir>/agent-transcripts/<chat-id>/<chat-id>.jsonl

one JSON object per line, `{"role": "user" | "assistant", "message": {"content":
[...]}}`, plus `{"type": "turn_ended"}` markers (captured on this machine, not
assumed). Measured: 74 transcripts there, 26 of them invisible to the Manager
until this module existed, including every recent desktop chat.

This is the LOW-LEVEL layer: it lists transcripts and reads a bounded head. It
knows nothing about projects or the UI, and it does not import `session_context`
(which lazily imports `cursor_tasks`, which imports this). The digest in a later
change reads the same format through here, so discovery and the handoff cannot
come to disagree about what a Cursor record looks like.

Nothing here raises into a caller: an unreadable or malformed file yields `""` or
is left out, and the one record that is bad never ends a scan.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import NamedTuple

#: How much of a transcript's head is read to find its first prompt. A first
#: prompt can be huge (people paste whole plans); a 70-character title never
#: needs more than this.
HEAD_BYTES = 64 * 1024

#: Longest title the list shows.
TITLE_CHARS = 70

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_TIMESTAMP = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL)
#: For a head that ends INSIDE the first prompt: the JSON line is cut and cannot
#: be parsed, so the text after the opening tag is taken from the raw line.
_RAW_QUERY = re.compile(r'<user_query>(?:\\n)*(.{1,400})')
_SESSION_ID = re.compile(r"[A-Za-z0-9_\-]+")


class Transcript(NamedTuple):
    """One transcript file, described without opening it."""
    path: str
    project_dir: str     # the folder Cursor named after the workspace (lossy)
    session_id: str
    mtime: float
    mtime_ns: int
    size: int


def projects_dir() -> str:
    """``~/.cursor/projects``, resolved at call time (tests redirect the home)."""
    return os.path.join(os.path.expanduser("~"), ".cursor", "projects")


def encode_name(path: str) -> str:
    """Cursor's folder name for a workspace path, as a case-insensitive key.

    Every run of non-alphanumerics becomes one dash (`D:\\_scratch\\hooks-test`
    is `D-scratch-hooks-test`), which is NOT Claude's encoding and is lossy: it
    is only ever used to COMPARE a known path with a directory name, never to
    turn a name back into a path. Lower-cased because the drive letter's case
    follows how the window was opened (`d-scratch-...` and `C-Users-...` coexist).
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", path or "").strip("-").lower()


def list_transcripts() -> "list[Transcript]":
    """Every transcript on disk, newest first. Stats files; opens none.

    A file counts only when its stem equals its folder's name (the observed
    layout), so a stray `.jsonl` beside a transcript is not listed as a chat. A
    duplicate id across workspaces keeps the most recently written one, the same
    choice `session_context.find_cursor_transcript` makes.
    """
    pattern = os.path.join(projects_dir(), "*", "agent-transcripts", "*", "*.jsonl")
    best: "dict[str, Transcript]" = {}
    for path in glob.glob(pattern):
        folder = os.path.basename(os.path.dirname(path))
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem != folder or not _SESSION_ID.fullmatch(stem):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        project_dir = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        found = Transcript(path, project_dir, stem, stat.st_mtime,
                           stat.st_mtime_ns, stat.st_size)
        held = best.get(stem)
        if held is None or found.mtime_ns > held.mtime_ns:
            best[stem] = found
    return sorted(best.values(), key=lambda t: t.mtime_ns, reverse=True)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(block.get("text") or "") for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def user_query(text: str) -> str:
    """What the person typed, out of Cursor's `<user_query>` wrapper.

    A message without the wrapper is kept minus its timestamp, so an unseen shape
    loses no real prompt.
    """
    found = _USER_QUERY.search(text)
    if found:
        return found.group(1).strip()
    return _TIMESTAMP.sub("", text).strip()


def first_user_query(path: str, head_bytes: int = HEAD_BYTES) -> str:
    """The first prompt in the transcript, read from a bounded head. "" if none."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(head_bytes)
    except OSError:
        return ""
    for raw in head.decode("utf-8", "replace").splitlines():
        if '"role"' not in raw or '"user"' not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            # The head ended inside this record. Take what the raw line holds
            # after the opening tag rather than lose the title.
            found = _RAW_QUERY.search(raw)
            if found:
                return found.group(1).replace("\\n", " ").replace('\\"', '"').strip()
            continue
        if not isinstance(record, dict) or record.get("role") != "user":
            continue
        said = user_query(_text_of((record.get("message") or {}).get("content")))
        if said:
            return said
    return ""


def title_from_query(text: str) -> str:
    """A list title from a prompt: one line, secrets masked, at most TITLE_CHARS."""
    from helpers.agent_handoff import redact     # lazy: keeps discovery light
    flat = " ".join(redact(text or "").split())
    if len(flat) > TITLE_CHARS:
        flat = flat[:TITLE_CHARS - 3].rstrip() + "..."
    return flat
