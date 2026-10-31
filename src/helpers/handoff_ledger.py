"""A project's shared handoff ledger: what one agent leaves for the next.

`.tokensave/handoff.jsonl`, one JSON object per line, append-only. It is how a
Claude session and a Cursor session working on the same project can see each
other's decisions and open questions without either one's private store being
readable by the other.

WHAT EACH FIELD IS WORTH. An entry mixes statements of different weight, and
the schema keeps them apart instead of flattening them:

  * `agent_reported_files` is what the AGENT said it changed. A claim.
  * `observed_changed_files` is what git reported when the entry was written,
    measured by the Manager. A fact about that moment.
  * `goal`, `decisions`, `next_steps` and `open_questions` are the agent's own
    words. They are never promoted to verified project state by being written
    down here.

Writing is validated and bounded, because the writer is called by agents and an
unvalidated ledger is how a long-lived project ends up with a megabyte of noise
that every future session is asked to read:

  * every string is capped, every list is capped, and what was cut is NAMED in
    `truncated` rather than silently shortened;
  * common secret shapes are masked (best effort: `agent_handoff.redact`);
  * an empty entry is refused;
  * the id is derived from the content, so an agent that retries after a
    timeout writes nothing twice.

Concurrency: two agents can finish at the same moment, so an append takes a lock
file (created exclusively, broken only when stale) and writes one line. A read
never needs the lock, because a line is complete or absent: the writer first
makes sure the file ends in a newline, so an interrupted earlier write cannot
glue itself to the next entry.

Reading never pretends: lines that do not parse are COUNTED and reported, not
dropped, and an unreadable file is a different state from an absent one.

The ledger is machine-local. `.tokensave/` is git-ignored by the baseline, and
`is_ignored` asks git rather than assuming, because a file holding the person's
decisions that is not ignored is one `git add -A` from being published.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import subprocess
import time

from constants import CREATE_NO_WINDOW

SCHEMA_VERSION = 1
LEDGER_DIR = ".tokensave"
LEDGER_BASENAME = "handoff.jsonl"
LOCK_SUFFIX = ".lock"

KIND_HANDOFF = "handoff"
#: Recorded by a hook, never written by hand: that a session ended, and where
#: its transcript is. It carries no summary because a hook has none to give.
KIND_SESSION = "session"
KINDS = (KIND_HANDOFF, KIND_SESSION)

SOURCE_PUBLISHED = "published"
SOURCE_HOOK = "hook"

MAX_GOAL_CHARS = 500
MAX_ITEM_CHARS = 400
MAX_ITEMS = 20
MAX_FILES = 50
MAX_PATH_CHARS = 300

#: Retention. Over either bound after an append, the file is cut to the newest
#: KEEP_ENTRIES lines; the number cut is reported.
MAX_ENTRIES = 200
KEEP_ENTRIES = 150
MAX_BYTES = 512_000

LOCK_TIMEOUT_SECONDS = 5.0
#: A lock older than this belongs to a writer that died. Appends take
#: milliseconds, so thirty seconds is "dead", not "slow".
STALE_LOCK_SECONDS = 30.0

STATE_ABSENT = "absent"
STATE_OK = "ok"
STATE_UNREADABLE = "unreadable"

_AGENT = re.compile(r"[a-z0-9][a-z0-9_\-]{0,31}")


class LedgerError(Exception):
    """Something about a requested write is not acceptable. Message is for people."""


class LockTimeout(LedgerError):
    pass


@dataclasses.dataclass(frozen=True)
class AppendResult:
    ok: bool
    handoff_id: str = ""
    #: True when an entry with this id was already there, so nothing was written.
    duplicate: bool = False
    path: str = ""
    #: Lines cut by retention on this write.
    trimmed: int = 0
    error: str = ""


@dataclasses.dataclass(frozen=True)
class LedgerRead:
    state: str
    entries: tuple = ()
    #: Lines that were not valid entries. Counted, never silently skipped: a
    #: ledger that looks complete while missing lines is the failure this exists
    #: to prevent.
    bad_lines: int = 0
    path: str = ""
    error: str = ""


def ledger_path(project: str) -> str:
    return os.path.join(project, LEDGER_DIR, LEDGER_BASENAME)


def normalize_agent(agent: str) -> str:
    """An agent id: lowercase letters, digits, dash, underscore, up to 32."""
    cleaned = (agent or "").strip().lower()
    if not _AGENT.fullmatch(cleaned):
        raise LedgerError(
            f"agent {agent!r} is not a valid id (lowercase letters, digits, "
            f"'-' and '_', at most 32 characters)")
    return cleaned


# ── building an entry (pure) ─────────────────────────────────────────────────

def _clean(text, limit: int, field: str, cut: list) -> str:
    from helpers.agent_handoff import redact
    flat = " ".join(str(text).split())
    flat = redact(flat)
    if len(flat) > limit:
        cut.append(field)
        flat = flat[:limit - 3] + "..."
    return flat


def _clean_list(values, limit_items: int, limit_chars: int, field: str,
                cut: list) -> list:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise LedgerError(f"{field} must be a list of strings")
    out = []
    for value in values:
        if not isinstance(value, str):
            raise LedgerError(f"{field} must contain only strings")
        text = _clean(value, limit_chars, field, cut)
        if text:
            out.append(text)
    if len(out) > limit_items:
        cut.append(field)
        out = out[:limit_items]
    return out


def content_id(agent: str, session_id: str, kind: str, goal: str,
               decisions: list, next_steps: list, open_questions: list,
               files: list) -> str:
    """`h` + 12 hex of the entry's CONTENT. No clock, no measured state.

    Time and git state are left out on purpose: a retry a minute later, after
    another file changed, is still the same handoff and must not become two.
    """
    blob = json.dumps([agent, session_id, kind, goal, decisions, next_steps,
                       open_questions, files], sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
    return "h" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def build_entry(*, agent: str, session_id: str = "", kind: str = KIND_HANDOFF,
                goal: str = "", decisions=None, next_steps=None,
                open_questions=None, files=None, source: str = SOURCE_PUBLISHED,
                branch: str = "", base_commit: str = "", observed=None,
                extra: "dict | None" = None, now=None) -> dict:
    """Validate and bound one entry. Raises `LedgerError`; never half-builds."""
    if kind not in KINDS:
        raise LedgerError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    agent = normalize_agent(agent)
    cut: list = []
    goal_text = _clean(goal or "", MAX_GOAL_CHARS, "goal", cut)
    decisions_l = _clean_list(decisions, MAX_ITEMS, MAX_ITEM_CHARS, "decisions", cut)
    steps_l = _clean_list(next_steps, MAX_ITEMS, MAX_ITEM_CHARS, "next_steps", cut)
    questions_l = _clean_list(open_questions, MAX_ITEMS, MAX_ITEM_CHARS,
                              "open_questions", cut)
    files_l = _clean_list(files, MAX_FILES, MAX_PATH_CHARS,
                          "agent_reported_files", cut)
    session = _clean(session_id or "", 120, "session_id", cut)
    if kind == KIND_HANDOFF and not (goal_text or decisions_l or steps_l
                                     or questions_l):
        raise LedgerError("nothing to record: give a goal, a decision, a next "
                          "step or an open question")
    when = now or datetime.datetime.now().astimezone()
    entry = {
        "schema_version": SCHEMA_VERSION,
        "handoff_id": content_id(agent, session, kind, goal_text, decisions_l,
                                 steps_l, questions_l, files_l),
        "kind": kind,
        "agent": agent,
        "session_id": session,
        "ts": when.isoformat(timespec="seconds"),
        "branch": _clean(branch, 200, "branch", cut),
        "base_commit": _clean(base_commit, 64, "base_commit", cut),
        "goal": goal_text,
        "decisions": decisions_l,
        "next_steps": steps_l,
        "open_questions": questions_l,
        "agent_reported_files": files_l,
        "observed_changed_files": list(observed or [])[:MAX_FILES],
        "provenance": {"source": source},
    }
    if extra:
        entry["provenance"].update(
            {str(k): _clean(v, 300, "provenance", cut) for k, v in extra.items()})
    if cut:
        entry["truncated"] = sorted(set(cut))
    return entry


# ── locking and appending ─────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked(project: str, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Hold the ledger lock. A stale lock is broken; a live one is waited on."""
    path = ledger_path(project) + LOCK_SUFFIX
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > STALE_LOCK_SECONDS:
                    os.remove(path)
                    continue
            except OSError:
                continue              # it vanished between the two calls: retry
            if time.monotonic() > deadline:
                raise LockTimeout("the handoff ledger is locked by another "
                                  "writer; try again")
            time.sleep(0.02)
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {time.time()}")
        break
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def _read_lines(path: str) -> list:
    with open(path, "rb") as fh:
        data = fh.read()
    return [ln for ln in data.split(b"\n") if ln.strip()]


def _ids_in(lines: list) -> set:
    ids = set()
    for raw in lines:
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("handoff_id"), str):
            ids.add(record["handoff_id"])
    return ids


def append(project: str, entry: dict) -> AppendResult:
    """Append one validated entry. Idempotent on `handoff_id`. Never raises."""
    path = ledger_path(project)
    handoff_id = str(entry.get("handoff_id") or "")
    try:
        line = json.dumps(entry, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
        with _locked(project):
            existing = _read_lines(path) if os.path.isfile(path) else []
            if handoff_id and handoff_id in _ids_in(existing):
                return AppendResult(True, handoff_id, True, path)
            glue = b""
            if os.path.isfile(path) and os.path.getsize(path):
                with open(path, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        glue = b"\n"      # an interrupted write left a partial line
            with open(path, "ab") as fh:
                fh.write(glue + line)
                fh.flush()
                os.fsync(fh.fileno())
            trimmed = _trim_if_needed(path)
        return AppendResult(True, handoff_id, False, path, trimmed)
    except LockTimeout as exc:
        return AppendResult(False, handoff_id, path=path, error=str(exc))
    except OSError as exc:
        return AppendResult(False, handoff_id, path=path,
                            error=f"could not write the ledger: {exc}")


def _trim_if_needed(path: str) -> int:
    """Cut to the newest KEEP_ENTRIES lines when over a bound. Lock held."""
    lines = _read_lines(path)
    if len(lines) <= MAX_ENTRIES and os.path.getsize(path) <= MAX_BYTES:
        return 0
    kept = lines[-KEEP_ENTRIES:]
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(b"\n".join(kept) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(lines) - len(kept)


# ── reading and clearing ───────────────────────────────────────────────────────

def read(project: str, limit: "int | None" = None) -> LedgerRead:
    """Entries oldest first (the newest `limit` of them). Never raises."""
    path = ledger_path(project)
    if not os.path.exists(path):
        return LedgerRead(STATE_ABSENT, path=path)
    try:
        lines = _read_lines(path)
    except OSError as exc:
        return LedgerRead(STATE_UNREADABLE, path=path, error=str(exc))
    entries, bad = [], 0
    for raw in lines:
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            bad += 1
            continue
        if isinstance(record, dict) and isinstance(record.get("handoff_id"), str):
            entries.append(record)
        else:
            bad += 1
    if limit is not None and limit >= 0:
        entries = entries[-limit:] if limit else []
    return LedgerRead(STATE_OK, tuple(entries), bad, path)


def clear(project: str) -> "tuple[bool, str]":
    """Delete the ledger. Deliberate and total; called only on a person's say-so."""
    path = ledger_path(project)
    try:
        with _locked(project):
            if os.path.exists(path):
                os.remove(path)
    except LedgerError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"could not clear the ledger: {exc}"
    return True, ""


def is_ignored(project: str, git_exe: str = "") -> "bool | None":
    """Does git ignore the ledger? `None` when git cannot say.

    Three answers on purpose. `False` means "this file would be committed by a
    careless `git add -A`", which is a finding; `None` means git did not answer
    (not a repository, git missing), which is not the same fact.
    """
    try:
        proc = subprocess.run(
            [git_exe or "git", "-C", project, "check-ignore", "-q", "--",
             f"{LEDGER_DIR}/{LEDGER_BASENAME}"],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


# ── the one validated writer ────────────────────────────────────────────────────

def publish(project: str, *, agent: str, session_id: str = "", goal: str = "",
            decisions=None, next_steps=None, open_questions=None, files=None,
            git_exe: str = "") -> "tuple[AppendResult, list]":
    """What an agent calls when it finishes meaningful work. Returns
    `(result, warnings)`.

    The Manager measures `branch`, `base_commit` and `observed_changed_files`
    itself; the agent supplies only its own words and its own file claims.
    Raises `LedgerError` for an entry that should not be written.
    """
    from helpers.agent_handoff import _git, observe_repository
    seen = observe_repository(project, git_exe)
    ok, head = _git(git_exe, project, "rev-parse", "HEAD")
    entry = build_entry(
        agent=agent, session_id=session_id, goal=goal, decisions=decisions,
        next_steps=next_steps, open_questions=open_questions, files=files,
        branch=seen["branch"], base_commit=head.strip() if ok else "",
        observed=[ln.strip() for ln in seen["dirty"]])
    result = append(project, entry)
    warnings = []
    if seen["git_error"]:
        # First line only: git's own message for "no commits yet" runs to five.
        why = (seen["git_error"].splitlines() or [""])[0]
        warnings.append(f"git did not answer, so branch and changed files are "
                        f"empty: {why}")
    if result.ok and is_ignored(project, git_exe) is False:
        warnings.append(f"{LEDGER_DIR}/{LEDGER_BASENAME} is NOT git-ignored in "
                        f"this project; it holds your decisions and should not "
                        f"be committed")
    if result.ok and result.trimmed:
        warnings.append(f"retention cut {result.trimmed} older line(s)")
    return result, warnings
