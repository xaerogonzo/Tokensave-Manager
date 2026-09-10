"""helpers/instructions_split.py — move an append-only lesson log out of the
file every message loads, leaving an index behind.

Three projects here carry instruction chains between 280 KB and 960 KB. Almost
all of it is a **lesson log**: sections appended after each investigation, each
worth keeping and none of it needed on every message. `CLAUDE.md` is read in
full every single time, so the log is paid for continuously and consulted
rarely.

**The boundary is proposed, never decided.** The obvious mechanical
discriminators do not survive contact with the fleet — ALL-CAPS headings are
81% of one project's sections, 34% of another's and 0% of a third's, while all
three are logs. Casing is a project-local convention, so keying on it would be
extracting somebody's ritual and calling it a rule. What IS reliable is the
shape: a log grows by appending, so the operational instructions are at the top
and the log is the tail. This module finds the point where the kept part stops
fitting a byte budget, presents the whole ordered section list with cumulative
sizes, and lets a person move the line.

**Nothing is written until the complete transformation has been shown.** The
plan is the new `CLAUDE.md` in full, the new target file in full, and the
heading map — computed, returned, and only then applied. Nothing is written and
then presented.

**A source that moved is never overwritten.** `apply_split` takes the digest the
plan was computed from and refuses if the file has changed since. Two of these
projects have live Claude sessions appending to them right now; one grew 6.6 KB
between two commands while this was being written.

**The target is never `@include`d.** That is the entire point: the index is the
cheap part and the log is the expensive part, and including it would recreate
the problem the split exists to fix. `helpers/instructions_posture` will report
the chain unchanged in weight if that ever happens by accident.

Pure apart from `read_source` and `apply_split`. No Tk.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re

#: A heading that opens a top-level section. Level 1 is the document title and
#: is never a section boundary — moving it would decapitate the file.
_HEADING = re.compile(r"^(#{2})\s+(.*?)\s*#*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
#: Directive detection is delegated to `instructions_posture.scan_text`, which
#: already knows that a `@` inside a fenced block is a code sample and not an
#: include. There is deliberately no second regex here.

#: Where the log goes. Under `docs/` because that is where a thing you read
#: deliberately lives, and NOT anywhere the include chain reaches.
DEFAULT_TARGET = "docs/LESSONS.md"

#: How much of the file to keep loaded, in bytes. The same review threshold
#: `doctor_rules` uses, and for the same reason: it names a cost rather than a
#: fault.
DEFAULT_KEEP_BYTES = 50_000


@dataclasses.dataclass(frozen=True)
class Section:
    """One top-level section, with the byte cost of carrying it."""

    index: int
    title: str
    start: int                 # line index of the heading itself
    end: int                   # exclusive
    size: int                  # bytes, heading included
    has_directive: bool = False

    @property
    def anchor(self) -> str:
        """GitHub-style anchor, for the index entry to link to."""
        slug = re.sub(r"[^\w\s-]", "", self.title.lower())
        return re.sub(r"[\s_]+", "-", slug).strip("-")


@dataclasses.dataclass(frozen=True)
class SplitPlan:
    """The complete transformation, computed before anything is written."""

    source_rel: str
    target_rel: str
    preamble_lines: int
    kept: tuple
    moved: tuple
    #: The complete new contents. Not a diff — a diff of a 960 KB file is not
    #: something a person can check, and the point is that they can.
    new_source: str
    new_target: str
    #: sha256 of the source this was computed from. `apply_split` refuses if the
    #: file no longer matches.
    digest: str
    blocked: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked and bool(self.moved)

    @property
    def kept_bytes(self) -> int:
        return len(self.new_source.encode("utf-8"))

    @property
    def moved_bytes(self) -> int:
        return sum(s.size for s in self.moved)

    def summary(self) -> str:
        if self.blocked:
            return "refused: %s" % self.blocked
        return ("%d of %d sections move; %s stays loaded, %s moves to %s"
                % (len(self.moved), len(self.kept) + len(self.moved),
                   f"{self.kept_bytes:,} B", f"{self.moved_bytes:,} B",
                   self.target_rel))


# ── pure computation ─────────────────────────────────────────────────────────

def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_sections(text: str) -> "tuple[int, list]":
    """`(preamble_line_count, sections)`.

    Fenced blocks are skipped, for the same reason `instructions_posture` skips
    them: a `##` inside a code sample is not a section, and treating it as one
    would split a file in the middle of an example.
    """
    lines = text.splitlines()
    fence = None
    marks: list = []
    for i, line in enumerate(lines):
        hit = _FENCE.match(line)
        if hit:
            token = hit.group(1)
            fence = None if (fence and token[0] == fence) else (fence or token[0])
            continue
        if fence is not None:
            continue
        head = _HEADING.match(line)
        if head:
            marks.append((i, head.group(2)))

    # Which lines are REAL directives is `instructions_posture`'s question, not
    # this module's. Asking it rather than re-matching `^@` is what stops a
    # decorator inside a code sample reading as an include: the first version
    # here did re-match, and reported a directive in a section of all three
    # target projects that in fact carry exactly one, on line 1. One owner for
    # a shared fact.
    from helpers.instructions_posture import scan_text
    directives, _indented, _unclosed = scan_text(text)
    directive_lines = {lineno - 1 for lineno, _raw in directives}

    preamble = marks[0][0] if marks else len(lines)
    sections = []
    for n, (start, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        size = sum(len(l) + 1 for l in lines[start:end])
        sections.append(Section(
            index=n, title=title, start=start, end=end, size=size,
            has_directive=any(start <= d < end for d in directive_lines)))
    return preamble, sections


def _render_index(moved, target_rel: str) -> str:
    """What stays behind: one line per moved section, and where it went.

    The index is the whole justification for the move — a section nobody can
    find is a section that has been deleted with extra steps.
    """
    out = [
        "## Lessons (moved out of this file)",
        "",
        "These were appended here after individual investigations. They live",
        "in [`%s`](%s) and are **not** loaded on every" % (target_rel,
                                                            target_rel),
        "message — this index is. **If a title below names what you are",
        "about to touch, read that section before you start.** Headings there",
        "are verbatim, so grep the file for the line.",
        "",
    ]
    # Titles only, deliberately: a per-entry `#anchor` repeats the title almost
    # character for character, and once the log is gone this index IS the
    # remaining cost. Measured on the largest project: anchors took the index
    # from ~8.5 KB to ~17 KB, which is most of what the split leaves behind, in
    # exchange for a click. The heading is verbatim in the target, so a search
    # finds it either way.
    for section in moved:
        out.append("- %s" % section.title)
    out.append("")
    return "\n".join(out)


def compute_split(text: str, source_rel: str = "CLAUDE.md",
                  target_rel: str = DEFAULT_TARGET,
                  keep_bytes: int = DEFAULT_KEEP_BYTES,
                  keep_sections: "int | None" = None,
                  move_indices=None) -> SplitPlan:
    """The complete transformation. Pure — reads nothing, writes nothing.

    Three ways to say what moves, in increasing specificity:

    * nothing given — the byte budget suggests a tail boundary;
    * `keep_sections` — a boundary a person moved;
    * `move_indices` — an explicit set, which is the only one general enough.

    **The tail is not always the log, and that was measured rather than
    assumed.** This module opened by claiming a log grows by appending, so the
    operational instructions are at the top. Two of the three projects it was
    written for disprove it:

        Fortuna    the 339 KB log is section [2] of 5, with operational
                   sections on BOTH sides of it
        OpenChem   section [103] is the LAST one and is a 64 KB operational
                   standard, so a tail split would carry it off

    A boundary can only express "everything after here". A set can express what
    is actually true of these files, which is that the log is a contiguous run
    somewhere in the middle. The budget still picks the opening suggestion,
    because something has to, and a person still moves it.
    """
    preamble, sections = _compute_sections(text)
    lines = text.splitlines()

    if not sections:
        return SplitPlan(source_rel, target_rel, preamble, (), (), text, "",
                         digest_of(text),
                         blocked="the file has no top-level sections to move")

    if keep_sections is None:
        # Keep from the top until the next section would not fit. The preamble
        # is always kept and always counted -- it is where the include chain
        # lives.
        running = sum(len(l) + 1 for l in lines[:preamble])
        keep_sections = 0
        for section in sections:
            if running + section.size > keep_bytes and keep_sections > 0:
                break
            running += section.size
            keep_sections += 1

    if move_indices is None:
        keep_sections = max(0, min(keep_sections, len(sections)))
        chosen = {s.index for s in sections[keep_sections:]}
    else:
        chosen = {int(i) for i in move_indices}

    kept = tuple(s for s in sections if s.index not in chosen)
    moved = tuple(s for s in sections if s.index in chosen)

    if not moved:
        return SplitPlan(source_rel, target_rel, preamble, kept, (), text, "",
                         digest_of(text),
                         blocked="nothing is over the budget; there is no "
                                 "split to make")

    # The chain must not move. A directive below the boundary means the include
    # graph runs through the tail, and relocating it would break the very thing
    # the instruction file exists to do. Refuse rather than reorder.
    trapped = [s.title for s in moved if s.has_directive]
    if trapped:
        return SplitPlan(
            source_rel, target_rel, preamble, kept, moved, text, "",
            digest_of(text),
            blocked=("a section below the split point carries an @include, so "
                     "the chain runs through the part that would move: %s"
                     % "; ".join(trapped[:3])))

    # Join with the terminator the file already uses. `splitlines()` discards
    # them, so re-joining with a hard-coded "\n" converts a CRLF file wholesale
    # -- and every one of the three projects this exists for is CRLF. The real
    # change would then be one line in a diff that touched all 17,850 of them.
    # Owned by `instructions_wiring` rather than reimplemented here: a shared
    # fact gets one owner.
    from helpers.instructions_wiring import dominant_newline
    nl = dominant_newline(text)

    # The index goes exactly where the moved run was, not appended at the end.
    # With a middle split that is the difference between a document that still
    # reads in order and one whose sections have quietly been reshuffled around
    # the reader.
    index_lines = _render_index(moved, target_rel).split("\n")
    out = list(lines[:preamble])
    placed = False
    for section in sections:
        if section.index in chosen:
            if not placed:
                out.extend(index_lines)
                placed = True
            continue
        out.extend(lines[section.start:section.end])
    new_source = nl.join(out).rstrip(nl) + nl

    body = ["# Lessons",
            "",
            "Moved out of [`%s`](../%s) so they are not loaded on every"
            % (source_rel, source_rel),
            "message. Nothing here was edited; the sections are verbatim and in"
            " their",
            "original order.",
            ""]
    for section in moved:
        body.extend(lines[section.start:section.end])
    new_target = nl.join(body).rstrip(nl) + nl

    return SplitPlan(source_rel, target_rel, preamble, kept, moved,
                     new_source, new_target, digest_of(text))


# ── IO ───────────────────────────────────────────────────────────────────────

def read_source(project_root: str, source_rel: str = "CLAUDE.md") -> str:
    """The instruction file, or "" when it cannot be read.

    `newline=""` so a CRLF file stays CRLF through the round trip. A split that
    silently rewrote every line ending would bury the real change in a
    whole-file diff.
    """
    path = os.path.join(project_root, source_rel)
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ""


def apply_split(project_root: str, plan: SplitPlan) -> "tuple[bool, str]":
    """Write the plan, refusing if the source moved since it was computed.

    The guard is not ceremony. Two of the projects this exists for have live
    Claude sessions appending to their instruction files; one grew 6.6 KB
    between two commands while this module was being written. A plan computed
    from a file that has since changed would drop whatever arrived in between.
    """
    if plan.blocked:
        return False, "refused: %s" % plan.blocked

    current = read_source(project_root, plan.source_rel)
    if not current:
        return False, "could not re-read %s" % plan.source_rel
    if digest_of(current) != plan.digest:
        return False, ("%s changed since the plan was computed -- nothing was "
                       "written. Re-run the proposal." % plan.source_rel)

    target = os.path.join(project_root, plan.target_rel)
    if os.path.exists(target):
        return False, ("%s already exists; refusing to overwrite it"
                       % plan.target_rel)

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Target first: if this fails, the source is untouched and the project
        # is exactly as it was.
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(plan.new_target)
        with open(os.path.join(project_root, plan.source_rel), "w",
                  encoding="utf-8", newline="") as handle:
            handle.write(plan.new_source)
    except OSError as exc:
        return False, "write failed: %s" % exc

    return True, plan.summary()
