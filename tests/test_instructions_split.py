"""Guards on `helpers/instructions_split.py`.

The load-bearing ones, each protecting something that would be silent:

* the include chain must never end up in the part that moves;
* nothing is written from a plan computed against a file that has since
  changed — two of the target projects have live sessions appending to them;
* the moved content is verbatim, because a "split" that edits is a rewrite;
* the target is never reachable from the include chain, which is the whole
  point of moving it.
"""

import os

import pytest

from helpers.instructions_split import (
    DEFAULT_TARGET, SplitPlan, _compute_sections, apply_split, children_of,
    compute_split, digest_of, is_our_target, read_source,
)
from helpers.instructions_posture import scan_text


def doc(sections, preamble="@BASIC_INSTRUCTIONS.md\n"):
    out = ["# Project — notes", "", preamble.rstrip("\n"), ""]
    for title, body in sections:
        out.append("## %s" % title)
        out.append("")
        out.append(body)
        out.append("")
    return "\n".join(out)


def big(n):
    return "x" * n


# ── finding the sections ─────────────────────────────────────────────────────

class TestSections:
    def test_a_heading_inside_a_fence_is_not_a_section(self):
        text = doc([("Real", "```\n## Not a heading\n```\nbody")])
        plan = compute_split(text, keep_sections=0)
        assert [s.title for s in plan.moved] == ["Real"]

    def test_level_one_is_never_a_section_boundary(self):
        """Moving the document title would decapitate the file."""
        text = doc([("A", big(10)), ("B", big(10))])
        plan = compute_split(text, keep_sections=1)
        assert "# Project — notes" in plan.new_source
        assert "# Project — notes" not in plan.new_target

    def test_the_preamble_is_always_kept(self):
        """It is where the include chain lives."""
        text = doc([("A", big(10))])
        plan = compute_split(text, keep_sections=0)
        assert "@BASIC_INSTRUCTIONS.md" in plan.new_source


# ── the chain must not move ──────────────────────────────────────────────────

class TestTheChainNeverMoves:
    def test_a_directive_below_the_boundary_refuses_the_split(self):
        """Relocating an `@include` would break the file's whole purpose.

        Refused rather than reordered: silently hoisting the section would
        change the document a person wrote, and silently moving it would break
        the chain.
        """
        text = doc([("Head", big(10)),
                    ("Carries the chain", "@shared/extra.md\nmore")])
        plan = compute_split(text, keep_sections=1)
        assert plan.blocked
        assert "@include" in plan.blocked
        assert plan.ok is False

    def test_a_directive_above_the_boundary_is_fine(self):
        text = doc([("Carries the chain", "@shared/extra.md"),
                    ("Log entry", big(50))])
        plan = compute_split(text, keep_sections=1)
        assert not plan.blocked
        assert "@shared/extra.md" in plan.new_source

    def test_the_target_is_never_an_include(self):
        """The entire point. Including the log recreates the problem."""
        text = doc([("Head", big(10)), ("Log", big(200))])
        plan = compute_split(text, keep_sections=1)
        directives, _indented, _fence = scan_text(plan.new_source)
        targets = [raw for _line, raw in directives]
        assert DEFAULT_TARGET not in targets
        assert not any("LESSONS" in t for t in targets)


# ── the boundary ─────────────────────────────────────────────────────────────

class TestBoundary:
    def test_the_budget_picks_a_starting_suggestion(self):
        text = doc([("A", big(1000)), ("B", big(1000)), ("C", big(1000))])
        plan = compute_split(text, keep_bytes=2500)
        assert len(plan.kept) == 2
        assert len(plan.moved) == 1

    def test_at_least_one_section_is_kept_even_if_it_blows_the_budget(self):
        """A file whose first section alone exceeds the budget still splits.

        Keeping zero sections would leave a file that is only an index, which
        is a different and much larger decision than moving a log.
        """
        text = doc([("Huge", big(9000)), ("B", big(10))])
        plan = compute_split(text, keep_bytes=100)
        assert len(plan.kept) == 1

    def test_an_explicit_boundary_overrides_the_budget(self):
        """The expected path: the budget suggests, the human decides."""
        text = doc([("A", big(10)), ("B", big(10)), ("C", big(10))])
        plan = compute_split(text, keep_bytes=10_000, keep_sections=1)
        assert [s.title for s in plan.moved] == ["B", "C"]

    def test_nothing_to_move_is_reported_not_pretended(self):
        text = doc([("A", big(10))])
        plan = compute_split(text, keep_bytes=10_000)
        assert plan.blocked
        assert plan.ok is False

    def test_a_file_with_no_sections_is_refused(self):
        plan = compute_split("# Title\n\njust prose\n")
        assert plan.blocked
        assert "no top-level sections" in plan.blocked


# ── the transformation ───────────────────────────────────────────────────────

class TestTransformation:
    def test_moved_content_is_verbatim_and_in_order(self):
        """A split that edits is a rewrite, and nobody asked for a rewrite."""
        text = doc([("Head", big(10)),
                    ("First lesson", "alpha body"),
                    ("Second lesson", "beta body")])
        plan = compute_split(text, keep_sections=1)
        assert "## First lesson" in plan.new_target
        assert "alpha body" in plan.new_target
        assert plan.new_target.index("First lesson") < \
            plan.new_target.index("Second lesson")

    def test_every_moved_section_is_named_in_the_index(self):
        """A section nobody can find has been deleted with extra steps."""
        text = doc([("Head", big(10)), ("Lesson one", big(50)),
                    ("Lesson two", big(50))])
        plan = compute_split(text, keep_sections=1)
        for section in plan.moved:
            assert section.title in plan.new_source

    def test_the_index_points_at_the_target_file(self):
        text = doc([("Head", big(10)), ("Lesson", big(50))])
        plan = compute_split(text, keep_sections=1)
        assert DEFAULT_TARGET in plan.new_source

    def test_nothing_is_lost_between_the_two_files(self):
        """Every section title survives on one side or the other."""
        titles = ["Head", "One", "Two", "Three"]
        text = doc([(t, big(40)) for t in titles])
        plan = compute_split(text, keep_sections=1)
        for title in titles:
            assert (title in plan.new_source) or (title in plan.new_target)

    def test_the_kept_file_is_smaller_than_it_was(self):
        text = doc([("Head", big(100)), ("Log", big(5000))])
        plan = compute_split(text, keep_sections=1)
        assert plan.kept_bytes < len(text.encode("utf-8"))
        assert plan.moved_bytes > 0


# ── writing ──────────────────────────────────────────────────────────────────

class TestApply:
    def _project(self, tmp_path, text):
        (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8", newline="")
        return str(tmp_path)

    def test_a_clean_apply_writes_both_files(self, tmp_path):
        text = doc([("Head", big(10)), ("Log", big(500))])
        root = self._project(tmp_path, text)
        plan = compute_split(text, keep_sections=1)
        ok, message = apply_split(root, plan)
        assert ok, message
        assert (tmp_path / "docs" / "LESSONS.md").is_file()
        assert "Log" in (tmp_path / "docs" / "LESSONS.md").read_text(
            encoding="utf-8")

    def test_a_source_that_changed_is_never_overwritten(self, tmp_path):
        """The guard that matters. Two target projects have live sessions.

        A plan computed from an older file would drop whatever was appended in
        between — silently, because the result still looks like a valid split.
        """
        text = doc([("Head", big(10)), ("Log", big(500))])
        root = self._project(tmp_path, text)
        plan = compute_split(text, keep_sections=1)

        (tmp_path / "CLAUDE.md").write_text(
            text + "\n## Appended by a live session\n\nnew\n",
            encoding="utf-8", newline="")

        ok, message = apply_split(root, plan)
        assert ok is False
        assert "changed since" in message
        # And the appended content is still there.
        assert "Appended by a live session" in (
            tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    def test_an_existing_target_is_never_clobbered(self, tmp_path):
        text = doc([("Head", big(10)), ("Log", big(500))])
        root = self._project(tmp_path, text)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "LESSONS.md").write_text("mine", encoding="utf-8")
        plan = compute_split(text, keep_sections=1)
        ok, message = apply_split(root, plan)
        assert ok is False
        assert "already exists" in message
        assert (tmp_path / "docs" / "LESSONS.md").read_text(
            encoding="utf-8") == "mine"

    def test_a_blocked_plan_writes_nothing(self, tmp_path):
        text = doc([("Head", big(10)), ("Chain", "@other.md")])
        root = self._project(tmp_path, text)
        plan = compute_split(text, keep_sections=1)
        ok, _message = apply_split(root, plan)
        assert ok is False
        assert not (tmp_path / "docs").exists()

    def test_crlf_survives_the_round_trip(self, tmp_path):
        """A whole-file line-ending change would bury the real diff."""
        text = doc([("Head", big(10)), ("Log", big(500))]).replace("\n", "\r\n")
        root = self._project(tmp_path, text)
        plan = compute_split(text, keep_sections=1)
        ok, message = apply_split(root, plan)
        assert ok, message
        raw = (tmp_path / "CLAUDE.md").read_bytes()
        assert b"\r\n" in raw

    def test_read_source_of_a_missing_file_is_empty_not_an_exception(
            self, tmp_path):
        assert read_source(str(tmp_path)) == ""


def test_digest_changes_with_content():
    assert digest_of("a") != digest_of("b")


# ── moving a run that is not the tail ────────────────────────────────────────
#
# The premise the module opened with -- "a log grows by appending, so the tail
# is the log" -- was falsified by two of the three projects it was written for:
# Fortuna's 339 KB log is section [2] of 5 with operational sections on both
# sides, and OpenChem's LAST section is a 64 KB operational standard. A boundary
# can only say "everything after here"; these files need a set.

class TestExplicitSelection:
    def test_a_middle_run_can_move_while_both_edges_stay(self):
        text = doc([("Commands", big(40)), ("File map", big(40)),
                    ("The log", big(4000)),
                    ("Scope fences", big(40)), ("Packaging traps", big(40))])
        plan = compute_split(text, move_indices={2})
        assert [s.title for s in plan.moved] == ["The log"]
        assert [s.title for s in plan.kept] == [
            "Commands", "File map", "Scope fences", "Packaging traps"]

    def test_the_index_lands_where_the_log_was(self):
        """Otherwise the surviving sections are quietly reshuffled."""
        text = doc([("First", big(40)), ("The log", big(400)),
                    ("Last", big(40))])
        plan = compute_split(text, move_indices={1})
        body = plan.new_source
        assert body.index("## First") < body.index("Lessons (moved") \
            < body.index("## Last")

    def test_a_trailing_operational_section_can_be_kept(self):
        """OpenChem's case: the last section is a standard, not a lesson."""
        text = doc([("Head", big(40)), ("Log one", big(400)),
                    ("Log two", big(400)), ("Verification standard", big(900))])
        plan = compute_split(text, move_indices={1, 2})
        assert "Verification standard" in [s.title for s in plan.kept]
        assert plan.new_source.rstrip().endswith(big(900))

    def test_an_explicit_selection_still_refuses_to_move_the_chain(self):
        text = doc([("Head", big(40)), ("Carries", "@other.md"),
                    ("Log", big(400))])
        plan = compute_split(text, move_indices={1, 2})
        assert plan.blocked
        assert "@include" in plan.blocked

    def test_nothing_is_lost_with_a_middle_split(self):
        titles = ["A", "B", "C", "D", "E"]
        text = doc([(t, big(60)) for t in titles])
        plan = compute_split(text, move_indices={1, 2, 3})
        for title in titles:
            assert (title in plan.new_source) or (title in plan.new_target)

    def test_an_empty_selection_is_reported_not_pretended(self):
        text = doc([("A", big(40)), ("B", big(40))])
        plan = compute_split(text, move_indices=set())
        assert plan.blocked
        assert plan.ok is False


# ── granularity: `##` is not the unit of a lesson ────────────────────────────
#
# Measured on the projects this module was written for:
#
#   OpenChem  a KEPT operational section of 64,030 B holding twenty `###`
#             lessons -- a `##`-only model can move it whole or not at all,
#             and neither answer is right
#   Fortuna   an entire 339 KB log under ONE `##`, its entries marked by bold
#             opening sentences, so the index it produced was a single line
#             pointing at a 346 KB file
#
# Indexing Fortuna's entries individually IS reliable (547 paragraph-initial
# against 34 wrap artifacts, and the same ratio holds on the other two), but it
# costs 32,840 B -- more than that project now keeps loaded in total. So the
# index counts them instead. That is this module's own trade applied to itself.

def doc3(spec, preamble="@BASIC_INSTRUCTIONS.md\n"):
    """`spec` is [(level, title, body), ...]."""
    out = ["# Project — notes", "", preamble.rstrip("\n"), ""]
    for level, title, body in spec:
        out.append("%s %s" % ("#" * level, title))
        out.append("")
        out.append(body)
        out.append("")
    return "\n".join(out)


class TestTwoLevels:
    def test_a_subsection_is_a_section_in_its_own_right(self):
        text = doc3([(2, "Standard", big(40)),
                     (3, "Lesson A", big(300)),
                     (3, "Lesson B", big(300))])
        plan = compute_split(text, move_indices={1, 2})
        assert [s.title for s in plan.moved] == ["Lesson A", "Lesson B"]
        assert [s.title for s in plan.kept] == ["Standard"]

    def test_a_parent_carries_its_children(self):
        """Leaving them behind would orphan them under the next heading."""
        text = doc3([(2, "Log", big(40)),
                     (3, "One", big(300)),
                     (3, "Two", big(300)),
                     (2, "Kept", big(40))])
        plan = compute_split(text, move_indices={0})
        assert [s.title for s in plan.moved] == ["Log", "One", "Two"]
        assert "## Kept" in plan.new_source

    def test_own_size_excludes_children_and_total_includes_them(self):
        """The number on screen has to be what a reader actually pays."""
        text = doc3([(2, "Standard", big(100)),
                     (3, "Lesson", big(5000))])
        _pre, sections = _compute_sections(text)
        parent = sections[0]
        assert parent.size < 1000, "own size counted its children"
        assert parent.total_size > 5000
        assert sections[1].parent == 0

    def test_a_deeper_heading_is_not_a_section(self):
        """Level 4 stays inside its `###`; two levels is the whole model."""
        text = doc3([(2, "Top", big(40)),
                     (3, "Middle", "#### Deep" + "\n\n" + big(200))])
        plan = compute_split(text, move_indices={1})
        assert [s.title for s in plan.moved] == ["Middle"]
        assert "#### Deep" in plan.new_target


class TestTheNestingTrap:
    def test_the_index_never_lands_inside_a_kept_parent(self):
        """A level-2 index heading there re-parents the siblings after it.

        The shape matters, and the first version of this test could not express
        it. A parent's OWN body always precedes its first `###`, so moving the
        first child and asserting "the parent's prose comes first" holds even
        when the index IS written at the child's line -- it survived exactly
        that mutation.

        The trap needs a kept sibling AFTER the moved one. Writing the index at
        the moved child's line puts a `##` heading above `### Kept sibling`,
        which then silently stops belonging to `Standard` and becomes part of
        the index. Still valid Markdown, and wrong.
        """
        text = doc3([(2, "Standard", "KEEP-ME " + big(200)),
                     (3, "Moved lesson", big(4000)),
                     (3, "Kept sibling", "SIBLING-BODY " + big(100)),
                     (2, "After", big(40))])
        plan = compute_split(text, move_indices={1})

        body = plan.new_source
        assert "### Kept sibling" in body
        index_at = body.index("## Lessons (moved")
        assert body.index("### Kept sibling") < index_at, (
            "the index was written above a sibling it re-parents")
        assert body.index("SIBLING-BODY") < index_at
        assert body.index("## Standard") < body.index("KEEP-ME")

    def test_an_orphaned_subsection_says_where_it_came_from(self):
        """Otherwise it is filed under whatever `##` precedes it over there."""
        text = doc3([(2, "Verification standard", big(60)),
                     (3, "Lesson", big(400))])
        plan = compute_split(text, move_indices={1})
        assert "## From: Verification standard" in plan.new_target
        assert plan.new_target.index("## From:") < \
            plan.new_target.index("### Lesson")

    def test_a_moved_parent_needs_no_context_heading(self):
        text = doc3([(2, "Log", big(40)), (3, "Lesson", big(400))])
        plan = compute_split(text, move_indices={0})
        # Line-anchored: the target's own header explains `## From:` in prose,
        # and a substring test matched that instead of a heading.
        assert not [l for l in plan.new_target.splitlines()
                    if l.startswith("## From:")]


class TestASecondSplit:
    """Granularity is what makes a second split worth doing, and the first
    attempt at one produced two identical `## Lessons` headings in one file."""

    def _first(self, text):
        # Move ONLY "Lesson one": a boundary would carry off the sections the
        # second split is about, which is what the first draft of this fixture
        # did.
        return compute_split(text, move_indices={1})

    def test_the_index_is_merged_not_duplicated(self):
        text = doc3([(2, "Head", big(40)),
                     (2, "Lesson one", big(4000)),
                     (2, "Standard", big(40)),
                     (3, "Lesson two", big(4000))])
        first = self._first(text)
        # Now split again, from the file the first split produced.
        _pre, sections = _compute_sections(first.new_source)
        later = next(s for s in sections if s.title == "Lesson two")
        second = compute_split(first.new_source, move_indices={later.index},
                               target_text=first.new_target)

        assert second.new_source.count("## Lessons (moved out of this file)") == 1
        assert "- Lesson one" in second.new_source, "the earlier entry was lost"
        assert "- Lesson two" in second.new_source

    def test_the_existing_target_is_kept_verbatim(self):
        text = doc3([(2, "Head", big(40)),
                     (2, "Lesson one", "FIRST-BODY " + big(4000)),
                     (2, "Standard", big(40)),
                     (3, "Lesson two", "SECOND-BODY " + big(4000))])
        first = self._first(text)
        _pre, sections = _compute_sections(first.new_source)
        later = next(s for s in sections if s.title == "Lesson two")
        second = compute_split(first.new_source, move_indices={later.index},
                               target_text=first.new_target)

        assert "FIRST-BODY" in second.new_target
        assert "SECOND-BODY" in second.new_target
        assert second.appending is True
        assert second.new_target.startswith(first.new_target.rstrip("\n")[:60])

    def test_a_target_this_tool_did_not_write_is_refused(self):
        """Appending to somebody's own notes is a smaller surprise than
        overwriting them, and still a surprise."""
        text = doc3([(2, "Head", big(40)), (2, "Log", big(4000))])
        plan = compute_split(text, keep_sections=1,
                             target_text="# My own notes\n\nmine\n")
        assert plan.blocked
        assert "not written by this tool" in plan.blocked
        assert plan.ok is False


class TestAppendGuards:
    def _project(self, tmp_path, text, target=None):
        (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8", newline="")
        if target is not None:
            (tmp_path / "docs").mkdir(exist_ok=True)
            (tmp_path / "docs" / "LESSONS.md").write_text(
                target, encoding="utf-8", newline="")
        return str(tmp_path)

    def test_a_target_that_changed_is_never_appended_to(self, tmp_path):
        """Two files are rewritten, so two digests are checked."""
        text = doc3([(2, "Head", big(40)),
                     (2, "Lesson one", big(4000)),
                     (2, "Standard", big(40)),
                     (3, "Lesson two", big(4000))])
        first = compute_split(text, move_indices={1})
        root = self._project(tmp_path, first.new_source, first.new_target)

        _pre, sections = _compute_sections(first.new_source)
        later = next(s for s in sections if s.title == "Lesson two")
        plan = compute_split(first.new_source, move_indices={later.index},
                             target_text=first.new_target)

        (tmp_path / "docs" / "LESSONS.md").write_text(
            first.new_target + "\n## Added by hand\n\nnote\n",
            encoding="utf-8", newline="")

        ok, message = apply_split(root, plan)
        assert ok is False
        assert "changed since" in message
        assert "Added by hand" in (
            tmp_path / "docs" / "LESSONS.md").read_text(encoding="utf-8")

    def test_a_clean_second_apply_writes_both_files(self, tmp_path):
        text = doc3([(2, "Head", big(40)),
                     (2, "Lesson one", big(4000)),
                     (2, "Standard", big(40)),
                     (3, "Lesson two", big(4000))])
        first = compute_split(text, move_indices={1})
        root = self._project(tmp_path, first.new_source, first.new_target)
        _pre, sections = _compute_sections(first.new_source)
        later = next(s for s in sections if s.title == "Lesson two")
        plan = compute_split(first.new_source, move_indices={later.index},
                             target_text=first.new_target)

        ok, message = apply_split(root, plan)
        assert ok, message
        target = (tmp_path / "docs" / "LESSONS.md").read_text(encoding="utf-8")
        assert target.count("# Lessons") == 1, "a second header was written"
        assert "### Lesson two" in target


class TestAHeadinglessLog:
    def test_entries_are_counted_not_listed(self):
        """Fortuna's shape: one `##`, no `###`, entries led by bold sentences.

        Listing them is reliable and costs 32,840 B on the real file, which is
        more than that project keeps loaded in total. So the index says how
        many and how they are marked.
        """
        entries = "\n\n".join("**Rule %d.** body text here" % n
                              for n in range(1, 21))
        text = doc3([(2, "Head", big(40)),
                     (2, "Rules that are easy to break", entries)])
        plan = compute_split(text, move_indices={1})

        assert "20 entries" in plan.new_source
        assert "bold sentence" in plan.new_source
        # And NOT one index line per entry.
        assert plan.new_source.count("- ") < 5

    def test_a_section_with_moved_children_gets_no_count(self):
        """The children ARE the index; a count beside them is noise."""
        text = doc3([(2, "Head", big(40)),
                     (2, "Log", "\n\n".join("**Rule %d.** b" % n
                                            for n in range(1, 21))),
                     (3, "Named lesson", big(200))])
        plan = compute_split(text, move_indices={1})
        assert "  - Named lesson" in plan.new_source
        assert "entries, each opening" not in plan.new_source

    def test_a_short_section_gets_no_count(self):
        text = doc3([(2, "Head", big(40)),
                     (2, "Log", "**One.** a\n\n**Two.** b")])
        plan = compute_split(text, move_indices={1})
        assert "entries, each opening" not in plan.new_source

    def test_a_bold_lead_in_inside_a_fence_is_not_an_entry(self):
        fenced = "```\n" + "\n\n".join("**Rule %d.** x" % n
                                       for n in range(1, 30)) + "\n```"
        text = doc3([(2, "Head", big(40)), (2, "Log", fenced)])
        plan = compute_split(text, move_indices={1})
        assert "entries, each opening" not in plan.new_source
