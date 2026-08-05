"""worktree_health — detect + repair git worktrees with no tokensave index.

tokensave resolves its project by searching UPWARD from cwd for `.tokensave/`.
A fresh git worktree has none, so a Claude Code session started inside one
gets confident, well-formed answers about a DIFFERENT checkout — the search
walks up past the worktree boundary and finds a sibling project's index.
tokensave itself detects this precisely (a `worktree_mismatch` block on every
call) but still answers anyway. This module removes the condition instead of
re-detecting it.

Two entry points mirror the two places this gets checked:
  * `find_orphaned_worktrees_for_project` — one project, used by Doctor
    (an on-demand, user-triggered check).
  * `find_orphaned_worktrees` — every known project, used by the Manager's
    startup sweep.

Both build on `helpers.claude_tasks.scan_worktrees`, already used by the Tasks
tab — this module does not re-implement `git worktree list --porcelain`
parsing.

`repair_worktree_index` is the sharpest edge here: `tokensave init` on an
already-initialized project can no-op without that being obvious from the
exit code alone (behavior observed to vary between interactive/TTY and piped
invocation). This module never trusts the exit code to mean "a rebuild
happened" — it decides `init` vs `sync --force` itself, from `.tokensave/`
directory existence, BEFORE running anything.
"""

from __future__ import annotations

import os
import subprocess
from typing import Tuple

try:
    from constants import CREATE_NO_WINDOW
except ImportError:
    CREATE_NO_WINDOW = 0

from helpers.claude_tasks import scan_worktrees

_REPAIR_TIMEOUT = 300   # a full index can take a while on a large project


def _has_own_index(worktree_path: str) -> bool:
    return os.path.isdir(os.path.join(worktree_path, ".tokensave"))


def find_orphaned_worktrees_for_project(project_path: str, git_exe: str) -> list:
    """Worktrees of ONE project that have no `.tokensave/` of their own.

    Each entry: ``{"worktree_path": str, "branch": str, "head": str}``.
    Empty list on any error (fail-open — `scan_worktrees` already does this).
    """
    return [
        {"worktree_path": wt["path"], "branch": wt["branch"], "head": wt["head"]}
        for wt in scan_worktrees(project_path, git_exe)
        if not _has_own_index(wt["path"])
    ]


def find_orphaned_worktrees(projects: list, git_exe: str) -> list:
    """Orphaned worktrees across EVERY known project (the startup-sweep shape).

    Each entry adds ``project_path`` / ``project_name`` to the per-project
    shape above. Only projects with a `.git` are worth asking `git worktree
    list` about at all.
    """
    out: list = []
    for proj in projects:
        if not proj.get("has_git"):
            continue
        for orphan in find_orphaned_worktrees_for_project(proj["path"], git_exe):
            out.append({
                **orphan,
                "project_path": proj["path"],
                "project_name": proj.get("name") or os.path.basename(proj["path"]),
            })
    return out


def repair_worktree_index(tokensave_exe: str, worktree_path: str) -> Tuple[bool, str, str]:
    """Give a worktree its own tokensave index. Returns (ok, action, detail).

    ``action`` is always ``"init"`` or ``"sync --force"`` — decided by
    checking `.tokensave/` existence FIRST, never by interpreting what `init`
    happened to exit with. That directory check is the only thing this
    function trusts.
    """
    if not tokensave_exe or not os.path.isfile(tokensave_exe):
        return False, "", "tokensave is not installed"

    if _has_own_index(worktree_path):
        argv = [tokensave_exe, "sync", "--force", worktree_path]
        action = "sync --force"
    else:
        argv = [tokensave_exe, "init", worktree_path]
        action = "init"

    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=_REPAIR_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, action, f"timed out after {_REPAIR_TIMEOUT}s"
    except OSError as exc:
        return False, action, f"could not launch tokensave: {exc}"

    if proc.returncode == 0:
        return True, action, (proc.stdout or "").strip()
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
    return False, action, detail[:300] or f"exit {proc.returncode}"
