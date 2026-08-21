"""tokensave_daemon — find running ``tokensave serve`` processes, and say
honestly how confident we are about which project each one serves.

## Why this is harder than the CodeGraph equivalent

``codegraph daemon`` prints its own table, project path included, so
``codegraph_daemon.py`` only has to parse it. ``tokensave serve`` prints
nothing and most instances carry no project argument, which is filed upstream
as tokensave #421. Measured on one developer machine: **eight** servers
running, two with ``-p "<path>"`` and six bare.

That gap is not academic. A server pinned to the wrong project answers
queries about a codebase you are not in, with a plausible-looking result and
no warning — which is exactly what happened while planning this module.

## The attribution contract

Every server carries one of four states, and the UI must not blur them:

===============  ==========================================================
authoritative    Verified tokensave binary AND ``-p <path>`` resolving to a
                 known project. Directly stoppable.
heuristic        The ``-shm`` correlation below matched exactly one project.
                 A guess with good evidence. Must be labelled as a guess and
                 must not be stopped without the user confirming the project.
unattributed     No match. Never stoppable — we do not know what it serves.
ambiguous        More than one candidate. Never stoppable, for the same
                 reason, only worse.
===============  ==========================================================

## The ``-shm`` heuristic, and precisely where it breaks

SQLite creates ``tokensave.db-shm`` when it opens the database in WAL mode,
so the file's mtime coincides with the moment a server attached to it.
Measured while planning:

* this project's ``-shm`` read 1:46:59 PM; PID 25984 started 1:46:59 PM
* another project's read 8/19 3:23:47 PM; PID 44224 started 8/19 3:23:47 PM
  — and *that* PID independently carried ``-p``, so the labelled processes
  double as free ground truth for validating the heuristic

Also measured: the ``-shm`` mtime is stamped at open and is **not** bumped by
later writes (its ``.db`` and ``-wal`` siblings were 26 s newer at the time).
That stability is what makes it usable as an identity at all.

It is still only a heuristic, and it has a known-broken case that follows
directly from "created for the FIRST connection":

    server A opens project X at T0   ->  -shm mtime = T0
    server A stays alive
    server B opens project X at T1   ->  -shm still reads T0
    => B matches nothing, and is reported `unattributed`

The converse is worse: a bare server that happens to start near an unrelated
project's ``-shm`` creation looks like a match. So a lone candidate is only
ever ``heuristic``, never ``authoritative`` — and identity revalidation at
kill time does not change that. Those are independent guarantees:

    Process identity validation prevents PID reuse. It does not upgrade
    heuristic project attribution into authoritative attribution.

## Respawning

Claude Code restarts its MCP servers, so PIDs churn: between two checks
minutes apart during the incident that prompted this module, several PIDs
vanished and others appeared. "Stop all" therefore does not converge and
would churn the user's live sessions. Stop the one identified holder.

No Tk here — plain data out, so the dialog stays a presentation layer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from helpers.proc_kill import ProcessIdentity, kill_process, process_identity

try:
    from constants import CREATE_NO_WINDOW
except ImportError:                                     # standalone / test use
    CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Attribution states. Strings rather than an enum so they survive the JSON
# round-trip in tests and logs without ceremony.
AUTHORITATIVE = "authoritative"
HEURISTIC = "heuristic"
UNATTRIBUTED = "unattributed"
AMBIGUOUS = "ambiguous"

#: Seconds of slack between a process start and an ``-shm`` mtime. The
#: measured pairs agreed to the second; this allows for clock granularity and
#: filesystem timestamp rounding without opening the window wide enough to
#: sweep in an unrelated project.
DEFAULT_TOLERANCE_S = 2.0

_PROC_TIMEOUT = 20
_SHM_RELPATH = os.path.join(".tokensave", "tokensave.db-shm")

# `-p <path>` / `-p "<path>"` / `--project <path>` anywhere in the command line.
_PROJECT_ARG_RE = re.compile(
    r'(?:^|\s)(?:-p|--project)[=\s]+(?:"([^"]+)"|(\S+))')

_TOKENSAVE_IMAGES = {"tokensave", "tokensave.exe"}


@dataclass(frozen=True)
class TokensaveServer:
    """One running ``tokensave serve``, with its attribution and evidence."""
    pid: int
    command_line: str
    started_at: float                      # epoch seconds
    image: str = ""
    project: "str | None" = None
    attribution: str = UNATTRIBUTED
    detail: str = ""
    identity: "ProcessIdentity | None" = None
    candidates: tuple = field(default_factory=tuple)
    #: How the manager's wrapper chose this project, when it spawned the
    #: server: "pin", "most-recent-index", "none", or "" for servers the
    #: wrapper did not start (a Claude Code session, say).
    selection: str = ""

    @property
    def can_stop(self) -> bool:
        """Whether a Stop control may be offered at all.

        ``unattributed`` and ``ambiguous`` are excluded on purpose: stopping a
        server whose project we cannot name means possibly killing the one
        serving a colleague's live session instead of the one holding the lock.
        """
        return self.attribution in (AUTHORITATIVE, HEURISTIC)

    @property
    def needs_confirmation(self) -> bool:
        """A heuristic attribution must be confirmed against the named project."""
        return self.attribution == HEURISTIC

    @property
    def is_guess(self) -> bool:
        return self.attribution in (HEURISTIC, AMBIGUOUS)


# -- wrapper run records (Roadmap-10 phase B) ------------------------------

#: A record is only trusted when its timestamp is close to the process's own
#: start time. PIDs are reused, and a stale record would otherwise explain a
#: brand-new server with a dead one's reason.
_RECORD_MAX_SKEW_S = 60.0


def _records_dir() -> str:
    return os.path.join(os.environ.get("USERPROFILE", ""),
                        ".tokensave", "wrapper-runs")


def read_wrapper_records() -> dict:
    """Every readable run record, keyed by the tokensave PID it describes.

    The wrapper writes one immediately after spawning. It is the only source
    that knows *why* a project was chosen: a pin, or the most-recently-indexed
    fallback, which is a moving target when several projects are active.

    Fail-open like everything else here -- a missing or corrupt record means
    "no extra information", never an error.
    """
    out: dict = {}
    try:
        names = os.listdir(_records_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_records_dir(), name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            continue
        out[pid] = data
    return out


def _record_for(server, records: dict) -> "dict | None":
    """The record describing *server*, or None if there is none we can trust."""
    record = records.get(server.pid)
    if not record:
        return None
    try:
        written = float(record.get("written_at") or 0)
    except (TypeError, ValueError):
        return None
    if not written or not server.started_at:
        return None
    if abs(written - server.started_at) > _RECORD_MAX_SKEW_S:
        return None            # a dead server's record wearing a reused PID
    return record


#: Human-readable explanation per selection reason.
_SELECTION_DETAIL = {
    "pin": "chosen by the pinned project",
    "most-recent-index": ("chosen as the most recently indexed project -- "
                          "nothing was pinned, so this moves when another "
                          "project syncs"),
    "none": ("the wrapper found no project to serve, so this server was "
             "started without one"),
}


# ── discovery ─────────────────────────────────────────────────────────────

def list_tokensave_servers(tokensave_exe: str = "",
                           known_projects: "list | None" = None,
                           *, tolerance_s: float = DEFAULT_TOLERANCE_S
                           ) -> "list[TokensaveServer]":
    """Every running tokensave server, attributed as well as honestly possible.

    *tokensave_exe* is the configured binary. When given, a process must be
    that executable to be treated as one of ours — "the image is named
    tokensave.exe" is not identification, and an unrelated binary with that
    name must not be offered a Stop button.

    Fail-open: any enumeration problem yields ``[]`` rather than raising,
    matching the convention in ``codegraph_daemon`` and
    ``codegraph_freshness``.
    """
    procs = _enumerate_processes()
    if not procs:
        return []
    expected = _normalise_path(tokensave_exe) if tokensave_exe else ""
    servers = []
    for proc in procs:
        if not _is_tokensave(proc, expected):
            continue
        servers.append(TokensaveServer(
            pid=proc["pid"],
            command_line=proc.get("cmdline", ""),
            started_at=proc.get("started_at", 0.0),
            image=os.path.basename(proc.get("exe", "")),
            identity=process_identity(proc["pid"]),
        ))
    return attribute_servers(servers, known_projects or [],
                             tolerance_s=tolerance_s)


def attribute_servers(servers: "list[TokensaveServer]",
                      known_projects: list,
                      *, tolerance_s: float = DEFAULT_TOLERANCE_S
                      ) -> "list[TokensaveServer]":
    """Assign each server an attribution state. Pure — no process access.

    Split out from :func:`list_tokensave_servers` so the interesting logic can
    be tested against constructed inputs rather than whatever happens to be
    running on the machine.
    """
    projects = [p for p in (known_projects or []) if p]
    shm_times = {p: _shm_mtime(p) for p in projects}
    shm_times = {p: t for p, t in shm_times.items() if t is not None}
    records = read_wrapper_records()

    # Pass 1 — the authoritative ones, which also stake a claim on a project.
    out, claimed = [], {}
    for srv in servers:
        declared = _declared_project(srv.command_line)
        if declared and _match_known(declared, projects):
            resolved = _match_known(declared, projects)
            claimed.setdefault(resolved, []).append(srv.pid)
            out.append(_with(srv, project=resolved, attribution=AUTHORITATIVE,
                             detail="declared with -p on the command line"))
        elif declared:
            # It named a project we do not know about. Still not a guess —
            # but not a project the manager can act on either.
            out.append(_with(srv, project=declared, attribution=AUTHORITATIVE,
                             detail="declared with -p (not a known project)"))
        else:
            out.append(srv)

    # Pass 2 — the -shm correlation for whatever is left.
    resolved = []
    for srv in out:
        if srv.attribution == AUTHORITATIVE:
            resolved.append(srv)
            continue
        matches = [p for p, t in shm_times.items()
                   if abs(t - srv.started_at) <= tolerance_s]
        resolved.append(_attribute_by_shm(srv, matches, claimed, servers,
                                          shm_times, tolerance_s))
    return [_with_selection(srv, records) for srv in resolved]


def _with_selection(server, records: dict):
    """Attach the wrapper's stated reason, where one applies.

    Deliberately additive: this never changes an attribution. The wrapper
    already passes `-p`, so its servers were authoritative before this
    existed -- what was missing was never *which* project, but *why* that one.
    """
    record = _record_for(server, records)
    if record is None:
        return server
    reason = str(record.get("reason") or "")
    detail = _SELECTION_DETAIL.get(reason)
    if not detail:
        return _with(server, selection=reason)
    joined = "%s; %s" % (server.detail, detail) if server.detail else detail
    return _with(server, selection=reason, detail=joined)


def _attribute_by_shm(srv, matches, claimed, all_servers, shm_times,
                      tolerance_s):
    """Turn a candidate list into one of the three non-authoritative states."""
    if not matches:
        return _with(srv, attribution=UNATTRIBUTED, detail=(
            "no project database was opened at this process's start time. A "
            "server that attached to an already-open database does not "
            "restamp it, so this is expected for a second server on one "
            "project"))
    if len(matches) > 1:
        return _with(srv, attribution=AMBIGUOUS, candidates=tuple(sorted(matches)),
                     detail="%d projects opened a database within %.0fs of this "
                            "process starting" % (len(matches), tolerance_s))

    project = matches[0]
    # One project matched — but if something else already claims it, or another
    # bare server matches it equally well, we cannot say which holds the lock.
    rivals = [pid for pid in claimed.get(project, []) if pid != srv.pid]
    peers = [s.pid for s in all_servers
             if s.pid != srv.pid
             and abs(shm_times[project] - s.started_at) <= tolerance_s]
    if rivals or peers:
        return _with(srv, attribution=AMBIGUOUS, candidates=(project,),
                     detail="%s also matches PID(s) %s" % (
                         os.path.basename(project) or project,
                         ", ".join(str(p) for p in sorted(set(rivals + peers)))))
    return _with(srv, project=project, attribution=HEURISTIC,
                 candidates=(project,),
                 detail="database-open timestamp matches this process's start "
                        "time; no other server matches it")


# ── stopping ──────────────────────────────────────────────────────────────

def stop_tokensave_server(server: TokensaveServer,
                          *, confirmed: bool = False) -> "tuple[bool, str]":
    """Terminate *server*, refusing anything the contract does not allow.

    Enforced here rather than only in the dialog: this is the function that
    ends a process, so it is the place a mistake actually costs something.
    """
    if not server.can_stop:
        return False, (
            "this server's project could not be identified (%s), so stopping "
            "it might kill the wrong one" % server.attribution)
    if server.needs_confirmation and not confirmed:
        return False, (
            "attribution to %s is a heuristic, not a fact — confirm the "
            "project before stopping this server"
            % (server.project or "an unknown project"))
    return kill_process(server.pid, tree=False, graceful=True,
                        expect=server.identity)


# ── process enumeration ───────────────────────────────────────────────────

def _enumerate_processes() -> list:
    if sys.platform == "win32":
        return _enumerate_windows()
    return _enumerate_posix()


def _enumerate_windows() -> list:
    """PowerShell/CIM, because a command line is what we need.

    The toolhelp snapshot API is cheaper but cannot return a command line,
    and the command line is the only authoritative attribution signal there
    is.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE '%tokensave%'\" | "
        "Select-Object ProcessId, ExecutablePath, CommandLine, "
        # DateTimeOffset applies the machine's UTC offset. Subtracting a
        # bare unix-epoch literal instead gives LOCAL midnight, so the result
        # is off by the whole UTC offset -- four hours on the machine this was
        # written on, which is 7200x the correlation tolerance. Every -shm
        # match then failed silently and every bare server came back
        # unattributed, with nothing raised. Milliseconds because the -shm
        # mtime has sub-second precision and matches turn on fractions.
        "@{n='Start';e={[DateTimeOffset]::new($_.CreationDate)"
        ".ToUnixTimeMilliseconds()/1000}} | ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_PROC_TIMEOUT,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_cim_json(proc.stdout or "")


def _parse_cim_json(raw: str) -> list:
    """ConvertTo-Json emits an object for one result and an array for many."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        try:
            started = float(row.get("Start") or 0)
        except (TypeError, ValueError):
            started = 0.0
        out.append({"pid": pid,
                    "exe": row.get("ExecutablePath") or "",
                    "cmdline": row.get("CommandLine") or "",
                    "started_at": started})
    return out


def _enumerate_posix() -> list:
    out = []
    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []
    boot = _boot_time()
    for name in pids:
        pid = int(name)
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
            exe = os.readlink("/proc/%s/exe" % name)
        except OSError:
            continue
        out.append({"pid": pid, "exe": exe, "cmdline": cmdline,
                    "started_at": _posix_start_time(name, boot)})
    return out


def _boot_time() -> float:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _posix_start_time(pid_name: str, boot: float) -> float:
    try:
        with open("/proc/%s/stat" % pid_name, encoding="utf-8",
                  errors="replace") as fh:
            fields = fh.read().rsplit(") ", 1)[-1].split()
        ticks = float(fields[19])
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return 0.0


# ── small helpers ─────────────────────────────────────────────────────────

def _is_tokensave(proc: dict, expected_exe: str) -> bool:
    """Is this one of ours? Name alone is not identification."""
    exe = proc.get("exe") or ""
    if expected_exe:
        return _normalise_path(exe) == expected_exe
    return os.path.basename(exe).casefold() in _TOKENSAVE_IMAGES


def _declared_project(command_line: str) -> "str | None":
    m = _PROJECT_ARG_RE.search(command_line or "")
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _match_known(declared: str, projects: list) -> "str | None":
    """Resolve a declared path against known projects, comparing normalised.

    Case, separators and trailing slashes all vary between how a project is
    stored in the manager's config and how it appears on a command line.
    """
    target = _normalise_path(declared)
    for project in projects:
        if _normalise_path(project) == target:
            return project
    return None


def _normalise_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except (OSError, ValueError):
        return os.path.normcase(path)


def _shm_mtime(project_root: str) -> "float | None":
    try:
        return os.path.getmtime(os.path.join(project_root, _SHM_RELPATH))
    except OSError:
        return None


def _with(server: TokensaveServer, **changes) -> TokensaveServer:
    """A copy with fields replaced — TokensaveServer is frozen."""
    data = {
        "pid": server.pid,
        "command_line": server.command_line,
        "started_at": server.started_at,
        "image": server.image,
        "project": server.project,
        "attribution": server.attribution,
        "detail": server.detail,
        "identity": server.identity,
        "candidates": server.candidates,
        "selection": server.selection,
    }
    data.update(changes)
    return TokensaveServer(**data)
