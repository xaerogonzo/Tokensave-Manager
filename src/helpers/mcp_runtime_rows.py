"""mcp_runtime_rows -- what MCP servers are running RIGHT NOW, as display rows.

The Tasks tab's "MCP servers" view. Pure: no Tk, and the two listers are
injectable so the logic is tested from constructed inputs, not from whatever
happens to be running on the machine.

Two rules from the posture work (D1b) govern this module:

* **Unknown is never "none running".** Each source reports its own status:
  ``ok``, ``failed`` (could not ask) or ``not_installed``. A source that
  failed contributes NO rows and says so; it is never rendered as an empty
  list under a green summary.
* **Never sum two sources into one verdict.** The summary names each source
  separately. tokensave and CodeGraph answer different questions and fail in
  different ways.

Servers belong to PROJECTS, not to sessions. Neither lister collects parent
process evidence, so nothing here claims "this session owns this server".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from helpers.codegraph_daemon import ListingFailed, list_codegraph_daemons
from helpers.tokensave_daemon import (
    AMBIGUOUS, AUTHORITATIVE, HEURISTIC, UNATTRIBUTED,
    EnumerationFailed, list_tokensave_servers,
)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_NOT_INSTALLED = "not_installed"

_ATTRIBUTION_LABEL = {
    AUTHORITATIVE: "confirmed",
    HEURISTIC: "guess",
    AMBIGUOUS: "ambiguous",
    UNATTRIBUTED: "unknown project",
}


@dataclass(frozen=True)
class RuntimeRow:
    """One running MCP server, ready to render."""
    server: str          # "tokensave" | "codegraph"
    project: str         # path, or "" when it could not be identified
    pid: int
    started: str         # already formatted; "" when unknown
    version: str
    attribution: str     # human label, see _ATTRIBUTION_LABEL
    is_guess: bool       # True -> render as a guess, not a fact


@dataclass(frozen=True)
class SourceStatus:
    name: str
    status: str          # STATUS_*
    count: int = 0
    detail: str = ""     # reason, for failed / not_installed


@dataclass(frozen=True)
class RuntimeSnapshot:
    rows: tuple
    sources: tuple       # of SourceStatus, one per source, never merged

    @property
    def summary(self) -> str:
        """One clause per source. Deliberately not a total."""
        return "  ·  ".join(_clause(s) for s in self.sources)


def _clause(s: SourceStatus) -> str:
    if s.status == STATUS_FAILED:
        return f"{s.name}: could not check ({s.detail})"
    if s.status == STATUS_NOT_INSTALLED:
        return f"{s.name}: not installed"
    guesses = f", {s.detail}" if s.detail else ""
    return f"{s.name}: {s.count} running{guesses}"


def _fmt_epoch(ts: float) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%d %b %H:%M", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def _tokensave_rows(servers) -> tuple:
    rows = []
    for s in servers:
        rows.append(RuntimeRow(
            server="tokensave",
            project=s.project or "",
            pid=s.pid,
            started=_fmt_epoch(s.started_at),
            version=s.version or "",
            attribution=_ATTRIBUTION_LABEL.get(s.attribution, s.attribution),
            is_guess=s.is_guess,
        ))
    return tuple(rows)


def _codegraph_rows(daemons) -> tuple:
    # CodeGraph prints the project path itself, so it is always a stated fact.
    return tuple(RuntimeRow(
        server="codegraph", project=d["path"], pid=d["pid"],
        started=f"up {d['uptime']}", version=d["version"],
        attribution="confirmed", is_guess=False,
    ) for d in daemons)


def collect(tokensave_exe: str, codegraph_exe: str, known_projects: list,
            *,
            list_ts: Callable = list_tokensave_servers,
            list_cg: Callable = list_codegraph_daemons) -> RuntimeSnapshot:
    """Ask both sources and return rows plus one honest status per source."""
    rows: list = []
    sources: list = []

    try:
        servers = list_ts(tokensave_exe, known_projects, strict=True)
    except EnumerationFailed as exc:
        sources.append(SourceStatus("tokensave", STATUS_FAILED,
                                    detail=str(exc) or "process scan failed"))
    else:
        ts_rows = _tokensave_rows(servers)
        rows.extend(ts_rows)
        unsure = sum(1 for r in ts_rows if r.attribution != "confirmed")
        sources.append(SourceStatus(
            "tokensave", STATUS_OK, count=len(ts_rows),
            detail=f"{unsure} not confirmed" if unsure else ""))

    if not codegraph_exe:
        sources.append(SourceStatus("codegraph", STATUS_NOT_INSTALLED))
    else:
        try:
            daemons = list_cg(codegraph_exe, strict=True)
        except ListingFailed as exc:
            sources.append(SourceStatus("codegraph", STATUS_FAILED,
                                        detail=str(exc) or "listing failed"))
        else:
            cg_rows = _codegraph_rows(daemons)
            rows.extend(cg_rows)
            sources.append(SourceStatus("codegraph", STATUS_OK,
                                        count=len(cg_rows)))

    rows.sort(key=lambda r: (r.project.lower() or "￿", r.server, r.pid))
    return RuntimeSnapshot(rows=tuple(rows), sources=tuple(sources))
