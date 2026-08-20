"""tests/test_doc_drafter_quality.py — the doc-drafter's quality filters.

Split out of the former ``tests/smoke_test.py``, which had grown to 909 lines
covering five unrelated subsystems. Same tests, unchanged; they simply live
next to the code they describe now.

These guard the filters that decide whether a model's output is fit to show:
truncation detection, prose-vs-markup discrimination, wrapped-bullet repair,
section alignment scoring, and the no-op / duplicate bullet rejects.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

# ── Make src/ importable from any working directory ───────────────────────────
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

from helpers.doc_drafter import (       # noqa: E402
    _looks_truncated,
    _merge_wrapped_bullets,
    _is_noop_bullet,
    _is_duplicate,
    _STRUCTURAL_MARKUP_RE,
    _select_candidate_sections,
    _scope_prefix_tokens,
    _subject_tokens,
    _path_tokens,
)


class TestLooksTruncated(unittest.TestCase):
    """Bullets that ARE truncated must be flagged; substantive bullets must pass."""

    # ── should be flagged ──────────────────────────────────────────────────────

    def test_3_word_stopword_end(self):
        self.assertTrue(_looks_truncated("- update the"))

    def test_short_no_markup_ends_on_to(self):
        self.assertTrue(_looks_truncated("- fixed the database connection pool to"))

    def test_short_no_markup_ends_on_for(self):
        self.assertTrue(_looks_truncated("- upgrade daemon connection handling for"))

    def test_scope_prefix_stripped_before_markup_check(self):
        # Scope parens `(foo)` are metadata, not structural markup
        self.assertTrue(_looks_truncated("- (foo) added X for"))

    def test_trailing_slash_operator(self):
        self.assertTrue(_looks_truncated("- migrated parser state /"))

    def test_trailing_equals_operator(self):
        self.assertTrue(_looks_truncated("- db handling ="))

    def test_unmatched_open_paren(self):
        self.assertTrue(_looks_truncated("- retry logic ("))

    def test_prose_comma_does_not_bypass_stopword_check(self):
        # G1: before the fix, a comma anywhere in the bullet would trip the old
        # broad markup regex and pass the bullet through even when it ends on a
        # stopword.  With the G1 fix, comma is excluded — so a bullet ending on
        # "for" (a stopword) that also has a prose comma is correctly flagged.
        self.assertTrue(_looks_truncated("- updated connection pool, for"))

    def test_plain_stopword_end_no_markup(self):
        self.assertTrue(_looks_truncated("- the handler delegates to"))

    # ── should NOT be flagged ─────────────────────────────────────────────────

    def test_parenthetical_markup_passes(self):
        self.assertFalse(_looks_truncated(
            "- updated database connection pool (timeout=30s) and retry for"))

    def test_backtick_markup_passes(self):
        self.assertFalse(_looks_truncated("- updated `db_pool` configuration for"))

    def test_dotted_filename_passes(self):
        # config.json → matches \b\w+[._]\w+\b
        self.assertFalse(_looks_truncated("- updated config.json file to"))

    def test_snake_case_passes(self):
        # db_pool → matches \b\w+[._]\w+\b
        self.assertFalse(_looks_truncated("- updated db_pool to"))

    def test_long_bullet_word_count_bypass(self):
        # ≥12 content words → high-word-count safety valve kicks in
        self.assertFalse(_looks_truncated(
            "- added primary retry logic to handle transient failures during "
            "peak load conditions when downstream service stalls for"))

    def test_ends_on_substantive_word(self):
        self.assertFalse(_looks_truncated(
            "- scrub CHANGELOG corruption and tone-down prompts cleanly"))

    def test_ends_with_period(self):
        self.assertFalse(_looks_truncated("- fixed the connection pool."))

    def test_ends_with_colon(self):
        self.assertFalse(_looks_truncated("- three new features:"))

    def test_ends_with_closing_paren(self):
        self.assertFalse(_looks_truncated("- (see release notes)"))

    def test_empty_string(self):
        self.assertFalse(_looks_truncated(""))

    def test_none(self):
        self.assertFalse(_looks_truncated(None))

    def test_slash_markup_in_path_passes(self):
        self.assertFalse(_looks_truncated(
            "- moved helpers/doc_drafter.py into the new subpackage for"))


class TestStructuralMarkupRe(unittest.TestCase):

    def _matches(self, text):
        return bool(_STRUCTURAL_MARKUP_RE.search(text))

    # ── should match (structural) ──────────────────────────────────────────────
    def test_backtick(self):          self.assertTrue(self._matches("use `db_pool`"))
    def test_parens(self):            self.assertTrue(self._matches("timeout=30s (default)"))
    def test_slash(self):             self.assertTrue(self._matches("src/helpers/foo.py"))
    def test_equals(self):            self.assertTrue(self._matches("timeout=30"))
    def test_snake_case(self):        self.assertTrue(self._matches("db_pool config"))
    def test_dotted_filename(self):   self.assertTrue(self._matches("config.json"))
    def test_bracket(self):           self.assertTrue(self._matches("[key] value"))
    def test_pipe(self):              self.assertTrue(self._matches("cmd | grep"))
    def test_curly(self):             self.assertTrue(self._matches("dict {key: val}"))

    # ── should NOT match (prose) ───────────────────────────────────────────────
    def test_plain_prose(self):
        self.assertFalse(self._matches("updated the connection pool"))

    def test_prose_comma(self):
        # G1 fix: prose comma is not structural
        self.assertFalse(self._matches("updated the pool, added retry"))

    def test_prose_period_mid_sentence(self):
        # Plain words — no dot between \w chars
        self.assertFalse(self._matches("just plain words here"))


class TestMergeWrappedBullets(unittest.TestCase):

    def test_continuation_joined(self):
        inp = "- move parser state machine into\n  shared retry coordinator"
        out = _merge_wrapped_bullets(inp)
        self.assertEqual(
            out,
            "- move parser state machine into shared retry coordinator",
        )

    def test_tree_chars_not_joined(self):
        inp = "- something\n  ├── child node"
        lines = _merge_wrapped_bullets(inp).split("\n")
        # Must stay as two separate lines
        self.assertEqual(len(lines), 2)
        self.assertIn("├── child node", lines[1])

    def test_pipe_tree_char_not_joined(self):
        inp = "- something\n  │   subitem"
        lines = _merge_wrapped_bullets(inp).split("\n")
        self.assertEqual(len(lines), 2)

    def test_non_bullet_preceding_line_not_joined(self):
        # Preceding line is prose (doesn't start with "- " or "* ")
        inp = "Some prose paragraph\n  continuation"
        lines = _merge_wrapped_bullets(inp).split("\n")
        # Should NOT be joined — preceding line isn't a bullet
        self.assertEqual(len(lines), 2)

    def test_idempotent_on_single_line(self):
        line = "- already on one line without issues"
        self.assertEqual(_merge_wrapped_bullets(line), line)

    def test_multi_continuation_lines(self):
        inp = "- started refactoring the\n  parser module for\n  better reuse"
        out = _merge_wrapped_bullets(inp)
        self.assertEqual(out, "- started refactoring the parser module for better reuse")

    def test_star_bullet_also_joined(self):
        inp = "* first part of\n  the bullet text"
        out = _merge_wrapped_bullets(inp)
        self.assertEqual(out, "* first part of the bullet text")

    def test_only_one_space_indent_not_joined(self):
        # Requires ≥2 space indent (^\s{2,})
        inp = "- bullet\n continuation"
        lines = _merge_wrapped_bullets(inp).split("\n")
        self.assertEqual(len(lines), 2)


class TestAlignmentScoring(unittest.TestCase):

    _DOC_WITH_DOC_DRAFTER = """
## doc_drafter

The doc_drafter module builds prompts for markdown documentation.
It handles truncation filtering and candidate section selection.

## Worker Timeout

Handles timeout retries for background workers.

## Unrelated Section

This has nothing to do with doc_drafter or timeouts.
"""

    def test_strong_path_token_produces_alignment(self):
        # "src/helpers/doc_drafter.py" → path tokens include "doc-drafter" and "doc_drafter"
        # "doc_drafter" appears in the section body → strong_body_hit → score ≥ 2
        commits = [{"subject": "fix something minor"}]
        changed = ["src/helpers/doc_drafter.py"]
        _, aligned = _select_candidate_sections(self._DOC_WITH_DOC_DRAFTER, changed, commits)
        self.assertTrue(aligned, "Path token in body must produce alignment")

    def test_scope_prefix_produces_alignment(self):
        # feat(doc-drafter) → strong tokens include "doc-drafter" and "doc_drafter"
        # These appear in section title → strong_title_hit → score ≥ 6
        commits = [{"subject": "fix(doc-drafter): fix truncation"}]
        _, aligned = _select_candidate_sections(self._DOC_WITH_DOC_DRAFTER, [], commits)
        self.assertTrue(aligned, "Scope prefix matching title must produce alignment")

    def test_g3_saturating_cap_prevents_false_alignment(self):
        """G3: two weak-body hits cap at 1 point — cannot cross threshold of 2."""
        doc = """
## Configuration Options

The configuration system handles data updates and stores system records.
"""
        # "update" and "records" appear in body — but these are weak (subject) tokens
        # with no path/scope token, no title match → capped score = 1 < 2 → not aligned
        commits = [{"subject": "fix: update records management"}]
        _, aligned = _select_candidate_sections(doc, [], commits)
        self.assertFalse(aligned,
                         "Two stray subject-word body hits must NOT reach alignment threshold (G3)")

    def test_no_tokens_returns_false_alignment(self):
        # No commits, no changed files → no tokens → aligned=False
        _, aligned = _select_candidate_sections(self._DOC_WITH_DOC_DRAFTER, [], [])
        self.assertFalse(aligned)

    def test_fallback_still_returns_sections(self):
        # Even with no alignment, fallback returns top-K by size
        sections, aligned = _select_candidate_sections(self._DOC_WITH_DOC_DRAFTER, [], [])
        self.assertFalse(aligned)
        self.assertGreater(len(sections), 0,
                           "Fallback must return sections even when not aligned")

    def test_empty_document_returns_empty_list(self):
        sections, aligned = _select_candidate_sections("", [], [])
        self.assertEqual(sections, [])
        self.assertFalse(aligned)

    def test_flat_document_no_headings_returns_empty(self):
        sections, aligned = _select_candidate_sections("Just plain text.", [], [])
        self.assertEqual(sections, [])
        self.assertFalse(aligned)

    def test_body_budget_drops_excess_sections(self):
        # With max_body_chars=50, only the smallest (or first-scored) section fits
        doc = """
## Tiny

Short.

## Large Section

""" + ("x " * 500)  # 1000-char body — exceeds budget alone

        commits = [{"subject": "fix tiny"}]
        sections, _ = _select_candidate_sections(doc, [], commits, max_body_chars=50)
        # At most one section can fit under the 50-char budget
        self.assertLessEqual(len(sections), 1)

    def test_single_title_hit_always_aligns(self):
        # A strong token that hits the TITLE gets score = 3*2 = 6 ≥ 2
        doc = """
## doc_drafter Prompt Builder

Some body text unrelated to other topics.
"""
        commits = [{"subject": "feat(doc-drafter): update builder"}]
        _, aligned = _select_candidate_sections(doc, [], commits)
        self.assertTrue(aligned, "Title match on strong token must align")


class TestTokenHelpers(unittest.TestCase):

    def test_path_tokens_extracts_basename(self):
        tokens = _path_tokens(["src/helpers/doc_drafter.py"])
        self.assertIn("doc_drafter", tokens)

    def test_path_tokens_generates_hyphen_variant(self):
        tokens = _path_tokens(["src/helpers/doc_drafter.py"])
        self.assertIn("doc-drafter", tokens)

    def test_path_tokens_includes_parent_dir(self):
        tokens = _path_tokens(["src/helpers/doc_drafter.py"])
        self.assertIn("helpers", tokens)

    def test_path_tokens_empty_input(self):
        self.assertEqual(_path_tokens([]), set())
        self.assertEqual(_path_tokens(None), set())

    def test_scope_prefix_extracts_conventional_scope(self):
        commits = [{"subject": "feat(doc-drafter): fix truncation filter"}]
        tokens = _scope_prefix_tokens(commits)
        self.assertIn("doc-drafter", tokens)
        self.assertIn("doc_drafter", tokens)

    def test_scope_prefix_empty_on_no_scope(self):
        commits = [{"subject": "fix: update the database connection"}]
        self.assertEqual(_scope_prefix_tokens(commits), set())

    def test_scope_prefix_empty_commits(self):
        self.assertEqual(_scope_prefix_tokens([]), set())

    def test_subject_tokens_extracts_words(self):
        commits = [{"subject": "fix: update database connection timeout"}]
        tokens = _subject_tokens(commits)
        self.assertIn("database", tokens)
        self.assertIn("timeout", tokens)

    def test_subject_tokens_excludes_short_words(self):
        # All extracted tokens must be ≥ 4 chars (re.findall(r"\b\w{4,}\b"))
        commits = [{"subject": "add bug fix"}]
        for tok in _subject_tokens(commits):
            self.assertGreaterEqual(len(tok), 4,
                                    f"Token '{tok}' is too short (< 4 chars)")

    def test_subject_tokens_exclude_scope_prefix(self):
        # Subject tokens and scope prefix tokens should be disjoint when used together
        commits = [{"subject": "feat(doc-drafter): updated prompt builder"}]
        scope  = _scope_prefix_tokens(commits)
        subj   = _subject_tokens(commits)
        # "doc-drafter" and "doc_drafter" should be in scope, not in weak subject set
        self.assertNotIn("doc-drafter", subj - scope)


class TestIsNoopBullet(unittest.TestCase):

    def test_none_literal(self):     self.assertTrue(_is_noop_bullet("- None"))
    def test_na_slash(self):         self.assertTrue(_is_noop_bullet("- N/A"))
    def test_na_dot(self):           self.assertTrue(_is_noop_bullet("- N.A."))
    def test_tbd(self):              self.assertTrue(_is_noop_bullet("- TBD"))
    def test_nothing(self):          self.assertTrue(_is_noop_bullet("- nothing"))
    def test_no_changes(self):       self.assertTrue(_is_noop_bullet("- no changes"))
    def test_placeholder(self):      self.assertTrue(_is_noop_bullet("- placeholder"))

    def test_substantive_not_noop(self):
        self.assertFalse(_is_noop_bullet("- added new authentication feature"))

    def test_empty_not_noop(self):
        self.assertFalse(_is_noop_bullet(""))


class TestIsDuplicate(unittest.TestCase):

    def test_identical_bullets_are_duplicates(self):
        b = "- added new authentication feature to the login flow"
        self.assertTrue(_is_duplicate(b, b))

    def test_paraphrased_bullets_are_duplicates(self):
        # Share enough high-value tokens to exceed the Jaccard (0.60) or
        # overlap (0.65) threshold.  Using four shared tokens out of five total.
        a = "- added tokensave grounding integration codegraph support"
        b = "- wired tokensave grounding integration codegraph support"
        self.assertTrue(_is_duplicate(a, b))

    def test_unrelated_bullets_not_duplicates(self):
        a = "- fixed database connection timeout handling"
        b = "- updated the UI colour scheme and button padding"
        self.assertFalse(_is_duplicate(a, b))

    def test_empty_vs_non_empty_not_duplicate(self):
        self.assertFalse(_is_duplicate("", "- something substantive"))
        self.assertFalse(_is_duplicate("- something substantive", ""))

    def test_both_empty_not_duplicate(self):
        self.assertFalse(_is_duplicate("", ""))


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("Token Save Manager — Logic-layer smoke tests (Roadmap-7)")
    print("=" * 70)
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print()
    total = result.testsRun
    fails = len(result.failures) + len(result.errors)
    status = "ALL PASSED" if not fails else "FAILURES DETECTED"
    print(f"{status}: {total - fails}/{total} passed")
    sys.exit(0 if result.wasSuccessful() else 1)

