#!/usr/bin/env python3
"""tests/smoke_test.py — Logic-layer smoke tests for Token Save Manager (Roadmap-7).

Covers all logic-testable helpers from the v4.8 testing checklist.
No Tk window is opened; no real network calls are made (urllib is mocked
where needed); no real files are written outside tempdir.

Sections:
  1.  _looks_truncated                   (doc-drafter quality filter)
  2.  _STRUCTURAL_MARKUP_RE              (G1 — prose vs. technical chars)
  3.  _merge_wrapped_bullets             (Revision B — Ollama line-wrap fix)
  4.  build_combined_grounding           (G4 — dedup-first, combined cap)
  5.  _select_candidate_sections         (alignment scoring + G3 sat. cap)
  6.  _is_safe_zip_member                (G-C — Zip-Slip guard)
  7.  extract_tokensave_zip              (G-C — full extraction logic)
  8.  is_manager_installed               (G-F — uninstall path gating)
  9.  latest_tokensave_release           (G-E — 403 rate-limit detection)
  10. _claude_code_mcp_has_codegraph     (v4.7 — MCP status detection)
  11. _files_from_diff                   (v4.2 — changed-files from diff)
  12. _path_tokens / _scope_prefix / _subject_tokens (alignment tokenisers)
  13. _is_noop_bullet                    (noop-placeholder filter)
  14. _is_duplicate                      (Jaccard dedup)

Run:
    python tests/smoke_test.py
    python -m pytest tests/smoke_test.py -v   # if pytest is installed
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

# ── Imports under test ────────────────────────────────────────────────────────
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
from helpers.doc_grounding import build_combined_grounding  # noqa: E402
from helpers.install_tokensave import (                     # noqa: E402
    _is_safe_zip_member,
    extract_tokensave_zip,
    is_manager_installed,
    latest_tokensave_release,
)
from helpers.mcp import _claude_code_mcp_has_codegraph      # noqa: E402
from helpers.commit_messages import _files_from_diff        # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _looks_truncated — doc-drafter quality filter
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _STRUCTURAL_MARKUP_RE — G1 prose vs. technical characters
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _merge_wrapped_bullets — Revision B (Ollama line-wrap fix)
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 4. build_combined_grounding — G4 dedup-first, combined cap
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildCombinedGrounding(unittest.TestCase):

    def test_both_empty_returns_empty(self):
        self.assertEqual(build_combined_grounding("", ""), "")

    def test_none_inputs_treated_as_empty(self):
        self.assertEqual(build_combined_grounding(None, None), "")

    def test_only_tokensave_block_passthrough(self):
        block = "## Tokensave Context\nfoo\nbar"
        result = build_combined_grounding(block, "")
        self.assertIn("foo", result)
        self.assertIn("bar", result)

    def test_only_codegraph_block_passthrough(self):
        block = "## CodeGraph Context\nbaz\nqux"
        result = build_combined_grounding("", block)
        self.assertIn("baz", result)
        self.assertIn("qux", result)

    def test_dedup_removes_identical_lines(self):
        shared_line = "identical line present in both blocks"
        ts = f"## TS\n{shared_line}\nunique to tokensave"
        cg = f"## CG\n{shared_line}\nunique to codegraph"
        result = build_combined_grounding(ts, cg)
        self.assertEqual(result.count(shared_line), 1,
                         "Shared line must appear exactly once (dedup-first)")

    def test_both_unique_lines_preserved(self):
        ts = "## TS\nunique_ts_content"
        cg = "## CG\nunique_cg_content"
        result = build_combined_grounding(ts, cg)
        self.assertIn("unique_ts_content", result)
        self.assertIn("unique_cg_content", result)

    def test_first_seen_wins(self):
        # tokensave block comes first — its ordering is preserved
        ts = "line A\nline B"
        cg = "line B\nline C"   # line B is a dup
        result = build_combined_grounding(ts, cg)
        self.assertLess(result.index("line A"), result.index("line C"),
                        "tokensave order (first-seen) must be preserved")

    def test_combined_cap_applied(self):
        # per_source_cap=100 → combined cap = 200 chars; 300-char input must be truncated
        big_ts = "x " * 200   # ~400 chars
        result = build_combined_grounding(big_ts, "", per_source_cap=100)
        self.assertLessEqual(len(result), 300,
                             "Result must be capped at ~2 × per_source_cap")

    def test_identical_large_blocks_deduplicated(self):
        # Two identical blocks → result has each unique line exactly once.
        # Use single-digit suffixes only (0-9) to avoid substring ambiguity:
        # "line 1" would be a substring of "line 10", "line 11", etc.
        block = "\n".join(f"unique_content_line_{i}" for i in range(10))
        result = build_combined_grounding(block, block)
        for i in range(10):
            self.assertEqual(result.count(f"unique_content_line_{i}"), 1,
                             f"unique_content_line_{i} must appear exactly once")

    def test_blank_lines_preserved_for_readability(self):
        ts = "section A\n\nsection B"
        result = build_combined_grounding(ts, "")
        self.assertIn("\n\n", result, "Blank lines must be preserved")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _select_candidate_sections — alignment scoring + G3 saturating cap
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _is_safe_zip_member — G-C Zip-Slip guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsSafeZipMember(unittest.TestCase):

    _EXTRACT = os.path.abspath("/tmp/extract_dir_smoke_test")

    def _safe(self, member):
        return _is_safe_zip_member(member, self._EXTRACT)

    # ── safe members ──────────────────────────────────────────────────────────
    def test_normal_subpath(self):        self.assertTrue(self._safe("src/helpers/foo.py"))
    def test_flat_filename(self):         self.assertTrue(self._safe("tokensave.exe"))
    def test_nested_subdir(self):         self.assertTrue(self._safe("bin/tokensave.exe"))
    def test_deeper_nesting(self):        self.assertTrue(self._safe("a/b/c/d.py"))

    # ── rejected members ──────────────────────────────────────────────────────
    def test_parent_traversal(self):
        self.assertFalse(self._safe("../etc/passwd"))

    def test_deep_parent_traversal(self):
        self.assertFalse(self._safe("a/../../etc/shadow"))

    def test_absolute_unix_path(self):
        self.assertFalse(self._safe("/absolute/path/file.txt"))

    def test_windows_drive_letter(self):
        # After replace("\\","/"), "C:\Windows" → "C:/Windows" → name[1] == ":"
        self.assertFalse(self._safe("C:\\Windows\\System32\\bad.dll"))

    def test_double_dot_only(self):
        self.assertFalse(self._safe(".."))

    def test_traversal_within_path(self):
        self.assertFalse(self._safe("safe/../../../escape"))


# ═══════════════════════════════════════════════════════════════════════════════
# 7. extract_tokensave_zip — G-C full extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTokensaveZip(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="tsm_smoke_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_zip(self, members: dict) -> str:
        """Write a zip with ``{member_name: content_bytes}`` to a temp file."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        zip_path = os.path.join(self._tmpdir, "test.zip")
        with open(zip_path, "wb") as fh:
            fh.write(buf.getvalue())
        return zip_path

    def _write_malicious_zip(self, safe_name, evil_name) -> str:
        """Write a zip that contains both a safe member and a traversal member."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(safe_name, b"safe content")
            zf.writestr(evil_name, b"escape attempt")
        zip_path = os.path.join(self._tmpdir, "malicious.zip")
        with open(zip_path, "wb") as fh:
            fh.write(buf.getvalue())
        return zip_path

    def test_valid_flat_zip_extracts_exe(self):
        zip_path = self._write_zip({"tokensave.exe": b"fake binary content"})
        extract_dir = os.path.join(self._tmpdir, "out1")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "")
        self.assertTrue(exe_path.endswith("tokensave.exe"))
        self.assertTrue(os.path.isfile(exe_path))

    def test_valid_nested_zip_extracts_exe(self):
        zip_path = self._write_zip({
            "bin/tokensave.exe": b"binary",
            "bin/README.md": b"readme",
        })
        extract_dir = os.path.join(self._tmpdir, "out2")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "")
        self.assertTrue(exe_path.endswith("tokensave.exe"))

    def test_no_exe_in_archive_returns_error(self):
        zip_path = self._write_zip({"README.txt": b"docs", "LICENCE": b"MIT"})
        extract_dir = os.path.join(self._tmpdir, "out3")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(exe_path, "")
        self.assertEqual(err, "no_exe_in_archive")

    def test_zip_slip_traversal_aborts_cleanly(self):
        """Traversal member → zip_slip; safe member must NOT be extracted."""
        zip_path = self._write_malicious_zip("safe_file.txt", "../escape.txt")
        extract_dir = os.path.join(self._tmpdir, "out4")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(exe_path, "")
        self.assertEqual(err, "zip_slip")
        # First-pass validation aborts before any write
        self.assertFalse(
            os.path.isfile(os.path.join(extract_dir, "safe_file.txt")),
            "No file must be extracted when zip_slip is detected",
        )

    def test_absolute_path_member_aborts(self):
        zip_path = self._write_malicious_zip("ok.txt", "/absolute/path/evil.txt")
        extract_dir = os.path.join(self._tmpdir, "out5")
        _, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "zip_slip")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. is_manager_installed — G-F uninstall gating
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsManagerInstalled(unittest.TestCase):

    def _make_exe(self, directory, name="tokensave.exe"):
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write("fake binary")
        return path

    def test_exe_inside_manager_dir_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self._make_exe(d)
            with mock.patch("helpers.install_tokensave.manager_install_dir",
                            return_value=d):
                self.assertTrue(is_manager_installed(exe))

    def test_exe_outside_manager_dir_returns_false(self):
        with tempfile.TemporaryDirectory() as managed:
            with tempfile.TemporaryDirectory() as other:
                exe = self._make_exe(other)
                with mock.patch("helpers.install_tokensave.manager_install_dir",
                                return_value=managed):
                    self.assertFalse(is_manager_installed(exe))

    def test_lookalike_prefix_path_rejected(self):
        """C:\\TokenSaveManager-Lookalike\\exe must NOT pass via string prefix match.

        os.path.commonpath() correctly handles this — the common path would be
        the parent of both dirs, not the manager dir itself.
        """
        with tempfile.TemporaryDirectory() as d:
            sibling = d + "-Lookalike"
            os.makedirs(sibling, exist_ok=True)
            try:
                exe = self._make_exe(sibling)
                with mock.patch("helpers.install_tokensave.manager_install_dir",
                                return_value=d):
                    self.assertFalse(is_manager_installed(exe))
            finally:
                shutil.rmtree(sibling, ignore_errors=True)

    def test_empty_path_returns_false(self):
        self.assertFalse(is_manager_installed(""))

    def test_nonexistent_exe_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            ghost = os.path.join(d, "does_not_exist.exe")
            with mock.patch("helpers.install_tokensave.manager_install_dir",
                            return_value=d):
                self.assertFalse(is_manager_installed(ghost))


# ═══════════════════════════════════════════════════════════════════════════════
# 9. latest_tokensave_release — G-E rate-limit detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestLatestTokensaveRelease(unittest.TestCase):

    @staticmethod
    def _mock_urlopen(status=200, body=b""):
        """Return a context-manager-compatible mock for urllib.request.urlopen.

        MagicMock.__enter__() returns a NEW sub-mock by default, so resp.status
        would read the sub-mock's attribute, not ours. Fix: set
        __enter__.return_value = resp so `with urlopen(...) as r: r.status`
        reads the attributes we configured.
        """
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp   # critical: return self, not sub-mock
        return mock.patch("urllib.request.urlopen", return_value=resp)

    @staticmethod
    def _mock_urlopen_exc(exc):
        return mock.patch("urllib.request.urlopen", side_effect=exc)

    def test_rate_limit_403_returns_rate_limit_tag(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=403, msg="Forbidden",
                                     hdrs=None, fp=None)
        with self._mock_urlopen_exc(exc):
            version, url, err = latest_tokensave_release()
        self.assertIsNone(version)
        self.assertIsNone(url)
        self.assertEqual(err, "rate_limit",
                         "HTTP 403 must return the 'rate_limit' tag so caller "
                         "can show the actionable 'wait or install manually' message")

    def test_successful_response_parses_windows_asset(self):
        payload = json.dumps({
            "tag_name": "v6.1.0",
            "assets": [
                {"name": "tokensave-v6.1.0-x86_64-linux.tar.gz",
                 "browser_download_url": "https://example.com/linux.tar.gz"},
                {"name": "tokensave-v6.1.0-x86_64-windows.zip",
                 "browser_download_url": "https://example.com/windows.zip"},
            ],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            version, url, err = latest_tokensave_release()
        self.assertEqual(version, "6.1.0")
        self.assertIn("windows.zip", url)
        self.assertIsNone(err)

    def test_v_prefix_stripped_from_tag(self):
        payload = json.dumps({
            "tag_name": "v6.0.3",
            "assets": [{"name": "tokensave-v6.0.3-x86_64-windows.zip",
                        "browser_download_url": "https://example.com/w.zip"}],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            version, _, _ = latest_tokensave_release()
        self.assertEqual(version, "6.0.3", "'v' prefix must be stripped from tag_name")

    def test_no_windows_asset_returns_error(self):
        payload = json.dumps({
            "tag_name": "v6.1.0",
            "assets": [{"name": "tokensave-v6.1.0-x86_64-linux.tar.gz",
                        "browser_download_url": "https://example.com/l.tar.gz"}],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            _, _, err = latest_tokensave_release()
        self.assertEqual(err, "no_windows_asset")

    def test_other_http_error_returns_http_tag(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=500, msg="Server Error",
                                     hdrs=None, fp=None)
        with self._mock_urlopen_exc(exc):
            _, _, err = latest_tokensave_release()
        self.assertEqual(err, "http_500")

    def test_malformed_json_returns_json_err(self):
        with self._mock_urlopen(status=200, body=b"{not valid json"):
            _, _, err = latest_tokensave_release()
        self.assertTrue(err.startswith("json_err"),
                        f"Expected 'json_err…' but got: {err!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. _claude_code_mcp_has_codegraph — v4.7 MCP status detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestClaudeCodeMcpHasCodegraph(unittest.TestCase):

    def _write_cfg(self, data: dict, tmpdir: str) -> str:
        path = os.path.join(tmpdir, ".claude.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_canonical_entry_detected_by_key(self):
        cfg = {"mcpServers": {"codegraph": {
            "type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, key = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)
        self.assertEqual(key, "codegraph")

    def test_namespaced_key_detected(self):
        cfg = {"mcpServers": {"mcp-codegraph": {
            "type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)

    def test_detected_by_command_field(self):
        cfg = {"mcpServers": {"my-custom-key": {
            "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)

    def test_no_codegraph_entry_returns_false(self):
        cfg = {"mcpServers": {"tokensave": {
            "type": "stdio", "command": "tokensave", "args": ["server"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertFalse(wired)

    def test_empty_mcp_servers_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(
                self._write_cfg({"mcpServers": {}}, d))
        self.assertFalse(wired)

    def test_missing_file_returns_false(self):
        wired, _ = _claude_code_mcp_has_codegraph("/nonexistent/dir/.claude.json")
        self.assertFalse(wired)

    def test_malformed_json_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            wired, _ = _claude_code_mcp_has_codegraph(path)
        self.assertFalse(wired)

    def test_codegraph_in_args_detected(self):
        # Variant: key is generic but args include "codegraph"
        cfg = {"mcpServers": {"generic-mcp": {
            "command": "node", "args": ["run", "codegraph", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. _files_from_diff — v4.2 changed-files extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestFilesFromDiff(unittest.TestCase):

    def test_parses_multiple_files(self):
        diff = (
            "diff --git a/src/helpers/foo.py b/src/helpers/foo.py\n"
            "+++ b/src/helpers/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " pass\n"
            "diff --git a/src/dialogs/bar.py b/src/dialogs/bar.py\n"
            "+++ b/src/dialogs/bar.py\n"
            "@@ -1,1 +1,2 @@\n"
            " pass\n"
        )
        files = _files_from_diff(diff)
        self.assertIn("src/helpers/foo.py", files)
        self.assertIn("src/dialogs/bar.py", files)

    def test_excludes_dev_null(self):
        diff = "+++ /dev/null\n"
        files = _files_from_diff(diff)
        self.assertNotIn("/dev/null", files)
        self.assertEqual(files, [])

    def test_empty_diff_returns_empty(self):
        self.assertEqual(_files_from_diff(""), [])

    def test_none_diff_returns_empty(self):
        self.assertEqual(_files_from_diff(None), [])

    def test_single_file_diff(self):
        diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n pass\n"
        files = _files_from_diff(diff)
        self.assertIn("src/app.py", files)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Alignment tokenisers — _path_tokens, _scope_prefix_tokens, _subject_tokens
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 13. _is_noop_bullet — placeholder filter
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 14. _is_duplicate — Jaccard dedup
# ═══════════════════════════════════════════════════════════════════════════════

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
