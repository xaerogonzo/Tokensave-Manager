"""manager_changes — which of the Manager's writes are safe to commit, and how to say so.

Three questions that are easy to run together, and must not be:

    Which paths might be ours?      CANDIDATES  what `apply_to_project` reported it wrote.
                                                Never an authorization.
    Is each exactly our change?     OWNERSHIP   proven from evidence recorded around the write
                                                plus what git says NOW.
    What did we commit?             VERIFICATION read back from git after the commit
                                                (`batch_commit`).

A path matching a Manager pattern proves nothing. `.gitignore` or `CLAUDE.md` may
hold a person's edit beside ours, and a baseline copy may have been edited while a
dialog sat open. So ownership comes from **evidence**, not from names:

    written[path]        the content hash the Manager left on disk
    dirty_before[path]   paths git already reported dirty (working tree OR index)
                         before the Manager wrote anything

`FULL` iff the path was not dirty before AND its content is still exactly what we
wrote AND git shows it new or modified. Anything else is excluded with a reason.
There is **no partial-file staging** in this release: a file holding someone else's
change is left entirely alone rather than split.

Wording lives here too, and is data. `GROUP_RULES` labels a path for the MESSAGE and
never confers eligibility; `COMBINATIONS` maps each combination of groups to one
scope and subject, so `compose` has no decision tree to grow into. Subjects are
fixtures, tested one by one.

Pure: no git, no Tk. `batch_commit` supplies the facts.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

# ── ownership ────────────────────────────────────────────────────────────

FULL = "full"
#: Someone else's change is in the file, or it changed after we wrote it.
MIXED = "mixed"
#: A candidate that no longer differs from HEAD (already committed, or ignored).
NONE = "none"
#: No evidence was recorded, so nothing can be proven.
UNKNOWN = "unknown"
#: Git would refuse to stage it (tracked but matches .gitignore).
BLOCKED = "blocked"

REASON_NO_EVIDENCE = "no record of what the Manager wrote, so ownership cannot be proven"
REASON_DIRTY_BEFORE = "had uncommitted changes before the Manager wrote it"
REASON_CHANGED_SINCE = "changed after the Manager wrote it"
REASON_UNCHANGED = "no longer differs from the last commit"
REASON_TRACKED_IGNORED = "tracked but matches .gitignore, so git will not stage it"


@dataclass(frozen=True)
class OperationEvidence:
    """What was true around one project's write. Recorded by the writer."""

    #: rel path -> content hash of what the Manager left on disk ("" = deleted).
    written: dict = field(default_factory=dict)
    #: rel path -> content hash at the moment BEFORE the Manager wrote, for every
    #: path git already reported dirty in the working tree or the index.
    dirty_before: dict = field(default_factory=dict)
    #: False when git could not be read at either end. Then nothing is eligible.
    complete: bool = False


@dataclass(frozen=True)
class CurrentPath:
    """What git and the disk say about one candidate right now."""

    sha: str = ""
    #: The path appears in `git status` (new, modified or deleted).
    differs: bool = False
    tracked_ignored: bool = False


@dataclass(frozen=True)
class ChangedFile:
    path: str
    ownership: str
    reason: str = ""
    group: str = ""


def assess(evidence: "OperationEvidence | None", current: dict) -> "tuple[ChangedFile, ...]":
    """Ownership of every candidate, from evidence and CURRENT facts. Pure.

    *current* maps rel path -> `CurrentPath` for each candidate; a candidate
    missing from it is treated as unreadable, never as clean.
    """
    if evidence is None or not evidence.complete:
        paths = sorted(evidence.written) if evidence else sorted(current)
        return tuple(ChangedFile(p, UNKNOWN, REASON_NO_EVIDENCE, group_of(p))
                     for p in paths)
    out = []
    for path in sorted(evidence.written):
        now = current.get(path)
        group = group_of(path)
        if now is None:
            out.append(ChangedFile(path, UNKNOWN, "could not be read", group))
        elif path in evidence.dirty_before:
            out.append(ChangedFile(path, MIXED, REASON_DIRTY_BEFORE, group))
        elif now.sha != evidence.written[path]:
            out.append(ChangedFile(path, MIXED, REASON_CHANGED_SINCE, group))
        elif not now.differs:
            out.append(ChangedFile(path, NONE, REASON_UNCHANGED, group))
        elif now.tracked_ignored:
            out.append(ChangedFile(path, BLOCKED, REASON_TRACKED_IGNORED, group))
        else:
            out.append(ChangedFile(path, FULL, "", group))
    return tuple(out)


def eligible(files) -> "tuple[ChangedFile, ...]":
    return tuple(f for f in files if f.ownership == FULL)


# ── wording (data) ───────────────────────────────────────────────────────

G_INSTRUCTIONS = "instructions"
G_LESSONS = "lessons"
G_IGNORE = "ignore"
G_AGENTS = "agents"
G_OTHER = "other"

#: First match wins. A LABEL for the message; never a reason to stage a file.
GROUP_RULES = (
    ("project-baseline.md", G_INSTRUCTIONS),
    ("CLAUDE.md", G_INSTRUCTIONS),
    ("BASIC_INSTRUCTIONS.md", G_INSTRUCTIONS),
    ("docs/gotchas/*", G_LESSONS),
    ("docs/LESSONS.md", G_LESSONS),
    (".gitignore", G_IGNORE),
    ("AGENTS.md", G_AGENTS),
    (".cursor/rules/tokensave.mdc", G_AGENTS),
)


def group_of(path: str) -> str:
    rel = path.replace("\\", "/")
    for pattern, group in GROUP_RULES:
        if fnmatch.fnmatchcase(rel, pattern):
            return group
    return G_OTHER


#: User-facing names, never the internal ids.
GROUP_LABEL = {
    G_INSTRUCTIONS: "project instructions",
    G_LESSONS: "shared lessons",
    G_IGNORE: "ignore rules",
    G_AGENTS: "agent rules",
}

#: Why each group is written. One sentence each, stated as the reason.
GROUP_WHY = {
    G_INSTRUCTIONS: "Each project carries a copy of the shared baseline so it "
                    "loads in every session.",
    G_LESSONS: "The baseline indexes these lessons, so they are delivered "
               "with it.",
    G_IGNORE: "Files the Manager writes follow the git visibility of the file "
              "that refers to them.",
    G_AGENTS: "Agents that cannot follow an include get the baseline's content "
              "inline.",
}

_I, _L, _G, _A = G_INSTRUCTIONS, G_LESSONS, G_IGNORE, G_AGENTS

#: frozenset(groups) -> (scope, subject phrase). Complete for every non-empty
#: combination of the four groups; a path in no group falls back to GENERIC.
COMBINATIONS = {
    frozenset({_I}): ("instructions", "refresh project instructions"),
    frozenset({_L}): ("instructions", "deliver shared lessons"),
    frozenset({_G}): ("gitignore", "align ignore rules with local-only files"),
    frozenset({_A}): ("agents", "update agent rules"),
    frozenset({_I, _L}): ("instructions",
                          "refresh instructions and deliver shared lessons"),
    frozenset({_I, _G}): ("instructions",
                          "refresh instructions and align ignore rules"),
    frozenset({_I, _A}): ("instructions",
                          "refresh instructions and update agent rules"),
    frozenset({_L, _G}): ("instructions",
                          "deliver shared lessons and align ignore rules"),
    frozenset({_L, _A}): ("instructions",
                          "deliver shared lessons and update agent rules"),
    frozenset({_G, _A}): ("agents", "update agent rules and align ignore rules"),
    frozenset({_I, _L, _G}): ("instructions",
                              "update instructions, lessons and ignore rules"),
    frozenset({_I, _L, _A}): ("instructions",
                              "update instructions, lessons and agent rules"),
    frozenset({_I, _G, _A}): ("instructions",
                              "update instructions, agent rules and ignore rules"),
    frozenset({_L, _G, _A}): ("instructions",
                              "update lessons, agent rules and ignore rules"),
    frozenset({_I, _L, _G, _A}): ("instructions",
                                  "update instructions, lessons and all rules"),
}

GENERIC = ("manager", "refresh generated project files")

#: The verbs a subject may start with; a test holds every subject to this.
APPROVED_VERBS = ("refresh", "deliver", "update", "add", "align")

SUBJECT_MAX = 72
#: Names are listed only up to this many; beyond it a count says enough.
NAMES_MAX = 6

_ORDER = (G_INSTRUCTIONS, G_LESSONS, G_AGENTS, G_IGNORE, G_OTHER)


@dataclass(frozen=True)
class Message:
    subject: str
    body: str

    def text(self) -> str:
        return self.subject + "\n\n" + self.body if self.body else self.subject


def _subject(groups: frozenset) -> str:
    known = groups - {G_OTHER}
    scope, phrase = (COMBINATIONS.get(frozenset(known), GENERIC)
                     if known and G_OTHER not in groups else GENERIC)
    return "chore(%s): %s" % (scope, phrase)


def compose(files, version: str) -> Message:
    """The commit message for exactly the files that will be committed. Pure.

    *files* must be the FINAL eligible set, so a count always describes the real
    commit: 17 planned and one excluded says sixteen. Same set, same message.
    """
    files = sorted(eligible(files), key=lambda f: f.path)
    if not files:
        return Message("", "")
    by_group: dict = {}
    for f in files:
        by_group.setdefault(f.group or G_OTHER, []).append(f.path)
    subject = _subject(frozenset(by_group))
    why = [GROUP_WHY[g] for g in _ORDER if g in by_group and g in GROUP_WHY]
    lines = []
    for group in _ORDER:
        paths = by_group.get(group)
        if not paths:
            continue
        label = GROUP_LABEL.get(group, "other generated files")
        if len(paths) <= NAMES_MAX:
            lines.append("- %s: %s" % (label, ", ".join(paths)))
        else:
            lines.append("- %s: %d files" % (label, len(paths)))
    body = "\n".join(why + ["", "Changed:"] + lines +
                     ["", "Written-By: TokenSave Manager %s" % version])
    return Message(subject, body)
