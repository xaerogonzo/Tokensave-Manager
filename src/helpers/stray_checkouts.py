"""helpers/stray_checkouts.py -- extra checkouts of a project that nothing tracks.

WHY THIS EXISTS. An agent that needs the suite run at another commit makes a
second working copy. Measured on OpenChem Studio: two ``git worktree add``
checkouts loose at ``D:\\`` (``ocs-master``, ``ocs-s5``, both detached HEAD),
eighteen pytest logs beside them, and none of it discoverable afterwards.
Nothing removes them, each is a full copy plus a ``.venv``, and until Doctor
notices, a session started in one is answered from the ORIGINAL checkout's
graph. ``worktree_health`` already repairs a worktree's missing index; this is
the other half: where the copies ARE, and whether anyone knows.

WHAT IT CAN AND CANNOT SEE. ``git worktree list`` is authoritative for
*registered* worktrees, so those are reported with certainty. A copy made with
``cp -r`` or a fresh ``git clone`` is not registered with anything, and git
cannot list it. The only such copies visible from here are the ones sitting in
the one folder the baseline rule names (``<drive>:\\_scratch``), so that folder
is listed and everything else is stated to be out of reach -- "found none"
would be a claim this module cannot make.

Deliberately NOT measured: size. ``du`` over one drive-root worktree that
carries a ``.venv`` did not finish inside two minutes, and Doctor must not
stall on it. Folder age is the folder's own mtime, which moves when a
top-level entry is added, so it is a hint and is labelled one.

Never claims a checkout is abandoned: a detached worktree at another commit
may be a suite still running. It reports the facts and the way out.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from helpers.claude_tasks import _parse_worktrees_porcelain

SCRATCH_DIRNAME = "_scratch"
_GIT_TIMEOUT_S = 15

NOT_APPLICABLE = "not_applicable"   # the project is not a git repository
CLEAR = "clear"                     # every registered worktree is sanctioned
STRAY = "stray"                     # at least one is somewhere unsanctioned
UNKNOWN = "unknown"                 # git could not be asked


@dataclass(frozen=True)
class Checkout:
    path: str
    head: str
    branch: str          # "" = detached HEAD
    exists: bool         # False = registered, but the folder is gone
    has_index: bool
    modified: "float | None"


@dataclass
class StrayReport:
    state: str
    reason: str = ""
    scratch_root: str = ""
    strays: "list[Checkout]" = field(default_factory=list)
    #: entries in the scratch folder that no worktree registration explains
    unregistered: "list[str]" = field(default_factory=list)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def default_scratch_root(project_path: str) -> str:
    """``<drive>:\\_scratch`` on the project's own drive."""
    drive, _ = os.path.splitdrive(os.path.abspath(project_path))
    return os.path.join(drive + os.sep, SCRATCH_DIRNAME)


def _inside(path: str, root: str) -> bool:
    root_n = _norm(root)
    return _norm(path).startswith(root_n + os.sep)


def _list_worktrees(project_path: str,
                    git_exe: str) -> "tuple[list[dict], str]":
    """``(worktrees, problem)`` -- unlike ``scan_worktrees``, errors are said.

    ``scan_worktrees`` returns ``[]`` on any failure, which would make "could
    not ask git" read as "no extra checkouts". Only the parsing is shared.
    """
    from constants import CREATE_NO_WINDOW

    if not git_exe:
        return [], "no git executable is configured"
    try:
        out = subprocess.run(
            [git_exe, "-C", project_path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], "could not run git (%s)" % exc
    if out.returncode != 0:
        msg = (out.stderr or "").strip()[:120]
        return [], "git worktree list failed (%s)" % (msg or out.returncode)
    return _parse_worktrees_porcelain(out.stdout), ""


def _facts(wt: dict) -> Checkout:
    path = os.path.normpath(wt["path"])
    exists = os.path.isdir(path)
    try:
        modified = os.path.getmtime(path) if exists else None
    except OSError:
        modified = None
    return Checkout(
        path=path, head=wt["head"], branch=wt["branch"], exists=exists,
        has_index=exists and os.path.isdir(os.path.join(path, ".tokensave")),
        modified=modified)


def _unregistered_in(scratch_root: str, project_path: str,
                     registered: "set[str]") -> "list[str]":
    """Scratch entries named for this project that no registration explains.

    Attributed by the ``<repo>-`` prefix the baseline rule prescribes, since
    the scratch folder is shared by every project on the drive.
    """
    prefix = os.path.basename(os.path.abspath(project_path)).lower() + "-"
    try:
        names = sorted(os.listdir(scratch_root))
    except OSError:
        return []
    return [os.path.join(scratch_root, n) for n in names
            if n.lower().startswith(prefix)
            and _norm(os.path.join(scratch_root, n)) not in registered]


def scan(project_path: str, git_exe: str,
         scratch_root: "str | None" = None) -> StrayReport:
    """Registered worktrees outside the two sanctioned places, and the
    scratch folder's unexplained entries."""
    if not project_path or not os.path.exists(
            os.path.join(project_path, ".git")):
        return StrayReport(NOT_APPLICABLE)
    scratch = scratch_root or default_scratch_root(project_path)
    worktrees, problem = _list_worktrees(project_path, git_exe)
    if problem:
        return StrayReport(UNKNOWN, problem, scratch)

    claude_dir = os.path.join(project_path, ".claude", "worktrees")
    me = _norm(project_path)
    registered = {_norm(wt["path"]) for wt in worktrees}
    report = StrayReport(CLEAR, scratch_root=scratch)
    for wt in worktrees:
        if _norm(wt["path"]) == me:
            continue                   # the folder the caller asked about
        if _inside(wt["path"], claude_dir) or _inside(wt["path"], scratch):
            continue
        report.strays.append(_facts(wt))
    report.unregistered = _unregistered_in(scratch, project_path, registered)
    if report.strays or report.unregistered:
        report.state = STRAY
    return report
