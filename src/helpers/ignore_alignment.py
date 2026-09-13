"""helpers/ignore_alignment.py — Manager-written instruction files follow git.

The invariant: every instruction companion the Manager writes, and which
exists, has the git visibility of the file that refers to it.

Paid for by KicomAI, whose `.gitignore` keeps `CLAUDE.md` and
`BASIC_INSTRUCTIONS.md` local-only. Repair wrote `project-baseline.md` and a
split wrote `docs/LESSONS.md`, and git ignored neither, so the next ordinary
commit would have published the copy and the lessons while the file that uses
them stayed private. Nothing reported it.

Three rules shape the module:

- **Git is the authority.** Visibility is what `git ls-files` and
  `git check-ignore` say, never our own reading of ignore patterns. Broad
  rules, negations and nested `.gitignore` files are git's problem.
- **Unknown is never fine.** Every requested path gets exactly one state. A
  path git did not mention is `OPEN` only when both calls exited cleanly, and
  it is never defaulted to `LOCAL`. Nothing is done on anything unknown or
  missing.
- **Alignment only writes `.gitignore`.** It never creates a companion, never
  untracks (`git rm --cached` changes the index and the next push), never
  removes a user's ignore rule, and never stages or commits. A tracked or
  open referrer with an ignored companion is reported, not changed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from constants import CREATE_NO_WINDOW
from helpers import baseline_copy as bc
from helpers.gitignore import ensure_pattern
from helpers.instructions_posture import BASIC_MD, CLAUDE_MD, canonical
from helpers.instructions_split import DEFAULT_TARGET as LESSONS_REL

GIT_TIMEOUT_S = 15

# ── vocabulary ───────────────────────────────────────────────────────────

KIND_BASIC = "basic"
KIND_BASELINE_COPY = "baseline_copy"
KIND_LESSONS = "lessons"

FS_EXISTS = "exists"
FS_MISSING = "missing"
FS_UNREADABLE = "unreadable"
#: Normalises outside the project root. Rejected before git sees it.
FS_ESCAPES = "escapes"

REPO = "repo"
#: git answers, but for a parent folder: not this project's repository.
NOT_OWN_REPO = "not_own_repo"
NO_REPO = "no_repo"

TRACKED = "tracked"
#: Untracked, and git reports it ignored.
LOCAL = "local"
#: Untracked, and git does not report it ignored.
OPEN = "open"
UNKNOWN = "unknown"

_COMMENT = "# TokenSave Manager: local-only, like %s"


@dataclass(frozen=True)
class CompanionPair:
    companion_rel: str
    referrer_rel: str
    kind: str


@dataclass(frozen=True)
class PathFact:
    fs: str
    git: str
    reason: str = ""


@dataclass(frozen=True)
class Facts:
    repo: str
    paths: dict = field(default_factory=dict)
    reason: str = ""

    def of(self, rel: str) -> PathFact:
        return self.paths.get(rel, PathFact(FS_MISSING, UNKNOWN,
                                            "not measured"))


@dataclass(frozen=True)
class Alignment:
    """What `decide` concluded. Pure data."""

    #: Pairs whose companion should be ignored to match its referrer.
    pending: tuple = ()
    #: Facts worth showing that the Manager will not act on.
    notes: tuple = ()
    #: Why nothing could be decided for some pair.
    unknown: tuple = ()

    @property
    def patterns(self) -> tuple:
        return tuple("/" + pair.companion_rel for pair in self.pending)


@dataclass(frozen=True)
class AlignResult:
    #: Companions git confirmed as ignored after the write.
    confirmed: tuple = ()
    #: Patterns written whose companion git still does not ignore.
    unconfirmed: tuple = ()
    changed_files: tuple = ()
    notes: tuple = ()
    error: str = ""

    def render(self) -> str:
        parts = ["%s is now ignored to match local-only %s (file unchanged)"
                 % (pair.companion_rel, pair.referrer_rel)
                 for pair in self.confirmed]
        parts += ["%s: ignore rule added but git still does not ignore it"
                  % rel for rel in self.unconfirmed]
        if self.error:
            parts.append(self.error)
        return "; ".join(parts)


# ── which files are companions (pure over a posture + one stat) ──────────

def safe_rel(root: str, rel: str) -> "str | None":
    """*rel* with forward slashes, or None when it escapes *root*."""
    if not rel or os.path.isabs(rel) or os.path.splitdrive(rel)[0] \
            or rel.startswith(("/", "\\")):
        return None
    norm = os.path.normpath(rel)
    if norm == os.pardir or norm.startswith(os.pardir + os.sep):
        return None
    joined = canonical(os.path.join(root, norm))
    base = canonical(root)
    if joined != base and not joined.startswith(base.rstrip("\\/") + os.sep):
        return None
    return norm.replace(os.sep, "/")


def companions_of(posture, root: str = "") -> tuple:
    """The Manager-written companions of one project, with their referrers."""
    root = root or posture.display_root
    chain = set(posture.chain)
    pairs = []
    if canonical(os.path.join(root, BASIC_MD)) in chain:
        pairs.append(CompanionPair(BASIC_MD, CLAUDE_MD, KIND_BASIC))
    copy_path = canonical(os.path.join(root, bc.COPY_BASENAME))
    if copy_path in chain:
        live = {d[0] for d in posture.baseline_directives
                if canonical(os.path.join(root, d[0])) in chain}
        if len(live) == 1:
            pairs.append(CompanionPair(bc.COPY_BASENAME, live.pop(),
                                       KIND_BASELINE_COPY))
    if os.path.isfile(os.path.join(root, LESSONS_REL)):
        pairs.append(CompanionPair(LESSONS_REL, CLAUDE_MD, KIND_LESSONS))
    return tuple(pairs)


# ── measuring (IO) ───────────────────────────────────────────────────────

def _git(git_exe: str, root: str, args: list, stdin: str = "",
         literal: bool = False):
    # Literal pathspecs for ls-files only. Measured: `check-ignore` exits 128
    # with "pathspec magic not supported by this command: 'literal'" - it
    # takes plain paths anyway, and read over stdin they are not pathspecs.
    env = dict(os.environ)
    env.pop("GIT_LITERAL_PATHSPECS", None)
    if literal:
        env["GIT_LITERAL_PATHSPECS"] = "1"
    return subprocess.run(
        [git_exe, "-C", root] + args, input=stdin.encode("utf-8"),
        capture_output=True, timeout=GIT_TIMEOUT_S, env=env,
        creationflags=CREATE_NO_WINDOW)


def _fs_state(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return FS_MISSING
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return FS_UNREADABLE
    return FS_EXISTS


def _repo_state(root: str, git_exe: str) -> "tuple[str, str]":
    try:
        proc = _git(git_exe, root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return UNKNOWN, "git rev-parse: %s" % exc
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        if "not a git repository" in err.lower():
            return NO_REPO, ""
        return UNKNOWN, "git rev-parse exit %d: %s" % (proc.returncode, err)
    top = proc.stdout.decode("utf-8", "replace").strip()
    if canonical(top) != canonical(root):
        return NOT_OWN_REPO, "inside the repository at %s" % top
    return REPO, ""


def _nul_split(data: bytes) -> set:
    return {part.decode("utf-8", "replace").replace("\\", "/")
            for part in data.split(b"\0") if part}


def _git_states(root: str, git_exe: str, rels: list) -> "tuple[dict, str]":
    """TRACKED / LOCAL / OPEN for every rel, or ({}, reason) on any failure."""
    try:
        listed = _git(git_exe, root, ["ls-files", "-z", "--full-name", "--"]
                      + rels, literal=True)
        if listed.returncode != 0:
            return {}, "git ls-files exit %d: %s" % (
                listed.returncode,
                listed.stderr.decode("utf-8", "replace").strip())
        ignored = _git(git_exe, root, ["check-ignore", "-z", "--stdin"],
                       "".join(rel + "\0" for rel in rels))
        if ignored.returncode not in (0, 1):
            return {}, "git check-ignore exit %d: %s" % (
                ignored.returncode,
                ignored.stderr.decode("utf-8", "replace").strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {}, "git: %s" % exc
    tracked = _nul_split(listed.stdout)
    local = _nul_split(ignored.stdout) if ignored.returncode == 0 else set()
    states = {}
    for rel in rels:
        if rel in tracked:
            states[rel] = TRACKED
        elif rel in local:
            states[rel] = LOCAL
        else:
            states[rel] = OPEN
    return states, ""


def read_facts(root: str, git_exe: str, rels) -> Facts:
    """Filesystem and git facts for *rels*. Every rel gets exactly one."""
    rels = list(dict.fromkeys(rels))
    paths: dict = {}
    measurable = []
    for rel in rels:
        clean = safe_rel(root, rel)
        if clean is None:
            paths[rel] = PathFact(FS_ESCAPES, UNKNOWN, "outside the project")
        else:
            measurable.append(clean)
    if not git_exe:
        return Facts(UNKNOWN, _unknown_all(root, rels, paths, "no git "
                                           "executable configured"),
                     "no git executable configured")
    repo, reason = _repo_state(root, git_exe)
    if repo != REPO:
        return Facts(repo, _unknown_all(root, rels, paths,
                                        reason or repo), reason)
    states, reason = _git_states(root, git_exe, measurable) \
        if measurable else ({}, "")
    for rel in measurable:
        git = states.get(rel, UNKNOWN)
        paths[rel] = PathFact(_fs_state(root, rel), git,
                              reason if git == UNKNOWN else "")
    return Facts(REPO, paths, reason)


def _unknown_all(root, rels, paths, reason) -> dict:
    out = dict(paths)
    for rel in rels:
        if rel not in out:
            out[rel] = PathFact(_fs_state(root, rel), UNKNOWN, reason)
    return out


# ── deciding (pure) ──────────────────────────────────────────────────────

def decide(pairs, facts: Facts) -> Alignment:
    """What alignment each pair needs. Acts only on complete, known facts."""
    pending, notes, unknown = [], [], []
    if facts.repo in (NO_REPO, NOT_OWN_REPO):
        return Alignment()
    for pair in pairs:
        comp, ref = facts.of(pair.companion_rel), facts.of(pair.referrer_rel)
        if facts.repo != REPO or UNKNOWN in (comp.git, ref.git):
            unknown.append("%s: %s" % (pair.companion_rel,
                                       comp.reason or ref.reason
                                       or facts.reason or "git state unknown"))
            continue
        if comp.fs != FS_EXISTS or ref.fs != FS_EXISTS:
            continue
        if ref.git == LOCAL and comp.git == OPEN:
            pending.append(pair)
        elif ref.git == LOCAL and comp.git == TRACKED:
            notes.append("%s is committed while %s is local-only; "
                         "`git rm --cached %s` would untrack it"
                         % (pair.companion_rel, pair.referrer_rel,
                            pair.companion_rel))
        elif ref.git in (TRACKED, OPEN) and comp.git == LOCAL:
            notes.append("%s is ignored by an existing rule, so clones and "
                         "worktrees won't get it" % pair.companion_rel)
    return Alignment(tuple(pending), tuple(notes), tuple(unknown))


def split_visibility_text(facts: "Facts | None", source_rel: str,
                          target_rel: str) -> str:
    """What a split confirmation may truthfully say about git. Pure.

    Replaces a hard-coded "both files are in git, so this is revertible",
    which was false in exactly the repository that motivated this module.
    """
    if facts is None or facts.repo == UNKNOWN:
        return "Could not determine git state: %s" % (
            facts.reason if facts else "not measured")
    if facts.repo in (NO_REPO, NOT_OWN_REPO):
        return "%s is not in its own git repository, so git cannot revert " \
               "this." % source_rel
    source, target = facts.of(source_rel), facts.of(target_rel)
    if UNKNOWN in (source.git, target.git):
        return "Could not determine git state: %s" % (
            source.reason or target.reason or "unknown")
    if source.git == LOCAL:
        if target.git == LOCAL and target.fs == FS_EXISTS:
            return ("%s and %s are both local-only (ignored), so this is not "
                    "revertible through git." % (source_rel, target_rel))
        return ("%s is local-only (ignored), so this is not revertible through "
                "git. %s will be ignored to match." % (source_rel, target_rel))
    if target.git == LOCAL:
        return ("%s is in git, but %s is ignored by an existing rule, so the "
                "moved sections will not be committed." % (source_rel,
                                                           target_rel))
    return ("%s is in git; %s will be a file to commit with it, so this is "
            "revertible once committed." % (source_rel, target_rel)) \
        if source.git == TRACKED else \
        ("Neither file is committed yet; commit both to make this revertible.")


def pending_text(alignment: Alignment) -> str:
    return "; ".join("%s would be picked up by a commit while %s is "
                     "local-only" % (p.companion_rel, p.referrer_rel)
                     for p in alignment.pending)


# ── applying (IO, compare-and-apply) ─────────────────────────────────────

def _rels(pairs) -> list:
    out = []
    for pair in pairs:
        out += [pair.companion_rel, pair.referrer_rel]
    return out


def _gitignore_bytes(root: str) -> "bytes | None":
    try:
        with open(os.path.join(root, ".gitignore"), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def align(root: str, git_exe: str, pairs) -> AlignResult:
    """Ignore each companion that should be, then report what git confirms.

    The scan that showed "pending" is never the authorization: facts are
    re-read here, immediately before writing, and the result is what git
    says afterwards rather than what was attempted.
    """
    pairs = tuple(pairs)
    if not pairs:
        return AlignResult()
    before = _gitignore_bytes(root)
    attempted: list = []
    fresh = Alignment()
    # A chain can be two deep: `project-baseline.md` refers back to
    # BASIC_INSTRUCTIONS.md, which only becomes local once ITS rule is
    # written. So decide again on fresh facts until nothing new is pending,
    # bounded by the number of pairs.
    for _pass in range(len(pairs)):
        fresh = decide(pairs, read_facts(root, git_exe, _rels(pairs)))
        todo = [p for p in fresh.pending if p not in attempted]
        if not todo:
            break
        for pair in todo:
            attempted.append(pair)
            _added, detail = ensure_pattern(root, "/" + pair.companion_rel,
                                            _COMMENT % pair.referrer_rel)
            if detail.startswith(("Could not", "Not a git")):
                return AlignResult(notes=fresh.notes, error=detail)
    if not attempted:
        return AlignResult(notes=fresh.notes + fresh.unknown)

    # What git says now, not what was attempted.
    after = read_facts(root, git_exe, _rels(pairs))
    confirmed = tuple(p for p in attempted
                      if after.of(p.companion_rel).git == LOCAL)
    unconfirmed = tuple(p.companion_rel for p in attempted
                        if p not in confirmed)
    changed = (".gitignore",) if _gitignore_bytes(root) != before else ()
    return AlignResult(confirmed, unconfirmed, changed,
                       decide(pairs, after).notes)
