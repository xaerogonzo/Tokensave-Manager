"""Pure detection for the Housekeeping dialog.

No subprocess, no Tk, no module-level path resolution. Everything here takes its
inputs as arguments and returns plain data, so the interesting logic — which
entries are stale and *why*, which backups are safe to delete — is deterministic
and unit-testable without a terminal or a live tokensave install.

Running ``tokensave doctor`` is deliberately NOT this module's job.
`DoctorController` owns every doctor invocation (one env normalization, one
subprocess shape) and hands the transcript here. Two subtly different
invocations of the same command is exactly the drift worth avoiding.

What "stale" actually means
---------------------------
`tokensave doctor` reports these as "stale project(s) in global DB", but that
phrase is misleading. On the machine this was built against, all 12 rows in
``~/.tokensave/global.db → projects`` were live, and the 8 reported paths instead
appeared in ``turns.project_hash`` / ``parse_offsets.file_path`` — **token-cost
history** keyed to Claude Code session logs.

That is an observation about one tokensave version, not a law. So `source` is
*resolved* per entry rather than assumed, `unknown` is a first-class outcome, and
nothing here ever infers a source from absence: "not in ``projects``" does not
imply cost history. If the DB cannot be read, or the schema is not what we
expect, every entry stays `unknown` and the scan still succeeds.

Deleting is somebody else's job
-------------------------------
tokensave stays the only writer of its own database — this module opens it
read-only and only to label rows. The backup side does perform real deletions,
which is why `revalidate_backup` exists: the file is re-stat'd and re-hashed
immediately before removal, so a file that changed between the scan and the
click is skipped rather than destroyed.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import sqlite3
from dataclasses import dataclass, field, replace

from helpers.claude_tasks import encode_project_path

# ── Vocabulary ────────────────────────────────────────────────────────────────
REASON_UNCLASSIFIED = "unclassified"
REASON_DIR_MISSING = "dir_missing"
REASON_NOT_INDEXED = "not_indexed"

SOURCE_UNKNOWN = "unknown"
SOURCE_COST_HISTORY = "cost_history"
SOURCE_PROJECT_ROW = "project_row"

_HASH_CHUNK = 65536

# name.bak | name.backup | name.backup.<one suffix>. Anything else — including
# `foo.backup.tar.gz` — is ambiguous and deliberately ignored.
_BACKUP_RE = re.compile(r"^(?P<base>.+?)\.(?:bak|backup(?:\.[^.]+)?)$",
                        re.IGNORECASE)

# The stale block in `tokensave doctor` output.
_BULLET_RE = re.compile(r"^\s*[•*\-]\s+(.+?)\s*$")
# Opening the block requires a COUNT. A healthy system prints
# "✔ No stale projects in global DB", which contains both "stale project" and
# "global DB" — a substring test alone therefore enters the block on a clean
# run and keeps scanning for bullets, ready to misread any later bulleted line
# as a stale path. Requiring a leading number is what distinguishes
# "8 stale project(s) in global DB" from the all-clear.
_STALE_HEADER_RE = re.compile(r"\b(\d+)\s+stale projects?\b.*\bglobal DB\b",
                              re.IGNORECASE)


@dataclass(frozen=True)
class StaleEntry:
    path: str
    reason: str = REASON_UNCLASSIFIED
    source: str = SOURCE_UNKNOWN
    session_logs_present: bool = False

    @property
    def may_regenerate(self) -> bool:
        """True when purging is likely to be undone by the next ``cost`` run.

        A directory that is gone cannot be re-indexed, but its Claude Code
        session logs are still on disk and still parseable — so the records can
        come back. An entry whose directory still exists is a different story:
        re-indexing it clears the report naturally.
        """
        return self.reason == REASON_DIR_MISSING and self.session_logs_present

    @property
    def source_label(self) -> str:
        return {
            SOURCE_COST_HISTORY: "Cost history",
            SOURCE_PROJECT_ROW: "Project registry",
        }.get(self.source, "Unrecognised record")

    @property
    def reason_label(self) -> str:
        return {
            REASON_DIR_MISSING: "directory missing",
            REASON_NOT_INDEXED: "directory exists · not indexed",
        }.get(self.reason, "unclassified")


@dataclass(frozen=True)
class BackupCandidate:
    """A ``*.bak``-style file paired with the live file it shadows."""
    path: str
    counterpart: str
    size: int
    mtime: float
    sha256: str = ""
    identical: bool = False


@dataclass
class BackupScan:
    duplicates: list = field(default_factory=list)   # identical -> selectable
    kept: int = 0                                    # differing / orphaned

    @property
    def kept_label(self) -> str:
        if not self.kept:
            return ""
        noun = "backup" if self.kept == 1 else "backups"
        return f"{self.kept} {noun} kept — different contents"


def default_backup_roots(home: "str | None" = None) -> "list[str]":
    """Explicit, non-overlapping roots.

    Resolved at call time, never at import, so the ``fake_home`` fixture can
    redirect it (see ``tests/test_no_import_time_path_resolution.py``).

    Deliberately NOT a recursive sweep of ``~/.claude`` — that tree holds
    high-volume session state and legitimate backups that are none of our
    business.
    """
    h = home or os.path.expanduser("~")
    return [
        os.path.join(h, ".claude", "rules"),
        os.path.join(h, ".tokensave"),
    ]


# ── Stale entries ─────────────────────────────────────────────────────────────

def parse_stale_entries(transcript: "str | list[str]") -> "list[StaleEntry]":
    """Extract the stale-entry block from ``tokensave doctor`` output.

    Canonical parser — `DoctorController` delegates here rather than keeping its
    own copy. Entries come back `REASON_UNCLASSIFIED`; `classify_stale_entries`
    fills in the filesystem-derived fields.
    """
    lines = (transcript.splitlines() if isinstance(transcript, str)
             else list(transcript))
    in_block = False
    paths: list = []
    for line in lines:
        if not in_block and _STALE_HEADER_RE.search(line):
            in_block = True
            continue
        if not in_block:
            continue
        if "Re-run" in line and "tokensave doctor" in line:
            break
        m = _BULLET_RE.match(line)
        if m:
            paths.append(m.group(1).strip())
        elif paths and not line.startswith((" ", "\t")):
            break
    return [StaleEntry(path=p) for p in paths]


def classify_stale_entries(entries: "list[StaleEntry]",
                           home: "str | None" = None) -> "list[StaleEntry]":
    """Fill in `reason` and `session_logs_present` from the filesystem.

    Session-log lookup goes strictly forward: real path → normalize →
    `encode_project_path` → inspect only that directory. Never the reverse —
    the encoding maps ``\\``, ``.``, ``_`` and spaces all onto ``-``, so an
    encoded name cannot be decoded back to a unique real path.

    A hit means *"session logs exist for this encoded project"*. That is grounds
    for the regeneration warning; it is not proof those particular logs produced
    these particular records.
    """
    h = home or os.path.expanduser("~")
    projects_dir = os.path.join(h, ".claude", "projects")
    out: list = []
    for e in entries:
        if os.path.isdir(e.path):
            reason = REASON_NOT_INDEXED
        else:
            reason = REASON_DIR_MISSING
        encoded = encode_project_path(os.path.normpath(e.path))
        session_dir = os.path.join(projects_dir, encoded)
        has_logs = False
        try:
            if os.path.isdir(session_dir):
                has_logs = any(n.endswith(".jsonl")
                               for n in os.listdir(session_dir))
        except OSError:
            has_logs = False
        out.append(replace(e, reason=reason, session_logs_present=has_logs))
    return out


def resolve_entry_source(entries: "list[StaleEntry]",
                         global_db: str) -> "list[StaleEntry]":
    """Label each entry `project_row`, `cost_history`, or `unknown`.

    Conservative by construction. Every one of these leaves entries `unknown`
    rather than guessing:

      * the DB file is absent, locked, or unreadable
      * a table or column we need is missing (the schema moved under us)
      * the path matches neither a registry row nor any history record

    In particular, absence is never evidence: "not in ``projects``" does not
    make something cost history. The history structures are checked positively,
    and anything unmatched stays `unknown`. `unknown` entries never receive
    source-specific UI or deletion semantics.
    """
    if not entries:
        return []
    if not global_db or not os.path.isfile(global_db):
        return [replace(e, source=SOURCE_UNKNOWN) for e in entries]

    con = None
    try:
        uri = pathlib.Path(os.path.abspath(global_db)).as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        project_paths = set()
        if "projects" in tables:
            try:
                project_paths = {os.path.normcase(r[0])
                                 for r in con.execute("SELECT path FROM projects")
                                 if r[0]}
            except sqlite3.Error:
                project_paths = set()

        hashes = set()
        have_history = False
        if "turns" in tables:
            try:
                hashes = {r[0] for r in con.execute(
                    "SELECT DISTINCT project_hash FROM turns") if r[0]}
                have_history = True
            except sqlite3.Error:
                have_history = False

        offsets: list = []
        if "parse_offsets" in tables:
            try:
                offsets = [r[0] for r in con.execute(
                    "SELECT file_path FROM parse_offsets") if r[0]]
                have_history = True
            except sqlite3.Error:
                pass

        out: list = []
        for e in entries:
            norm = os.path.normcase(os.path.normpath(e.path))
            encoded = encode_project_path(os.path.normpath(e.path))
            if norm in project_paths:
                out.append(replace(e, source=SOURCE_PROJECT_ROW))
                continue
            if have_history and (
                    encoded in hashes
                    or any(encoded in (o or "") for o in offsets)):
                out.append(replace(e, source=SOURCE_COST_HISTORY))
                continue
            out.append(replace(e, source=SOURCE_UNKNOWN))
        return out
    except (sqlite3.Error, OSError, ValueError):
        return [replace(e, source=SOURCE_UNKNOWN) for e in entries]
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


# ── Redundant backups ─────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    """Streamed digest — these files are small, but never assume that."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _counterpart_for(path: pathlib.Path) -> "pathlib.Path | None":
    m = _BACKUP_RE.match(path.name)
    if not m:
        return None
    return path.with_name(m.group("base"))


def _within(root: pathlib.Path, candidate: pathlib.Path) -> bool:
    """True when ``candidate`` really lives under ``root``.

    Both sides are resolved first, so this rejects ``..`` traversal *and*
    symlinks that point outside the allowed root.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_redundant_backups(roots: "list[str]") -> BackupScan:
    """Find ``*.bak``-style files that are byte-identical to their live file.

    Only exact duplicates are returned as `duplicates` (the selectable set).
    Everything else — different contents, no counterpart, ambiguous name,
    outside the root — is counted into `kept` and never offered for deletion.
    Listing those individually would just be noise; the count is the useful part.

    Comparison short-circuits on size before hashing, since a size mismatch is
    already proof of difference.
    """
    scan = BackupScan()
    for root_str in roots:
        root = pathlib.Path(root_str)
        if not root.is_dir():
            continue
        try:
            names = sorted(root.iterdir())
        except OSError:
            continue
        for cand in names:
            if not cand.is_file() or not _within(root, cand):
                continue
            live = _counterpart_for(cand)
            if live is None:
                continue                       # not a backup-shaped name
            if not live.exists() or not live.is_file() or not _within(root, live):
                scan.kept += 1                 # orphaned backup — keep it
                continue
            try:
                cs = cand.stat()
                ls = live.stat()
                if cs.st_size != ls.st_size:
                    scan.kept += 1
                    continue
                digest = _sha256_file(str(cand))
                identical = digest == _sha256_file(str(live))
            except OSError:
                scan.kept += 1
                continue
            if not identical:
                scan.kept += 1
                continue
            scan.duplicates.append(BackupCandidate(
                path=str(cand), counterpart=str(live), size=cs.st_size,
                mtime=cs.st_mtime, sha256=digest, identical=True))
    return scan


def revalidate_backup(cand: BackupCandidate) -> bool:
    """Re-check a candidate immediately before deleting it.

    Closes the scan → user-waits → file-changes → delete race. Size, mtime and
    digest must all still match what the scan recorded, and the live counterpart
    must still be identical; anything else means the world moved and the file is
    left alone.
    """
    try:
        st = os.stat(cand.path)
        if st.st_size != cand.size or st.st_mtime != cand.mtime:
            return False
        if not os.path.isfile(cand.counterpart):
            return False
        if os.path.getsize(cand.counterpart) != cand.size:
            return False
        return (_sha256_file(cand.path) == cand.sha256
                and _sha256_file(cand.counterpart) == cand.sha256)
    except OSError:
        return False
