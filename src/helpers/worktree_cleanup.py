"""worktree_cleanup — removing a worktree, and surviving the half-state.

``git worktree remove`` does not fail atomically. When the directory cannot be
deleted — because something holds a file open — git **still deregisters the
worktree**: it disappears from ``git worktree list`` and its metadata under
``.git/worktrees/`` is pruned, while the directory stays on disk. Observed
end-to-end on Windows on 2026-08-19.

Two consequences the UI has to get right, because guessing produces advice
that cannot work:

* **"Try again" is wrong.** The git command has nothing left to do. Worse, the
  leftover directory is no longer a valid worktree — its ``.git`` file points
  at pruned metadata — so git commands run inside it fail in confusing ways.
* **The leftover directory is not disposable.** It usually contains exactly
  the uncommitted work that caused the delete to fail in the first place.

## Observation over interpretation

The return code and stderr describe what git *tried*. Whether the worktree is
still registered is a fact, and it is cheap to look up. So this module always
re-lists after a failed removal, and the state machine runs on that:

    remove -> re-list -> registered? -> inspect directory -> classify

``lock_kind`` is parsed from stderr and is **evidence only** — a hint for the
user about which lock holder to go after, never a decision input. The two
holders behave differently and are fixed differently:

* ``tokensave_db`` — a ``tokensave serve`` process holding
  ``.tokensave/tokensave.db``. Fixable: identify and stop it (see
  ``helpers/tokensave_daemon.py``).
* ``worktree_directory`` — a live session's own working directory. Not
  fixable from inside that session; it clears when the session exits.

A useful signal for the user: after stopping a daemon, the error *changes*
from naming the ``.db`` to naming the bare directory. That shift is how you
know the database lock actually released.

No Tk here — plain data out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

_GIT_TIMEOUT = 15

LOCK_NONE = "none"
LOCK_TOKENSAVE_DB = "tokensave_db"
LOCK_WORKTREE_DIRECTORY = "worktree_directory"
LOCK_UNKNOWN = "unknown"

# Directories whose contents are noise for a "has this changed?" signature:
# a background indexer touching its own database must not look like the user
# editing their work.
_SIGNATURE_SKIP_DIRS = {".git", ".tokensave", ".codegraph", "__pycache__",
                        "node_modules", ".venv", "venv"}


@dataclass(frozen=True)
class DirectorySignature:
    """A cheap fingerprint, for spotting change between inspect and delete.

    Not a hash of the contents — walking a large worktree twice is already the
    cost ceiling here. Count, total size and the newest mtime together catch
    the case that matters: the user carried on working in the leftover
    directory after git deregistered it.
    """
    path: str
    file_count: int
    total_bytes: int
    latest_mtime: float

    def matches(self, other: "DirectorySignature | None") -> bool:
        if other is None:
            return False
        return (self.file_count == other.file_count
                and self.total_bytes == other.total_bytes
                # mtimes survive a copy at differing precision; a second of
                # slack avoids refusing a delete over filesystem rounding.
                and abs(self.latest_mtime - other.latest_mtime) < 1.0)


@dataclass(frozen=True)
class WorktreeRemoveResult:
    """What actually happened, separated from what git said about it."""
    success: bool
    deregistered: bool          # observed by re-listing, NOT from the exit code
    directory_exists: bool
    stderr: str = ""
    lock_kind: str = LOCK_NONE
    signature: "DirectorySignature | None" = None

    @property
    def is_half_state(self) -> bool:
        """Git let go, the filesystem did not. The case the old code missed."""
        return self.deregistered and self.directory_exists

    @property
    def retry_would_help(self) -> bool:
        """Only true while git still has something left to do."""
        return not self.deregistered


# ── removal ───────────────────────────────────────────────────────────────

def remove_worktree(git_exe: str, main_repo: str, worktree_path: str,
                    *, force: bool = False) -> WorktreeRemoveResult:
    """Run ``git worktree remove`` and then observe what is actually true."""
    cmd = [git_exe, "-C", main_repo, "worktree", "remove", worktree_path]
    if force:
        cmd.append("--force")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT,
                              encoding="utf-8", errors="replace")
        rc, stderr = proc.returncode, (proc.stderr or proc.stdout or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        rc, stderr = 1, str(exc)

    exists = os.path.isdir(worktree_path)
    if rc == 0 and not exists:
        return WorktreeRemoveResult(success=True, deregistered=True,
                                    directory_exists=False)

    # The load-bearing step: ask git what it now believes, rather than
    # inferring it from how the command exited.
    deregistered = not is_registered(git_exe, main_repo, worktree_path)
    return WorktreeRemoveResult(
        success=(rc == 0 and not exists),
        deregistered=deregistered,
        directory_exists=exists,
        stderr=stderr.strip(),
        lock_kind=classify_lock(stderr),
        signature=directory_signature(worktree_path) if exists else None,
    )


def is_registered(git_exe: str, main_repo: str, worktree_path: str) -> bool:
    """Does git still list *worktree_path* as a worktree of *main_repo*?"""
    try:
        proc = subprocess.run(
            [git_exe, "-C", main_repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            # Cannot tell. Report "still registered" so callers stay on the
            # cautious branch and never offer to delete on a failed lookup.
            return True
    except (OSError, subprocess.TimeoutExpired):
        return True
    target = _norm(worktree_path)
    return any(_norm(line[9:].strip()) == target
               for line in (proc.stdout or "").splitlines()
               if line.startswith("worktree "))


def classify_lock(stderr: str) -> str:
    """Name the likely lock holder. EVIDENCE ONLY — never a decision input."""
    text = (stderr or "").lower()
    if not text:
        return LOCK_NONE
    if "tokensave.db" in text or ".tokensave" in text:
        return LOCK_TOKENSAVE_DB
    if any(sign in text for sign in
           ("permission denied", "being used by another process",
            "resource busy", "access is denied", "failed to delete")):
        return LOCK_WORKTREE_DIRECTORY
    return LOCK_UNKNOWN


# ── the leftover directory ────────────────────────────────────────────────

def directory_signature(path: str) -> "DirectorySignature | None":
    """Fingerprint *path*, or None if it cannot be walked."""
    if not os.path.isdir(path):
        return None
    count = total = 0
    latest = 0.0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SIGNATURE_SKIP_DIRS]
            for name in files:
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue          # vanished mid-walk; not worth failing over
                count += 1
                total += st.st_size
                latest = max(latest, st.st_mtime)
    except OSError:
        return None
    return DirectorySignature(path=path, file_count=count,
                              total_bytes=total, latest_mtime=latest)


def delete_orphan_directory(git_exe: str, main_repo: str, path: str,
                            expected: "DirectorySignature | None"
                            ) -> "tuple[bool, str]":
    """Delete a deregistered leftover directory, re-verifying all of it first.

    Every precondition is rechecked here rather than trusted from whenever the
    dialog last looked, because "git no longer knows about it" is not the same
    as "it is safe to destroy". Refuses when:

      * git still lists it as a worktree — removal is the right tool, not rmtree
      * it is gone already — nothing to do, and not an error
      * its contents changed since inspection — the user is probably still
        working in there
    """
    if is_registered(git_exe, main_repo, path):
        return False, ("git still lists this as a worktree — remove it with "
                       "git rather than deleting the directory")
    if not os.path.isdir(path):
        return True, "already gone"
    if expected is not None:
        actual = directory_signature(path)
        if actual is None:
            return False, "could not re-read the directory to verify it"
        if not actual.matches(expected):
            return False, (
                "the leftover directory changed since it was inspected "
                "(%d files, %s -> %d files, %s) — re-scan before deleting"
                % (expected.file_count, human_size(expected.total_bytes),
                   actual.file_count, human_size(actual.total_bytes)))
    try:
        shutil.rmtree(path, onerror=_clear_readonly)
    except OSError as exc:
        return False, "could not delete: %s" % exc
    return (not os.path.isdir(path)), ("deleted" if not os.path.isdir(path)
                                       else "some files could not be deleted")


def _clear_readonly(func, path, _exc):
    """rmtree callback: git objects are read-only on Windows."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


# ── small helpers ─────────────────────────────────────────────────────────

def _norm(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except (OSError, ValueError):
        return os.path.normcase(path)


def human_size(size: int) -> str:
    """Readable byte count, for telling the user what is at stake."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.0f %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f GB" % value
