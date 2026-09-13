"""helpers/index_provenance.py — which tokensave built this graph, when we know.

WHY THIS EXISTS. ``.tokensave/config.json``'s ``last_indexed_version`` reads
like provenance and is not. tokensave's MCP server re-indexes only on a MAJOR
bump or a schema change; on a patch or minor bump it simply advances the
marker (``run_version_reindex`` in ``src/mcp/server.rs``, present since at
least 7.10.0, and documented in upstream ``TOKENSAVE-VERSIONING.md``). So the
field means *the last version that evaluated this project for maintenance*.

Measured 2026-09-12, straight after 7.11.1 -> 7.12.1: both this repository and
Fortuna Lab read ``last_indexed_version = 7.12.1`` while their
``last_full_sync_at`` was Sep 8 and Aug 19 -- graphs built before the release
whose resolver fixes (#522, #503) they were silently credited with. Doctor's
index-freshness rule compared that field against the installed binary and went
quiet, which is the failure it was written to prevent.

WHAT THIS KNOWS, AND WHAT IT REFUSES TO GUESS. tokensave records *when* the
last full index ran (``metadata.last_full_sync_at`` in the index DB) and never
*which binary* ran it. So provenance exists only for full indexes the Manager
ran itself: it reads the binary's version immediately before the run, reads
``last_full_sync_at`` after, and writes both down -- only when the timestamp
actually advanced. A full index run anywhere else changes the timestamp, the
record stops matching, and the verdict becomes UNKNOWN rather than an
inference.

Deliberately NOT used: the executable's mtime, or any release date. A binary
can be copied, restored or rebuilt from source without its semantics
changing, and a source build can predate the release it becomes. A timestamp
comparison would manufacture STALE out of filesystem accidents.

The version comparison is EXACT. tokensave's bump kinds encode required
*maintenance*, not whether extractor output changed (7.12.0 changed resolver
output as a minor), so there is no sound way to say which differences matter.

Pure-ish: stdlib only, no Tk, read-only against the index (``mode=ro``).
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

CURRENT = "current"
BUILT_BY_OLDER = "built_by_older"
UNKNOWN = "unknown"

_DIRNAME = ".tokensave-manager"
_FILENAME = "index-provenance.json"


def provenance_path(project_root: str) -> str:
    return os.path.join(project_root, _DIRNAME, _FILENAME)


def is_full_index_argv(args) -> bool:
    """Whether a tokensave argv (without the executable) rebuilds the graph.

    ``init`` indexes from scratch and ``sync --force`` rebuilds; a plain
    ``sync`` re-extracts changed files only and must never be recorded as
    provenance, or an incremental sync would launder an old graph.
    """
    args = list(args or [])
    if not args:
        return False
    if args[0] == "init":
        return True
    return args[0] == "sync" and "--force" in args[1:]


def read_last_full_sync_at(project_root: str) -> "tuple[int | None, str]":
    """``(timestamp, problem)`` from the active index DB -- exactly one set.

    Read-only. The DB is resolved through ``graph_trust.db_path_for`` so a
    project on a tracked feature branch reads that branch's index, not the
    default one.
    """
    from helpers.graph_trust import db_path_for

    db = db_path_for(project_root)
    if not db:
        return (None, "no tokensave index for this project")
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_full_sync_at'"
        ).fetchone()
    except sqlite3.Error as exc:
        return (None, f"cannot read the index metadata ({exc})")
    finally:
        if conn is not None:
            conn.close()
    if row is None:
        return (None, "the index records no last_full_sync_at")
    try:
        return (int(str(row[0]).strip()), "")
    except ValueError:
        return (None, f"last_full_sync_at is not a timestamp ({row[0]!r})")


def _db_rel(project_root: str) -> str:
    from helpers.graph_trust import db_path_for

    db = db_path_for(project_root)
    if not db:
        return ""
    try:
        return os.path.relpath(db, project_root).replace("\\", "/")
    except ValueError:            # different drive on Windows
        return db


def _ensure_private_dir(project_root: str) -> str:
    """Create ``.tokensave-manager/`` with a self-ignoring ``.gitignore``.

    Not every project's .gitignore names this directory, and a provenance
    file is machine-specific by definition: committing it would assert that
    another checkout's graph was built by this machine's binary.
    """
    d = os.path.join(project_root, _DIRNAME)
    os.makedirs(d, exist_ok=True)
    ignore = os.path.join(d, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("*\n")
    return d


def record_full_index(project_root: str, version: str,
                      full_sync_before: "int | None") -> "tuple[bool, str]":
    """Write provenance after a Manager-run full index. ``(written, reason)``.

    Written only when ``last_full_sync_at`` demonstrably advanced past the
    value read before the run -- a zero exit that did not rebuild anything is
    not evidence that this version built the graph.
    """
    if not version:
        return (False, "the tokensave version was unknown before the run")
    after, problem = read_last_full_sync_at(project_root)
    if after is None:
        return (False, problem)
    if full_sync_before is not None and after <= full_sync_before:
        return (False, "last_full_sync_at did not advance")
    record = {"version": version, "full_sync_at": after,
              "db": _db_rel(project_root)}
    d = _ensure_private_dir(project_root)
    target = os.path.join(d, _FILENAME)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, target)
    return (True, "")


@dataclasses.dataclass(frozen=True)
class PendingFullIndex:
    """What was true immediately BEFORE a full index, so the after can prove it."""
    project_root: str
    version: str
    version_problem: str
    full_sync_before: "int | None"


def begin_full_index(args, project_root: str,
                     tokensave_exe: str) -> "PendingFullIndex | None":
    """Capture the binary version and prior timestamp; None for other argv.

    The version is read before the run on purpose: an upgrade landing while
    the index runs must not be credited with a graph the old binary built.
    """
    if not is_full_index_argv(args):
        return None
    from helpers.doctor_rules import _installed_extractor_version

    version, problem = _installed_extractor_version(tokensave_exe)
    before, _ = read_last_full_sync_at(project_root)
    return PendingFullIndex(project_root, version, problem, before)


def finish_full_index(pending: PendingFullIndex) -> str:
    """Record after a successful run; return one line saying what happened."""
    written, reason = record_full_index(
        pending.project_root, pending.version, pending.full_sync_before)
    if written:
        return ("Recorded: this graph was fully built by tokensave %s."
                % pending.version)
    return "Index provenance not recorded: %s." % (
        pending.version_problem or reason)


def report_full_index(pending: "PendingFullIndex | None", log) -> None:
    """Record after a successful run and hand the one-line result to *log*.

    A no-op for ``None``, so a streaming runner can call it after every
    successful command without knowing which ones rebuild the graph.
    """
    if pending is not None:
        log(finish_full_index(pending))


def read_record(project_root: str) -> "tuple[dict | None, str]":
    path = provenance_path(project_root)
    if not os.path.isfile(path):
        return (None, "")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return (None, f"cannot read {_DIRNAME}/{_FILENAME} ({exc})")
    if (not isinstance(data, dict) or not isinstance(data.get("version"), str)
            or not isinstance(data.get("full_sync_at"), int)):
        return (None, f"{_DIRNAME}/{_FILENAME} is not a provenance record")
    return (data, "")


@dataclasses.dataclass(frozen=True)
class Provenance:
    state: str
    running: str = ""
    built_by: str = ""
    full_sync_at: "int | None" = None
    reason: str = ""


def verdict(project_root: str, running_version: str) -> Provenance:
    """CURRENT / BUILT_BY_OLDER / UNKNOWN for one project's active index.

    BUILT_BY_OLDER is named for what is known -- a *different* version built
    it -- and is used for downgrades too; the caller words the remedy.
    """
    full, full_problem = read_last_full_sync_at(project_root)
    record, rec_problem = read_record(project_root)
    if full is None:
        return Provenance(UNKNOWN, running_version, reason=full_problem)
    if rec_problem:
        return Provenance(UNKNOWN, running_version, full_sync_at=full,
                          reason=rec_problem)
    if record is None:
        return Provenance(
            UNKNOWN, running_version, full_sync_at=full,
            reason="no full index has been run from the Manager, so which "
                   "tokensave built this graph is not recorded anywhere")
    if record["full_sync_at"] != full:
        return Provenance(
            UNKNOWN, running_version, built_by=record["version"],
            full_sync_at=full,
            reason="a full index ran outside the Manager since the last "
                   "recorded one, so the recorded version no longer "
                   "describes this graph")
    if record.get("db") and record["db"] != _db_rel(project_root):
        return Provenance(
            UNKNOWN, running_version, built_by=record["version"],
            full_sync_at=full,
            reason="the record describes a different index DB (another "
                   "branch's graph)")
    if not running_version:
        return Provenance(UNKNOWN, "", built_by=record["version"],
                          full_sync_at=full,
                          reason="the installed tokensave version is unknown")
    state = CURRENT if record["version"] == running_version else BUILT_BY_OLDER
    return Provenance(state, running_version, built_by=record["version"],
                      full_sync_at=full)
