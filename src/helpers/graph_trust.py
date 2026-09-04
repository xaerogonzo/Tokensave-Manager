"""graph_trust — how much of tokensave's call graph can be believed.

tokensave's Python extractor resolves a call on an untracked receiver by
matching the bare method name against every indexed symbol. Where exactly one
symbol in the project carries that name, the call binds to it regardless of
which directory it lives in. Test doubles are named after the API they stand
in for — a fake toolkit widget defines ``after`` and ``winfo_width``, a fake
logger defines ``info`` — so production code binds into the test tree.

The resulting edges are impossible by construction: the test tree imports
production code, never the reverse. Everything derived from ``calls`` edges
inherits them — ``circular``, ``file_dependents``, ``impact``, ``callers``,
``dead_code``, the ``acyclicity`` health dimension, and through it the
``quality_signal`` aggregate.

This module counts them, so a consumer can say how much of the graph it is
willing to believe. It does not repair anything: the defect is upstream
(``docs/upstream-issues/tokensave-python-bare-name-fallback.md``) and the
local job is to stop reporting a number whose basis is known to be wrong.

**It reports the population it examined, not only its findings.** "No
impossible edges across 15388" and "no impossible edges across 0" are the
same empty result and completely different claims, which is why
``insufficient`` is a state of its own and zero examined edges can never
read as ``trustworthy``.

Pure: no Tk, no subprocess. Reads the index read-only via a ``mode=ro`` URI.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field

from helpers.test_discovery import is_test_path


# ── States ───────────────────────────────────────────────────────────────
#
# Four, not a boolean. The two that are easy to conflate are `unknown` (we
# could not look) and `insufficient` (we looked and there was nothing to
# see); collapsing either into `trustworthy` is how an unread graph becomes
# a clean bill of health.

STATE_UNKNOWN      = "unknown"        # no index, unreadable, unknown schema
STATE_INSUFFICIENT = "insufficient"   # examined, but population too small
STATE_TRUSTWORTHY  = "trustworthy"    # examined a real population, none found
STATE_TAINTED      = "tainted"        # impossible edges present

#: Below this many edges, a zero result says more about the index than the
#: code. A real project indexes thousands; anything under this is an empty,
#: failed or partial index. A judgement call, not a measurement — which is
#: why it is a parameter.
MIN_MEANINGFUL_EDGES = 50

#: Cap on the collision sample. This travels in a CLI envelope and possibly
#: to the extension; a pathological repository must not be able to put
#: thousands of names into it.
MAX_COLLISIONS = 20


@dataclass(frozen=True)
class Collision:
    """One name that production code binds to inside the test tree."""
    target_name: str
    target_file: str
    count: int

    def __str__(self) -> str:
        return f"{self.target_name} ({self.target_file}) x{self.count}"


@dataclass(frozen=True)
class GraphTrust:
    """What one inspection of one project's index found.

    ``detail`` carries the reason for ``unknown`` / ``insufficient``. It is
    empty for the two states that speak for themselves.
    """
    state: str
    detail: str = ""
    edges_examined: int = 0
    impossible_edges: int = 0
    source_files_affected: int = 0
    collisions: tuple = field(default_factory=tuple)
    db_path: str = ""

    @property
    def is_conclusive(self) -> bool:
        """True when the inspection actually reached a population."""
        return self.state in (STATE_TRUSTWORTHY, STATE_TAINTED)

    @property
    def is_tainted(self) -> bool:
        return self.state == STATE_TAINTED

    def summary(self) -> str:
        """One line, always naming the population it measured."""
        if self.state == STATE_UNKNOWN:
            return f"graph trust unknown — {self.detail}"
        if self.state == STATE_INSUFFICIENT:
            return f"graph trust inconclusive — {self.detail}"
        if self.state == STATE_TRUSTWORTHY:
            return (f"graph looks sound — no impossible edges "
                    f"across {self.edges_examined} examined")
        return (f"graph is contaminated — {self.impossible_edges} impossible "
                f"edge(s) from {self.source_files_affected} source file(s), "
                f"across {self.edges_examined} examined")


# ── Locating the index ───────────────────────────────────────────────────

def db_path_for(project_root: str) -> str:
    """Return the active tokensave DB for *project_root*, or "".

    Per-branch indexes are named in ``.tokensave/branch-meta.json``; older
    single-branch projects have only ``.tokensave/tokensave.db``. A
    ``db_file`` naming a file that is not there is treated as absent rather
    than trusted, because a stale pointer is not evidence about this branch.
    """
    ts = os.path.join(project_root, ".tokensave")
    meta = os.path.join(ts, "branch-meta.json")
    try:
        with open(meta, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        name = (raw or {}).get("db_file") or ""
        if name:
            cand = name if os.path.isabs(name) else os.path.join(ts, name)
            if os.path.isfile(cand):
                return cand
    except (OSError, ValueError, AttributeError):
        pass
    fallback = os.path.join(ts, "tokensave.db")
    return fallback if os.path.isfile(fallback) else ""


# ── Schema tolerance ─────────────────────────────────────────────────────

_REQUIRED = {
    "nodes": {"id", "file_path"},
    "edges": {"source", "target", "kind"},
}


def _schema_gap(conn) -> str:
    """Return "" when the schema is usable, else why it is not.

    Several helpers here read tokensave's DB directly, so schema drift is a
    known integration risk. An unusable schema must surface as ``unknown``,
    never as zero findings.
    """
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        return f"cannot read schema ({exc})"
    for table, cols in _REQUIRED.items():
        if table not in have:
            return f"table '{table}' missing"
        try:
            present = {r[1] for r in conn.execute(
                f"PRAGMA table_info({table})")}
        except sqlite3.Error as exc:
            return f"cannot read columns of '{table}' ({exc})"
        missing = cols - present
        if missing:
            return (f"table '{table}' missing column(s): "
                    f"{', '.join(sorted(missing))}")
    return ""


# ── The inspection ───────────────────────────────────────────────────────

def inspect_graph(project_root: str, *,
                  min_edges: int = MIN_MEANINGFUL_EDGES,
                  max_collisions: int = MAX_COLLISIONS) -> GraphTrust:
    """Count edges from production code into the test tree.

    The predicate is deliberately one-way. A ``tests/`` to ``src/`` edge is
    a test calling the code under test, which is what a test is; only the
    reverse is impossible. Inverting this would flag the entire suite.

    Self-edges are excluded. They are a real population here (the same
    upstream fallback produces phantom recursive self-edges) but they are a
    different defect and belong to a different measurement.
    """
    db = db_path_for(project_root)
    if not db:
        return GraphTrust(STATE_UNKNOWN, "no tokensave index for this project")

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return GraphTrust(STATE_UNKNOWN, f"cannot open index ({exc})",
                          db_path=db)

    try:
        gap = _schema_gap(conn)
        if gap:
            return GraphTrust(STATE_UNKNOWN,
                              f"unsupported tokensave schema — {gap}",
                              db_path=db)

        try:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes").fetchone()[0]
            rows = conn.execute(
                "SELECT s.file_path, t.file_path, t.name "
                "FROM edges e "
                "JOIN nodes s ON s.id = e.source "
                "JOIN nodes t ON t.id = e.target "
                "WHERE e.source <> e.target"
            ).fetchall()
        except sqlite3.Error as exc:
            return GraphTrust(STATE_UNKNOWN, f"cannot read graph ({exc})",
                              db_path=db)

        examined = len(rows)
        if node_count <= 0 or examined <= 0:
            return GraphTrust(STATE_INSUFFICIENT,
                              f"index holds {node_count} node(s) and "
                              f"{examined} inspectable edge(s)",
                              edges_examined=examined, db_path=db)
        if examined < min_edges:
            return GraphTrust(STATE_INSUFFICIENT,
                              f"only {examined} inspectable edge(s), below "
                              f"the floor of {min_edges} — a zero result "
                              f"here would describe the index, not the code",
                              edges_examined=examined, db_path=db)

        tally: dict = {}
        sources: set = set()
        impossible = 0
        for src_file, dst_file, dst_name in rows:
            if not is_test_path(dst_file or ""):
                continue
            if is_test_path(src_file or ""):
                continue
            impossible += 1
            sources.add(src_file)
            key = (dst_name or "", dst_file or "")
            tally[key] = tally.get(key, 0) + 1

        if not impossible:
            return GraphTrust(STATE_TRUSTWORTHY, edges_examined=examined,
                              db_path=db)

        top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        collisions = tuple(
            Collision(target_name=name, target_file=path, count=n)
            for (name, path), n in top[:max_collisions]
        )
        return GraphTrust(STATE_TAINTED,
                          edges_examined=examined,
                          impossible_edges=impossible,
                          source_files_affected=len(sources),
                          collisions=collisions,
                          db_path=db)
    finally:
        if conn is not None:
            conn.close()
