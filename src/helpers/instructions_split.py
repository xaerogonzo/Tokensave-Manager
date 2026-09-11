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
_HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*#*$")
#: A paragraph that opens with a bold sentence. Measured across all three
#: projects, this is a RELIABLE entry marker rather than a local ritual --
#: 547 vs 34 wrap artifacts on one, 180 vs 16 and 814 vs 57 on the others.
#: It is used only to COUNT entries for a navigation note, never to split:
#: indexing them individually costs 32,840 B on the project that needs it
#: most, which is more than that project currently keeps loaded at all.
_BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*")
#: Below this many, a section is small enough to read and the note is noise.
_ENTRY_NOTE_MIN = 8
#: The heading the index is written under. Detection and rendering share it,
#: because a SECOND split of an already-split file must merge into the index
#: that is there rather than write a rival one beside it -- which is exactly
#: what granularity makes possible, and what it did on the first attempt.
_INDEX_TITLE = "Lessons (moved out of this file)"
#: How a target this module wrote identifies itself. Appending is only ever
#: offered for our own file: adding to somebody's hand-written notes because
#: they happened to choose the same path is a smaller surprise than
#: overwriting them, but it is still a surprise.
_TARGET_MARKER = "so they are not loaded on every"


def is_index_section(section) -> bool:
    """Is this row the index a previous split wrote?

    It must not be offered as movable. Ticking it sends the index itself into
    the target, which both loses it from the loaded file and stops the merge
    from finding it -- so the next split writes a second index beside nothing.
    """
    return section.level == 2 and section.title == _INDEX_TITLE


def is_our_target(target_text: str) -> bool:
    """Did a previous split write this file?"""
    head = target_text.replace(chr(13), "").lstrip().split(chr(10))
    return bool(head) and head[0].strip() == "# Lessons" and         _TARGET_MARKER in target_text[:600]
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
#: Directive detection is delegated to `instructions_posture.scan_text`, which
#: already knows that a `@` inside a fenced block is a code sample and not an
#: include. There is deliberately no second regex here.

#: Where the log goes. Under `docs/` because that is where a thing you read
#: deliberately lives, and NOT anywhere the include chain reaches.
DEFAULT_TARGET = "docs/LESSONS.md"

#: The file Claude Code actually reads, and the one a split empties. A constant
#: because it is the default of five separate parameters.
DEFAULT_SOURCE = "CLAUDE.md"

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
    size: int                  # bytes of THIS section's own body, heading
                               # included and children excluded
    has_directive: bool = False
    level: int = 2
    #: Index of the enclosing level-2 section, or None at the top.
    parent: "int | None" = None
    #: Own bytes plus every descendant's. What a reader actually pays.
    total_size: int = 0
    #: Paragraph-initial bold lead-ins in the own body. A cheap navigation
    #: note for a log that carries no headings at all -- which is the shape
    #: of the largest one here.
    entry_count: int = 0

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
    #: True when this adds to a target a previous split already wrote. The
    #: refusal to overwrite still stands for a target we did not write.
    appending: bool = False
    #: sha256 of the existing target, when appending. Guarded exactly like the
    #: source: two files are being rewritten, so two digests are checked.
    target_digest: str = ""

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
    """`(preamble_line_count, sections)` as a two-level tree.

    Fenced blocks are skipped, for the same reason `instructions_posture` skips
    them: a `##` inside a code sample is not a section, and treating it as one
    would split a file in the middle of an example.

    **A section is `##` OR `###`, because `##` is not the unit of a lesson.**
    Measured on the projects this exists for: one keeps a 64,030 B operational
    section holding twenty `###` lessons, which a `##`-only model can only move
    whole or not at all. A section's own `size` therefore EXCLUDES its
    children, and `total_size` is what a reader actually pays.
    """
    lines = text.splitlines()
    fence = None
    fenced = []
    marks: list = []
    for i, line in enumerate(lines):
        hit = _FENCE.match(line)
        if hit:
            token = hit.group(1)
            fence = None if (fence and token[0] == fence) else (fence or token[0])
            fenced.append(True)
            continue
        fenced.append(fence is not None)
        if fence is not None:
            continue
        head = _HEADING.match(line)
        if head:
            marks.append((i, len(head.group(1)), head.group(2)))

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
    last_top = None
    for n, (start, level, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        size = sum(len(l) + 1 for l in lines[start:end])
        entries = sum(1 for j in range(start, end)
                      if not fenced[j] and _BOLD_LEAD.match(lines[j])
                      and j > 0 and not lines[j - 1].strip())
        if level == 2:
            last_top = n
            parent = None
        else:
            parent = last_top
        sections.append(Section(
            index=n, title=title, start=start, end=end, size=size,
            has_directive=any(start <= d < end for d in directive_lines),
            level=level, parent=parent, total_size=size, entry_count=entries))

    # Second pass: a parent's total includes its children. Done here rather
    # than in the loop because a parent is built before its children exist.
    totals = {s.index: s.size for s in sections}
    for section in sections:
        if section.parent is not None:
            totals[section.parent] += section.size
    sections = [dataclasses.replace(s, total_size=totals[s.index])
                for s in sections]
    return preamble, sections


def children_of(sections, index: int) -> list:
    """Every level-3 section under `index`, in document order."""
    return [s for s in sections if s.parent == index]


def with_descendants(sections, chosen: set) -> set:
    """Ticking a parent moves what is inside it.

    One pass is enough: there are two levels, and a parent always precedes its
    children in index order.
    """
    out = set(chosen)
    for section in sections:
        if section.parent is not None and section.parent in out:
            out.add(section.index)
    return out


def _render_index(moved, target_rel: str, carried=()) -> str:
    """What stays behind: the moved titles, nested, and where they went.

    The index is the whole justification for the move — a section nobody can
    find is a section that has been deleted with extra steps.

    **A log with no headings gets a count, not 547 index entries.** One project
    here keeps its entire 339 KB log under a single `##`, its entries marked by
    bold opening sentences. Indexing those individually is reliable — they are
    paragraph-initial in 547 cases against 34 wrap artifacts — and costs
    32,840 B, which is MORE than that project now keeps loaded in total. So the
    index says how many there are and how they are marked, and the reader greps.
    That is the trade this module exists to make, applied to itself.
    """
    out = [
        "## " + _INDEX_TITLE,
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
    # Entries a previous split already listed. They are carried verbatim: the
    # sections they name are in the target file, not in this one, so there is
    # nothing here to re-derive them from.
    out.extend(carried)
    moved_indices = {s.index for s in moved}
    for section in moved:
        if section.parent is not None and section.parent in moved_indices:
            out.append("  - %s" % section.title)
            continue
        line = "- %s" % section.title
        has_children = any(s.parent == section.index for s in moved)
        if not has_children and section.entry_count >= _ENTRY_NOTE_MIN:
            line += (" — %d entries, each opening with a bold sentence"
                     % section.entry_count)
        out.append(line)
    out.append("")
    return "\n".join(out)


def _existing_entries(lines, section) -> list:
    """The `- ` lines of an index a previous split wrote.

    Read rather than regenerated, because the sections they name have already
    moved: this file no longer contains anything to re-derive them from.
    """
    out = []
    for line in lines[section.start:section.end]:
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(line.rstrip())
    return out


def _index_position(sections, moved, chosen: set) -> int:
    """The line the index goes on: where the moved run was.

    With a NESTED move the obvious answer is wrong in a way that is quiet. If
    the first moved thing is a `###` inside a KEPT `##`, putting a level-2
    index heading at its line would terminate the parent section, and every
    remaining line of that parent would silently read as part of the index. So
    the index goes after the parent's subtree instead, which is still where the
    reader was looking.
    """
    first = moved[0]
    if first.parent is not None and first.parent not in chosen:
        kin = [s for s in sections
               if s.index == first.parent or s.parent == first.parent]
        return max(s.end for s in kin)
    return first.start


def _render_new_source(lines, preamble, sections, moved, chosen,
                       target_rel: str, nl: str) -> str:
    """The source with the moved run replaced by an index, in place.

    Extracted from `compute_split` (2026-09-11), which was complexity 28
    against a cap of 18. Not an arbitrary slice taken to move a number: the
    docstring already described this as choose-then-render-two-files, and
    this is one of the two renders. Pure, like everything else here.
    """
    # The index goes exactly where the moved run was, not appended at the end.
    # With a middle split that is the difference between a document that still
    # reads in order and one whose sections have quietly been reshuffled around
    # the reader.
    # A SECOND split merges into the index the first one wrote, rather than
    # writing a rival block beside it. Granularity is what makes a second split
    # worth doing -- one project keeps a 64 KB operational section holding
    # twenty `###` lessons -- and the first attempt produced two identical
    # `## Lessons` headings in the same file.
    prior = next((s for s in sections
                  if s.level == 2 and s.title == _INDEX_TITLE
                  and s.index not in chosen), None)
    carried = _existing_entries(lines, prior) if prior is not None else []
    replaced = {prior.index} if prior is not None else set()

    index_lines = _render_index(moved, target_rel, carried).split("\n")
    # Back where the previous index was, when there is one: that is where the
    # reader already knows to look.
    insert_at = (prior.start if prior is not None
                 else _index_position(sections, moved, chosen))
    out = list(lines[:preamble])
    placed = False
    for section in sections:
        if not placed and section.start >= insert_at:
            out.extend(index_lines)
            placed = True
        if section.index in chosen or section.index in replaced:
            continue
        out.extend(lines[section.start:section.end])
    if not placed:
        out.extend(index_lines)
    new_source = nl.join(out).rstrip(nl) + nl
    return new_source


def _render_new_target(lines, sections, moved, chosen, source_rel: str,
                       target_text: str, appending: bool, nl: str) -> str:
    """The target: whatever is already there, verbatim, plus what arrives.

    The other half of the render. The refusal for a target this tool did not
    write stays in `compute_split`, because it returns a blocked PLAN rather
    than a string -- a helper that can only answer one question is easier to
    be sure about than one that sometimes answers a different one.
    """
    if appending:
        # Everything already there, verbatim, plus what is arriving. Rewriting
        # the existing target would put this module in the business of editing
        # a file a person may since have edited themselves.
        body = target_text.replace("\r\n", "\n").rstrip("\n").split("\n") + [""]
    else:
        body = ["# Lessons",
                "",
                "Moved out of [`%s`](../%s) so they are not loaded on every"
                % (source_rel, source_rel),
                "message. Section CONTENT is verbatim and in its original",
                "order. The only lines this file adds are `## From:` headings,",
                "which say where a subsection came from when its parent stayed",
                "behind.",
                ""]
    # A `###` whose parent stayed behind would otherwise be filed under
    # whatever unrelated `##` happens to precede it here. Measured: moving
    # twenty subsections out of one project's operational section landed them
    # all under a Gasteiger lesson. The heading is ADDED context, never an edit
    # to what moved.
    context = None
    for section in moved:
        if section.parent is not None and section.parent not in chosen:
            if context != section.parent:
                context = section.parent
                body.append("## From: %s" % sections[context].title)
                body.append("")
        else:
            context = None
        body.extend(lines[section.start:section.end])
    new_target = nl.join(body).rstrip(nl) + nl
    return new_target


def compute_split(text: str, source_rel: str = "CLAUDE.md",
                  target_rel: str = DEFAULT_TARGET,
                  keep_bytes: int = DEFAULT_KEEP_BYTES,
                  keep_sections: "int | None" = None,
                  move_indices=None, target_text: str = "") -> SplitPlan:
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

    # A `##` carries its `###` children with it. Leaving them behind would
    # orphan them under whatever heading happened to follow.
    chosen = with_descendants(sections, chosen)

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

    new_source = _render_new_source(lines, preamble, sections, moved,
                                    chosen, target_rel, nl)

    appending = bool(target_text.strip())
    if appending and not is_our_target(target_text):
        return SplitPlan(
            source_rel, target_rel, preamble, kept, moved, text, "",
            digest_of(text),
            blocked=("%s exists and was not written by this tool, so there is "
                     "nothing safe to add it to" % target_rel))
    new_target = _render_new_target(lines, sections, moved, chosen,
                                    source_rel, target_text, appending, nl)

    return SplitPlan(source_rel, target_rel, preamble, kept, moved,
                     new_source, new_target, digest_of(text),
                     appending=appending,
                     target_digest=digest_of(target_text) if appending else "")


# ── what else in the repository is keyed to this file ────────────────────────
#
# A split moves content across a file boundary, and therefore across every
# boundary anything else keyed to that filename draws. Measured across the three
# projects this module was applied to, in three distinct shapes:
#
#   a guard's inclusion list   17,870 lines of citations left a doc-currency
#                              guard's reach; the covered list is a hand-kept
#                              literal, so nothing went red
#   a whole-file exemption     the source was exempted ENTIRELY for containing a
#                              syntax example, so moving content out swept 21
#                              real citations for the first time -- by accident
#   prose citations            "see CLAUDE.md" in committed source, now pointing
#                              at a file that no longer holds the thing cited.
#                              LexForge: 27 mentions across 22 files.
#                              Fortuna:  25 across 14.
#
# None of the three produces an error. The file still exists; the reader is just
# sent to the wrong place. So the split reports the population it is about to
# strand, and claims nothing beyond that.

#: Directories never scanned. A deliberate omission, counted separately from a
#: file we could not read -- those are different facts (see `ScanStats`).
_SCAN_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".tokensave", ".tokensave-manager", ".pyscope",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vs",
    ".tox", ".nox", ".eggs", ".next", ".nuxt", ".gradle",
    # Build output and vendored trees. A mention inside one is a COPY of an
    # authored mention, and "fix the citation in dist/" is never the action.
    "dist", "build", "out", "target", "site-packages", "vendor",
    "coverage", "htmlcov",
    # Whole duplicate checkouts. Measured on one project: `.claude/worktrees/`
    # held a second copy of a 36 MB vendored bundle, so a scan counted three
    # of everything and hit its cap before reaching the authored source.
    ".claude",
})

#: Read no single file larger than this. A guard list, an exemption or a prose
#: citation lives in an authored file; the things that blow the budget are
#: minified vendor bundles. Measured on one project: twelve files over 2 MB held
#: 45% of 311 MB of candidates.
MAX_FILE_BYTES = 2_000_000

#: Suffixes read at all. An allowlist rather than a binary sniff: a mention is a
#: textual thing, and guessing at unknown formats would put decode failures into
#: `unreadable_files` for files nobody expected to be scanned.
_SCAN_SUFFIXES = frozenset({
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".ps1", ".psm1", ".bat",
    ".cmd", ".sh", ".toml", ".json", ".jsonc", ".yaml", ".yml", ".cfg",
    ".ini", ".md", ".markdown", ".rst", ".txt", ".html", ".css",
})

#: Which half of the report a file lands in. Prose mentions in a CHANGELOG are
#: usually fine; a guard list in a .py is where the risk concentrates.
_DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt"})

#: Total bytes read before the scan stops. Hitting it makes the result a LOWER
#: BOUND, which is a field rather than a log line -- see `ScanStats.truncated`.
DEFAULT_SCAN_CAP_BYTES = 40_000_000


def mention_pattern(basename: str) -> "re.Pattern":
    """Match `basename` as a PATH TOKEN, never as a substring.

    A substring search makes the count noise immediately, and noise would
    undercut the only claim this scan makes -- that the number is a population
    worth a person's attention.

    The rule, and both halves are load-bearing:

    * the character before must not be a word character, so `MY_CLAUDE.md` and
      `my-CLAUDE.md` are different files and do not match;
    * the character after must not begin an extension, so `CLAUDE.md.bak` and
      `CLAUDE.mdx` do not match -- while `see CLAUDE.md.` at the end of a
      sentence does, because there the dot is punctuation and nothing follows it.

    Case-insensitive, deliberately: this matches TEXT INSIDE FILES, where prose
    casing varies and a mention is a mention. It is not a filesystem path
    comparison, which is a different question with a different owner
    (`instructions_posture.canonical`). Sharing one rule between the two gives
    either a case-sensitive prose search or a path comparison that matches
    things the user never named.
    """
    stem = re.escape(basename)
    return re.compile(r"(?<![A-Za-z0-9_\-])" + stem +
                      r"(?![A-Za-z0-9_\-])(?!\.[A-Za-z0-9])",
                      re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class ScanStats:
    """How much of the repository the scan actually saw.

    `truncated`, or a non-empty `unreadable_files`, means every count derived
    from this scan is a **lower bound** and must be rendered as one. That is
    this project's "unknown is never zero" rule, and it is why these are fields:
    a caller cannot print "22 files mention CLAUDE.md" while holding evidence
    that the scan was incomplete, unless it chooses to ignore a field it can see.
    """

    scanned_files: int = 0
    #: Directories pruned by `_SCAN_EXCLUDED_DIRS`, counted rather than walked:
    #: walking an excluded tree to size it defeats the exclusion, and on one
    #: project that tree was a duplicate checkout.
    excluded_dirs: int = 0
    #: Files skipped for a suffix outside the allowlist. A deliberate omission
    #: -- NOT the same as unreadable.
    excluded_files: int = 0
    #: Skipped for exceeding `MAX_FILE_BYTES`. Deliberate like an exclusion, but
    #: unlike one it is a file we might have expected to read, so it makes the
    #: result a lower bound.
    oversize_files: tuple = ()
    #: Permission denied, decode failure. An absence of knowledge, and never
    #: equivalent to "contains no mention".
    unreadable_files: tuple = ()
    scanned_bytes: int = 0
    cap_bytes: int = DEFAULT_SCAN_CAP_BYTES
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return (not self.truncated and not self.unreadable_files
                and not self.oversize_files)


@dataclasses.dataclass(frozen=True)
class MentionReport:
    """Textual mentions of the source path elsewhere in the repository.

    **This reports textual mentions and nothing more. It does NOT establish
    that a mention is live, semantic, stale, executable, or user-visible. It is
    a population, not a dependency graph.**

    That sentence is here rather than in a commit message because the next
    caller will otherwise treat this as proof of a dependency. All four of

        see CLAUDE.md        # CLAUDE.md        "CLAUDE.md"        path = "CLAUDE.md"

    are the same thing to this scan, and only the last is a dependency.
    """

    #: `(rel_path, kind, occurrences)` with `kind` in {"code", "docs"}, code
    #: first: that is where a guard list or an exemption would live.
    files_with_mentions: tuple = ()
    #: Deliberately a second, separately named field. One file mentioning the
    #: source twice is ONE file and TWO occurrences, and the two answer
    #: different questions -- nothing here is called `count`.
    total_mentions: int = 0
    stats: ScanStats = dataclasses.field(default_factory=ScanStats)

    @property
    def code_files(self) -> tuple:
        return tuple(f for f in self.files_with_mentions if f[1] == "code")

    @property
    def doc_files(self) -> tuple:
        return tuple(f for f in self.files_with_mentions if f[1] == "docs")

    @property
    def is_lower_bound(self) -> bool:
        """Render as "at least N" rather than "N"."""
        return not self.stats.complete

    def summary(self) -> str:
        n = len(self.files_with_mentions)
        lead = "at least %d files" % n if self.is_lower_bound else "%d files" % n
        return ("%s in this repository mention %s (%d code, %d docs)"
                % (lead, self._source, len(self.code_files),
                   len(self.doc_files)))

    #: Set by `textual_mentions`; only used for rendering.
    _source: str = "CLAUDE.md"


def textual_mentions(entries, stats: ScanStats, source_rel: str = "CLAUDE.md",
                     target_rel: str = DEFAULT_TARGET) -> MentionReport:
    """Count mentions of `source_rel` across `entries`. Pure.

    `entries` is an iterable of `(rel_path, text)` with forward-slash relative
    paths, so this function never touches a filesystem and a report is
    comparable across machines.

    The source and the target are excluded from their own report: the target's
    own header links back to the source, and counting that would be the tool
    reporting itself.
    """
    pattern = mention_pattern(os.path.basename(source_rel))
    skip = {source_rel.replace("\\", "/").lower(),
            target_rel.replace("\\", "/").lower()}

    found = []
    total = 0
    for rel, text in entries:
        rel = rel.replace("\\", "/")
        if rel.lower() in skip:
            continue
        hits = len(pattern.findall(text))
        if not hits:
            continue
        suffix = os.path.splitext(rel)[1].lower()
        kind = "docs" if suffix in _DOC_SUFFIXES else "code"
        found.append((rel, kind, hits))
        total += hits

    # Code first -- a guard list or an exemption lives there, and a CHANGELOG
    # mentioning the file by name is usually exactly right.
    found.sort(key=lambda row: (row[1] != "code", row[0].lower()))
    return MentionReport(files_with_mentions=tuple(found), total_mentions=total,
                         stats=stats, _source=os.path.basename(source_rel))


def read_repo_text(project_root: str,
                   cap_bytes: int = DEFAULT_SCAN_CAP_BYTES,
                   max_file_bytes: int = MAX_FILE_BYTES, _walk=os.walk):
    """`(entries, stats)` for `textual_mentions`. The only IO here.

    **Files are visited in lexical order**, and that is not tidiness. A byte cap
    applied to `os.walk`'s own ordering makes the result non-reproducible: the
    same unchanged repository could report 22 files today and 19 tomorrow,
    purely because a different file consumed the cap first. Collect, sort, then
    read.

    Excluded directories are counted, never walked. Sizing an excluded tree
    means descending into it, which is the opposite of excluding it -- and on
    one project that tree was a full duplicate checkout.

    `_walk` is a seam, and it exists because the guard cannot be written
    without one: every filesystem this runs on already hands back a small
    directory in name order, so deleting the sort changes nothing observable
    and the test passes against the implementation it rejects. It did exactly
    that. Handing the walk a different order is the only way to see the sort
    work.
    """
    root = os.path.abspath(project_root)
    candidates = []
    excluded_dirs = 0
    excluded_files = 0
    oversize = []
    for dirpath, dirnames, filenames in _walk(root):
        pruned = [d for d in dirnames if d in _SCAN_EXCLUDED_DIRS]
        excluded_dirs += len(pruned)
        dirnames[:] = [d for d in dirnames if d not in _SCAN_EXCLUDED_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in _SCAN_SUFFIXES:
                excluded_files += 1
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            if size > max_file_bytes:
                oversize.append(rel)
                continue
            candidates.append((rel, full))
    candidates.sort(key=lambda pair: pair[0].lower())

    entries = []
    unreadable = []
    used = 0
    truncated = False
    for rel, full in candidates:
        if used >= cap_bytes:
            truncated = True
            break
        try:
            with open(full, encoding="utf-8-sig", newline="") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError, ValueError):
            unreadable.append(rel)
            continue
        used += len(text.encode("utf-8", "replace"))
        entries.append((rel, text))

    return entries, ScanStats(
        scanned_files=len(entries), excluded_dirs=excluded_dirs,
        excluded_files=excluded_files, oversize_files=tuple(sorted(oversize)),
        unreadable_files=tuple(unreadable), scanned_bytes=used,
        cap_bytes=cap_bytes, truncated=truncated)


def scan_mentions(project_root: str, source_rel: str = "CLAUDE.md",
                  target_rel: str = DEFAULT_TARGET,
                  cap_bytes: int = DEFAULT_SCAN_CAP_BYTES,
                  max_file_bytes: int = MAX_FILE_BYTES) -> MentionReport:
    """IO wrapper over `read_repo_text` + `textual_mentions`."""
    entries, stats = read_repo_text(project_root, cap_bytes, max_file_bytes)
    return textual_mentions(entries, stats, source_rel, target_rel)


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
    exists = os.path.exists(target)
    if exists and not plan.appending:
        return False, ("%s already exists; refusing to overwrite it"
                       % plan.target_rel)
    if plan.appending:
        # Two files are being rewritten, so two digests are checked. Appending
        # to a target that moved would interleave this content with whatever
        # arrived in between.
        if not exists:
            return False, ("%s was there when the plan was computed and is "
                           "not now" % plan.target_rel)
        if digest_of(read_source(project_root, plan.target_rel)) !=                 plan.target_digest:
            return False, ("%s changed since the plan was computed -- nothing "
                           "was written." % plan.target_rel)

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
