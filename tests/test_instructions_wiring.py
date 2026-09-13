"""Tests for helpers/instructions_wiring.

Properties asserted repeatedly: a re-run changes nothing, every byte outside the
managed line survives (line endings included), the copy is written before any
include is pointed at it, and a file edited since the plan is never written.

`posture_of` hands `read_project` an explicit empty `~/.claude.json` projects
map, so nothing depends on the machine running the tests. With it, an include of
the template outside the project is never a healthy chain: on Windows the
backslashes stop Claude Code parsing it, elsewhere the missing approval does.
"""

import dataclasses
import os
import shutil
import subprocess

import pytest

from helpers import baseline_copy as bc
from helpers import instructions_posture as ip
from helpers import instructions_wiring as iw


BASELINE_TEXT = "# Project Baseline Rules\n\n## Tokensave: Use It First\n"
LOCAL = bc.LOCAL_INCLUDE_LINE


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "project-baseline.md").write_text(BASELINE_TEXT, encoding="utf-8")
    return d


def inc_line(templates):
    return "@" + str(templates / "project-baseline.md")


def baseline_of(templates):
    return ip.canonical(str(templates / "project-baseline.md"))


def make_project(tmp_path, name="proj", claude=None, basic=None, copy=False):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if claude is not None:
        (root / "CLAUDE.md").write_bytes(claude.encode("utf-8"))
    if basic is not None:
        (root / "BASIC_INSTRUCTIONS.md").write_bytes(basic.encode("utf-8"))
    if copy:
        (root / "project-baseline.md").write_bytes(
            bc.render_copy(BASELINE_TEXT).encode("utf-8"))
    return root


def posture_of(root, templates, claude_projects=None):
    return ip.read_project(str(root), root.name, str(templates),
                           baseline_of(templates), BASELINE_TEXT, "",
                           {} if claude_projects is None else claude_projects)


def wire(root, templates, template_text=""):
    p = posture_of(root, templates)
    plan = iw.plan_wiring(p, has_template=bool(template_text))
    return plan, iw.apply_wiring(str(root), plan, root.name, template_text,
                                 BASELINE_TEXT)


def actions(plan):
    return [(s.action, s.path) for s in plan.steps]


# -- planning -------------------------------------------------------------

def test_a_project_on_its_own_current_copy_plans_nothing(tmp_path, templates):
    root = make_project(tmp_path, claude=LOCAL + "\n", copy=True)
    p = posture_of(root, templates)
    assert p.healthy
    assert iw.plan_wiring(p).is_noop


def test_a_resolving_template_include_is_localized_copy_first(tmp_path,
                                                             templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    p = posture_of(root, templates)
    assert p.reach == ip.REACH_RESOLVED and not p.healthy
    assert actions(iw.plan_wiring(p)) == [
        (iw.ACTION_WRITE_COPY, "project-baseline.md"),
        (iw.ACTION_LOCALIZE, "BASIC_INSTRUCTIONS.md")]


def test_an_approved_external_include_is_still_localized(tmp_path, templates):
    """Loading on this machine is not the goal state: a committed machine path
    breaks every other checkout and every worktree."""
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    p = dataclasses.replace(posture_of(root, templates),
                            delivery=ip.DELIVERY_EXTERNAL_APPROVED)
    assert iw.ACTION_LOCALIZE in [a for a, _ in actions(iw.plan_wiring(p))]


def test_undetermined_delivery_is_blocked(tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    p = dataclasses.replace(posture_of(root, templates),
                            delivery=ip.DELIVERY_UNKNOWN)
    plan = iw.plan_wiring(p)
    assert plan.blocked and plan.steps == ()


def test_orphan_is_localized_and_linked(tmp_path, templates):
    root = make_project(tmp_path, basic=inc_line(templates) + "\n")
    assert actions(iw.plan_wiring(posture_of(root, templates))) == [
        (iw.ACTION_WRITE_COPY, "project-baseline.md"),
        (iw.ACTION_LOCALIZE, "BASIC_INSTRUCTIONS.md"),
        (iw.ACTION_LINK_BASIC, "CLAUDE.md")]


def test_absent_with_no_basic_plans_the_copy_and_both_files(tmp_path,
                                                            templates):
    root = make_project(tmp_path, claude="# Notes\n")
    plan = iw.plan_wiring(posture_of(root, templates), has_template=True)
    assert plan.steps[0].action == iw.ACTION_WRITE_COPY
    assert set(plan.files) == {"project-baseline.md", "BASIC_INSTRUCTIONS.md",
                               "CLAUDE.md"}


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


@pytest.mark.parametrize("content,reason", [
    ("# my own baseline\n", "did not write"),
    (bc.render_copy(BASELINE_TEXT) + "hand edit\n", "edited by hand"),
    ("<!-- tokensave-manager:copy project-baseline.md sha256=nothex -->\n",
     "damaged"),
])
def test_an_existing_root_file_the_manager_may_not_overwrite_blocks(
        tmp_path, templates, content, reason):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    (root / "project-baseline.md").write_bytes(content.encode("utf-8"))
    plan = iw.plan_wiring(posture_of(root, templates))
    assert reason in plan.blocked
    assert plan.steps == ()


def test_an_outdated_copy_plans_only_the_refresh(tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=LOCAL + "\n")
    (root / "project-baseline.md").write_bytes(
        bc.render_copy("# older baseline\n").encode("utf-8"))
    p = posture_of(root, templates)
    assert p.reach == ip.REACH_STALE and p.copy_state == bc.COPY_OUTDATED
    plan = iw.plan_wiring(p)
    assert actions(plan) == [(iw.ACTION_REFRESH_COPY, "project-baseline.md")]
    assert plan.steps[0].expect == p.copy_recorded_sha


def test_two_baseline_includes_are_refused_not_localized_twice(tmp_path,
                                                               templates):
    """Double-load is reported, not silently edited away."""
    inc = inc_line(templates)
    root = make_project(tmp_path, claude=inc + "\n@BASIC_INSTRUCTIONS.md\n",
                        basic=inc + "\n")
    p = posture_of(root, templates)
    assert ip.ADVISORY_DOUBLE_LOAD in p.advisories
    before = (root / "CLAUDE.md").read_bytes()
    plan, result = wire(root, templates)
    assert plan.blocked
    assert result.changed_files == ()
    assert (root / "CLAUDE.md").read_bytes() == before
    assert not (root / "project-baseline.md").exists()


def test_planner_refuses_an_excluded_project(tmp_path, templates):
    root = make_project(tmp_path, claude="# upstream clone\n")
    p = dataclasses.replace(posture_of(root, templates), excluded=True,
                            exclude_reason="vendor clone")
    plan = iw.plan_wiring(p, has_template=True)
    assert plan.steps == ()
    assert plan.blocked == "vendor clone"
    before = sorted(os.listdir(root))
    iw.apply_wiring(str(root), plan, root.name, "x", BASELINE_TEXT)
    assert sorted(os.listdir(root)) == before


# -- deterministic creation ----------------------------------------------

def test_created_claude_md_is_byte_for_byte_stable():
    text = iw.compute_created_claude("Widget Factory")
    assert text == "# Widget Factory — Claude Instructions\n\n@BASIC_INSTRUCTIONS.md\n"
    assert iw.compute_created_claude("Widget Factory") == text


# -- compare-and-apply line surgery ---------------------------------------

def test_localize_replaces_the_planned_line_and_keeps_its_terminator():
    text = "# T\r\n@C:\\old\\project-baseline.md\r\nprose\r\n"
    out, changed, refusal = iw.compute_localize(
        text, 2, "C:\\old\\project-baseline.md")
    assert (changed, refusal) == (True, "")
    assert out == "# T\r\n@project-baseline.md\r\nprose\r\n"


def test_localize_follows_a_line_that_moved_when_it_is_unique():
    text = "new first line\n# T\n@C:/old/project-baseline.md\n"
    out, changed, _ = iw.compute_localize(text, 2, "C:/old/project-baseline.md")
    assert changed and out.endswith("@project-baseline.md\n")


@pytest.mark.parametrize("text", [
    "# T\n@C:/somewhere/else/project-baseline.md\n",       # edited
    "@C:/old/project-baseline.md\n@C:/old/project-baseline.md\n",  # ambiguous
    "# T\n",                                                # removed
])
def test_localize_refuses_when_the_directive_is_not_what_was_planned(text):
    out, changed, refusal = iw.compute_localize(text, 5,
                                                "C:/old/project-baseline.md")
    assert (out, changed) == (text, False)
    assert refusal


# -- applying -------------------------------------------------------------

def test_localizing_makes_the_project_healthy_on_a_current_copy(tmp_path,
                                                               templates):
    prose = "We used to keep project-baseline.md elsewhere.\r\n"
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\r\n",
                        basic="# B\r\n\r\n" + inc_line(templates) + "\r\n" + prose)
    _plan, result = wire(root, templates)
    assert result.ok, result.error
    assert set(result.changed_files) == {"project-baseline.md",
                                         "BASIC_INSTRUCTIONS.md"}
    after = (root / "BASIC_INSTRUCTIONS.md").read_bytes()
    assert after == ("# B\r\n\r\n@project-baseline.md\r\n" + prose).encode()
    p = posture_of(root, templates)
    assert p.healthy and p.copy_state == bc.COPY_CURRENT
    assert p.baseline_match == ip.CONTENT_MATCH
    assert p.reached_baseline == ip.canonical(str(root / "project-baseline.md"))


def test_the_project_root_may_contain_spaces(tmp_path, templates):
    root = make_project(tmp_path, name="my project dir",
                        claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    _plan, result = wire(root, templates)
    assert result.ok, result.error
    assert posture_of(root, templates).healthy


def test_a_file_edited_between_plan_and_apply_gets_nothing_written(
        tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    plan = iw.plan_wiring(posture_of(root, templates))
    edited = b"@C:/somebody/else/project-baseline.md\n"
    (root / "BASIC_INSTRUCTIONS.md").write_bytes(edited)

    result = iw.apply_wiring(str(root), plan, root.name, "", BASELINE_TEXT)

    assert not result.ok and "changed since the plan" in result.error
    assert (root / "BASIC_INSTRUCTIONS.md").read_bytes() == edited
    assert not (root / "project-baseline.md").exists()


def test_the_include_is_never_repointed_when_the_copy_cannot_be_written(
        tmp_path, templates):
    """Write-then-localize, whatever order the plan lists the steps in."""
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    plan = iw.plan_wiring(posture_of(root, templates))
    reversed_plan = iw.WiringPlan(steps=tuple(reversed(plan.steps)))
    before = (root / "BASIC_INSTRUCTIONS.md").read_bytes()

    result = iw.apply_wiring(str(root), reversed_plan, root.name, "",
                             baseline_template_text="")   # copy write fails

    assert not result.ok
    assert (root / "BASIC_INSTRUCTIONS.md").read_bytes() == before


def test_link_is_prepended_and_the_rest_is_untouched(tmp_path, templates):
    body = "# Title\n\nSome authored prose.\n\n## A section\n\nmore\n"
    root = make_project(tmp_path, claude=body, basic=LOCAL + "\n", copy=True)
    _plan, result = wire(root, templates)
    assert result.ok
    after = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert after.endswith(body)
    assert after.startswith("@BASIC_INSTRUCTIONS.md")


def test_crlf_line_endings_survive(tmp_path, templates):
    body = "# Title\r\n\r\nAuthored prose.\r\n"
    root = make_project(tmp_path, claude=body, basic=LOCAL + "\n", copy=True)
    wire(root, templates)
    raw = (root / "CLAUDE.md").read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0


def test_lf_file_stays_lf(tmp_path, templates):
    root = make_project(tmp_path, claude="# Title\n\nprose\n",
                        basic=LOCAL + "\n", copy=True)
    wire(root, templates)
    assert b"\r\n" not in (root / "CLAUDE.md").read_bytes()


def test_rerun_after_localizing_changes_nothing(tmp_path, templates):
    root = make_project(tmp_path, claude="# Title\n\nprose\n",
                        basic=inc_line(templates) + "\n")
    _plan, first = wire(root, templates)
    assert first.ok, first.error
    names = ("CLAUDE.md", "BASIC_INSTRUCTIONS.md", "project-baseline.md")
    snapshot = {n: ((root / n).read_bytes(), os.stat(root / n).st_mtime_ns)
                for n in names}

    plan2, second = wire(root, templates)

    assert plan2.is_noop
    assert second.changed_files == ()
    assert {n: ((root / n).read_bytes(), os.stat(root / n).st_mtime_ns)
            for n in names} == snapshot


@pytest.mark.skipif(not shutil.which("git"), reason="git not on PATH")
def test_a_committed_localized_project_stays_clean_on_a_rerun(tmp_path,
                                                             templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    _plan, first = wire(root, templates)
    assert first.ok, first.error

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "wire")

    wire(root, templates)
    assert git("status", "--porcelain") == ""


def test_basic_instructions_is_not_touched_when_it_already_carries(tmp_path,
                                                                  templates):
    body = LOCAL + "\n\n# Real project notes\n"
    root = make_project(tmp_path, basic=body, copy=True)
    _plan, result = wire(root, templates)
    assert "BASIC_INSTRUCTIONS.md" not in result.changed_files
    assert (root / "BASIC_INSTRUCTIONS.md").read_text(encoding="utf-8") == body


def test_an_outdated_copy_is_refreshed_and_nothing_else(tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=LOCAL + "\n")
    (root / "project-baseline.md").write_bytes(
        bc.render_copy("# older\n").encode("utf-8"))
    _plan, result = wire(root, templates)
    assert result.changed_files == ("project-baseline.md",)
    assert posture_of(root, templates).healthy


def test_authored_contradiction_survives_wiring(tmp_path, templates):
    """Wiring adds a line. It does not edit what the project already says."""
    prose = "## Token-Saving Tool Usage\n\nUse the Explore subagent.\n"
    root = make_project(tmp_path, claude=prose, basic=LOCAL + "\n", copy=True)
    wire(root, templates)
    after = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert prose in after
    assert ip.ADVISORY_CONTRADICTS in posture_of(root, templates).advisories


def test_absent_without_a_template_wires_the_one_hop_shape(tmp_path, templates):
    """A blank template path must not leave a project unwired."""
    root = make_project(tmp_path, claude="# Notes\n")
    plan = iw.plan_wiring(posture_of(root, templates), has_template=False)
    assert plan.files == ("project-baseline.md", "CLAUDE.md")
    result = iw.apply_wiring(str(root), plan, root.name, "", BASELINE_TEXT)
    assert result.ok
    assert (root / "CLAUDE.md").read_text(encoding="utf-8").startswith(LOCAL)
    assert posture_of(root, templates).healthy


# -- applying to one project: outcomes are data, not sentences ------------
#
# These assert on MEMBERS. If any of them needs `in result.render()` to tell two
# situations apart, the shape has regressed.

def apply_one(root, templates, template_text="", posture=None):
    return iw.apply_to_project(
        posture or posture_of(root, templates), baseline_of(templates),
        str(templates), template_text, bool(template_text), BASELINE_TEXT,
        claude_projects={})


class TestApplyOutcome:
    def test_a_clean_wire_reports_wired_and_the_files_it_touched(
            self, tmp_path, templates):
        root = make_project(tmp_path, claude="# P\n",
                            basic=inc_line(templates) + "\n")
        result = apply_one(root, templates)
        assert result.outcome == iw.OUTCOME_WIRED, result.render()
        assert result.wrote is True
        assert "CLAUDE.md" in result.changed_files
        assert "project-baseline.md" in result.changed_files

    def test_an_already_healthy_project_is_not_a_write(self, tmp_path,
                                                       templates):
        root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                            basic=LOCAL + "\n", copy=True)
        result = apply_one(root, templates)
        assert result.outcome == iw.OUTCOME_ALREADY_RESOLVED
        assert result.wrote is False
        assert result.changed_files == ()

    def test_a_project_that_changed_since_the_preview_is_its_own_outcome(
            self, tmp_path, templates):
        """The safety net doing its job, and it must not look like a refusal."""
        root = make_project(tmp_path, claude="# P\n",
                            basic=inc_line(templates) + "\n")
        stale = posture_of(root, templates)          # ORPHANED at this point

        (root / "CLAUDE.md").write_bytes(b"# P\n\n@BASIC_INSTRUCTIONS.md\n")

        result = apply_one(root, templates, posture=stale)
        assert result.outcome == iw.OUTCOME_SKIPPED_STATE_CHANGED
        assert result.changed_files == ()
        assert not (root / "project-baseline.md").exists()

    def test_a_blocked_plan_is_a_different_member_from_a_stale_one(
            self, tmp_path, templates):
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
