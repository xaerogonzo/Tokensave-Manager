"""tests/test_doc_drafter_filters.py — helpers/doc_drafter_filters.py (pure logic)."""
from __future__ import annotations

import pytest

from helpers.doc_drafter_filters import (
    _is_noop_bullet,
    _looks_truncated,
    _is_duplicate,
    _is_duplicate_from_sets,
    _token_set,
    _normalise_bullet,
    _sanitise_raw_draft,
    _merge_wrapped_bullets,
    _filter_bullets,
    parse_grouped_bullets,
    split_readme_subsection,
    architecture_parse_draft,
    roadmap_parse_draft,
    memory_parse_draft,
    generic_parse_draft,
    _strip_preamble_and_fences,
    _filter_freeform,
    _mirror_contract_check,
    _preserve_score,
)


# ── _is_noop_bullet ──────────────────────────────────────────────────────────

class TestIsNoopBullet:
    def test_none_returns_true(self):
        assert _is_noop_bullet("none")

    def test_na_returns_true(self):
        assert _is_noop_bullet("n/a")

    def test_nothing_returns_true(self):
        assert _is_noop_bullet("nothing")

    def test_tbd_returns_true(self):
        assert _is_noop_bullet("TBD")

    def test_no_changes_returns_true(self):
        assert _is_noop_bullet("no changes")

    def test_nothing_to_add_returns_true(self):
        assert _is_noop_bullet("nothing to add")

    def test_empty_returns_false(self):
        assert not _is_noop_bullet("")

    def test_real_bullet_returns_false(self):
        assert not _is_noop_bullet("- fixed the retry logic in db_pool")

    def test_bullet_with_dash_prefix(self):
        assert _is_noop_bullet("- none")

    def test_case_insensitive(self):
        assert _is_noop_bullet("NONE")
        assert _is_noop_bullet("N/A")


# ── _looks_truncated ──────────────────────────────────────────────────────────

class TestLooksTruncated:
    def test_ends_with_period_not_truncated(self):
        assert not _looks_truncated("- fixed the retry logic in db_pool.")

    def test_ends_with_stopword_no_markup_truncated(self):
        assert _looks_truncated("- updated the connection for")

    def test_ends_with_stopword_has_markup_not_truncated(self):
        assert not _looks_truncated("- updated db_pool for")

    def test_ends_with_colon_not_truncated(self):
        assert not _looks_truncated("- added three things:")

    def test_ends_with_dangling_open_paren_truncated(self):
        assert _looks_truncated("- calls helper_fn (")

    def test_ends_with_slash_truncated(self):
        assert _looks_truncated("- added file/")

    def test_empty_string_not_truncated(self):
        assert not _looks_truncated("")

    def test_long_bullet_ending_stopword_not_truncated(self):
        long = "- " + "word " * 12 + "to"
        assert not _looks_truncated(long)

    def test_real_meaningful_bullet_not_truncated(self):
        assert not _looks_truncated("- refactored the commit dialog to use ProposalBridge.")

    def test_ends_with_closing_paren_not_truncated(self):
        assert not _looks_truncated("- fixed the retry logic (in db_pool)")


# ── _token_set ────────────────────────────────────────────────────────────────

class TestTokenSet:
    def test_empty_returns_empty(self):
        assert _token_set("") == set()

    def test_strips_stopwords(self):
        tokens = _token_set("the quick fox")
        assert "the" not in tokens

    def test_includes_content_words(self):
        tokens = _token_set("refactored connection pool")
        assert "refactored" in tokens
        assert "connection" in tokens
        assert "pool" in tokens

    def test_lowercased(self):
        tokens = _token_set("UPPERCASE lower")
        assert "uppercase" in tokens

    def test_short_words_excluded(self):
        tokens = _token_set("a to of go it")
        assert not any(len(t) <= 2 for t in tokens)


# ── _is_duplicate ─────────────────────────────────────────────────────────────

class TestIsDuplicate:
    def test_identical_bullets_are_duplicates(self):
        b = "- fixed the connection pool retry logic"
        assert _is_duplicate(b, b)

    def test_completely_different_not_duplicate(self):
        a = "- added dark mode support"
        b = "- fixed the login redirect"
        assert not _is_duplicate(a, b)

    def test_high_overlap_is_duplicate(self):
        # High word overlap → Jaccard or overlap threshold met
        a = "- fixed connection pool retry support timeout"
        b = "- fixed connection pool retry support timeout logic"
        assert _is_duplicate(a, b)

    def test_empty_not_duplicate(self):
        assert not _is_duplicate("", "- something")
        assert not _is_duplicate("- something", "")


# ── _normalise_bullet ─────────────────────────────────────────────────────────

class TestNormaliseBullet:
    def test_strips_dash(self):
        assert _normalise_bullet("- foo bar") == "foo bar"

    def test_strips_asterisk(self):
        assert _normalise_bullet("* foo bar") == "foo bar"

    def test_lowercased(self):
        assert _normalise_bullet("- FOO BAR") == "foo bar"

    def test_collapses_whitespace(self):
        assert _normalise_bullet("-   multiple   spaces") == "multiple spaces"

    def test_empty_string(self):
        assert _normalise_bullet("") == ""


# ── _sanitise_raw_draft ───────────────────────────────────────────────────────

class TestSanitiseRawDraft:
    def test_removes_trailing_code_fence(self):
        text = "- bullet\n```"
        result = _sanitise_raw_draft(text)
        assert "```" not in result

    def test_removes_trailing_generated_marker(self):
        text = "- bullet\n*generated by AI*"
        result = _sanitise_raw_draft(text)
        assert "*generated" not in result

    def test_preserves_real_content(self):
        text = "- added feature X\n- fixed bug Y"
        result = _sanitise_raw_draft(text)
        assert "feature X" in result
        assert "fixed bug Y" in result

    def test_removes_leading_code_fence(self):
        text = "```\n- bullet"
        result = _sanitise_raw_draft(text)
        assert result.strip().startswith("- bullet")


# ── _merge_wrapped_bullets ────────────────────────────────────────────────────

class TestMergeWrappedBullets:
    def test_wraps_continuation_onto_bullet(self):
        text = "- move parser state machine into\n  shared retry coordinator"
        result = _merge_wrapped_bullets(text)
        assert "\n" not in result.strip()
        assert "coordinator" in result

    def test_does_not_merge_unindented_lines(self):
        text = "- bullet one\nnot indented"
        result = _merge_wrapped_bullets(text)
        assert "\n" in result

    def test_preserves_tree_chars(self):
        text = "- bullet\n  │ tree line"
        result = _merge_wrapped_bullets(text)
        assert "│" in result
        lines = result.splitlines()
        assert len(lines) == 2


# ── _filter_bullets ───────────────────────────────────────────────────────────

class TestFilterBullets:
    def test_removes_noop_bullets(self):
        text = "- none\n- real bullet here"
        result, trunc, dup, noop = _filter_bullets(text, [])
        assert "none" not in result.lower() or "real" in result
        assert noop >= 1

    def test_removes_truncated_bullets(self):
        text = "- updated the connection for\n- real bullet done."
        result, trunc, dup, noop = _filter_bullets(text, [])
        assert trunc >= 1
        assert "done." in result

    def test_dedup_against_existing(self):
        existing = ["- fixed the retry logic in the pool"]
        text = "- fixed the retry logic in the pool\n- new improvement here."
        result, _, dup, _ = _filter_bullets(text, existing)
        assert dup >= 1
        assert "improvement" in result

    def test_keeps_good_bullets(self):
        text = "- added dark mode support.\n- improved startup performance."
        result, trunc, dup, noop = _filter_bullets(text, [])
        assert "dark mode" in result
        assert "startup" in result
        assert trunc == 0 and dup == 0 and noop == 0

    def test_in_draft_dedup(self):
        text = "- improved retry logic.\n- improved retry logic."
        result, _, dup, _ = _filter_bullets(text, [])
        assert dup >= 1

    def test_non_bullet_lines_dropped(self):
        text = "Some prose paragraph.\n- real bullet here.\nMore prose."
        result, _, _, _ = _filter_bullets(text, [])
        assert "prose" not in result
        assert "real bullet" in result


# ── parse_grouped_bullets ─────────────────────────────────────────────────────

class TestParseGroupedBullets:
    def test_simple_single_section(self):
        text = "### Added\n- first bullet\n- second bullet"
        pairs = parse_grouped_bullets(text)
        assert len(pairs) == 1
        title, body = pairs[0]
        assert title == "Added"
        assert "first bullet" in body

    def test_multiple_sections(self):
        text = "### Added\n- feat A\n### Fixed\n- bug B"
        pairs = parse_grouped_bullets(text)
        assert len(pairs) == 2
        titles = [t for t, _ in pairs]
        assert "Added" in titles
        assert "Fixed" in titles

    def test_prose_between_bullets_dropped(self):
        text = "### Added\nSome prose paragraph.\n- bullet one."
        pairs = parse_grouped_bullets(text)
        body = pairs[0][1]
        assert "prose" not in body
        assert "bullet one" in body

    def test_empty_section_omitted(self):
        text = "### Added\n"
        pairs = parse_grouped_bullets(text)
        assert pairs == []

    def test_no_sections(self):
        text = "- orphan bullet"
        assert parse_grouped_bullets(text) == []


# ── split_readme_subsection ───────────────────────────────────────────────────

class TestSplitReadmeSubsection:
    def test_extracts_bold_header(self):
        text = "**My Feature**\n- bullet one\n- bullet two"
        result = split_readme_subsection(text)
        assert result is not None
        header, bullets = result
        assert header == "**My Feature**"
        assert "bullet one" in bullets

    def test_returns_none_when_no_header(self):
        text = "- orphan bullet"
        assert split_readme_subsection(text) is None

    def test_header_with_colon(self):
        text = "**New feature:**\n- content"
        result = split_readme_subsection(text)
        assert result is not None

    def test_bullets_after_header(self):
        text = "**Section**\n- first\n- second"
        _, bullets = split_readme_subsection(text)
        assert "first" in bullets and "second" in bullets


# ── architecture_parse_draft ──────────────────────────────────────────────────

class TestArchitectureParseDraft:
    def test_extracts_section(self):
        text = "## Overview\nsome content here"
        (sections,) = architecture_parse_draft(text)
        assert len(sections) == 1
        assert sections[0][0] == "Overview"

    def test_extracts_multiple_sections(self):
        text = "## Intro\ntext A\n## Detail\ntext B"
        (sections,) = architecture_parse_draft(text)
        assert len(sections) == 2

    def test_empty_draft_returns_empty(self):
        (sections,) = architecture_parse_draft("")
        assert sections == []

    def test_no_sections_returns_empty(self):
        (sections,) = architecture_parse_draft("just some prose, no headings")
        assert sections == []


# ── roadmap_parse_draft ───────────────────────────────────────────────────────

class TestRoadmapParseDraft:
    def test_extracts_roadmap_section(self):
        text = "## Roadmap 9 — Shadow Links\nFull shadow-link scanner"
        (sections,) = roadmap_parse_draft(text)
        assert len(sections) == 1
        n, theme, body = sections[0]
        assert n == 9
        assert "Shadow Links" in theme

    def test_extracts_multiple_roadmaps(self):
        text = "## Roadmap 8 — Tools\nstuff\n## Roadmap 9 — More\nother"
        (sections,) = roadmap_parse_draft(text)
        assert len(sections) == 2

    def test_empty_returns_empty(self):
        (sections,) = roadmap_parse_draft("")
        assert sections == []


# ── memory_parse_draft ────────────────────────────────────────────────────────

class TestMemoryParseDraft:
    def test_returns_body(self):
        text = "User prefers terse responses."
        (body,) = memory_parse_draft(text)
        assert "terse" in body

    def test_empty_returns_none(self):
        (body,) = memory_parse_draft("")
        assert body is None

    def test_whitespace_only_returns_none(self):
        (body,) = memory_parse_draft("   ")
        assert body is None


# ── generic_parse_draft ───────────────────────────────────────────────────────

def test_generic_parse_draft_extracts_sections():
    text = "## Goals\nsome goals\n## Non-goals\nsome non-goals"
    (sections,) = generic_parse_draft(text)
    assert len(sections) == 2
    titles = [t for t, _ in sections]
    assert "Goals" in titles


# ── _strip_preamble_and_fences ────────────────────────────────────────────────

class TestStripPreambleAndFences:
    def test_removes_prose_before_heading(self):
        text = "Based on my analysis:\n\n## Overview\n- bullet"
        result = _strip_preamble_and_fences(text)
        assert "Based on my analysis" not in result
        assert "Overview" in result

    def test_unwraps_code_fence(self):
        text = "## Section\n```markdown\n- content\n```"
        result = _strip_preamble_and_fences(text)
        assert "```" not in result
        assert "content" in result

    def test_strips_footer_marker_when_no_content_follows(self):
        text = "## Section\n- real bullet\nThese bullets capture the changes."
        result = _strip_preamble_and_fences(text)
        assert "These bullets" not in result

    def test_no_change_on_clean_text(self):
        text = "## Section\n- clean bullet here."
        result = _strip_preamble_and_fences(text)
        assert "clean bullet" in result


# ── _filter_freeform ──────────────────────────────────────────────────────────

class TestFilterFreeform:
    def test_strips_end_marker(self):
        text = "## Notes\nsome content\n<<<END_OF_DRAFT>>>"
        result, trunc, dup, noop = _filter_freeform(text)
        assert "END_OF_DRAFT" not in result

    def test_removes_placeholder_lines(self):
        text = "## Notes\n- Roadmap-TODO item\n- real content here."
        result, _, _, noop = _filter_freeform(text)
        assert noop >= 1

    def test_preserves_real_content(self):
        text = "## Notes\n- real content here."
        result, trunc, dup, noop = _filter_freeform(text)
        assert "real content" in result
        assert trunc == 0 and dup == 0


# ── _mirror_contract_check ────────────────────────────────────────────────────

class TestMirrorContractCheck:
    def test_passes_when_no_existing(self):
        ok, matched, missing, examples = _mirror_contract_check([], [])
        assert ok

    def test_passes_when_all_preserved(self):
        existing = ["- refactored connection pool", "- added dark mode"]
        draft = ["- refactored connection pool", "- added dark mode"]
        ok, matched, missing, _ = _mirror_contract_check(draft, existing)
        assert ok
        assert matched == 2

    def test_fails_when_too_many_missing(self):
        existing = ["- feature A here", "- feature B there", "- feature C done"]
        draft = ["- completely different entry"]
        ok, matched, missing, examples = _mirror_contract_check(draft, existing)
        # 0 preserved out of 3 = ratio < 0.75, missing > 1 → fail
        assert not ok
