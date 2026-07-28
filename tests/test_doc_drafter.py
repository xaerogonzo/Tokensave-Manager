"""tests/test_doc_drafter.py — helpers/doc_drafter.py (re-export facade).

The facade re-exports every public name from the five family modules.
These tests verify that the re-exports exist and are the same objects
as the originals — the implementation correctness is tested in
the individual module test files (test_doc_drafter_filters.py, etc.).
"""
from __future__ import annotations

import helpers.doc_drafter as dd
import helpers.doc_drafter_filters as ddf
import helpers.doc_drafter_prompts as ddp
import helpers.doc_drafter_git as ddg


# ── Re-export identity checks ─────────────────────────────────────────────────

def test_is_sparse_reexported():
    assert dd.is_sparse is ddg.is_sparse


def test_prompt_build_result_reexported():
    assert dd.PromptBuildResult is ddp.PromptBuildResult


def test_strip_end_marker_reexported():
    assert dd._strip_end_marker is ddp._strip_end_marker


def test_is_noop_bullet_reexported():
    assert dd._is_noop_bullet is ddf._is_noop_bullet


def test_looks_truncated_reexported():
    assert dd._looks_truncated is ddf._looks_truncated


def test_is_duplicate_reexported():
    assert dd._is_duplicate is ddf._is_duplicate


def test_filter_bullets_reexported():
    assert dd._filter_bullets is ddf._filter_bullets


def test_parse_grouped_bullets_reexported():
    assert dd.parse_grouped_bullets is ddf.parse_grouped_bullets


def test_split_readme_subsection_reexported():
    assert dd.split_readme_subsection is ddf.split_readme_subsection


def test_build_changelog_prompt_reexported():
    assert dd.build_changelog_prompt is ddp.build_changelog_prompt


def test_build_readme_prompt_reexported():
    assert dd.build_readme_prompt is ddp.build_readme_prompt


def test_resolve_commit_range_reexported():
    assert dd.resolve_commit_range is ddg.resolve_commit_range


def test_changed_file_paths_reexported():
    assert dd.changed_file_paths is ddg.changed_file_paths


def test_read_blueprint_context_reexported():
    assert dd.read_blueprint_context is ddg.read_blueprint_context


def test_changelog_filter_draft_reexported():
    assert dd.changelog_filter_draft is ddf.changelog_filter_draft


def test_readme_filter_draft_reexported():
    assert dd.readme_filter_draft is ddf.readme_filter_draft


def test_architecture_parse_draft_reexported():
    assert dd.architecture_parse_draft is ddf.architecture_parse_draft


def test_roadmap_parse_draft_reexported():
    assert dd.roadmap_parse_draft is ddf.roadmap_parse_draft


def test_memory_parse_draft_reexported():
    assert dd.memory_parse_draft is ddf.memory_parse_draft


def test_generic_parse_draft_reexported():
    assert dd.generic_parse_draft is ddf.generic_parse_draft


def test_architecture_filter_draft_reexported():
    assert dd.architecture_filter_draft is ddf.architecture_filter_draft


def test_roadmap_filter_draft_reexported():
    assert dd.roadmap_filter_draft is ddf.roadmap_filter_draft


def test_memory_filter_draft_reexported():
    assert dd.memory_filter_draft is ddf.memory_filter_draft


def test_generic_filter_draft_reexported():
    assert dd.generic_filter_draft is ddf.generic_filter_draft


# ── Smoke: facade functions are callable ──────────────────────────────────────

def test_is_sparse_callable_via_facade():
    assert dd.is_sparse([]) is False


def test_strip_end_marker_callable_via_facade():
    assert dd._strip_end_marker("before\n<<<END_OF_DRAFT>>>") == "before"


def test_prompt_build_result_constructible_via_facade():
    r = dd.PromptBuildResult(system="s", user="u")
    assert r.system == "s"
    assert r.user == "u"


def test_is_noop_bullet_callable_via_facade():
    assert dd._is_noop_bullet("none") is True
    assert dd._is_noop_bullet("- real content.") is False
