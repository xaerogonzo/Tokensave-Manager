"""Search several indexed projects at once, from the manager.

tokensave v7.10.0 can federate a query across roots, but only over MCP: the
CLI answers ``graph_root and graph_branch are available only through MCP; use
--project <path>``. The manager talks to tokensave through the CLI, so it fans
out one ``tokensave tool search --project <path>`` per project and merges the
results here.

**Merging is by RANK, never by score.** This is the one thing that is easy to
get wrong and produces a confidently-wrong answer when you do. BM25 scores are
computed per database and are not calibrated between them — a query against
two real projects here scored 19.3 in one and 11.2 in the other for equally
good matches. Sorting the union by score would front-load whichever project
happens to score higher and read as "that project is more relevant", which is
not a fact the numbers support. Round-robin by each hit's rank *within its own
project* compares like with like, and is what tokensave's own federation does
for the same reason.

Pure module — stdlib only, no Tkinter. The subprocess runner is injectable so
the merge logic is testable without spawning anything.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from constants import CREATE_NO_WINDOW

# Per-project cap, mirroring tokensave's own federation. Without it one large
# project can crowd out every other, which defeats the point of asking several.
PER_PROJECT_CAP = 25


@dataclass(frozen=True)
class Hit:
    """One search result, tagged with the project it came from."""

    project:      str          # display name (basename)
    project_path: str
    name:         str
    kind:         str
    file:         str          # repo-relative, forward slashes
    line:         int
    signature:    str
    rank:         int          # 0-based position within its OWN project

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


def parse_search_output(text: str, project_path: str) -> "list[Hit]":
    """Turn one project's ``tokensave tool search`` JSON into Hits.

    Returns [] for anything unparseable: one project with a broken index must
    not take down a search across five.
    """
    try:
        data = json.loads(text or "")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    name = os.path.basename(project_path.rstrip("/\\")) or project_path
    hits = []
    for rank, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("name") or "").strip()
        if not sym:
            continue
        try:
            line = int(row.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        hits.append(Hit(
            project=name,
            project_path=project_path,
            name=sym,
            kind=str(row.get("kind") or ""),
            file=str(row.get("file") or "").replace("\\", "/"),
            line=line,
            signature=str(row.get("signature") or ""),
            rank=rank,
        ))
    return hits


def interleave(per_project: "dict[str, list[Hit]]",
               cap: int = PER_PROJECT_CAP) -> "list[Hit]":
    """Round-robin the per-project lists by rank.

    Takes each project's best hit, then each project's second, and so on —
    so position in the merged list reflects "how good is this within its own
    project", the only comparison the scores actually support.

    Project order is the caller's dict order, which is the order the user
    selected them in; ties at the same rank are broken by that rather than by
    an incomparable score.
    """
    lists = [hits[:cap] for hits in per_project.values() if hits]
    if not lists:
        return []
    out: list[Hit] = []
    for depth in range(max(len(l) for l in lists)):
        for lst in lists:
            if depth < len(lst):
                out.append(lst[depth])
    return out


def _run_search(exe: str, project_path: str, query: str,
                limit: int) -> "tuple[str, str]":
    """Run one project's search. Returns (stdout, error_message)."""
    if not exe:
        return "", "no tokensave executable configured"
    cmd = [exe, "tool", "search", query, "--project", project_path,
           "--limit", str(limit)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, FileNotFoundError) as exc:
        return "", str(exc)
    except subprocess.TimeoutExpired:
        return "", "timed out after 60s"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "", (detail[-1][:160] if detail else
                    f"exit {proc.returncode}")
    return proc.stdout or "", ""


def search_projects(exe: str, project_paths: "list[str]", query: str,
                    limit: int = PER_PROJECT_CAP,
                    runner=None) -> "tuple[list[Hit], list[tuple[str, str]]]":
    """Search every project and merge.

    Returns ``(hits, failures)`` where *failures* is ``[(project, reason)]``.
    Failures are returned rather than raised, and rather than silently
    dropped: "no results in that project" and "that project could not be
    searched" are different answers, and collapsing them would let a broken
    index masquerade as a clean miss.

    *runner* is injectable for tests: ``runner(exe, path, query, limit)``
    returning ``(stdout, error)``.
    """
    run = runner or _run_search
    query = (query or "").strip()
    if not query:
        return [], []

    per_project: dict[str, list[Hit]] = {}
    failures: list[tuple[str, str]] = []
    for path in project_paths:
        name = os.path.basename(str(path).rstrip("/\\")) or str(path)
        out, err = run(exe, path, query, limit)
        if err:
            failures.append((name, err))
            continue
        per_project[path] = parse_search_output(out, path)
    return interleave(per_project, limit), failures


def format_hit_line(hit: Hit) -> str:
    """One rendered row: project first, because that is the new information."""
    kind = f" [{hit.kind}]" if hit.kind else ""
    return f"{hit.project}  ·  {hit.name}{kind}  —  {hit.location}"
