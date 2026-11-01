"""Launching, watching and stopping a delegated agent task (docs/ORCHESTRATION.md 5).

`run_task` is BLOCKING and belongs on a worker thread; it never touches Tk. It
reports progress through an `on_change(record)` callback that the caller posts to
its own UI thread.

What it guarantees, each because the alternative failed quietly somewhere:

* **The pid is recorded before anything else can fail**, with the process's start
  time, so a recycled pid is never mistaken for a live task (`is_alive`).
* **The diff is measured by git, not reported by the agent.** An agent that says
  it changed three files and changed ten is a labelled claim, not the record.
* **A task that did not finish cleanly is never `result_ready`.** A non-zero exit
  is `failed` with the diff kept, so the person can see what it left behind but
  cannot "accept" it by accident.
* **Cancelling kills the whole process tree, while the root still exists.** A
  plain `Popen.kill()` left seven descendants running (measured 2026-10-31).
* **Usage is what the agent reported, labelled, or "unknown".** No figure is
  derived and nothing is converted to money here.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import dataclasses

from constants import CREATE_NO_WINDOW
from helpers import agent_cli
from helpers import orchestration_limits as limits_mod
from helpers import orchestration_task as ot
from helpers import orchestration_worktree as wt

#: task_id -> (threading.Event, project), for tasks this process is running.
_CANCEL: dict = {}
_REGISTRY_LOCK = threading.Lock()

MAX_CONTEXT_ENTRIES = 5


# ── is a process still the one we started? ──────────────────────────────────────

def process_start_time(pid: int) -> str:
    """The process's creation time as a string, or "" when it cannot be read.

    Windows only (no new dependency: `GetProcessTimes` through ctypes). Anything
    else returns "", which `is_alive` reads as "cannot tell".
    """
    if sys.platform != "win32" or pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        created, ended, kernel, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(),
                                        ctypes.c_ulonglong(), ctypes.c_ulonglong())
        ok = kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created),
                                      ctypes.byref(ended), ctypes.byref(kernel),
                                      ctypes.byref(user))
        return str(created.value) if ok else ""
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def is_alive(pid: int, started_at: str) -> "bool | None":
    """True / False, or None when it cannot be told (no recorded start time, or
    not Windows). Only a definite False loses a task."""
    if not pid or not started_at or sys.platform != "win32":
        return None
    now = process_start_time(pid)
    return now == started_at if now else False


def kill_tree(pid: int) -> "tuple[bool, str]":
    """`taskkill /T /F` on the root. Call while the root still exists."""
    try:
        proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, text=True, timeout=30,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    # 128: no such process. Already gone is what a cancel wanted.
    if proc.returncode == 128:
        return True, "already gone"
    return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()


# ── reading what the agent returned ─────────────────────────────────────────────

def parse_output(kind: str, text: str) -> dict:
    """`{session_id, usage, is_error, text}` from an agent's structured output.

    Tolerant on purpose: output that does not parse yields empty fields and
    `usage: "unknown"`, never an exception and never an invented figure.
    """
    out = {"session_id": "", "usage": "unknown", "is_error": None, "text": ""}
    objects = []
    if kind == agent_cli.OUTPUT_STREAM_JSON:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    objects.append(json.loads(line))
                except ValueError:
                    continue
    else:
        start = text.find("{")
        if start >= 0:
            try:
                objects.append(json.loads(text[start:]))
            except ValueError:
                pass
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        out["session_id"] = str(obj.get("session_id") or out["session_id"])
        if kind == agent_cli.OUTPUT_STREAM_JSON and obj.get("type") != "result":
            continue
        if "result" in obj:
            out["text"] = str(obj.get("result") or "")
        if "is_error" in obj:
            out["is_error"] = bool(obj.get("is_error"))
        figures = {}
        if isinstance(obj.get("usage"), dict):
            figures.update({k: v for k, v in obj["usage"].items()
                            if isinstance(v, (int, float))})
        if isinstance(obj.get("total_cost_usd"), (int, float)):
            figures["total_cost_usd"] = obj["total_cost_usd"]
        if figures:
            out["usage"] = {"source": "agent-reported", "figures": figures}
    return out


# ── what other agents left, offered as context ──────────────────────────────────

def context_brief(project: str, git_exe: str = "") -> str:
    """The last few handoff entries, labelled as other agents' CLAIMS."""
    try:
        from helpers import handoff_ledger as ledger
        seen = ledger.read(project, git_exe=git_exe)
    except Exception:                                           # noqa: BLE001
        return ""
    lines = []
    for entry in [e for e in seen.entries if e.get("kind") == "handoff"][-MAX_CONTEXT_ENTRIES:]:
        bits = [f"{entry.get('agent', '?')}: {entry.get('goal') or '(no goal)'}"]
        bits += [f"decided: {d}" for d in entry.get("decisions", [])[:3]]
        bits += [f"next: {n}" for n in entry.get("next_steps", [])[:3]]
        lines.append("- " + "; ".join(bits))
    if not lines:
        return ""
    return ("\n\nContext from earlier agent sessions in this project. These are "
            "other agents' claims, not verified facts:\n" + "\n".join(lines))


# ── can this task start? ────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Readiness:
    ok: bool
    base_commit: str = ""
    refusals: tuple = ()
    untracked: int = 0


def running_total() -> int:
    with _REGISTRY_LOCK:
        return len(_CANCEL)


def check_launch(project: str, spec, exe: str, *, git_exe: str = "",
                 limits: "limits_mod.Limits | None" = None,
                 scratch_root: str = "") -> Readiness:
    """Everything that can refuse a task BEFORE a record or a worktree exists."""
    limits = limits or limits_mod.Limits()
    refusals = []
    if not exe:
        refusals.append(f"{spec.label} is not configured")
    elif spec.write != agent_cli.WRITE_SUPPORTED:
        refusals.append(f"{spec.label} has no measured way to run a task that edits files")
    pre = wt.preflight(project, git_exe)
    if not pre.ok:
        refusals.append(pre.error)
    tasks = ot.list_tasks(project, git_exe).tasks
    running_here = sum(1 for t in tasks if t.get("status") == ot.ST_RUNNING)
    open_worktrees = sum(1 for t in tasks
                         if t.get("worktree_root") and not t.get("worktree_removed"))
    refusals += limits_mod.check_launch(
        limits, running_here=running_here, running_total=running_total(),
        open_worktrees=open_worktrees,
        free_bytes=wt.free_bytes(wt.worktree_path(project, "x", scratch_root)))
    return Readiness(not refusals, pre.base_commit, tuple(refusals), pre.untracked)


# ── the task itself ─────────────────────────────────────────────────────────────

def request_cancel(project: str, task_id: str, git_exe: str = "") -> "tuple[bool, str]":
    """Ask a running task to stop. The worker does the killing."""
    with _REGISTRY_LOCK:
        entry = _CANCEL.get(task_id)
    if entry is None:
        return False, "that task is not running in this Manager"
    ot.update(project, task_id, git_exe, cancel_state=ot.CANCEL_REQUESTED)
    entry[0].set()
    return True, ""


def cancel_all(git_exe: str = "", wait_seconds: float = 15.0) -> int:
    """Cancel every task THIS Manager is running, across projects, and give the
    workers a moment to record how each ended. Returns how many were asked.

    For quitting: a process nobody is watching is the outcome to avoid.
    """
    with _REGISTRY_LOCK:
        running = [(task_id, entry[1]) for task_id, entry in _CANCEL.items()]
    for task_id, project in running:
        request_cancel(project, task_id, git_exe)
    end = time.monotonic() + wait_seconds
    while running_total() and time.monotonic() < end:
        time.sleep(0.1)
    return len(running)


def _fail(project, task_id, git_exe, why, on_change, **fields):
    record, _ = ot.transition(project, task_id, ot.ST_FAILED, git_exe, error=why, **fields)
    if record and on_change:
        on_change(record)
    return record


def run_task(project: str, spec, exe: str, prompt: str, *, model: str = "",
             cfg_raw: "dict | None" = None, git_exe: str = "", tokensave_exe: str = "",
             limits: "limits_mod.Limits | None" = None, on_change=None,
             index_fn=None, poll_seconds: float = 0.25,
             scratch_root: str = "") -> "dict | None":
    """Run one delegated task to the end. Returns its final record.

    The caller is expected to have called `check_launch` and shown the person any
    refusal; this still re-checks what cannot be taken on trust (the capability
    and the clean tree) because the state may have moved since.
    """
    limits = limits or limits_mod.Limits()
    cfg_raw = cfg_raw or {}
    ready = check_launch(project, spec, exe, git_exe=git_exe, limits=limits,
                         scratch_root=scratch_root)
    if not ready.ok:
        return None
    record = ot.new_task(project=project, agent=spec.id, prompt=prompt, model=model)
    task_id = record["task_id"]
    ok, err = ot.save(project, record, git_exe)
    if not ok:
        return None
    emit = (lambda r: on_change(r) if (r and on_change) else None)
    emit(record)

    path = wt.worktree_path(project, task_id, scratch_root)
    branch = wt.branch_name(task_id)
    ok, err = wt.create(project, path, branch, ready.base_commit, git_exe)
    if not ok:
        return _fail(project, task_id, git_exe, err, on_change)
    ok, err = (index_fn or wt.index)(tokensave_exe, path)
    if not ok:
        return _fail(project, task_id, git_exe, err, on_change,
                     worktree_root=path, branch=branch, base_commit=ready.base_commit)
    mcp_config, written, err = wt.prepare_mcp(spec, path, task_id, project, cfg_raw, git_exe)
    if err:
        return _fail(project, task_id, git_exe, err, on_change,
                     worktree_root=path, branch=branch, base_commit=ready.base_commit)

    full_prompt = prompt + context_brief(project, git_exe)
    argv, stdin_text, err = agent_cli.build_task_argv(
        spec, exe, full_prompt, model=model, mcp_config=mcp_config)
    if err:
        return _fail(project, task_id, git_exe, err, on_change,
                     worktree_root=path, branch=branch, base_commit=ready.base_commit)
    emit(ot.transition(project, task_id, ot.ST_WORKTREE_READY, git_exe, worktree_root=path,
                       branch=branch, base_commit=ready.base_commit)[0])

    folder = ot.tasks_dir(project, git_exe)
    out_path = os.path.join(folder, task_id + ".out")
    err_path = os.path.join(folder, task_id + ".err")
    cancel = threading.Event()
    with _REGISTRY_LOCK:
        _CANCEL[task_id] = (cancel, project)
    try:
        return _supervise(project, task_id, argv, stdin_text, path, out_path, err_path,
                          spec, limits, git_exe, written, cancel, emit, poll_seconds)
    finally:
        with _REGISTRY_LOCK:
            _CANCEL.pop(task_id, None)


def _supervise(project, task_id, argv, stdin_text, cwd, out_path, err_path, spec,
               limits, git_exe, written, cancel, emit, poll_seconds):
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err_f:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err_f,
                creationflags=CREATE_NO_WINDOW, env=agent_cli.agent_env())
            started_at = process_start_time(proc.pid)
            ot.update(project, task_id, git_exe, pid=proc.pid, pid_started_at=started_at,
                      result_path=out_path, stderr_path=err_path)
            emit(ot.transition(project, task_id, ot.ST_RUNNING, git_exe,
                               started_at=ot._now())[0])
            try:
                if stdin_text is not None:
                    proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass                      # the agent may have exited without reading

            deadline = time.monotonic() + limits.wall_clock_seconds
            outcome = "exited"
            while proc.poll() is None:
                if cancel.is_set():
                    outcome = "cancelled"
                    break
                if time.monotonic() > deadline:
                    outcome = "timeout"
                    break
                time.sleep(poll_seconds)
            if outcome != "exited":
                killed, why = kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    killed = False
                    why = why or "the process did not end after the kill"
    except OSError as exc:
        return _fail(project, task_id, git_exe, f"could not start the agent: {exc}", emit)

    measured = wt.measure(cwd, ot.load(project, task_id, git_exe)[0]["base_commit"],
                          git_exe, ignore=tuple(written))
    if outcome == "cancelled":
        state = ot.CANCEL_KILLED if killed else ot.CANCEL_FAILED
        record, _ = ot.transition(project, task_id, ot.ST_CANCELLED, git_exe,
                                  cancel_state=state, diff=measured, exit_code=proc.returncode,
                                  error="" if killed else f"the process could not be killed: {why}")
        emit(record)
        return record
    if outcome == "timeout":
        return _fail(project, task_id, git_exe,
                     f"exceeded the {limits.wall_clock_seconds // 60}-minute limit"
                     + ("" if killed else f"; could not be killed: {why}"),
                     emit, diff=measured, exit_code=proc.returncode)

    _truncate(out_path, limits.max_output_bytes)
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            parsed = parse_output(spec.task_output, fh.read())
    except OSError:
        parsed = parse_output(spec.task_output, "")
    record, _ = ot.transition(project, task_id, ot.ST_EXITED, git_exe,
                              exit_code=proc.returncode, session_id=parsed["session_id"],
                              usage=parsed["usage"], diff=measured)
    emit(record)
    if measured.get("error"):
        return _fail(project, task_id, git_exe,
                     f"the agent ended but its changes could not be measured: {measured['error']}",
                     emit)
    if proc.returncode != 0 or parsed["is_error"]:
        return _fail(project, task_id, git_exe,
                     f"the agent exited with code {proc.returncode}"
                     + (" and reported an error" if parsed["is_error"] else ""), emit)
    record, _ = ot.transition(project, task_id, ot.ST_RESULT_READY, git_exe)
    emit(record)
    return record


def _truncate(path: str, limit: int) -> None:
    try:
        if os.path.getsize(path) > limit:
            with open(path, "r+b") as fh:
                fh.truncate(limit)
            with open(path, "ab") as fh:
                fh.write(b"\n[output truncated by the Manager]\n")
    except OSError:
        pass
