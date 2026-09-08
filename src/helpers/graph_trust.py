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

**Two kinds, two defects, reported separately.** The same bare-name binding
happens on ``calls`` and on ``uses`` edges, and they do not move together.
tokensave 7.11.1 (upstream #508) taught the *call* fallback to demand
evidence before binding a lone candidate, and on this repository that took
``calls`` from 455 impossible edges to 29 — a 94% clearance. The *reference*
fallback was untouched: ``uses`` stayed at exactly 351, the same names
(``exe``, ``cfg_path``, ``python_exe``, ``proj``) at the same counts before
and after a full re-index.

So a single total is not reportable. Summed, that upgrade reads 806 → 380,
which looks like a fix that half-worked on one defect rather than what it
was: one defect essentially fixed and a second one, in a different resolver
path, entirely untouched. ``by_kind`` carries the split, and every consumer
that says anything about #503 should speak about ``kind("calls")`` rather
than about the total.

This module counts them, so a consumer can say how much of the graph it is
willing to believe. It does not repair anything: the defects are upstream
(``docs/upstream-issues/tokensave-python-bare-name-fallback.md`` for the
call path, ``tokensave-python-uses-bare-name.md`` for the reference path)
and the local job is to stop reporting a number whose basis is known to be
wrong.

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
class KindTally:
    """The impossible/examined split for one edge kind.

    Kept separate because the two kinds here answer to different upstream
    defects and move independently. Reporting only the sum let a 94%
    improvement in one render as "barely changed" -- see the module
    docstring.
    """
    kind: str
    impossible: int
    examined: int

    def __str__(self) -> str:
        return f"{self.kind} {self.impossible}/{self.examined}"


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
    by_kind: tuple = field(default_factory=tuple)
    db_path: str = ""

    @property
    def is_conclusive(self) -> bool:
        """True when the inspection actually reached a population."""
        return self.state in (STATE_TRUSTWORTHY, STATE_TAINTED)

    @property
    def is_tainted(self) -> bool:
        return self.state == STATE_TAINTED

    def kind(self, name: str) -> "KindTally | None":
        """The tally for one edge kind, or None if that kind was not seen.

        None means the kind is absent from this index, which is not the same
        as a kind present with zero impossible edges -- the same distinction
        the four states exist to keep.
        """
        for t in self.by_kind:
            if t.kind == name:
                return t
        return None

    def summary(self) -> str:
        """One line, always naming the population it measured."""
        if self.state == STATE_UNKNOWN:
            return f"graph trust unknown — {self.detail}"
        if self.state == STATE_INSUFFICIENT:
            return f"graph trust inconclusive — {self.detail}"
        if self.state == STATE_TRUSTWORTHY:
            return (f"graph looks sound — no impossible edges "
                    f"across {self.edges_examined} examined")
        split = ", ".join(f"{t.kind} {t.impossible}/{t.examined}"
                          for t in self.by_kind if t.impossible)
        detail = f" [{split}]" if split else ""
        return (f"graph is contaminated — {self.impossible_edges} impossible "
                f"edge(s) from {self.source_files_affected} source file(s), "
                f"across {self.edges_examined} examined{detail}")


# ── Locating the index ───────────────────────────────────────────────────

def _checked_out_branch(project_root: str) -> "str | None":
    """The branch this working tree is on, read from ``.git/HEAD``.

    A file read rather than ``git rev-parse``, because this module promises no
    subprocess and its callers run on a UI thread or inside a scan. ``.git`` is
    a *file* rather than a directory in a worktree or a submodule, naming the
    real git directory, so that one indirection is followed.

    Returns None for a detached HEAD (where the file holds a raw SHA) and for
    anything unreadable. Callers fall back to the default branch: "which branch
    is this?" being unanswerable is not the same as "there is no index".
    """
    git = os.path.join(project_root, ".git")
    try:
        if os.path.isfile(git):
            with open(git, "r", encoding="utf-8") as fh:
                pointer = fh.read().strip()
            if not pointer.startswith("gitdir:"):
                return None
            git = pointer.split(":", 1)[1].strip()
            if not os.path.isabs(git):
                git = os.path.join(project_root, git)
        with open(os.path.join(git, "HEAD"), "r", encoding="utf-8") as fh:
            head = fh.read().strip()
    except (OSError, ValueError):
        return None

    if not head.startswith("ref:"):
        return None                       # detached HEAD
    ref = head.split(":", 1)[1].strip()
    prefix = "refs/heads/"
    if not ref.startswith(prefix):
        return None
    # A branch name may itself contain "/" ("feature/experiment"), so strip the
    # prefix rather than taking the last path segment.
    return ref[len(prefix):] or None


def _candidate_db_files(raw, project_root: str,
                        branch: "str | None") -> list:
    """``db_file`` values worth trying, best first. Never raises.

    Order is the whole point. The checked-out branch comes first; the default
    branch second, because a branch tokensave is not tracking has no index of
    its own and the default's is the best available answer -- but it must never
    be preferred over the branch actually checked out.
    """
    if not isinstance(raw, dict):
        return []

    out: list = []
    branches = raw.get("branches")
    if isinstance(branches, dict):
        wanted = branch or _checked_out_branch(project_root)
        for key in (wanted, raw.get("default_branch")):
            if not isinstance(key, str):
                continue
            entry = branches.get(key)
            if isinstance(entry, dict):
                name = entry.get("db_file")
                if isinstance(name, str) and name and name not in out:
                    out.append(name)

    flat = raw.get("db_file")      # schema written by older tokensave versions
    if isinstance(flat, str) and flat and flat not in out:
        out.append(flat)
    return out


def db_path_for(project_root: str, *, branch: "str | None" = None) -> str:
    """Return the active tokensave DB for *project_root*, or "".

    Resolves the **checked-out branch's** index, which is not always
    ``tokensave.db``. ``tokensave branch add`` copies the ancestor DB to
    ``.tokensave/branches/<name>.db`` and records it under ``branches`` in
    ``branch-meta.json``, so a project on a feature branch has its own index.
    Reading the wrong one fails silently in the worst way: the graph is valid,
    it simply lacks every file that branch added, and nothing says so.

    Measured before this was written: a scratch project on ``feature/experiment``
    resolved to ``tokensave.db`` and could not see a file committed on that
    branch. The earlier implementation looked for a **top-level** ``db_file``,
    a shape no tokensave version on this machine writes -- 16 indexed projects,
    zero with that key -- so the branch lookup never fired and every caller
    silently got the default branch.

    A ``db_file`` naming a file that is not there is treated as absent rather
    than trusted, because a stale pointer is not evidence about this branch.
    """
    ts = os.path.join(project_root, ".tokensave")
    meta = os.path.join(ts, "branch-meta.json")
    try:
        with open(meta, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = None

    for name in _candidate_db_files(raw, project_root, branch):
        cand = name if os.path.isabs(name) else os.path.join(ts, name)
        if os.path.isfile(cand):
            return cand

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


# ── Index presence ───────────────────────────────────────────────────────
#
# A different question from graph *trust* above, and the distinction is
# load-bearing. Trust asks "can this graph be believed?"; presence asks "is
# there an index here at all, and will it open?" Only one presence answer
# justifies creating an index, because `tokensave init` overwrites whatever
# is already there. Collapsing these into one "is it OK?" boolean is how a
# repair path destroys a working index it merely failed to understand.

INDEX_ABSENT       = "absent"        # nothing to open
INDEX_UNOPENABLE   = "unopenable"    # a file is there; sqlite will not open it
INDEX_SCHEMA_DRIFT = "schema_drift"  # opens, but not a schema we recognise
INDEX_PRESENT      = "present"       # opens, schema usable


@dataclass(frozen=True)
class IndexState:
    """Whether *this* project has a usable tokensave index, and why not."""
    state: str
    detail: str = ""
    db_path: str = ""

    @property
    def may_initialise(self) -> bool:
        """Whether an index may be created here without asking a human first.

        ``absent`` only. Every other state has a file that ``tokensave init``
        would discard, so the choice belongs to the user rather than to the
        caller. Schema drift especially: it usually means the Manager is
        older or newer than the CLI that wrote the index, and re-indexing is
        not obviously the right remedy for a version mismatch.
        """
        return self.state == INDEX_ABSENT

    def summary(self) -> str:
        if self.state == INDEX_PRESENT:
            return "index present"
        return f"no usable index — {self.detail}"


def index_state(project_root: str) -> IndexState:
    """Classify the index at *project_root* without modifying anything.

    Read-only (``mode=ro``), and deliberately says *which* way an index is
    unusable. :func:`inspect_graph` flattens all three failures into
    ``unknown`` because for trust purposes they are the same answer; for
    deciding whether to run ``init`` they are emphatically not.
    """
    db = db_path_for(project_root)
    if not db:
        return IndexState(INDEX_ABSENT, "no tokensave index for this project")

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # sqlite3.connect() is lazy -- it does not read the file until a
        # statement runs, so a directory of random bytes named tokensave.db
        # "connects" happily and only fails later, inside the schema check,
        # where it would be misread as schema drift. Force the open here so
        # "this is not a database" and "this is a database I do not
        # recognise" stay the different answers they are: the first justifies
        # offering to rebuild, the second is usually a version mismatch.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        return IndexState(INDEX_UNOPENABLE, f"cannot open index ({exc})",
                          db_path=db)
    try:
        gap = _schema_gap(conn)
        if gap:
            return IndexState(INDEX_SCHEMA_DRIFT,
                              f"unsupported tokensave schema — {gap}",
                              db_path=db)
        return IndexState(INDEX_PRESENT, db_path=db)
    finally:
        if conn is not None:
            conn.close()


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
                "SELECT s.file_path, t.file_path, t.name, e.kind "
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
        seen_by_kind: dict = {}
        bad_by_kind: dict = {}
        impossible = 0
        for src_file, dst_file, dst_name, edge_kind in rows:
            kind = edge_kind or ""
            seen_by_kind[kind] = seen_by_kind.get(kind, 0) + 1
            if not is_test_path(dst_file or ""):
                continue
            if is_test_path(src_file or ""):
                continue
            impossible += 1
            bad_by_kind[kind] = bad_by_kind.get(kind, 0) + 1
            sources.add(src_file)
            key = (dst_name or "", dst_file or "")
            tally[key] = tally.get(key, 0) + 1

        # Every kind that was examined, including the clean ones: a kind
        # reporting 0/10656 is a result, and dropping it would leave a reader
        # unable to tell "this kind is clean" from "this kind was not looked
        # at". Ordered by impossible count so the worst reads first.
        by_kind = tuple(
            KindTally(k, bad_by_kind.get(k, 0), seen_by_kind[k])
            for k in sorted(seen_by_kind,
                            key=lambda k: (-bad_by_kind.get(k, 0), k))
        )

        if not impossible:
            return GraphTrust(STATE_TRUSTWORTHY, edges_examined=examined,
                              by_kind=by_kind, db_path=db)

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
                          by_kind=by_kind,
                          db_path=db)
    finally:
        if conn is not None:
            conn.close()
