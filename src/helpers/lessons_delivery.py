"""lessons_delivery — the shared gotchas, delivered into each project.

The baseline's index says "read `docs/gotchas/x.md` before you start". Until this
module existed the index was a dead link in every project but the Manager: the
files lived in `templates/gotchas/` and nothing copied them anywhere. Measured on
Fortuna Lab, which carried a `project-baseline.md` and no gotchas at all -- which
is why the same lessons kept being re-learned and re-patched per project.

Each lesson is a `managed_copy` (sha-headed, compare-and-apply, never overwriting
a hand edit), delivered to a fixed destination under `docs/gotchas/`.

**The corpus is an explicit inventory, not a glob.** A new file in
`templates/gotchas/` changes what every project receives only by editing
`LESSONS` here, and `tests/test_lessons_delivery.py` fails until somebody does.
Delivery is bounded: a fixed list, fixed destinations, no directory walk of a
project or of the templates.

**Status is per artifact and independent of reach.** `read_lessons` reports one
fact per lesson. Whether the baseline chain RESOLVES (`instructions_posture`) is
a different question from whether a lesson is current, and neither is folded into
the other: a stale gotcha never makes a healthy chain read as stale.

**A sync is not a transaction.** Ten lessons is ten independent writes; one
edited or unreadable file must not stop the rest, and must not be reported as
"the sync failed". Each artifact gets its own outcome.

**Nothing here deletes.** A lesson removed from the templates leaves its project
copy where it is; reporting orphans is a later, separate concern.

Imports `managed_copy` only. It must not import `ignore_alignment` (which
imports this to name the companions) or anything UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from helpers import managed_copy as mc

GOTCHA_DIR = "docs/gotchas"

_GOTCHA_NAMES = (
    "agent-scripting",
    "ci-green-for-the-wrong-reason",
    "claude-md-external-includes",
    "customtkinter",
    "elevation-and-privilege",
    "empty-is-not-unknown",
    "moving-content-moves-its-guards",
    "nuitka-build-setup",
    "powershell-silent-failures",
    "shared-python-packages",
    "tests-that-pass-without-testing",
    "tkinter-patterns",
    "verifying-a-gui",
    "windows-filesystem",
    "windows-subprocess",
    "wiring-in-an-external-analyzer",
)


@dataclass(frozen=True)
class LessonSpec:
    #: Relative to `template_dir`, POSIX-style. Also the identity the copy's
    #: header records, so two lessons with identical bodies stay distinct.
    source_rel: str
    #: Relative to the project root, POSIX-style. Fixed, never user input.
    dest_rel: str


def _dest(name: str) -> str:
    return "%s/%s" % (GOTCHA_DIR, name)


#: Every lesson the Manager delivers. `NUITKA_GOTCHAS.md` sits beside, not
#: inside, `templates/gotchas/` but the index names it, so it ships with them.
LESSONS: "tuple[LessonSpec, ...]" = tuple(
    LessonSpec("gotchas/%s.md" % n, _dest(n + ".md")) for n in _GOTCHA_NAMES
) + (LessonSpec("NUITKA_GOTCHAS.md", _dest("NUITKA_GOTCHAS.md")),)


@dataclass(frozen=True)
class Lesson:
    spec: LessonSpec
    #: The source's text, or "" when it could not be read.
    text: str


def dest_rels() -> "tuple[str, ...]":
    """Where every lesson lands, for anything that must name the companions."""
    return tuple(spec.dest_rel for spec in LESSONS)


def load_corpus(template_dir: str) -> "tuple[Lesson, ...]":
    """Read every lesson source. An unreadable one is kept, with empty text, so
    it is reported per artifact rather than silently dropped from the set."""
    return tuple(
        Lesson(spec, mc.read_source(os.path.join(template_dir,
                                                 *spec.source_rel.split("/"))))
        for spec in LESSONS)


# ── what is there ────────────────────────────────────────────────────────

#: A source we could not read: distinct from every copy state, because the
#: copy may be fine and the fault is ours.
STATE_SOURCE_UNREADABLE = "source_unreadable"

#: States a delivery may act on. Everything else is a person's file, a damaged
#: one, or one we cannot judge, and is never overwritten.
ACTIONABLE = (mc.COPY_ABSENT, mc.COPY_OUTDATED)


@dataclass(frozen=True)
class LessonFact:
    dest_rel: str
    source_rel: str
    state: str

    @property
    def actionable(self) -> bool:
        return self.state in ACTIONABLE


def read_lessons(project_root: str, corpus) -> "tuple[LessonFact, ...]":
    """One fact per lesson. Reads each destination and writes nothing."""
    out = []
    for lesson in corpus:
        spec = lesson.spec
        if not lesson.text:
            out.append(LessonFact(spec.dest_rel, spec.source_rel,
                                  STATE_SOURCE_UNREADABLE))
            continue
        facts = mc.read_copy(os.path.join(project_root, *spec.dest_rel.split("/")),
                             spec.source_rel)
        out.append(LessonFact(spec.dest_rel, spec.source_rel,
                              mc.classify_copy(facts, mc.content_sha(lesson.text))))
    return tuple(out)


def summarize(states) -> str:
    """"11 current, 1 outdated" -- counts in a fixed order, zeros omitted."""
    states = list(states)
    order = (mc.COPY_CURRENT, mc.COPY_OUTDATED, mc.COPY_ABSENT, mc.COPY_EDITED,
             mc.COPY_INVALID, mc.COPY_UNMANAGED, mc.COPY_UNREADABLE,
             STATE_SOURCE_UNREADABLE)
    parts = ["%d %s" % (states.count(s), s.replace("_", " "))
             for s in order if states.count(s)]
    return ", ".join(parts) or "none"


# ── delivering ───────────────────────────────────────────────────────────

OUT_CURRENT = "current"
OUT_WRITTEN = "written"
#: Not written on purpose: the file belongs to a person or is not ours.
OUT_REFUSED = "refused"
OUT_FAILED = "failed"

_REFUSED_STATES = (mc.COPY_EDITED, mc.COPY_UNMANAGED, mc.COPY_INVALID,
                   mc.COPY_UNREADABLE)


@dataclass(frozen=True)
class LessonOutcome:
    dest_rel: str
    outcome: str
    state_before: str = ""
    reason: str = ""


def sync_lessons(project_root: str, corpus) -> "tuple[LessonOutcome, ...]":
    """Deliver every lesson, independently. Never stops at the first problem."""
    out = []
    for lesson in corpus:
        spec = lesson.spec
        written = mc.write_managed(
            project_root,
            mc.ManagedCopySpec(spec.dest_rel, spec.source_rel, lesson.text))
        if written.ok:
            out.append(LessonOutcome(
                spec.dest_rel,
                OUT_WRITTEN if written.changed else OUT_CURRENT,
                written.state_before))
        elif written.state_before in _REFUSED_STATES:
            out.append(LessonOutcome(spec.dest_rel, OUT_REFUSED,
                                     written.state_before, written.error))
        else:
            out.append(LessonOutcome(spec.dest_rel, OUT_FAILED,
                                     written.state_before, written.error))
    return tuple(out)


def outcomes_text(outcomes) -> str:
    """"14 current, 2 written, 1 refused: docs/gotchas/x.md is edited ..."."""
    outcomes = list(outcomes)
    counts = ["%d %s" % (sum(1 for o in outcomes if o.outcome == kind), kind)
              for kind in (OUT_WRITTEN, OUT_CURRENT, OUT_REFUSED, OUT_FAILED)
              if any(o.outcome == kind for o in outcomes)]
    detail = ["%s: %s" % (o.dest_rel, o.reason) for o in outcomes
              if o.outcome in (OUT_REFUSED, OUT_FAILED) and o.reason]
    return ", ".join(counts) + ("  [" + "; ".join(detail) + "]" if detail else "")


def written_files(outcomes) -> "tuple[str, ...]":
    return tuple(o.dest_rel for o in outcomes if o.outcome == OUT_WRITTEN)
