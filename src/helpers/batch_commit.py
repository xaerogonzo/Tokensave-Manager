"""batch_commit — commit what the Manager wrote, and only that, with proof.

`manager_changes` decides what is eligible from facts; this module gathers those
facts from git and applies the decision. Four rules shape it, each paid for by a
way a "convenient" version would damage somebody's working tree:

- **Candidates are not authorization.** The paths a bulk operation reported are
  where to LOOK. Whether each is exactly the Manager's change is decided from
  evidence recorded around the write, and decided AGAIN immediately before
  committing, because a dialog can sit open for minutes.
- **The user's index is never consumed.** Files are staged with `git add --
  <paths>` and committed with `git commit --only -- <paths>`. `--only` commits the
  working-tree content of the named paths and disregards anything staged for other
  paths, so a file the user had already staged stays staged and stays OUT of the
  commit. `git reset` (which the manual commit dialog uses) is never run here.
- **A failure undoes only what this batch did.** If the commit fails (a hook, say)
  the paths this batch added to the index are reset, and nothing else. That is safe
  only because a path in the index before we started is never eligible.
- **The result is read back.** Git's exit code is not the report: afterwards the
  commit's own file list is compared with what was intended, and what is still
  uncommitted is counted.

Never `--no-verify`, never push, never amend. The private-repo sync is the
caller's, run per project after that project's verified commit.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from constants import CREATE_NO_WINDOW
from helpers import manager_changes as mch

GIT_TIMEOUT_S = 30
#: A pre-commit hook may run a model review; give it room.
COMMIT_TIMEOUT_S = 300

# ── project state ────────────────────────────────────────────────────────

READY = "ready"
#: Nothing the Manager wrote differs from the last commit (a local-only project,
#: or already committed). Not offered at all.
NOTHING_CHANGED = "nothing_changed"
#: The Manager wrote files, but none can be proven exclusively its own now.
NO_ELIGIBLE = "no_eligible"
BLOCKED = "blocked"

MAIN_BRANCHES = ("master", "main")

#: Repository operations in progress. One list, one helper, used everywhere.
_OPERATION_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                      "rebase-merge", "rebase-apply")


def repo_operation(git_dir: str) -> str:
    """The name of a merge / rebase / cherry-pick / revert in progress, or ""."""
    for marker in _OPERATION_MARKERS:
        if os.path.exists(os.path.join(git_dir, marker)):
            return marker
    return ""


@dataclass(frozen=True)
class BatchItem:
    """One project's contribution to a batch: where to look, and the proof."""

    root: str
    name: str
    candidates: tuple = ()
    evidence: "mch.OperationEvidence | None" = None


@dataclass(frozen=True)
class Inspection:
    root: str
    name: str
    state: str
    reason: str = ""
    branch: str = ""
    detached: bool = False
    files: tuple = ()
    #: Uncommitted changes in the project that are NOT the Manager's, left alone.
    other_changes: int = 0

    @property
    def main_branch(self) -> bool:
        return self.branch in MAIN_BRANCHES

    @property
    def eligible(self) -> tuple:
        return mch.eligible(self.files)

    @property
    def excluded(self) -> tuple:
        return tuple(f for f in self.files if f.ownership != mch.FULL
                     and f.ownership != mch.NONE)


# ── git plumbing ─────────────────────────────────────────────────────────

def _git(git_exe: str, root: str, args, timeout: int = GIT_TIMEOUT_S):
    """(returncode, stdout, stderr). rc -1 when git could not run at all."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", root] + list(args), capture_output=True,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, b"", str(exc).encode("utf-8", "replace")
    return proc.returncode, proc.stdout, proc.stderr


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _status_paths(git_exe: str, root: str, rels=None) -> "dict | None":
    """rel path -> two-letter XY, for what `git status` reports. None = unreadable."""
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if rels:
        args += ["--"] + list(rels)
    rc, out, _err = _git(git_exe, root, args)
    if rc != 0:
        return None
    entries = _text(out).split("\0")
    found, i = {}, 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1                        # a rename carries its old path next
        found[path] = xy
    return found


def _hash_paths(git_exe: str, root: str, rels) -> "dict | None":
    """rel path -> content hash of the file on disk ("" when it is not there)."""
    out = {}
    for rel in rels:
        full = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(full):
            out[rel] = ""
            continue
        rc, data, _err = _git(git_exe, root, ["hash-object", "--", rel])
        if rc != 0:
            return None
        out[rel] = _text(data).strip()
    return out


def capture_dirty(git_exe: str, root: str) -> "dict | None":
    """Every path git reports dirty in the working tree OR the index, hashed.

    Called BEFORE the Manager writes anything. None means git could not be read,
    and the caller must record no evidence rather than an empty "all clean".
    """
    if not git_exe or not os.path.exists(os.path.join(root, ".git")):
        return None
    status = _status_paths(git_exe, root)
    if status is None:
        return None
    return _hash_paths(git_exe, root, sorted(status))


def make_evidence(git_exe: str, root: str, dirty_before: "dict | None",
                  written) -> "mch.OperationEvidence":
    """Evidence for what was written, or an incomplete record when git failed."""
    written = sorted(set(written))
    if dirty_before is None:
        return mch.OperationEvidence(complete=False,
                                     written={p: "" for p in written})
    hashes = _hash_paths(git_exe, root, written)
    if hashes is None:
        return mch.OperationEvidence(complete=False,
                                     written={p: "" for p in written})
    return mch.OperationEvidence(written=hashes, dirty_before=dict(dirty_before),
                                 complete=True)


def _current(git_exe: str, root: str, rels) -> "dict | None":
    status = _status_paths(git_exe, root, rels)
    hashes = _hash_paths(git_exe, root, rels)
    if status is None or hashes is None:
        return None
    from helpers.git import _find_tracked_but_ignored
    stale = {p.replace("\\", "/") for p in _find_tracked_but_ignored(root, git_exe)}
    return {rel: mch.CurrentPath(sha=hashes[rel], differs=rel in status,
                                 tracked_ignored=rel in stale)
            for rel in rels}


# ── inspecting ───────────────────────────────────────────────────────────

def inspect_project(item: BatchItem, git_exe: str) -> Inspection:
    """Read-only. Run it for display, and again immediately before committing."""
    def blocked(reason):
        return Inspection(item.root, item.name, BLOCKED, reason)

    if not git_exe:
        return blocked("no git executable is configured")
    if not os.path.exists(os.path.join(item.root, ".git")):
        return blocked("not its own git repository")
    rc, out, _err = _git(git_exe, item.root, ["rev-parse", "--absolute-git-dir"])
    if rc != 0:
        return blocked("git could not read this repository")
    operation = repo_operation(_text(out).strip())
    if operation:
        return blocked("a %s is in progress; finish or abort it first"
                       % operation.replace("_HEAD", "").replace("-", " ").lower())

    rc, out, _err = _git(git_exe, item.root, ["symbolic-ref", "-q", "--short", "HEAD"])
    branch, detached = (_text(out).strip(), False) if rc == 0 else ("", True)
    rc, _out, _err = _git(git_exe, item.root, ["rev-parse", "--verify", "-q", "HEAD"])
    if rc != 0:
        return blocked("no commits yet; make the first commit yourself")

    candidates = sorted(set(item.candidates) | set(
        item.evidence.written if item.evidence else ()))
    current = _current(git_exe, item.root, candidates) if candidates else {}
    if current is None:
        return blocked("git could not be read")
    files = mch.assess(item.evidence, current) if item.evidence else tuple(
        mch.ChangedFile(p, mch.UNKNOWN, mch.REASON_NO_EVIDENCE, mch.group_of(p))
        for p in candidates)

    status = _status_paths(git_exe, item.root) or {}
    other = sum(1 for p in status if p not in set(candidates))
    live = [f for f in files if f.ownership != mch.NONE]
    if not live:
        state = NOTHING_CHANGED
    elif mch.eligible(files):
        state = READY
    else:
        state = NO_ELIGIBLE
    return Inspection(item.root, item.name, state, branch=branch,
                      detached=detached, files=files, other_changes=other)


# ── committing ───────────────────────────────────────────────────────────

COMMITTED = "committed"
NOTHING = "nothing"
REFUSED = "refused"
FAILED = "failed"
#: Git committed, but not exactly what was intended. Reported, never hidden.
MISMATCH = "mismatch"


@dataclass(frozen=True)
class CommitResult:
    root: str
    name: str
    outcome: str
    detail: str = ""
    sha: str = ""
    committed: tuple = ()
    remaining_manager: tuple = ()
    remaining_unrelated: int = 0


def _staged_now(git_exe: str, root: str) -> "set | None":
    rc, out, _err = _git(git_exe, root, ["diff", "--cached", "--name-only", "-z"])
    if rc != 0:
        return None
    return {p for p in _text(out).split("\0") if p}


def _tail(err: bytes, out: bytes) -> str:
    lines = (_text(err) + "\n" + _text(out)).strip().splitlines()
    return " | ".join(ln.strip() for ln in lines[-4:] if ln.strip())


def commit_project(item: BatchItem, message, git_exe: str) -> CommitResult:
    """Re-inspect, stage only the proven files, commit them, read the result back.

    *message* is text, or a callable taking the FRESH `Inspection` and returning
    text. The callable form is what the dialog uses: the words are then built
    from the files that survived this inspection, not from a list the user saw
    minutes ago, so a count always describes the real commit.
    """
    fresh = inspect_project(item, git_exe)
    if fresh.state == BLOCKED:
        return CommitResult(item.root, item.name, REFUSED, fresh.reason)
    if fresh.state == NOTHING_CHANGED:
        return CommitResult(item.root, item.name, NOTHING,
                            "nothing the Manager wrote differs from the last commit")
    paths = [f.path for f in fresh.eligible]
    if not paths:
        return CommitResult(item.root, item.name, REFUSED,
                            "none of the Manager's files can be proven "
                            "exclusively its own any more")
    message = message(fresh) if callable(message) else message
    if not message.strip():
        return CommitResult(item.root, item.name, REFUSED, "the message is empty")

    staged_before = _staged_now(git_exe, item.root)
    if staged_before is None:
        return CommitResult(item.root, item.name, FAILED,
                            "could not read the staging area")
    added = [p for p in paths if p not in staged_before]

    def undo():
        # Only what THIS batch put in the index. Every path here was clean
        # before we started, so this cannot touch the user's own staging.
        if added:
            _git(git_exe, item.root, ["reset", "-q", "--"] + added)

    rc, out, err = _git(git_exe, item.root, ["add", "--"] + paths)
    if rc != 0:
        undo()
        return CommitResult(item.root, item.name, FAILED,
                            "git add failed: " + _tail(err, out))
    rc, out, err = _git(git_exe, item.root,
                        ["commit", "-m", message, "--only", "--"] + paths,
                        timeout=COMMIT_TIMEOUT_S)
    if rc != 0:
        undo()
        return CommitResult(item.root, item.name, FAILED,
                            "git commit failed: " + _tail(err, out))

    rc, out, _err = _git(git_exe, item.root, ["rev-parse", "HEAD"])
    sha = _text(out).strip() if rc == 0 else ""
    rc, out, _err = _git(git_exe, item.root,
                         ["show", "--name-only", "--format=", "-z", "HEAD"])
    got = {p for p in _text(out).split("\0") if p} if rc == 0 else set()
    status = _status_paths(git_exe, item.root) or {}
    written = set(item.evidence.written) if item.evidence else set(paths)
    remaining = tuple(sorted(p for p in status if p in written))
    unrelated = sum(1 for p in status if p not in written)
    if got != set(paths):
        return CommitResult(
            item.root, item.name, MISMATCH,
            "committed %s, intended %s" % (sorted(got), sorted(paths)), sha,
            tuple(sorted(got)), remaining, unrelated)
    return CommitResult(item.root, item.name, COMMITTED, "", sha,
                        tuple(sorted(got)), remaining, unrelated)
