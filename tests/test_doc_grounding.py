"""tests/test_doc_grounding.py — combining tokensave and codegraph context.

Split out of the former ``tests/smoke_test.py``. Covers the dedup-first
ordering and the per-source cap, which together decide what evidence actually
reaches the model when both graph tools have something to say.
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

from helpers.doc_grounding import build_combined_grounding  # noqa: E402


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

