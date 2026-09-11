"""helpers/install_identity.py — where am I, and who owns the fleet?

Every retrofitted project's `BASIC_INSTRUCTIONS.md` carries an **absolute** path
to the shared baseline. That is the feature, not a defect: it is what makes one
edit to `project-baseline.md` reach every wired project at once. But it makes
this installation's location load-bearing for the whole fleet, and the Manager
had no record of where it was — so a move could only ever be *inferred* from
drift, never known.

**Two questions, and they stop being the same one the moment a second
installation exists:**

    Where am I?            FIRST_RUN / SAME / MOVED
                           config's recorded install dir vs this one

    Who owns the fleet?    OWNED_HERE / OWNED_ELSEWHERE / SPLIT / UNOWNED
                           derived from the PROJECTS, not from the config

`SAME` + `OWNED_ELSEWHERE` is *another install owns these*, and that is all it
is. It does not mean "transfer". This module cannot know whether that reflects a
deliberate handover, an interrupted repair, or a second install quietly taking
them, so **the state never encodes the action.** Transferring ownership is an
operation somebody invokes from that state.

**States describe the world. Operations are separate and explicit.**

Measured condition on the machine this was written for: a source checkout and a
`dist/` build, whose `templates/project-baseline.md` differed by four months.
Whichever ran last would own the fleet, and the other would see every project as
stale — indefinitely.

Path identity is **lexical**, reusing `instructions_posture.canonical`:
`normcase(normpath(abspath(path)))`, folding case and separators on Windows and
a no-op on POSIX, with deliberately no `realpath`. That is an intentional
invariant rather than an oversight — filesystem alias identity (junctions,
symlinks, 8.3 names) is out of scope, and an install reached through a junction
reads as a different install. Changing it would change baseline identity
fleet-wide, which is a different decision.

Pure apart from `baseline_facts`, which stats and hashes one file.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os

from helpers.instructions_posture import canonical

# ── where am I ───────────────────────────────────────────────────────────────

#: No recorded install dir. The absence of a record is NOT evidence of a move,
#: and conflating the two would make every first run after an upgrade announce
#: a relocation that never happened.
IDENTITY_FIRST_RUN = "first_run"
IDENTITY_SAME = "same"
IDENTITY_MOVED = "moved"

# ── who owns the fleet ───────────────────────────────────────────────────────

OWNED_HERE = "owned_here"
OWNED_ELSEWHERE = "owned_elsewhere"
#: Projects point at two or more different baselines. A first-class state: a
#: single "the fleet points at X" would be the population failure this project
#: keeps paying for, and it is reachable today with two installs on one machine.
OWNERSHIP_SPLIT = "split"
#: No project reaches any baseline at all. Precise predicate, deliberately not
#: a catch-all: see `read_ownership`.
OWNERSHIP_UNOWNED = "unowned"

#: The ONLY config values that travel with the installation.
#:
#: Working it through key by key is what makes this short, and the shortness is
#: the finding. `tokensave_exe`, `git_exe`, `python_exe`, `codegraph_exe`,
#: `pyscope_exe`, `ruff_exe`, `pyright_exe`, `markdownlint_exe` and `editor_cmd`
#: all point at software installed elsewhere and must never be rewritten by a
#: move. `search_roots`, `project_categories`, `instructions_skip_paths`,
#: `user_snippets` and `mcp_skip_warnings` are user data — and `search_roots` is
#: precisely why an allowlist is needed rather than a computed
#: "is-it-inside-the-old-install" rule: a search root *can* live inside the
#: Manager's folder, and rewriting it would silently repoint the user's project
#: discovery.
RELOCATABLE_KEYS = ("template_dir",)


@dataclasses.dataclass(frozen=True)
class InstallIdentity:
    """Where this installation is, against where the config says it was."""

    state: str
    #: Canonical, for comparison.
    recorded: str = ""
    current: str = ""
    #: As written, for showing and for rebasing a path without folding its case.
    recorded_display: str = ""
    current_display: str = ""

    @property
    def moved(self) -> bool:
        return self.state == IDENTITY_MOVED


@dataclasses.dataclass(frozen=True)
class Owner:
    """One baseline directory, and the projects pointing at it."""

    canonical_dir: str
    #: For showing. Derived from the posture's `reached_baseline`, which is
    #: already canonical -- so on Windows this arrives CASE-FOLDED and there is
    #: no un-folded spelling available to recover. Cosmetic, and named honestly
    #: rather than quietly re-cased into something the user never typed.
    display_dir: str
    projects: tuple = ()

    @property
    def count(self) -> int:
        return len(self.projects)


@dataclasses.dataclass(frozen=True)
class FleetOwnership:
    """Which installation's baseline the projects actually reach."""

    state: str
    #: Most projects first. Never collapsed to a single owner: a bare `SPLIT`
    #: with no per-owner counts would say nothing actionable.
    owners: tuple = ()
    #: Projects whose chain reaches no baseline at all. Reported separately and
    #: they NEVER move the ownership verdict — they are ordinary wiring work,
    #: and folding them in would overload "unknown" with a second meaning.
    unresolved: tuple = ()

    @property
    def is_split(self) -> bool:
        return self.state == OWNERSHIP_SPLIT

    def summary(self) -> str:
        if self.state == OWNERSHIP_UNOWNED:
            return "no project reaches a baseline"
        parts = ["%s (%d)" % (o.display_dir, o.count) for o in self.owners]
        tail = ("; %d unresolved" % len(self.unresolved)
                if self.unresolved else "")
        return "; ".join(parts) + tail


def read_identity(raw: dict, base_dir: str) -> InstallIdentity:
    """Compare the recorded install directory against this one."""
    recorded_display = (raw or {}).get("install_dir", "") or ""
    current = canonical(base_dir)
    if not recorded_display:
        return InstallIdentity(IDENTITY_FIRST_RUN, "", current, "", base_dir)
    recorded = canonical(recorded_display)
    state = IDENTITY_SAME if recorded == current else IDENTITY_MOVED
    return InstallIdentity(state, recorded, current, recorded_display, base_dir)


def read_ownership(projects, here_template_dir: str) -> FleetOwnership:
    """Group the fleet by the baseline directory each project actually reaches.

    Pure: takes already-classified postures and does no IO.

    **Ownership is computed only over projects that reach a baseline**, and
    `reached_baseline` being empty is the measured predicate for "does not" —
    an ORPHANED project carries the include but reaches nothing, and reports an
    empty string. `RESOLVED` and `STALE` both reach one; that is exactly the
    distinction that makes `STALE` mean "owned by a different install" rather
    than "broken".
    """
    here = canonical(here_template_dir) if here_template_dir else ""
    groups: dict = {}
    unresolved = []
    for project in projects:
        reached = getattr(project, "reached_baseline", "") or ""
        if not reached:
            unresolved.append(project.name)
            continue
        display_dir = os.path.dirname(reached)
        key = canonical(display_dir)
        entry = groups.setdefault(key, [display_dir, []])
        entry[1].append(project.name)

    owners = tuple(sorted(
        (Owner(key, value[0], tuple(value[1])) for key, value in groups.items()),
        key=lambda o: (-o.count, o.display_dir.lower())))

    if not owners:
        state = OWNERSHIP_UNOWNED
    elif len(owners) > 1:
        state = OWNERSHIP_SPLIT
    elif here and owners[0].canonical_dir == here:
        state = OWNED_HERE
    else:
        state = OWNED_ELSEWHERE

    return FleetOwnership(state, owners, tuple(unresolved))


# ── what a move invalidates ──────────────────────────────────────────────────

def _inside(path: str, parent: str) -> bool:
    """Lexical containment. Both arguments already canonical."""
    if not path or not parent:
        return False
    return path == parent or path.startswith(parent + os.sep)


def relocatable_updates(raw: dict, identity: InstallIdentity) -> tuple:
    """`((key, old, new), ...)` for config values a move invalidated.

    Two conditions, and both are required. The **allowlist** bounds what may
    ever be touched; **containment** decides whether this particular value was
    install-relative at all. A `template_dir` the user deliberately pointed at a
    shared folder outside the installation stays exactly where it is.
    """
    if not identity.moved:
        return ()
    out = []
    for key in RELOCATABLE_KEYS:
        value = (raw or {}).get(key, "") or ""
        if not value or not _inside(canonical(value), identity.recorded):
            continue
        tail = os.path.relpath(os.path.abspath(value),
                               os.path.abspath(identity.recorded_display))
        out.append((key, value,
                    os.path.normpath(os.path.join(identity.current_display,
                                                  tail))))
    return tuple(out)


# ── evidence about a baseline, which is not a version ────────────────────────

@dataclasses.dataclass(frozen=True)
class BaselineFacts:
    """Enough to tell two baselines apart without inventing a version order."""

    path: str = ""
    exists: bool = False
    size: int = 0
    mtime: float = 0.0
    #: Because size and mtime can coincide. Two DIFFERENT baselines must not be
    #: able to look equivalent to somebody reading only a date and a byte count.
    content_hash: str = ""

    def differs_from(self, other: "BaselineFacts") -> bool:
        return self.content_hash != other.content_hash


def baseline_facts(path: str) -> BaselineFacts:
    """Stat and hash one baseline. The only IO in this module."""
    if not path or not os.path.isfile(path):
        return BaselineFacts(path=path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        stat = os.stat(path)
    except OSError:
        return BaselineFacts(path=path)
    return BaselineFacts(path=path, exists=True, size=len(data),
                         mtime=stat.st_mtime,
                         content_hash=hashlib.sha256(data).hexdigest())


# ── what may be offered ──────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class RelocationPlan:
    """What a relocation would change, and whether it may run in bulk."""

    identity: InstallIdentity
    ownership: FleetOwnership
    config_updates: tuple = ()
    #: Projects whose baseline include would be repointed.
    projects: tuple = ()
    #: Non-empty means the bulk action is refused, with the reason.
    blocked: str = ""
    shipped_baseline: BaselineFacts = dataclasses.field(
        default_factory=BaselineFacts)
    reached_baseline: BaselineFacts = dataclasses.field(
        default_factory=BaselineFacts)

    @property
    def offers_bulk(self) -> bool:
        return not self.blocked and bool(self.projects or self.config_updates)

    @property
    def downgrades(self) -> bool:
        """Would repointing move the fleet onto DIFFERENT baseline content?

        Reported, never decided. There is no version order here and inventing
        one would be worse than showing both files: the release build on the
        machine this was written for shipped a baseline four months older than
        the source, and a silent "repoint to me" would have taken the whole
        fleet backwards.
        """
        return (self.shipped_baseline.exists and self.reached_baseline.exists
                and self.shipped_baseline.differs_from(self.reached_baseline))


def relocation_plan(identity: InstallIdentity, ownership: FleetOwnership,
                    raw: dict, here_template_dir: str) -> RelocationPlan:
    """Combine both questions into what may be offered. Pure.

    The refusal matrix is the point. "One action" must never become "one action
    that silently fixed half the fleet", which would defeat the reason `SPLIT`
    is a state at all.
    """
    updates = relocatable_updates(raw, identity)
    shipped = baseline_facts(os.path.join(here_template_dir,
                                          "project-baseline.md"))
    reached = BaselineFacts()
    if ownership.owners:
        reached = baseline_facts(os.path.join(ownership.owners[0].display_dir,
                                              "project-baseline.md"))

    blocked = ""
    projects: tuple = ()
    if ownership.state == OWNERSHIP_SPLIT:
        blocked = ("the fleet points at %d different baselines, so there is no "
                   "single thing to repoint: %s"
                   % (len(ownership.owners), ownership.summary()))
    elif ownership.state == OWNERSHIP_UNOWNED:
        blocked = ("no project reaches a baseline, so this is ordinary wiring "
                   "rather than a relocation")
    elif ownership.unresolved:
        blocked = ("%d project(s) reach no baseline, so a bulk repoint would "
                   "fix only part of the fleet: %s"
                   % (len(ownership.unresolved),
                      ", ".join(ownership.unresolved[:3])))
    elif ownership.state == OWNED_HERE:
        blocked = "the fleet already points at this installation"
    else:
        projects = ownership.owners[0].projects

    return RelocationPlan(identity=identity, ownership=ownership,
                          config_updates=updates, projects=projects,
                          blocked=blocked, shipped_baseline=shipped,
                          reached_baseline=reached)
