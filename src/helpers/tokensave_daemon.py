"""tokensave_daemon — find running ``tokensave serve`` processes, and say
honestly how confident we are about which project each one serves.

## Why this was harder than the CodeGraph equivalent — and mostly is not now

``codegraph daemon`` prints its own table, project path included, so
``codegraph_daemon.py`` only has to parse it. ``tokensave serve`` printed
nothing and most instances carried no project argument, which was filed
upstream as tokensave #421. Measured on one developer machine at the time:
**eight** servers running, two with ``-p "<path>"`` and six bare.

That gap was not academic. A server pinned to the wrong project answers
queries about a codebase you are not in, with a plausible-looking result and
no warning — which is exactly what happened while planning this module.

**tokensave 7.11.0 closed it.** Every ``serve`` now writes
``~/.tokensave/servers/<pid>.json`` once its database is open — recording
``pid``, ``started_at``, ``project_path``, ``argv_path``, ``db_path`` and
``version`` — and ``tokensave servers [--json]`` lists them. So the reverse
lookup the ``-shm`` correlation below was invented to approximate is now a
direct read, and a bare server is identifiable for the first time.

The heuristic stays anyway, for one reason: a server started by an **older
tokensave** is still running and still holds its lock, and it writes no
registry entry. Registry first, ``-shm`` second, and the two never blur —
``source`` records which one answered.

## The attribution contract

Every server carries one of four states, and the UI must not blur them:

===============  ==========================================================
authoritative    The server registry named this PID's project (7.11+), or the
                 process declares ``-p <path>``. Directly stoppable.
heuristic        The ``-shm`` correlation below matched exactly one project.
                 A guess with good evidence. Must be labelled as a guess and
                 must not be stopped without the user confirming the project.
unattributed     No match. Never stoppable — we do not know what it serves.
ambiguous        More than one candidate. Never stoppable, for the same
                 reason, only worse.
===============  ==========================================================

``attribution`` is **derived**, never assigned independently: it is computed
from ``source`` in one place, :func:`_attribution_for`. Two fields that can be
set separately are two fields that will eventually disagree, and the one that
gates a kill is the wrong one to let drift.

===============  ==========================================================
``source``       how the project was learned
===============  ==========================================================
live_registry    ``tokensave servers --json`` — the running binary answered
registry_file    a ``~/.tokensave/servers/<pid>.json`` we read ourselves,
                 **after** its ``started_at`` matched the live process
declared         ``-p`` on the command line
shm_heuristic    the ``-shm`` mtime correlation
none             nothing identified it
===============  ==========================================================

### Why a registry *file* is not a registry

``tokensave servers`` reaps entries for dead PIDs as it lists them; reading the
directory ourselves skips that reaping, so a leftover file will happily name a
project for a PID that died and was reused. Upstream records ``started_at`` as
the **OS-reported process start time** precisely so this is detectable: a live
process at that PID whose start time disagrees is a different process. So a
file-sourced entry is validated against the enumerated process before it is
believed, and **discarded** when it fails — not quietly downgraded to a guess,
because a stale record is not weak evidence, it is evidence about something
else.

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
import shutil
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

# How a project was learned. `attribution` is derived from this, never set
# beside it -- see `_attribution_for`.
SOURCE_LIVE_REGISTRY = "live_registry"
SOURCE_REGISTRY_FILE = "registry_file"
SOURCE_DECLARED = "declared"
SOURCE_SHM = "shm_heuristic"
SOURCE_NONE = "none"

#: Sources that name a project we can act on without asking the user first.
_AUTHORITATIVE_SOURCES = frozenset(
    (SOURCE_LIVE_REGISTRY, SOURCE_REGISTRY_FILE, SOURCE_DECLARED))


def _attribution_for(source: str) -> str:
    """The one place a source becomes a confidence level.

    Called on every path that sets a project, so `attribution` cannot be set
    to something `source` does not justify. `AMBIGUOUS` is not produced here:
    it describes a *count* of candidates rather than the quality of one, and
    the `-shm` path assigns it directly.
    """
    if source in _AUTHORITATIVE_SOURCES:
        return AUTHORITATIVE
    if source == SOURCE_SHM:
        return HEURISTIC
    return UNATTRIBUTED

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
    #: How `project` was learned. Always set alongside it — see
    #: `_attribution_for`, which is what turns this into `attribution`.
    source: str = SOURCE_NONE
    #: The index this server holds open, when the registry reported one. This
    #: is the field that answers "what is locking this directory", and it is
    #: not derivable from `project`: a per-branch database does not live at a
    #: fixed path under the project root.
    db_path: "str | None" = None
    #: tokensave's own version string, when the registry reported one.
    version: str = ""
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


# -- the tokensave server registry (tokensave 7.11+, upstream #421) --------

#: Tolerance between a registry entry's `started_at` and the process start
#: time we enumerated. Both are OS-reported process start times for the same
#: PID, so they should be identical; this only absorbs the second-vs-float
#: granularity difference between the two readers, and is deliberately far
#: tighter than `_RECORD_MAX_SKEW_S` — that one spans a *write* after a spawn,
#: this one compares the same quantity twice.
_REGISTRY_MAX_SKEW_S = 2.0

_REGISTRY_TIMEOUT = 15


def _registry_dir() -> str:
    return os.path.join(os.environ.get("USERPROFILE", "") or
                        os.path.expanduser("~"), ".tokensave", "servers")


def read_server_registry(tokensave_exe: str = "") -> dict:
    """Every registry entry tokensave will admit to, keyed by PID.

    Two sources, and the difference matters. `tokensave servers --json` reaps
    entries whose process is gone as it lists them, so what it returns is
    live. Reading the directory ourselves does not reap, so a leftover file
    for a recycled PID reads exactly like a live one — every entry from that
    path is tagged `registry_file` and must be validated against the process
    before it is believed.

    Each value gains a `"_source"` key saying which path produced it. Nothing
    else in the dict is ours; the rest is upstream's shape, untouched.

    Fail-open, like everything else here.
    """
    entries = _registry_via_cli(tokensave_exe) if tokensave_exe else None
    if entries is not None:
        return entries
    return _registry_via_files()


def _registry_via_cli(tokensave_exe: str) -> "dict | None":
    """Ask the binary. None — not {} — when it could not answer at all.

    The distinction is load-bearing: an empty registry (`[]`, no servers
    running) is an answer, and must not send us to the unreaped files. Only a
    missing binary, an old one with no `servers` subcommand, or a crash is a
    non-answer.
    """
    if not os.path.isfile(tokensave_exe):
        return None
    try:
        proc = subprocess.run(
            [tokensave_exe, "servers", "--json"],
            capture_output=True, text=True, timeout=_REGISTRY_TIMEOUT,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None                       # older tokensave: no such subcommand
    try:
        data = json.loads(proc.stdout or "[]")
    except (ValueError, UnicodeDecodeError):
        return None
    return _index_registry(data, SOURCE_LIVE_REGISTRY)


def _registry_via_files() -> dict:
    """The same records, read straight off disk and therefore unreaped."""
    entries = []
    try:
        names = os.listdir(_registry_dir())
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_registry_dir(), name),
                      encoding="utf-8") as fh:
                entries.append(json.load(fh))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return _index_registry(entries, SOURCE_REGISTRY_FILE)


def _index_registry(data, source: str) -> dict:
    """Key a list of registry records by PID, dropping anything unusable."""
    out: dict = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        record = dict(item)
        record["_source"] = source
        out[pid] = record
    return out


def _registry_entry_for(server, registry: dict) -> "dict | None":
    """The entry describing *server*, or None if it cannot be trusted.

    A `live_registry` entry is taken as given: the binary that owns the
    registry already reaped the dead ones, and second-guessing it would just
    reintroduce our own inference on top of an authoritative answer.

    A `registry_file` entry is checked against the process we enumerated,
    because nothing reaped it. Upstream records the OS-reported **process**
    start time, so this is a real identity check rather than a freshness
    guess: disagreement means PID reuse, and the entry is dropped.
    """
    entry = registry.get(server.pid)
    if not entry:
        return None
    if entry.get("_source") == SOURCE_LIVE_REGISTRY:
        return entry
    try:
        started = float(entry.get("started_at") or 0)
    except (TypeError, ValueError):
        return None
    if not started or not server.started_at:
        return None                # cannot verify, so do not claim
    if abs(started - server.started_at) > _REGISTRY_MAX_SKEW_S:
        return None                # a dead server's record wearing a live PID
    return entry


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
    registry = read_server_registry(tokensave_exe)
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
                             tolerance_s=tolerance_s, registry=registry)


def attribute_servers(servers: "list[TokensaveServer]",
                      known_projects: list,
                      *, tolerance_s: float = DEFAULT_TOLERANCE_S,
                      registry: "dict | None" = None
                      ) -> "list[TokensaveServer]":
    """Assign each server an attribution state. Pure — no process access.

    Split out from :func:`list_tokensave_servers` so the interesting logic can
    be tested against constructed inputs rather than whatever happens to be
    running on the machine.

    *registry* is the already-read output of :func:`read_server_registry`. It
    is passed in rather than read here on purpose: reading it is IO, and the
    validation this function performs on a file-sourced entry compares the
    entry's `started_at` against the `started_at` already captured on the
    server, so it needs no process access of its own. That keeps the whole of
    the interesting logic — including PID-reuse rejection — testable from
    constructed inputs.
    """
    projects = [p for p in (known_projects or []) if p]
    shm_times = {p: _shm_mtime(p) for p in projects}
    shm_times = {p: t for p, t in shm_times.items() if t is not None}
    records = read_wrapper_records()
    registry = registry or {}

    # Pass 1 — the authoritative ones, which also stake a claim on a project.
    #
    # The registry is consulted before `-p` because it is strictly better
    # evidence: it is what the server itself resolved, whereas `-p` is what it
    # was asked for. `argv_path` in the registry is often a relative "." that
    # only means anything from the server's own working directory, which is
    # exactly the ambiguity `project_path` resolves.
    out, claimed = [], {}
    for srv in servers:
        entry = _registry_entry_for(srv, registry)
        listed = str((entry or {}).get("project_path") or "")
        if listed:
            resolved = _match_known(listed, projects) or listed
            known = _match_known(listed, projects) is not None
            source = entry.get("_source", SOURCE_LIVE_REGISTRY)
            if known:
                claimed.setdefault(resolved, []).append(srv.pid)
            out.append(_with(
                srv, project=resolved, source=source,
                attribution=_attribution_for(source),
                db_path=str(entry.get("db_path") or "") or None,
                version=str(entry.get("version") or ""),
                detail=("named by the tokensave server registry"
                        if known else
                        "named by the tokensave server registry "
                        "(not a known project)")))
            continue

        declared = _declared_project(srv.command_line)
        if declared and _match_known(declared, projects):
            resolved = _match_known(declared, projects)
            claimed.setdefault(resolved, []).append(srv.pid)
            out.append(_with(srv, project=resolved, source=SOURCE_DECLARED,
                             attribution=_attribution_for(SOURCE_DECLARED),
                             detail="declared with -p on the command line"))
        elif declared:
            # It named a project we do not know about. Still not a guess —
            # but not a project the manager can act on either.
            out.append(_with(srv, project=declared, source=SOURCE_DECLARED,
                             attribution=_attribution_for(SOURCE_DECLARED),
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
    return _with(srv, project=project, source=SOURCE_SHM,
                 attribution=_attribution_for(SOURCE_SHM),
                 candidates=(project,),
                 detail="database-open timestamp matches this process's start "
                        "time; no other server matches it. This tokensave is "
                        "older than 7.11 or did not register itself")


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

class EnumerationFailed(RuntimeError):
    """Process enumeration could not run at all — distinct from "found none".

    The difference is the whole point. Collapsing both into ``[]`` is what let
    a manager whose PATH lacked the PowerShell directory report "no Desktop
    tokensave server is running" while two were, and "could not determine" for
    a question it had simply failed to ask. Callers that must not guess pass
    ``strict=True`` and catch this.
    """


def _powershell_exe() -> str:
    """Resolve PowerShell, without trusting PATH.

    Measured 2026-08-26: the manager, launched as a windowless ``pythonw.exe``,
    ran with a PATH that did not contain
    ``System32\\WindowsPowerShell\\v1.0``. A bare ``"powershell"`` then fails
    ``CreateProcess`` with WinError 2, the ``OSError`` was swallowed, and every
    enumeration silently returned nothing — so the Daemon Manager showed no
    servers and the Desktop-retirement gate could never be satisfied.

    Same lesson ``effective_scope`` already records for ``claude``: what a
    shell resolves and what ``CreateProcess`` resolves are not the same set.
    Falls back to the absolute System32 location, which is present on every
    supported Windows, and prefers ``pwsh`` when the machine has it.
    """
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    fallback = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32",
        "WindowsPowerShell", "v1.0", "powershell.exe")
    return fallback if os.path.isfile(fallback) else ""


def _enumerate_processes(name_like: str = "tokensave",
                         *, strict: bool = False) -> list:
    """Running processes whose image name contains *name_like*.

    The filter is a parameter rather than a constant because
    :mod:`helpers.mcp_desktop` needs the identical shape for ``claude.exe`` —
    it has to tell Claude Desktop from Claude Code, and only the executable
    path separates them. Duplicating the CIM plumbing to ask the same question
    about a different image name would mean two places to get the
    ``CreationDate`` conversion below wrong.

    ``strict`` raises :class:`EnumerationFailed` instead of returning ``[]``
    when the enumeration itself could not run. Default stays fail-open, which
    is right for a listing; it is wrong for a gate.
    """
    if sys.platform == "win32":
        return _enumerate_windows(name_like, strict=strict)
    return _enumerate_posix(name_like)


def _enumerate_windows(name_like: str = "tokensave",
                       *, strict: bool = False) -> list:
    """PowerShell/CIM, because a command line is what we need.

    The toolhelp snapshot API is cheaper but cannot return a command line,
    and the command line is the only authoritative attribution signal there
    is.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE '%%%s%%'\" | "
        % name_like.replace("'", "").replace('"', "") +
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
    exe = _powershell_exe()
    if not exe:
        if strict:
            raise EnumerationFailed("PowerShell could not be located")
        return []
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_PROC_TIMEOUT,
            # stdin is detached rather than inherited: the manager runs as a
            # windowless pythonw.exe, where an inherited handle is not a
            # usable one (docs/MCP_INTEGRATION_GOTCHAS.md, attempt 5).
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise EnumerationFailed("%s: %s" % (type(exc).__name__, exc))
        return []
    if strict and proc.returncode != 0:
        raise EnumerationFailed(
            (proc.stderr or "").strip()[:200] or
            "PowerShell exited %s" % proc.returncode)
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


def _enumerate_posix(name_like: str = "tokensave") -> list:
    """Every process whose image basename contains *name_like*.

    The Windows branch pushes this filter down into the CIM query; here it is
    applied after the fact, because /proc has to be walked either way. Callers
    still narrow further (``_is_tokensave`` verifies the binary), so this is a
    cheap pre-filter, not identification.

    Matched against the WHOLE path, not the basename. ``_is_tokensave`` accepts
    a configured binary by full-path equality regardless of what it is called,
    so a basename filter could drop a process the caller would have kept —
    a filter that is meant to be cheap must not also be narrowing.
    """
    out = []
    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []
    boot = _boot_time()
    needle = (name_like or "").lower()
    for name in pids:
        pid = int(name)
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
            exe = os.readlink("/proc/%s/exe" % name)
        except OSError:
            continue
        if needle and needle not in exe.lower():
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
