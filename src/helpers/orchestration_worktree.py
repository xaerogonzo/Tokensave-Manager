"""Everything a delegated task does to git and to the disk (docs/ORCHESTRATION.md 5).

Preflight on the person's checkout, create the task's own worktree, index it,
give the agent a way to reach tokensave there, and afterwards MEASURE what the
agent changed. Git answers every question about the diff; nothing here believes
what an agent says it did.

**Why a worktree and not the person's own checkout:** the person is usually
editing there, and an agent writing beside them is the dirty-tree problem with
no protection. A worktree is also cheap to throw away.

**Preflight is about tracked changes.** A checkout with modified or staged
tracked files is refused, naming them: a worktree is made from a commit, so the
task would silently lack the person's work. Untracked files are not in any
commit and so are not "lost" by branching; they are counted and reported rather
than refused, because a scratch file should not forbid delegating.
"""
import dataclasses
import json
import os
import shutil
import subprocess

from constants import CREATE_NO_WINDOW

GIT_TIMEOUT = 60
INDEX_TIMEOUT = 600
MAX_FILES_LISTED = 200


def _run(argv: list, cwd: "str | None" = None, timeout: int = GIT_TIMEOUT) -> "tuple[bool, str, str]":
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    return proc.returncode == 0, proc.stdout or "", (proc.stderr or "").strip()


def _git(git_exe: str, cwd: str, *args, timeout: int = GIT_TIMEOUT):
    return _run([git_exe or "git", "-C", cwd, *args], timeout=timeout)


# ── preflight ───────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Preflight:
    ok: bool
    base_commit: str = ""
    #: Tracked files with uncommitted changes. Non-empty means refused.
    dirty: tuple = ()
    untracked: int = 0
    error: str = ""


def preflight(project: str, git_exe: str = "") -> Preflight:
    if not project or not os.path.exists(os.path.join(project, ".git")):
        return Preflight(False, error="this folder is not its own git repository")
    ok, head, err = _git(git_exe, project, "rev-parse", "--verify", "HEAD")
    if not ok:
        return Preflight(False, error=f"the repository has no commit to branch "
                                      f"from: {err.splitlines()[0] if err else 'unknown'}")
    ok, status, err = _git(git_exe, project, "status", "--porcelain")
    if not ok:
        return Preflight(False, error=f"git status failed: {err}")
    dirty, untracked = [], 0
    for line in status.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            dirty.append(line[3:].strip())
    if dirty:
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        return Preflight(False, head.strip(), tuple(dirty), untracked,
                         f"uncommitted changes would not be in the task's copy "
                         f"({shown}); commit or stash them first")
    return Preflight(True, head.strip(), (), untracked)


# ── where a task lives ──────────────────────────────────────────────────────────

def worktree_path(project: str, task_id: str, scratch_root: str = "") -> str:
    """`<drive>:\\_scratch\\<repo>-<task_id>`: the repo's own convention for extra
    checkouts, so Doctor's stray-checkout scan already lists a forgotten one.
    `scratch_root` replaces `<drive>:\\_scratch` (tests, or a person who wants
    them elsewhere)."""
    drive, rest = os.path.splitdrive(os.path.abspath(project))
    repo = os.path.basename(rest.rstrip("\\/")) or "repo"
    if not scratch_root:
        root = (drive + os.sep) if drive else os.path.dirname(os.path.abspath(project))
        scratch_root = os.path.join(root, "_scratch")
    return os.path.join(scratch_root, f"{repo}-{task_id}")


def branch_name(task_id: str) -> str:
    return f"tsm/{task_id}"


def free_bytes(path: str) -> "int | None":
    """Free space on the drive a path would live on; None when unmeasurable."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# ── create, index, give it tokensave ────────────────────────────────────────────

def create(project: str, path: str, branch: str, base: str,
           git_exe: str = "") -> "tuple[bool, str]":
    if os.path.exists(path):
        return False, f"{path} already exists; not touching it"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        return False, f"could not create {os.path.dirname(path)}: {exc}"
    ok, _out, err = _git(git_exe, project, "worktree", "add", "-b", branch, path, base)
    return (True, "") if ok else (False, f"git worktree add failed: {err}")


def index(tokensave_exe: str, path: str) -> "tuple[bool, str]":
    """`tokensave init <path>`. A task without the graph is refused, not run blind."""
    if not tokensave_exe:
        return False, "tokensave is not configured, so the task would run without its graph"
    ok, _out, err = _run([tokensave_exe, "init", path], timeout=INDEX_TIMEOUT)
    if not ok:
        return False, f"tokensave init failed: {err.splitlines()[-1] if err else 'no message'}"
    if not os.path.isdir(os.path.join(path, ".tokensave")):
        return False, "tokensave init ran but left no index in the worktree"
    return True, ""


def prepare_mcp(spec, path: str, task_id: str, project: str, cfg_raw: dict,
                git_exe: str = "") -> "tuple[str, list, str]":
    """Make tokensave reachable for this agent in this worktree.

    Returns `(config_path, written, error)`. `written` lists files the Manager
    put INSIDE the worktree (so they are not mistaken for the agent's changes).
    A row that names a config by flag gets a file outside the worktree; a row
    that reads a project file gets one inside it. The entry's `-p .` is relative,
    so it follows the agent's working directory to the worktree.
    """
    from helpers.mcp_classify import _canonical_project_entry
    entry = _canonical_project_entry(cfg_raw)
    if spec.task_mcp_flag:
        from helpers import orchestration_task as ot
        out = os.path.join(ot.tasks_dir(project, git_exe), task_id + ".mcp.json")
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"mcpServers": {"tokensave": entry}}, fh, indent=1)
        except OSError as exc:
            return "", [], f"could not write the MCP config: {exc}"
        return out, [], ""
    if spec.task_mcp_file:
        from helpers.mcp_cursor import bind_cursor_project
        ok, why = bind_cursor_project(path, entry)
        if not ok:
            return "", [], why
        return "", [spec.task_mcp_file.replace("\\", "/")], ""
    return "", [], f"{spec.label} has no way to be given tokensave in a task"


# ── measuring what the agent changed ────────────────────────────────────────────

def measure(path: str, base: str, git_exe: str = "", ignore: tuple = ()) -> dict:
    """What changed since `base`, from git. Never raises; says when it could not.

    Counts committed AND uncommitted tracked changes (`git diff <base>` compares
    the working tree to the base), plus untracked files. `ignore` is for files
    the Manager itself wrote into the worktree.
    """
    skip = {p.replace("\\", "/") for p in ignore}
    out = {"files": [], "file_count": 0, "insertions": 0, "deletions": 0,
           "untracked": 0, "commits": 0, "error": ""}
    ok, numstat, err = _git(git_exe, path, "diff", "--numstat", base)
    if not ok:
        out["error"] = f"git diff failed: {err}"
        return out
    files = []
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[2].replace("\\", "/") in skip:
            continue
        add, rem, name = parts
        out["insertions"] += int(add) if add.isdigit() else 0
        out["deletions"] += int(rem) if rem.isdigit() else 0
        files.append(name)
    ok, others, _err = _git(git_exe, path, "ls-files", "--others", "--exclude-standard")
    new = [n for n in others.splitlines() if n and n.replace("\\", "/") not in skip] if ok else []
    out["untracked"] = len(new)
    ok, count, _err = _git(git_exe, path, "rev-list", "--count", f"{base}..HEAD")
    out["commits"] = int(count.strip()) if ok and count.strip().isdigit() else 0
    everything = files + new
    out["file_count"] = len(everything)
    out["files"] = everything[:MAX_FILES_LISTED]
    return out


# ── discarding ──────────────────────────────────────────────────────────────────

def discard(project: str, path: str, git_exe: str = "") -> "tuple[bool, str]":
    """Remove the task's worktree; the BRANCH is kept, because it holds the work.

    Goes through the existing half-state-aware remover: `git worktree remove`
    deregisters even when the delete fails, and the leftover directory is then
    not a valid worktree.
    """
    from helpers.worktree_cleanup import remove_worktree
    result = remove_worktree(git_exe or "git", project, path, force=True)
    if result.success:
        return True, ""
    if result.directory_exists:
        return False, (f"git let go of the worktree but could not delete {path}: "
                       f"{result.stderr or 'something holds a file open'}")
    return False, result.stderr or "could not remove the worktree"
