"""Tests for helpers/agent_rules.py — AGENTS.md and .cursor/rules/*.mdc.

These files are written by a tool INTO a file a human also edits, repeatedly.
That is the whole risk surface, so most of what is checked here is what must
survive a re-run: text above the block, text below it, the user's own heading,
a rule they added by hand. Idempotency is checked byte-for-byte rather than
"looks similar", because a re-run that rewrites identical content still dirties
git status and trains people to ignore the diff.
"""
import os

import pytest

from helpers.agent_rules import (
    BEGIN_MARKER,
    END_MARKER,
    _compute_agents_md,
    _compute_cursor_rule,
    agents_md_path,
    cursor_rule_path,
    read_baseline,
    write_agents_md,
    write_cursor_rule,
)

BASELINE = "## Tokensave: Use It First\n\nUse tokensave_context before Read.\n"
BASELINE_2 = "## Tokensave: Use It First\n\nUPDATED GUIDANCE HERE.\n"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


# ── AGENTS.md ────────────────────────────────────────────────────────────────

def test_new_agents_md_gets_a_preamble_and_a_block():
    text, changed = _compute_agents_md("", BASELINE, "MyProj")
    assert changed
    assert text.startswith("# Agent Instructions")
    assert "MyProj" in text
    assert BEGIN_MARKER in text and END_MARKER in text
    assert "Use tokensave_context before Read." in text


def test_baseline_content_is_inlined_not_referenced():
    """The entire point: Cursor cannot follow an @include, so no pointer."""
    text, _ = _compute_agents_md("", BASELINE)
    assert "@" not in text.split(BEGIN_MARKER)[1].split(END_MARKER)[0]
    assert "project-baseline.md" not in text


def test_rerun_with_identical_content_reports_no_change():
    first, _ = _compute_agents_md("", BASELINE)
    second, changed = _compute_agents_md(first, BASELINE)
    assert changed is False
    assert second == first


def test_rerun_is_byte_identical():
    first, _ = _compute_agents_md("", BASELINE)
    second, _ = _compute_agents_md(first, BASELINE)
    third, _ = _compute_agents_md(second, BASELINE)
    assert first == second == third


def test_updated_baseline_replaces_only_the_block():
    first, _ = _compute_agents_md("", BASELINE)
    second, changed = _compute_agents_md(first, BASELINE_2)
    assert changed
    assert "UPDATED GUIDANCE HERE." in second
    assert "Use tokensave_context before Read." not in second
    assert second.startswith("# Agent Instructions")


def test_user_text_above_the_block_survives():
    existing = ("# My Own Heading\n\nHand-written house rules.\n\n"
                + BEGIN_MARKER + "\nold\n" + END_MARKER + "\n")
    text, _ = _compute_agents_md(existing, BASELINE)
    assert "# My Own Heading" in text
    assert "Hand-written house rules." in text


def test_user_text_below_the_block_survives():
    existing = (BEGIN_MARKER + "\nold\n" + END_MARKER
                + "\n\n## My section\n\nDo not delete me.\n")
    text, _ = _compute_agents_md(existing, BASELINE)
    assert "Do not delete me." in text
    assert "## My section" in text


def test_an_existing_file_does_not_get_a_second_preamble():
    """The user may have rewritten their own header; do not re-impose ours."""
    existing = "# Their Own Title\n\n" + BEGIN_MARKER + "\nold\n" + END_MARKER + "\n"
    text, _ = _compute_agents_md(existing, BASELINE)
    assert text.count("# Agent Instructions") == 0
    assert "# Their Own Title" in text


def test_existing_file_with_no_markers_is_appended_to_not_replaced():
    existing = "# Pre-existing AGENTS.md\n\nSomeone wrote this by hand.\n"
    text, changed = _compute_agents_md(existing, BASELINE)
    assert changed
    assert "Someone wrote this by hand." in text
    assert BEGIN_MARKER in text


def test_a_truncated_marker_pair_does_not_mangle_the_file():
    """A half-written file must not be sliced against a boundary that is absent."""
    existing = "# Notes\n\n" + BEGIN_MARKER + "\nsomething interrupted\n"
    text, _ = _compute_agents_md(existing, BASELINE)
    assert "something interrupted" in text
    assert "# Notes" in text


# ── .cursor/rules/tokensave.mdc ──────────────────────────────────────────────

def test_new_cursor_rule_starts_with_frontmatter():
    text, changed = _compute_cursor_rule("", BASELINE)
    assert changed
    assert text.startswith("---\n")
    assert "alwaysApply: true" in text
    assert "description:" in text


def test_cursor_rule_frontmatter_is_before_the_block():
    """Cursor requires frontmatter at the very top, so it cannot be inside it."""
    text, _ = _compute_cursor_rule("", BASELINE)
    assert text.index("---") < text.index(BEGIN_MARKER)


def test_cursor_rule_inlines_the_baseline():
    text, _ = _compute_cursor_rule("", BASELINE)
    assert "Use tokensave_context before Read." in text


def test_cursor_rule_rerun_is_byte_identical():
    first, _ = _compute_cursor_rule("", BASELINE)
    second, changed = _compute_cursor_rule(first, BASELINE)
    assert changed is False
    assert second == first


def test_cursor_rule_never_grows_a_second_frontmatter_block():
    """Two frontmatter blocks make a file Cursor rejects outright."""
    first, _ = _compute_cursor_rule("", BASELINE)
    second, _ = _compute_cursor_rule(first, BASELINE_2)
    assert second.count("alwaysApply: true") == 1
    assert second.count("---\ndescription:") == 1


def test_cursor_rule_preserves_user_content_below_the_block():
    first, _ = _compute_cursor_rule("", BASELINE)
    edited = first + "\n## My extra rule\n\nAlways run the linter.\n"
    updated, changed = _compute_cursor_rule(edited, BASELINE_2)
    assert changed
    assert "Always run the linter." in updated
    assert "UPDATED GUIDANCE HERE." in updated


def test_cursor_rule_updates_the_managed_block_only():
    first, _ = _compute_cursor_rule("", BASELINE)
    updated, _ = _compute_cursor_rule(first, BASELINE_2)
    assert "Use tokensave_context before Read." not in updated
    assert "UPDATED GUIDANCE HERE." in updated


# ── Paths ────────────────────────────────────────────────────────────────────

def test_cursor_rule_uses_the_mdc_extension(project):
    """Cursor IGNORES plain .md in .cursor/rules — the extension is load-bearing."""
    path = cursor_rule_path(project)
    assert path.endswith(".mdc")
    assert os.path.dirname(path).endswith(os.path.join(".cursor", "rules"))


def test_agents_md_sits_at_the_project_root(project):
    assert agents_md_path(project) == os.path.join(project, "AGENTS.md")


# ── IO wrappers ──────────────────────────────────────────────────────────────

def test_write_agents_md_creates_the_file(project):
    ok, err, changed = write_agents_md(project, BASELINE, "MyProj")
    assert ok, err
    assert changed
    assert os.path.isfile(agents_md_path(project))


def test_write_cursor_rule_creates_the_nested_directories(project):
    ok, err, changed = write_cursor_rule(project, BASELINE)
    assert ok, err
    assert changed
    assert os.path.isfile(cursor_rule_path(project))


def test_second_write_reports_unchanged_and_leaves_bytes_alone(project):
    write_agents_md(project, BASELINE, "MyProj")
    before = open(agents_md_path(project), encoding="utf-8").read()
    ok, err, changed = write_agents_md(project, BASELINE, "MyProj")
    assert ok and changed is False, err
    assert open(agents_md_path(project), encoding="utf-8").read() == before


def test_both_outputs_carry_the_same_baseline(project):
    """One source, two renderings — nothing to keep in sync."""
    write_agents_md(project, BASELINE)
    write_cursor_rule(project, BASELINE)
    agents = open(agents_md_path(project), encoding="utf-8").read()
    rule = open(cursor_rule_path(project), encoding="utf-8").read()
    assert "Use tokensave_context before Read." in agents
    assert "Use tokensave_context before Read." in rule


def test_writes_use_lf_line_endings(project):
    """These files get committed; CRLF would churn the diff on every platform."""
    write_agents_md(project, BASELINE)
    with open(agents_md_path(project), "rb") as fh:
        assert b"\r\n" not in fh.read()


# ── Baseline source ──────────────────────────────────────────────────────────

def test_read_baseline_returns_empty_when_the_template_is_missing(tmp_path):
    """Callers skip the step with a message rather than writing an empty file
    that looks configured."""
    assert read_baseline(str(tmp_path)) == ""


def test_read_baseline_reads_the_real_project_template():
    from constants import _BASE_DIR
    text = read_baseline(os.path.join(_BASE_DIR, "templates"))
    assert "Tokensave" in text


# ── The Retrofit dialog actually builds with its new checkboxes ──────────────
#
# The helper tests above prove the CONTENT is right; this proves the two boxes
# that reach it exist and are wired. A Tk build error here would otherwise only
# surface when a user opened the dialog.

@pytest.mark.tk
def test_retrofit_dialog_builds_with_the_two_rules_checkboxes(tk_root, project):
    from dialogs.retrofit import RetrofitDialog

    captured = {}
    dlg = RetrofitDialog(tk_root, project,
                         lambda *a, **k: captured.update(kwargs=k, args=a))
    try:
        assert dlg.var_agents.get() is True       # on by default
        assert dlg.var_cursor_rule.get() is True
    finally:
        dlg.destroy()


@pytest.mark.tk
def test_retrofit_dialog_forwards_the_rules_flags(tk_root, project):
    """End to end through the real dialog: ticking only the rules boxes must
    still call the controller."""
    from dialogs.retrofit import RetrofitDialog

    captured = {}
    dlg = RetrofitDialog(tk_root, project,
                         lambda *a, **k: captured.update(kwargs=k, args=a))
    for name in ("var_ts", "var_bi", "var_nuitka", "var_shadow", "var_hook"):
        getattr(dlg, name).set(False)
    dlg.var_agents.set(True)
    dlg.var_cursor_rule.set(True)
    dlg._apply()
    assert captured["kwargs"]["add_agents"] is True
    assert captured["kwargs"]["add_cursor_rule"] is True
