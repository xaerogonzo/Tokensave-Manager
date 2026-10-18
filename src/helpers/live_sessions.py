"""live_sessions -- which Claude Code sessions are running RIGHT NOW, and is
tokensave serving each one?

The Tasks tab's session list is HISTORY: transcript files, with a dot that means
"modified in the last five minutes". Tokensave has no session concept, so "active
tokensave sessions" can only mean two things the process tree does know:

* a Claude Code session that is running, and
* the ``tokensave serve`` process it started, if any.

## What was measured (2026-10-18, Windows, Claude Desktop 2.42304 / claude-code 2.1.299)

* A live session started by the Claude desktop app is one ``claude.exe`` process
  with ``--output-format stream-json`` and ``--input-format stream-json``, a child
  of the app's own ``Claude.exe``. Its command line carries ``--resume=<uuid>``,
  which is the transcript file's name, plus ``--model=`` and ``--effort=``.
* Each session had exactly one ``tokensave.exe`` child. So a server CAN be tied to
  its session by parent process id; the ``tokensave_daemon`` lister simply never
  collected ``ParentProcessId``.
* The transcript's LAST ``cwd`` equalled the process's real folder for all eight
  sessions. Its FIRST ``cwd`` did not for one, which had moved through 127 folders.
  So the folder is read from the tail of the transcript. The files are up to
  163 MB; only the tail is read.

## What it does not claim

* **Terminal sessions are not detected.** A ``claude`` started in a shell has a
  different process shape that has not been measured here. The view says so; it
  does not present its list as every session.
* **Unknown is never none.** Process enumeration that could not run is a failure,
  not "no sessions". A session whose transcript or folder cannot be read has an
  unknown project, never a guessed one. A server whose project the registry did
  not state is UNVERIFIED, never "serving".
* **SERVING needs authoritative evidence.** Only a server whose project the
  tokensave registry (or ``-p``) named, equal to the session's folder.

No Tk, and every external read is injectable, so the classification is tested from
constructed process tables and not from whatever happens to be running.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from constants import CREATE_NO_WINDOW
from helpers.claude_tasks import _claude_projects_dir, _extract_session_meta
from helpers.instructions_posture import canonical
from helpers.tokensave_daemon import (
    AUTHORITATIVE,
    EnumerationFailed,
    _PROC_TIMEOUT,
    _powershell_exe,
    list_tokensave_servers,
)

# How a person-visible server state is named. Constants, not strings at call
# sites, so a typo cannot silently become a fifth state.
SERVING = "serving"          # one server, authoritative, this session's folder
OTHER_FOLDER = "other"       # one server, authoritative, a DIFFERENT folder
UNVERIFIED = "unverified"    # a server exists; its project is not established
NO_SERVER = "none"           # no tokensave process under this session
MULTIPLE = "multiple"        # more than one tokensave process under it

_TAIL_BYTES = 256 * 1024
#: A title is written when set or renamed, not on every turn, so it can sit
#: further from the end than the last folder does. Measured: present in the last
#: 1 MB of all eight live transcripts.
_TITLE_TAIL_BYTES = 1024 * 1024
_MAX_DEPTH = 8
_SESSION_FLAGS = ("--output-format stream-json", "--input-format stream-json")
_RESUME = re.compile(r"--resume=([0-9a-fA-F-]{36})")
_MODEL = re.compile(r"--model=(\S+)")
_EFFORT = re.compile(r"--effort=(\S+)")


@dataclass(frozen=True)
class LiveSession:
    """One running session, with everything stated about its tokensave server."""

    pid: int
    started_at: float
    session_id: str          # "" for a session with no --resume
    model: str
    effort: str
    project: str             # real folder, "" when it could not be read
    title: str
    state: str               # SERVING / OTHER_FOLDER / UNVERIFIED / NO_SERVER / MULTIPLE
    server_pids: tuple = ()
    server_project: str = ""
    detail: str = ""         # why UNVERIFIED, when it is


@dataclass(frozen=True)
class StrayServer:
    """A tokensave server that is under no running session we found."""

    pid: int
    started_at: float
    project: str
    attribution: str


@dataclass(frozen=True)
class LiveSnapshot:
    sessions: tuple = ()
    strays: tuple = ()
    #: Set when the process table could not be read. Then `sessions` is EMPTY BY
    #: FAILURE, which is a different fact from there being none.
    failed: str = ""
    #: Set when the tokensave server list could not be read. Sessions are still
    #: shown, with their servers UNVERIFIED rather than absent.
    servers_failed: str = ""

    @property
    def counts(self) -> dict:
        out = {SERVING: 0, OTHER_FOLDER: 0, UNVERIFIED: 0, NO_SERVER: 0,
               MULTIPLE: 0}
        for session in self.sessions:
            out[session.state] += 1
        return out

    @property
    def summary(self) -> str:
        """Facts, never a verdict. Failure is stated, not rendered as zero."""
        if self.failed:
            return "Could not check which sessions are running (%s)." % self.failed
        n = len(self.sessions)
        if not n:
            line = "No Claude Code sessions started by the desktop app are running."
        else:
            c = self.counts
            line = ("%d session%s running  ·  tokensave: %d serving their folder, "
                    "%d serving another folder, %d unconfirmed, %d none"
                    % (n, "" if n == 1 else "s", c[SERVING], c[OTHER_FOLDER],
                       c[UNVERIFIED] + c[MULTIPLE], c[NO_SERVER]))
        if self.strays:
            line += "  ·  %d tokensave server%s under no running session" % (
                len(self.strays), "" if len(self.strays) == 1 else "s")
        if self.servers_failed:
            line += "  ·  server list unavailable (%s)" % self.servers_failed
        return line


# -- process table --------------------------------------------------------

def parse_process_rows(raw: str) -> list:
    """`ConvertTo-Json` output to dicts. An object for one row, an array for many."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    rows = [data] if isinstance(data, dict) else data
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid, ppid = int(row.get("ProcessId")), int(row.get("ParentProcessId"))
        except (TypeError, ValueError):
            continue
        try:
            started = float(row.get("Start") or 0)
        except (TypeError, ValueError):
            started = 0.0
        out.append({"pid": pid, "ppid": ppid, "name": (row.get("Name") or "").lower(),
                    "cmd": row.get("Cmd") or "", "started_at": started})
    return out


def enumerate_processes(*, strict: bool = True) -> list:
    """The whole process table. Windows only; anything else is a failure.

    Command lines are fetched only for the two images that need them: this is
    ~600 rows, and a claude command line is 3.5 KB.
    """
    if sys.platform != "win32":
        if strict:
            raise EnumerationFailed("live session detection is Windows-only")
        return []
    exe = _powershell_exe()
    if not exe:
        raise EnumerationFailed("PowerShell could not be located")
    script = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, "
        "Name, @{n='Cmd';e={ if ($_.Name -in 'claude.exe','tokensave.exe') "
        "{ $_.CommandLine } }}, "
        "@{n='Start';e={ if ($_.CreationDate) { "
        "[DateTimeOffset]::new($_.CreationDate).ToUnixTimeMilliseconds()/1000 } }} "
        "| ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_PROC_TIMEOUT + 10,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnumerationFailed("%s: %s" % (type(exc).__name__, exc)) from exc
    if proc.returncode != 0:
        raise EnumerationFailed(
            (proc.stderr or "").strip()[:200] or "PowerShell exited %s"
            % proc.returncode)
    return parse_process_rows(proc.stdout)


# -- classification (pure) ------------------------------------------------

def parse_session_command(cmd: str) -> "tuple | None":
    """`(session_id, model, effort)` for a desktop-started session, else None."""
    if not all(flag in cmd for flag in _SESSION_FLAGS):
        return None
    sid = _RESUME.search(cmd)
    model, effort = _MODEL.search(cmd), _EFFORT.search(cmd)
    return (sid.group(1).lower() if sid else "",
            model.group(1) if model else "", effort.group(1) if effort else "")


def descendants(rows: list, root_pid: int) -> list:
    """Every row below *root_pid*, to a bounded depth, safe against cycles."""
    kids: dict = {}
    for row in rows:
        kids.setdefault(row["ppid"], []).append(row)
    seen, out, frontier = {root_pid}, [], [root_pid]
    for _ in range(_MAX_DEPTH):
        nxt = []
        for pid in frontier:
            for child in kids.get(pid, []):
                if child["pid"] in seen:
                    continue
                seen.add(child["pid"])
                out.append(child)
                nxt.append(child["pid"])
        frontier = nxt
    return out


def classify_server(session_project: str, server_rows: list,
                    by_pid: dict) -> tuple:
    """`(state, server_pids, server_project, detail)` for one session.

    SERVING is the only state that needs evidence, and it needs all of it: the
    server's project named by the registry (or `-p`), and equal to the session's
    folder. Every other gap is UNVERIFIED with its reason, never SERVING.
    """
    pids = tuple(sorted(r["pid"] for r in server_rows))
    if not pids:
        return NO_SERVER, (), "", ""
    if len(pids) > 1:
        return MULTIPLE, pids, "", "%d tokensave processes under this session" % len(pids)
    info = by_pid.get(pids[0])
    if info is None:
        return UNVERIFIED, pids, "", "the server list did not include this process"
    if info.attribution != AUTHORITATIVE or not info.project:
        return UNVERIFIED, pids, info.project or "", \
            "the server's project is a guess, not stated by tokensave"
    if not session_project:
        return UNVERIFIED, pids, info.project, "the session's folder could not be read"
    here, served = canonical(session_project), canonical(info.project)
    if here == served:
        return SERVING, pids, info.project, ""
    # Stated as a fact about the paths, not as a diagnosis: a session inside
    # `<project>\.claude\worktrees\x` whose server serves `<project>` is the
    # measured common case, and it means the session's tokensave answers about
    # a different checkout than the one being edited.
    inside = here.startswith(served.rstrip("\\/") + os.sep)
    return OTHER_FOLDER, pids, info.project, (
        "this session's folder is inside the served project (a worktree?)"
        if inside else "")


# -- wording (pure) ---------------------------------------------------------

def age_text(started_at: float, now: float) -> str:
    """`5m`, `3h`, `2d`. Empty when the start time is unknown."""
    if not started_at:
        return ""
    secs = max(0, int(now - started_at))
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def model_text(session: "LiveSession") -> str:
    model = session.model[len("claude-"):] if session.model.startswith("claude-") \
        else session.model
    return " · ".join(part for part in (model, session.effort) if part)


def server_text(session: "LiveSession") -> str:
    """One cell: what is known about this session's tokensave server."""
    served = os.path.basename(session.server_project.rstrip("\\/")) \
        or session.server_project
    if session.state == SERVING:
        return "✓ serving this folder"
    if session.state == OTHER_FOLDER:
        return "⚠ serving %s%s" % (
            served, " (this folder is inside it)" if session.detail else "")
    if session.state == NO_SERVER:
        return "✗ no tokensave server"
    if session.state == MULTIPLE:
        return "? %d servers" % len(session.server_pids)
    return "? %s" % (session.detail or "unconfirmed")


def running_session_ids() -> "frozenset | None":
    """Ids of the sessions running now, or None when that could not be read.

    None, not an empty set: the history list marks rows from this, and "could
    not look" must leave its marks alone rather than clear them as "none".
    """
    try:
        rows = enumerate_processes()
    except EnumerationFailed:
        return None
    ids = set()
    for row in rows:
        parsed = parse_session_command(row["cmd"]) if row["name"] == "claude.exe" else None
        if parsed and parsed[0]:
            ids.add(parsed[0])
    return frozenset(ids)


# -- the session's folder and title (IO) ----------------------------------

def locate_transcript(session_id: str) -> str:
    """The transcript path for *session_id*, or ""."""
    if not session_id:
        return ""
    hits = glob.glob(os.path.join(_claude_projects_dir(), "*", session_id + ".jsonl"))
    return hits[0] if hits else ""


def _tail_lines(path: str, tail_bytes: int) -> list:
    """The last *tail_bytes* of *path* as lines, newest FIRST. [] if unreadable.

    The first line of a tail read may be cut mid-record; every caller parses
    defensively for that reason.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return []
    return list(reversed(chunk.decode("utf-8", "replace").splitlines()))


def _latest(path: str, key: str, tail_bytes: int) -> str:
    """The newest non-empty string value of top-level *key* in the tail."""
    needle = '"%s"' % key
    for line in _tail_lines(path, tail_bytes):
        if needle not in line:
            continue
        try:
            value = json.loads(line).get(key)
        except ValueError:
            continue
        if value:
            return str(value)
    return ""


def read_last_cwd(path: str, tail_bytes: int = _TAIL_BYTES) -> str:
    """The most recent `cwd` recorded in *path*, reading only its tail. "" if none.

    Not the first one: a session can move, and the one measured that had moved
    through 127 folders disagreed with its own process under a head read.
    """
    return _latest(path, "cwd", tail_bytes)


def read_title(path: str) -> str:
    """The title the desktop app shows for this session, or "".

    `customTitle` (a `custom-title` entry) is what the sidebar shows and is the
    newest rename; `aiTitle` is the generated one. The head-of-file title the
    history list reads is only the last resort: in these transcripts it was
    present for 1 of 8, which is why that list is mostly "(untitled)".
    """
    for key in ("customTitle", "aiTitle"):
        found = _latest(path, key, _TITLE_TAIL_BYTES)
        if found:
            return found
    return _extract_session_meta(path)[1] or ""


# -- orchestration ---------------------------------------------------------

def collect(tokensave_exe: str, known_projects: list, *,
            list_rows: Callable = enumerate_processes,
            list_servers: Callable = list_tokensave_servers,
            find_transcript: Callable = locate_transcript,
            last_cwd: Callable = read_last_cwd,
            title_of: Callable = read_title) -> LiveSnapshot:
    """The running sessions, each with its tokensave server's state."""
    try:
        rows = list_rows()
    except EnumerationFailed as exc:
        return LiveSnapshot(failed=str(exc) or "process scan failed")

    by_pid, servers_failed = {}, ""
    try:
        by_pid = {s.pid: s for s in list_servers(tokensave_exe, known_projects,
                                                 strict=True)}
    except EnumerationFailed as exc:
        servers_failed = str(exc) or "server scan failed"

    sessions, claimed = [], set()
    for row in rows:
        parsed = parse_session_command(row["cmd"]) if row["name"] == "claude.exe" else None
        if parsed is None:
            continue
        sid, model, effort = parsed
        below = [r for r in descendants(rows, row["pid"]) if r["name"] == "tokensave.exe"]
        claimed.update(r["pid"] for r in below)
        transcript = find_transcript(sid)
        project = last_cwd(transcript) if transcript else ""
        state, pids, served, detail = classify_server(project, below, by_pid)
        if servers_failed and state != NO_SERVER:
            state, detail = UNVERIFIED, "the tokensave server list could not be read"
        sessions.append(LiveSession(
            pid=row["pid"], started_at=row["started_at"], session_id=sid,
            model=model, effort=effort, project=project,
            title=title_of(transcript) if transcript else "", state=state,
            server_pids=pids, server_project=served, detail=detail))

    strays = tuple(sorted(
        (StrayServer(s.pid, s.started_at, s.project or "", s.attribution)
         for pid, s in by_pid.items() if pid not in claimed),
        key=lambda s: s.started_at))
    sessions.sort(key=lambda s: s.started_at)
    return LiveSnapshot(sessions=tuple(sessions), strays=strays,
                        servers_failed=servers_failed)
