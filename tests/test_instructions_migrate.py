"""instructions_migrate: BASIC_INSTRUCTIONS.md is folded into CLAUDE.md in place.

The property under test is that the migration is a SPLICE: the text Claude loads
is the same before and after, in the same order, and nothing is deleted. The
fleet measurement behind it (11 of 14 chain projects hold authored rules in
BASIC, up to ~71 KB) is why "mostly a rename" was never an acceptable shortcut.
"""
from __future__ import annotations

import pytest

from helpers import baseline_copy as bc
from helpers import instructions_migrate as im
from helpers import instructions_posture as ip

BASELINE_TEXT = "# Project Baseline Rules\n\n## Tokensave: Use It First\n"
LOCAL = bc.LOCAL_INCLUDE_LINE
TEMPLATE = ("# [PROJECT NAME] - Claude Instructions\n\n" + LOCAL +
            "\n\n## Overview\n\n[Describe the project here]\n")
AUTHORED = (LOCAL + "\n\n# Rules\n\n- Always run the linter.\n"
            "- Never touch dist/.\n")


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "project-baseline.md").write_text(BASELINE_TEXT, encoding="utf-8")
    return d


def make(tmp_path, claude, basic, name="proj"):
    root = tmp_path / name
    root.mkdir()
    (root / "CLAUDE.md").write_bytes(claude.encode("utf-8"))
    (root / "BASIC_INSTRUCTIONS.md").write_bytes(basic.encode("utf-8"))
    (root / "project-baseline.md").write_bytes(
        bc.render_copy(BASELINE_TEXT).encode("utf-8"))
    return root


def posture(root, templates):
    baseline = ip.canonical(str(templates / "project-baseline.md"))
    return ip.read_project(str(root), root.name, str(templates), baseline,
                           BASELINE_TEXT, TEMPLATE, {})


def migrate(root, templates):
    p = posture(root, templates)
    baseline = ip.canonical(str(templates / "project-baseline.md"))
    return im.migrate_project(p, baseline, str(templates), TEMPLATE,
                              BASELINE_TEXT, claude_projects={})


# -- planning (pure) ------------------------------------------------------

def test_the_directive_line_is_replaced_by_basics_text_in_place():
    claude = "# Mine\n\n@BASIC_INSTRUCTIONS.md\n\n## After\n"
    plan = im.plan_migration(claude, AUTHORED, TEMPLATE)
    assert not plan.is_blocked, plan.blocked
    assert plan.new_claude_text == "# Mine\n\n" + AUTHORED + "\n## After\n"
    assert not plan.dropped_template


def test_every_authored_line_survives_in_order():
    plan = im.plan_migration("@BASIC_INSTRUCTIONS.md\n", AUTHORED, TEMPLATE)
    kept = [l for l in AUTHORED.splitlines() if l.strip()]
    got = [l for l in plan.new_claude_text.splitlines() if l.strip()]
    assert got == kept


def test_crlf_claude_gets_crlf_basic_text():
    plan = im.plan_migration("@BASIC_INSTRUCTIONS.md\r\n", AUTHORED, TEMPLATE)
    assert "\n" not in plan.new_claude_text.replace("\r\n", "")


def test_an_untouched_template_carries_only_its_baseline_include():
    plan = im.plan_migration("@BASIC_INSTRUCTIONS.md\n", TEMPLATE, TEMPLATE)
    assert plan.dropped_template
    assert plan.new_claude_text == LOCAL + "\n"
    assert "PROJECT NAME" not in plan.new_claude_text


def test_the_template_check_ignores_how_the_include_line_is_spelled():
    other = TEMPLATE.replace(LOCAL, "@D:/somewhere/project-baseline.md")
    assert im.is_untouched_template(other, TEMPLATE)


def test_one_edited_word_is_authored_not_a_template():
    edited = TEMPLATE.replace("[Describe the project here]", "A converter.")
    assert not im.is_untouched_template(edited, TEMPLATE)


@pytest.mark.parametrize("claude, basic, expect", [
    ("# Notes\n", AUTHORED, "does not include"),
    ("@BASIC_INSTRUCTIONS.md\n@BASIC_INSTRUCTIONS.md\n", AUTHORED, "more than once"),
    ("@BASIC_INSTRUCTIONS.md\n", AUTHORED + "```\nunclosed\n", "unclosed"),
    ("@BASIC_INSTRUCTIONS.md\n", "# no include anywhere\n", "neither file"),
    ("@BASIC_INSTRUCTIONS.md\n" + LOCAL + "\n", AUTHORED, "load twice"),
])
def test_refusals(claude, basic, expect):
    plan = im.plan_migration(claude, basic, TEMPLATE)
    assert plan.is_blocked and expect in plan.blocked
    assert plan.new_claude_text == ""


def test_an_include_pointing_outside_the_project_is_not_the_basic_file():
    plan = im.plan_migration("@../elsewhere/BASIC_INSTRUCTIONS.md\n",
                             AUTHORED, TEMPLATE)
    assert plan.is_blocked


# -- applying -------------------------------------------------------------

def test_migration_writes_claude_md_and_leaves_basic_byte_for_byte(
        tmp_path, templates):
    root = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", AUTHORED)
    basic_before = (root / "BASIC_INSTRUCTIONS.md").read_bytes()

    outcome = migrate(root, templates)

    assert outcome.outcome == im.OUTCOME_MIGRATED, outcome.reason
    assert outcome.changed_files == ("CLAUDE.md",)
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == AUTHORED
    assert (root / "BASIC_INSTRUCTIONS.md").read_bytes() == basic_before
    after = posture(root, templates)
    assert after.healthy
    assert ip.canonical(str(root / "BASIC_INSTRUCTIONS.md")) not in after.chain


def test_a_file_edited_between_plan_and_apply_is_not_written(tmp_path, templates):
    root = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", AUTHORED)
    claude_text, basic_text = im.read_pair(str(root))
    plan = im.plan_migration(claude_text, basic_text, TEMPLATE)
    (root / "CLAUDE.md").write_bytes(b"@BASIC_INSTRUCTIONS.md\n# edited\n")

    ok, error, stale = im.apply_migration(str(root), plan)

    assert not ok and stale and "changed since the plan" in error
    assert (root / "CLAUDE.md").read_bytes() == b"@BASIC_INSTRUCTIONS.md\n# edited\n"


def test_basic_edited_between_plan_and_apply_is_not_migrated(tmp_path, templates):
    root = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", AUTHORED)
    claude_text, basic_text = im.read_pair(str(root))
    plan = im.plan_migration(claude_text, basic_text, TEMPLATE)
    (root / "BASIC_INSTRUCTIONS.md").write_bytes(
        (AUTHORED + "- A rule added after the preview.\n").encode("utf-8"))

    ok, _error, stale = im.apply_migration(str(root), plan)

    assert not ok and stale
    assert (root / "CLAUDE.md").read_bytes() == b"@BASIC_INSTRUCTIONS.md\n"


def test_a_project_already_on_the_direct_shape_is_skipped_not_rewritten(
        tmp_path, templates):
    root = make(tmp_path, LOCAL + "\n\n# Mine\n", AUTHORED)
    before = (root / "CLAUDE.md").read_bytes()
    outcome = migrate(root, templates)
    assert outcome.outcome == im.OUTCOME_SKIPPED_BLOCKED
    assert (root / "CLAUDE.md").read_bytes() == before


def test_a_project_whose_chain_does_not_resolve_is_sent_to_repair_first(
        tmp_path, templates):
    root = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", AUTHORED)
    (root / "project-baseline.md").unlink()
    outcome = migrate(root, templates)
    assert outcome.outcome == im.OUTCOME_SKIPPED_BLOCKED
    assert "repair first" in outcome.reason


def test_an_untouched_template_migrates_without_pasting_the_template(
        tmp_path, templates):
    root = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", TEMPLATE)
    outcome = migrate(root, templates)
    assert outcome.outcome == im.OUTCOME_MIGRATED, outcome.reason
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert text == LOCAL + "\n" and "Overview" not in text
    assert posture(root, templates).healthy


# -- offering --------------------------------------------------------------

def test_the_panel_offers_a_migration_only_on_the_three_file_chain(
        tmp_path, templates):
    chain = make(tmp_path, "@BASIC_INSTRUCTIONS.md\n", AUTHORED, name="chain")
    direct = make(tmp_path, LOCAL + "\n", AUTHORED, name="direct")
    assert im.eligible(posture(chain, templates))
    assert not im.eligible(posture(direct, templates)), \
        "BASIC is unreferenced there; there is nothing to fold in"
