"""The durable record of a delegated agent task, and its lifecycle.

Stage A of docs/ORCHESTRATION.md. One JSON file per task, kept beside the handoff
ledger (inside the git common dir, so every worktree of a clone sees the same
tasks and git cannot track them). The FILE is the truth: nothing in memory is
authoritative, so a Manager that was killed mid-task can still say what happened.

The lifecycle is a state machine and its words are not interchangeable:

    created -> worktree_ready -> running -> exited -> result_ready -> accepted | rejected
                      \\             \\-> cancelled | failed | lost
                       \\-> failed

* `exited` means the process ended. It says nothing about whether it worked.
* `result_ready` means the Manager has MEASURED a diff and captured the output.
* `accepted` / `rejected` are a person's click; nothing else sets them.
* `lost` is a task recorded as `running` whose process is not there any more. It
  is never assumed finished and never assumed failed: the worktree is kept and
  the person is told what is in it.

An illegal transition is an error, never a quiet overwrite, because a status
that can jump backwards cannot be read back as history.

No UI imports, no subprocess: process liveness is injected (`reconcile`).
"""
import dataclasses
import datetime
import hashlib
import json
import os
import re
import threading

from helpers import handoff_ledger

SCHEMA_VERSION = 1

KIND_IMPLEMENT = "implement"
KIND_REVIEW = "review"                     # stage B

MODE_WRITE = "WRITE"
MODE_READ_ONLY = "READ_ONLY"

ST_CREATED = "created"
ST_WORKTREE_READY = "worktree_ready"
ST_RUNNING = "running"
ST_EXITED = "exited"
ST_RESULT_READY = "result_ready"
ST_ACCEPTED = "accepted"
ST_REJECTED = "rejected"
ST_CANCELLED = "cancelled"
ST_FAILED = "failed"
ST_LOST = "lost"

#: `from -> allowed next`. Everything absent is terminal.
LEGAL = {
    ST_CREATED: {ST_WORKTREE_READY, ST_FAILED},
    ST_WORKTREE_READY: {ST_RUNNING, ST_FAILED},
    ST_RUNNING: {ST_EXITED, ST_CANCELLED, ST_FAILED, ST_LOST},
    ST_EXITED: {ST_RESULT_READY, ST_FAILED, ST_CANCELLED},
    ST_RESULT_READY: {ST_ACCEPTED, ST_REJECTED},
}

#: A task in one of these still owns a live process or a pending measurement.
ACTIVE = (ST_CREATED, ST_WORKTREE_READY, ST_RUNNING, ST_EXITED)

CANCEL_NONE = "none"
CANCEL_REQUESTED = "requested"
CANCEL_KILLED = "killed"
CANCEL_FAILED = "failed"

MAX_PROMPT_CHARS = 8000

TASKS_SUBDIR = "orchestration"

#: A task record's file name. The folder also holds other files named after a
#: task (`<id>.mcp.json`, `<id>.out`, `<id>.err`); only THIS shape is a record.
#: Matching on ".json" alone counted the MCP config as a damaged record.
_RECORD_NAME = re.compile(r"t[0-9a-f]{12}\.json")

#: Serialises read-modify-write inside this process. Cross-process writers do not
#: exist: the CLI only reads.
_LOCK = threading.RLock()


class TaskError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class TaskList:
    tasks: tuple = ()
    #: Files that are not valid task records. Counted, never skipped silently: a
    #: list that looks complete while missing a task is the failure this prevents.
    bad_files: int = 0
    error: str = ""


def tasks_dir(project: str, git_exe: str = "") -> str:
    return os.path.join(os.path.dirname(handoff_ledger.ledger_path(project, git_exe)),
                        TASKS_SUBDIR)


def task_path(project: str, task_id: str, git_exe: str = "") -> str:
    return os.path.join(tasks_dir(project, git_exe), task_id + ".json")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_task(*, project: str, agent: str, prompt: str, model: str = "",
             mode: str = MODE_WRITE, kind: str = KIND_IMPLEMENT,
             parent_task_id: str = "", now: "str | None" = None,
             nonce: "str | None" = None) -> dict:
    """A fresh record in `created`. Pure: nothing is written."""
    from helpers.agent_handoff import redact
    created = now or _now()
    nonce = nonce if nonce is not None else os.urandom(6).hex()
    task_id = "t" + hashlib.sha1(
        f"{project}|{kind}|{created}|{nonce}".encode("utf-8")).hexdigest()[:12]
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "kind": kind,
        "parent_task_id": parent_task_id,
        "project_root": project,
        "worktree_root": "",
        "branch": "",
        "base_commit": "",
        "agent": agent,
        "model": model,
        "execution_mode": mode,
        "prompt": redact(" ".join(str(prompt).split()))[:MAX_PROMPT_CHARS],
        "status": ST_CREATED,
        "created_at": created,
        "started_at": "",
        "ended_at": "",
        "pid": 0,
        "pid_started_at": "",
        "exit_code": None,
        "result_path": "",
        "stderr_path": "",
        "session_id": "",
        "diff": None,
        "review_status": "none",
        "cancel_state": CANCEL_NONE,
        "usage": "unknown",
        "worktree_removed": False,
        "error": "",
    }


def _write_atomic(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save(project: str, record: dict, git_exe: str = "") -> "tuple[bool, str]":
    """Write a record. `(ok, error)`; never raises."""
    try:
        with _LOCK:
            _write_atomic(task_path(project, record["task_id"], git_exe), record)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return False, f"could not write the task record: {exc}"
    return True, ""


def load(project: str, task_id: str, git_exe: str = "") -> "tuple[dict | None, str]":
    """`(record, "")`, or `(None, why)`. A missing file and a damaged one differ."""
    path = task_path(project, task_id, git_exe)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None, f"no task {task_id}"
    except (OSError, ValueError) as exc:
        return None, f"task {task_id} could not be read: {exc}"
    if not isinstance(record, dict) or record.get("task_id") != task_id:
        return None, f"task {task_id} is not a valid record"
    return record, ""


def list_tasks(project: str, git_exe: str = "") -> TaskList:
    """Every task of a project, oldest first. Never raises."""
    folder = tasks_dir(project, git_exe)
    if not os.path.isdir(folder):
        return TaskList()
    try:
        names = sorted(n for n in os.listdir(folder) if _RECORD_NAME.fullmatch(n))
    except OSError as exc:
        return TaskList(error=f"could not list {folder}: {exc}")
    tasks, bad = [], 0
    for name in names:
        record, _why = load(project, name[:-5], git_exe)
        if record is None:
            bad += 1
        else:
            tasks.append(record)
    tasks.sort(key=lambda r: (r.get("created_at", ""), r.get("task_id", "")))
    return TaskList(tuple(tasks), bad)


def transition(project: str, task_id: str, to: str, git_exe: str = "",
               **fields) -> "tuple[dict | None, str]":
    """Move a task to `to`, applying `fields`, atomically. `(record, why)`.

    Refuses an illegal move and a task that is not there. `ended_at` is stamped
    on reaching a state nothing can leave, unless the caller supplied one.
    """
    with _LOCK:
        record, why = load(project, task_id, git_exe)
        if record is None:
            return None, why
        here = record.get("status")
        if to not in LEGAL.get(here, ()):
            return None, f"a task that is {here} cannot become {to}"
        record.update(fields)
        record["status"] = to
        if to not in LEGAL and not record.get("ended_at"):
            record["ended_at"] = _now()
        ok, err = save(project, record, git_exe)
        return (record, "") if ok else (None, err)


def update(project: str, task_id: str, git_exe: str = "",
           **fields) -> "tuple[dict | None, str]":
    """Change fields WITHOUT changing status (pid, paths, usage)."""
    with _LOCK:
        record, why = load(project, task_id, git_exe)
        if record is None:
            return None, why
        if "status" in fields:
            return None, "status changes go through transition()"
        record.update(fields)
        ok, err = save(project, record, git_exe)
        return (record, "") if ok else (None, err)


def reconcile(project: str, alive, git_exe: str = "") -> list:
    """Mark `running` tasks whose process is gone as `lost`. Returns their ids.

    `alive(pid, pid_started_at)` answers True, False or None. Only a definite
    False loses a task: None ("could not tell") leaves it running, because
    calling a live task lost would let the person start a second one over it.
    A recycled pid is the reason the start time is part of the question.
    """
    lost = []
    for record in list_tasks(project, git_exe).tasks:
        if record.get("status") != ST_RUNNING:
            continue
        verdict = alive(int(record.get("pid") or 0), str(record.get("pid_started_at") or ""))
        if verdict is False:
            done, _ = transition(project, record["task_id"], ST_LOST, git_exe,
                                 error="the process is gone but the task never "
                                       "recorded how it ended")
            if done:
                lost.append(record["task_id"])
    return lost
