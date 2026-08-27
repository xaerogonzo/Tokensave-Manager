"""helpers/test_lock.py — one test run per project at a time.

`test-run` is reachable from an editor button, so two clicks a second apart
would otherwise start two full pytest suites over the same tree. The extension
disables its own command while a run is in flight, but the CLI cannot rely on
its callers behaving: a lock here is the thing that actually holds.

**An OS-level file lock, not a PID file.** The tempting design records the
owner's pid and checks whether it is still alive, which needs two things this
codebase cannot have cheaply:

  * a portable liveness probe — `os.kill(pid, 0)` is the POSIX idiom, but on
    Windows signal 0 is `CTRL_C_EVENT`, so that call *signals* the process
    instead of probing it;
  * process start time, to tell a live owner from a recycled pid — obtainable
    only through `/proc` or `GetProcessTimes`, or a third-party dependency this
    project does not take.

An advisory lock on an open descriptor sidesteps both. The kernel releases it
when the owning process exits **however it exits**, so a crashed run leaves no
stale lock to reason about and there is no identity question to get wrong.

The pid and timestamp still get written into the file, but only so the refusal
message can name who holds it. They are diagnostics, never the decision.

Stdlib only, and the platform-specific modules are imported inside the branch
that uses them — `src/` may not import anything conditional at module scope.
"""
from __future__ import annotations

import contextlib
import json
import os
import time

#: Lives beside the Manager's other per-project state. Already project-scoped
#: by construction; `lock_path` canonicalises the root so two spellings of one
#: tree (a symlink, a mapped drive, differing case on Windows) share a lock and
#: two genuinely different trees never do.
_LOCK_DIRNAME = ".tokensave-manager"
_LOCK_FILENAME = "test-run.lock"


class TestRunBusy(RuntimeError):
    """Another test run holds the lock for this project."""


def lock_path(project_root: str) -> str:
    """Absolute path of the lock file for *project_root*, canonicalised."""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
    return os.path.join(root, _LOCK_DIRNAME, _LOCK_FILENAME)


def _acquire(fd: int) -> bool:
    """Take an exclusive, non-blocking advisory lock. False if already held."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                      # closing the fd releases it regardless


def read_holder(project_root: str) -> "dict | None":
    """Who claims to hold the lock, for the refusal message. Never raises.

    Advisory only: the file can outlive its lock, so a holder record here does
    NOT mean a run is in progress. `test_run_lock` decides that.
    """
    try:
        with open(lock_path(project_root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def test_run_lock(project_root: str):
    """Hold the project's test-run lock, or raise `TestRunBusy`.

    Raises rather than blocking: a caller that wanted to wait would be holding
    an editor command open for the length of a suite.
    """
    path = lock_path(project_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        # An unwritable project should not silently run two suites; refuse.
        raise TestRunBusy(f"could not take the test-run lock: {exc}") from exc

    if not _acquire(fd):
        os.close(fd)
        holder = read_holder(project_root) or {}
        pid = holder.get("pid")
        since = holder.get("started_at")
        detail = f" (pid {pid})" if pid else ""
        if since:
            detail += f", started {int(time.time() - since)}s ago"
        raise TestRunBusy(f"a test run is already in progress for this "
                          f"project{detail}")

    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "started_at": time.time(),
            "project": os.path.dirname(os.path.dirname(path)),
        }).encode("utf-8"))
        yield
    finally:
        _release(fd)
        os.close(fd)
        # Best-effort tidy. Leaving it behind is harmless — the lock is held on
        # the descriptor, not on the file existing.
        with contextlib.suppress(OSError):
            os.remove(path)
