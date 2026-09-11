"""Tests for helpers/instructions_wiring.

Two properties matter more than the rest and are asserted repeatedly: a re-run
changes nothing, and every byte outside the managed line survives — including
the line endings, which naive reading silently converts.
"""

import os

import pytest

from helpers import instructions_posture as ip
from helpers import instructions_wiring as iw


BASELINE_TEXT = "# Project Baseline Rules\n\n## Tokensave: Use It First\n"


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "project-baseline.md").write_text(BASELINE_TEXT, encoding="utf-8")
    return d


def inc_line(templates):
    return "@" + str(templates / "project-baseline.md")


def make_project(tmp_path, name="proj", claude=None, basic=None):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if claude is not None:
        (root / "CLAUDE.md").write_bytes(claude.encode("utf-8"))
    if basic is not None:
        (root / "BASIC_INSTRUCTIONS.md").write_bytes(basic.encode("utf-8"))
    return root


def posture_of(root, templates):
    return ip.read_project(str(root), root.name, str(templates),
                           ip.canonical(str(templates / "project-baseline.md")),
                           BASELINE_TEXT, "")


def wire(root, templates, template_text=""):
    p = posture_of(root, templates)
    plan = iw.plan_wiring(p, has_template=bool(template_text))
    return plan, iw.apply_wiring(
        str(root), plan, inc_line(templates), root.name, template_text,
        ip.canonical(str(templates / "project-baseline.md")))


# -- planning -------------------------------------------------------------

def test_resolved_project_plans_nothing(tmp_path, templates):
    root = make_project(tmp_path, claude=inc_line(templates) + "\n")
    plan = iw.plan_wiring(posture_of(root, templates))
    assert plan.is_noop
    assert plan.files == ()


def test_orphan_plans_only_the_claude_link(tmp_path, templates):
    root = make_project(tmp_path, basic=inc_line(templates) + "\n")
    plan = iw.plan_wiring(posture_of(root, templates))
    assert plan.files == ("CLAUDE.md",)
    assert plan.steps[0].action == iw.ACTION_LINK_BASIC


def test_absent_with_no_basic_plans_both_files(tmp_path, templates):
    root = make_project(tmp_path, claude="# Notes\n")
    plan = iw.plan_wiring(posture_of(root, templates), has_template=True)
    assert set(plan.files) == {"BASIC_INSTRUCTIONS.md", "CLAUDE.md"}


def test_unknown_is_blocked_not_planned(tmp_path, templates):
    root = tmp_path / "bad"
    root.mkdir()
    (root / "CLAUDE.md").write_bytes(b"\xff\xfe\x00\xc3\x28")
    plan = iw.plan_wiring(posture_of(root, templates))
    assert plan.blocked
    assert plan.steps == ()


def test_duplicate_directive_is_blocked(tmp_path, templates):
    root = make_project(
        tmp_path,
        claude="@BASIC_INSTRUCTIONS.md\n\ntext\n\n@BASIC_INSTRUCTIONS.md\n",
        basic="# nothing\n")
    plan = iw.plan_wiring(posture_of(root, templates))
    assert plan.blocked
    assert "duplication" in plan.blocked


# -- deterministic creation ----------------------------------------------

def test_created_claude_md_is_byte_for_byte_stable():
    text = iw.compute_created_claude("Widget Factory")
    assert text == "# Widget Factory — Claude Instructions\n\n@BASIC_INSTRUCTIONS.md\n"
    assert iw.compute_created_claude("Widget Factory") == text


# -- surgical edits -------------------------------------------------------

def test_link_is_prepended_and_the_rest_is_untouched(tmp_path, templates):
    body = "# Title\n\nSome authored prose.\n\n## A section\n\nmore\n"
    root = make_project(tmp_path, claude=body, basic=inc_line(templates) + "\n")
    _plan, result = wire(root, templates)
    assert result.ok
    after = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert after.endswith(body)
    assert after.startswith("@BASIC_INSTRUCTIONS.md")


def test_crlf_line_endings_survive(tmp_path, templates):
    body = "# Title\r\n\r\nAuthored prose.\r\n"
    root = make_project(tmp_path, claude=body, basic=inc_line(templates) + "\n")
    wire(root, templates)
    raw = (root / "CLAUDE.md").read_bytes()
    assert b"\r\n" in raw
    # The one inserted line uses the file's own terminator, so no line in the
    # file is left with a bare LF.
    assert raw.replace(b"\r\n", b"") .count(b"\n") == 0


def test_lf_file_stays_lf(tmp_path, templates):
    root = make_project(tmp_path, claude="# Title\n\nprose\n",
                        basic=inc_line(templates) + "\n")
    wire(root, templates)
    assert b"\r\n" not in (root / "CLAUDE.md").read_bytes()


def test_rerun_changes_nothing(tmp_path, templates):
    root = make_project(tmp_path, claude="# Title\n\nprose\n",
                        basic=inc_line(templates) + "\n")
    _plan, first = wire(root, templates)
    assert first.changed_files == ("CLAUDE.md",)
    snapshot = (root / "CLAUDE.md").read_bytes()

    plan2, second = wire(root, templates)
    assert plan2.is_noop
    assert second.changed_files == ()
    assert (root / "CLAUDE.md").read_bytes() == snapshot


def test_wiring_makes_the_chain_resolve(tmp_path, templates):
    root = make_project(tmp_path, basic=inc_line(templates) + "\n")
    assert posture_of(root, templates).reach == ip.REACH_ORPHANED
    wire(root, templates)
    assert posture_of(root, templates).reach == ip.REACH_RESOLVED


def test_basic_instructions_is_not_touched_when_it_already_carries(tmp_path,
                                                                  templates):
    body = inc_line(templates) + "\n\n# Real project notes\n"
    root = make_project(tmp_path, basic=body)
    _plan, result = wire(root, templates)
    assert "BASIC_INSTRUCTIONS.md" not in result.changed_files
    assert (root / "BASIC_INSTRUCTIONS.md").read_text(encoding="utf-8") == body


# -- stale repair ---------------------------------------------------------

def test_repair_rewrites_the_directive_and_not_the_prose(tmp_path, templates):
    old = tmp_path / "old"
    old.mkdir()
    (old / "project-baseline.md").write_text("old\n", encoding="utf-8")
    prose = "We used to keep project-baseline.md elsewhere.\n"
    body = "@" + str(old / "project-baseline.md") + "\n\n" + prose
    root = make_project(tmp_path, claude=body)

    assert posture_of(root, templates).reach == ip.REACH_STALE
    _plan, result = wire(root, templates)
    assert result.ok
    after = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert prose in after                      # the mention survives verbatim
    assert str(old) not in after               # the directive does not
    assert posture_of(root, templates).reach == ip.REACH_RESOLVED


def test_repair_leaves_a_current_directive_alone(tmp_path, templates):
    text = inc_line(templates) + "\nkeep\n"
    out, changed = iw.compute_repair_stale(
        text, inc_line(templates), str(templates),
        ip.canonical(str(templates / "project-baseline.md")))
    assert changed is False
    assert out == text


def test_repair_preserves_the_line_terminator(tmp_path, templates):
    text = "@C:/old/project-baseline.md\r\nkeep\r\n"
    out, changed = iw.compute_repair_stale(
        text, inc_line(templates), str(tmp_path),
        ip.canonical(str(templates / "project-baseline.md")))
    assert changed is True
    assert out.splitlines(keepends=True)[0].endswith("\r\n")


# -- the writer never deletes --------------------------------------------

def test_an_obsolete_direct_include_is_never_removed(tmp_path, templates):
    """Double-load is reported, not silently edited away.

    Deleting a line a human may have written is not a repair; the advisory
    exists so a person can decide.
    """
    inc = inc_line(templates)
    root = make_project(tmp_path, claude=inc + "\n@BASIC_INSTRUCTIONS.md\n",
                        basic=inc + "\n")
    p = posture_of(root, templates)
    assert ip.ADVISORY_DOUBLE_LOAD in p.advisories
    before = (root / "CLAUDE.md").read_bytes()
    _plan, result = wire(root, templates)
    assert result.changed_files == ()
    assert (root / "CLAUDE.md").read_bytes() == before


def test_authored_contradiction_survives_wiring(tmp_path, templates):
    """Wiring adds a line. It does not edit what the project already says."""
    prose = "## Token-Saving Tool Usage\n\nUse the Explore subagent.\n"
    root = make_project(tmp_path, claude=prose,
                        basic=inc_line(templates) + "\n")
    wire(root, templates)
    after = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert prose in after
    assert ip.ADVISORY_CONTRADICTS in posture_of(root, templates).advisories


def test_absent_without_a_template_wires_the_one_hop_shape(tmp_path, templates):
    """A blank template path must not leave a project unwired.

    `CLAUDE.md` -> baseline is a documented topology too, and choosing it here
    avoids creating a BASIC_INSTRUCTIONS.md the caller did not ask for.
    """
    root = make_project(tmp_path, claude="# Notes\n")
    plan = iw.plan_wiring(posture_of(root, templates), has_template=False)
    assert plan.files == ("CLAUDE.md",)
    result = iw.apply_wiring(str(root), plan, inc_line(templates), root.name, "",
                             ip.canonical(str(templates / "project-baseline.md")))
    assert result.ok
    assert posture_of(root, templates).reach == ip.REACH_RESOLVED


def test_planner_refuses_an_excluded_project(tmp_path, templates):
    """The guard is in the planner, not only on the button.

    `repairable` gates the UI; a caller reaching `plan_wiring` directly would
    walk past it. What this exclusion protects is writing Manager-authored
    files into somebody else's repository.
    """
    import dataclasses
    root = make_project(tmp_path, claude="# upstream clone\n")
    p = dataclasses.replace(posture_of(root, templates), excluded=True,
                            exclude_reason="vendor clone")
    plan = iw.plan_wiring(p, has_template=True)
    assert plan.steps == ()
    assert plan.blocked == "vendor clone"

    before = sorted(os.listdir(root))
    iw.apply_wiring(str(root), plan, inc_line(templates), root.name, "x", "")
    assert sorted(os.listdir(root)) == before


# -- applying to one project: outcomes are data, not sentences ------------
#
# What this replaced: the caller returned prose -- "skipped - state changed
# since preview" versus "skipped - %s" % plan.blocked -- and the UI branched on
# `outcome == "wired"`. So the safety case the three-stage flow exists for was
# distinguishable from an ordinary refusal only by parsing a string the code had
# formatted itself. It worked, and it was one rename from silently not working.
#
# These assert on MEMBERS. If any of them needs `in result.render()` to tell two
# situations apart, the shape has regressed.

def apply_one(root, templates, template_text=""):
    baseline = ip.canonical(str(templates / "project-baseline.md"))
    return iw.apply_to_project(
        posture_of(root, templates), baseline, str(templates),
        inc_line(templates), template_text, bool(template_text))


class TestApplyOutcome:
    def test_a_clean_wire_reports_wired_and_the_files_it_touched(
            self, tmp_path, templates):
        root = make_project(tmp_path, claude="# P\n",
                            basic=inc_line(templates) + "\n")
        result = apply_one(root, templates)
        assert result.outcome == iw.OUTCOME_WIRED
        assert result.wrote is True
        assert "CLAUDE.md" in " ".join(result.changed_files)

    def test_an_already_resolving_project_is_not_a_write(self, tmp_path,
                                                         templates):
        root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                            basic=inc_line(templates) + "\n")
        result = apply_one(root, templates)
        assert result.outcome == iw.OUTCOME_ALREADY_RESOLVED
        assert result.wrote is False
        assert result.changed_files == ()

    def test_a_project_that_changed_since_the_preview_is_its_own_outcome(
            self, tmp_path, templates):
        """The safety net doing its job, and it must not look like a refusal.

        The posture is captured, the project is then repaired by hand, and the
        stale plan must be discarded unapplied.
        """
        root = make_project(tmp_path, claude="# P\n",
                            basic=inc_line(templates) + "\n")
        stale = posture_of(root, templates)          # ORPHANED at this point

        # Somebody wires it by hand in between.
        (root / "CLAUDE.md").write_bytes(
            b"# P\n\n@BASIC_INSTRUCTIONS.md\n")

        baseline = ip.canonical(str(templates / "project-baseline.md"))
        result = iw.apply_to_project(stale, baseline, str(templates),
                                     inc_line(templates), "", False)

        assert result.outcome == iw.OUTCOME_SKIPPED_STATE_CHANGED
        assert result.changed_files == ()

    def test_a_blocked_plan_is_a_different_member_from_a_stale_one(
            self, tmp_path, templates):
        """The whole point of the change: two skips, two members.

        Nothing here reads `render()`. If telling these apart ever needs the
        sentence, the structure has gone.
        """
        root = make_project(
            tmp_path,
            claude="@BASIC_INSTRUCTIONS.md\n@BASIC_INSTRUCTIONS.md\n",
            basic="nothing\n")
        result = apply_one(root, templates)

        assert result.outcome == iw.OUTCOME_SKIPPED_BLOCKED
        assert result.outcome != iw.OUTCOME_SKIPPED_STATE_CHANGED
        assert result.is_skip is True
        assert result.reason

    def test_both_skips_are_skips_and_neither_is_a_write(self):
        assert iw.ApplyOutcome(iw.OUTCOME_SKIPPED_BLOCKED).is_skip
        assert iw.ApplyOutcome(iw.OUTCOME_SKIPPED_STATE_CHANGED).is_skip
        assert not iw.ApplyOutcome(iw.OUTCOME_SKIPPED_BLOCKED).wrote
        assert not iw.ApplyOutcome(iw.OUTCOME_SKIPPED_STATE_CHANGED).wrote

    def test_the_sentence_derives_from_the_outcome(self):
        """One direction. The text is built from the member, never parsed."""
        assert iw.ApplyOutcome(iw.OUTCOME_WIRED).render() == "wired"
        assert iw.ApplyOutcome(
            iw.OUTCOME_SKIPPED_STATE_CHANGED).render().startswith("skipped")
        with_reason = iw.ApplyOutcome(iw.OUTCOME_FAILED, "disk full").render()
        assert "failed" in with_reason and "disk full" in with_reason

    def test_an_unverified_write_is_neither_success_nor_failure(self):
        result = iw.ApplyOutcome(iw.OUTCOME_UNVERIFIED, "still absent",
                                 ("CLAUDE.md",))
        assert result.wrote is False
        assert result.is_skip is False
        assert result.changed_files == ("CLAUDE.md",)
