"""tests/test_doc_drafter_prompts.py — helpers/doc_drafter_prompts.py (pure helpers + builders)."""
from __future__ import annotations

import pytest

from helpers.doc_drafter_prompts import (
    PromptBuildResult,
    _strip_end_marker,
    _extract_subsection_headers,
    _render_commit_list,
    _extract_scope_prefixes,
    _summarise_existing_headings,
    _path_tokens,
    _scope_prefix_tokens,
    _subject_tokens,
    _select_candidate_sections,
    build_changelog_prompt,
    build_readme_prompt,
)


# ── PromptBuildResult ─────────────────────────────────────────────────────────

class TestPromptBuildResult:
    def test_basic_construction(self):
        r = PromptBuildResult(system="sys", user="usr")
        assert r.system == "sys"
        assert r.user == "usr"

    def test_defaults(self):
        r = PromptBuildResult(system="s", user="u")
        assert r.aligned is True
        assert r.warnings == ()

    def test_frozen(self):
        r = PromptBuildResult(system="s", user="u")
        with pytest.raises((AttributeError, TypeError)):
            r.system = "changed"  # type: ignore[misc]

    def test_aligned_false(self):
        r = PromptBuildResult(system="s", user="u", aligned=False)
        assert r.aligned is False

    def test_warnings_tuple(self):
        r = PromptBuildResult(system="s", user="u", warnings=("warn1",))
        assert r.warnings == ("warn1",)


# ── _strip_end_marker ─────────────────────────────────────────────────────────

class TestStripEndMarker:
    def test_strips_marker(self):
        text = "content here\n<<<END_OF_DRAFT>>>"
        assert _strip_end_marker(text) == "content here"

    def test_no_marker_unchanged(self):
        text = "no marker present"
        assert _strip_end_marker(text) == text

    def test_empty_unchanged(self):
        assert _strip_end_marker("") == ""

    def test_strips_everything_after(self):
        text = "keep\n<<<END_OF_DRAFT>>>\ndiscard"
        result = _strip_end_marker(text)
        assert "keep" in result
        assert "discard" not in result

    def test_case_insensitive(self):
        text = "content\n<<<end_of_draft>>>"
        result = _strip_end_marker(text)
        assert "<<<" not in result

    def test_rstrips_trailing_whitespace(self):
        text = "content  \n<<<END_OF_DRAFT>>>"
        result = _strip_end_marker(text)
        assert result == "content"

    def test_drops_unclosed_fence_at_truncation(self):
        text = "before\n```python\ncode\n<<<END_OF_DRAFT>>>"
        result = _strip_end_marker(text)
        assert "```" not in result


# ── _extract_subsection_headers ───────────────────────────────────────────────

class TestExtractSubsectionHeaders:
    def test_extracts_bold_header(self):
        body = "**My Feature**\n- bullet"
        headers = _extract_subsection_headers(body)
        assert headers == ["**My Feature**"]

    def test_multiple_headers(self):
        body = "**First**\n- a\n**Second**\n- b"
        headers = _extract_subsection_headers(body)
        assert len(headers) == 2

    def test_empty_body_returns_empty(self):
        assert _extract_subsection_headers("") == []

    def test_no_headers_returns_empty(self):
        assert _extract_subsection_headers("- orphan bullet") == []

    def test_header_with_colon(self):
        body = "**Feature:**\n- bullet"
        headers = _extract_subsection_headers(body)
        assert len(headers) == 1


# ── _render_commit_list ───────────────────────────────────────────────────────

class TestRenderCommitList:
    def test_renders_single_section(self):
        classified = {"Added": ["feature A", "feature B"]}
        result = _render_commit_list(classified)
        assert "### Added" in result
        assert "- feature A" in result

    def test_skips_empty_sections(self):
        classified = {"Added": ["feat"], "Fixed": []}
        result = _render_commit_list(classified)
        assert "### Fixed" not in result

    def test_empty_returns_empty(self):
        result = _render_commit_list({})
        assert result == ""

    def test_multiple_sections(self):
        classified = {"Added": ["A"], "Fixed": ["B"]}
        result = _render_commit_list(classified)
        assert "### Added" in result
        assert "### Fixed" in result


# ── _extract_scope_prefixes ───────────────────────────────────────────────────

class TestExtractScopePrefixes:
    def test_extracts_scope(self):
        bullets = ["- (commit-dialog) added feature"]
        scopes = _extract_scope_prefixes(bullets)
        assert "commit-dialog" in scopes

    def test_deduplicates(self):
        bullets = ["- (dialog) A", "- (dialog) B"]
        scopes = _extract_scope_prefixes(bullets)
        assert scopes.count("dialog") == 1

    def test_empty_list(self):
        assert _extract_scope_prefixes([]) == []

    def test_respects_limit(self):
        bullets = [f"- ({i}_scope) bullet" for i in range(20)]
        scopes = _extract_scope_prefixes(bullets, limit=5)
        assert len(scopes) <= 5


# ── _summarise_existing_headings ──────────────────────────────────────────────

class TestSummariseExistingHeadings:
    def test_extracts_h2(self):
        text = "# Title\n## Overview\n## Architecture"
        result = _summarise_existing_headings(text)
        assert "Overview" in result
        assert "Architecture" in result

    def test_skips_h1(self):
        text = "# Title\n## Section"
        result = _summarise_existing_headings(text)
        assert "Title" not in result

    def test_empty_returns_placeholder(self):
        result = _summarise_existing_headings("")
        assert "no existing sections" in result

    def test_respects_max_levels(self):
        text = "## H2\n### H3\n#### H4"
        result = _summarise_existing_headings(text, max_levels=3)
        assert "H2" in result
        assert "H3" in result
        assert "H4" not in result


# ── _path_tokens ──────────────────────────────────────────────────────────────

class TestPathTokens:
    def test_extracts_basename(self):
        tokens = _path_tokens(["src/helpers/commit_messages.py"])
        assert "commit_messages" in tokens

    def test_extracts_parent(self):
        tokens = _path_tokens(["src/helpers/commit_messages.py"])
        assert "helpers" in tokens

    def test_strips_extension(self):
        tokens = _path_tokens(["foo.py"])
        assert "foo" in tokens
        assert "foo.py" not in tokens

    def test_empty_list(self):
        assert _path_tokens([]) == set()

    def test_hyphen_variant_generated(self):
        tokens = _path_tokens(["src/commit_messages.py"])
        assert "commit-messages" in tokens


# ── _scope_prefix_tokens ─────────────────────────────────────────────────────

class TestScopePrefixTokens:
    def test_extracts_scope_from_conventional_commit(self):
        commits = [{"subject": "feat(commit-dialog): add feature"}]
        tokens = _scope_prefix_tokens(commits)
        assert "commit-dialog" in tokens

    def test_generates_underscore_variant(self):
        commits = [{"subject": "fix(commit-dialog): fix bug"}]
        tokens = _scope_prefix_tokens(commits)
        assert "commit_dialog" in tokens

    def test_empty_returns_empty(self):
        assert _scope_prefix_tokens([]) == set()

    def test_handles_string_commits(self):
        commits = ["feat(settings): add toggle"]
        tokens = _scope_prefix_tokens(commits)
        assert "settings" in tokens


# ── _subject_tokens ───────────────────────────────────────────────────────────

class TestSubjectTokens:
    def test_extracts_significant_words(self):
        commits = [{"subject": "improved connection pooling"}]
        tokens = _subject_tokens(commits)
        assert "improved" in tokens
        assert "connection" in tokens
        assert "pooling" in tokens

    def test_filters_stopwords(self):
        commits = [{"subject": "feat: add the feature"}]
        tokens = _subject_tokens(commits)
        assert "feat" not in tokens
        assert "the" not in tokens

    def test_filters_short_words(self):
        commits = [{"subject": "fix bug"}]
        tokens = _subject_tokens(commits)
        assert "fix" not in tokens  # in stopwords
        assert "bug" not in tokens  # <4 chars


# ── _select_candidate_sections ────────────────────────────────────────────────

class TestSelectCandidateSections:
    def test_empty_text_returns_empty(self):
        candidates, aligned = _select_candidate_sections("", [], [])
        assert candidates == []
        assert not aligned

    def test_no_signals_returns_top_k_by_size(self):
        text = "## Section A\n" + "word " * 50 + "\n## Section B\nshort"
        candidates, aligned = _select_candidate_sections(text, [], [])
        # Falls back to top-K by body size; aligned=False
        assert not aligned
        assert len(candidates) <= 5

    def test_path_token_hits_title(self):
        text = "## commit_messages\nsome body about commit messages"
        candidates, aligned = _select_candidate_sections(
            text,
            changed_files=["src/helpers/commit_messages.py"],
            commits=[],
        )
        assert len(candidates) >= 1
        assert aligned  # title hit ≥ threshold


# ── build_changelog_prompt ────────────────────────────────────────────────────

class TestBuildChangelogPrompt:
    def test_returns_prompt_build_result(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: False)
        monkeypatch.setattr("helpers.changelog_patch.read_section_bullets_from_text",
                            lambda *a, **k: [])
        result = build_changelog_prompt(
            commits=[{"hash": "abc123", "subject": "feat: add feature"}],
            classified={"Added": ["add feature"]},
            existing_unreleased="",
            project_name="MyProject",
            project_desc="test project",
            changed_files=["src/foo.py"],
            boundary_note="",
        )
        assert isinstance(result, PromptBuildResult)
        assert "MyProject" in result.user
        assert result.system  # non-empty
        assert result.aligned  # defaults to True

    def test_includes_project_name(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: False)
        monkeypatch.setattr("helpers.changelog_patch.read_section_bullets_from_text",
                            lambda *a, **k: [])
        result = build_changelog_prompt(
            commits=[], classified={},
            existing_unreleased="", project_name="SpecialProject",
            project_desc="", changed_files=[], boundary_note="",
        )
        assert "SpecialProject" in result.user

    def test_sparse_commits_add_note(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: True)
        monkeypatch.setattr("helpers.changelog_patch.read_section_bullets_from_text",
                            lambda *a, **k: [])
        result = build_changelog_prompt(
            commits=[{"hash": "a", "subject": "x"}], classified={},
            existing_unreleased="", project_name="P",
            project_desc="", changed_files=["foo.py"], boundary_note="",
        )
        assert "concise" in result.user.lower() or "sparse" in result.user.lower()


# ── build_readme_prompt ───────────────────────────────────────────────────────

class TestBuildReadmePrompt:
    def test_returns_prompt_build_result(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: False)
        result = build_readme_prompt(
            commits=[{"hash": "abc", "subject": "feat: thing"}],
            classified={"Added": ["thing"]},
            existing_highlights="",
            project_name="Proj",
            project_desc="",
            changed_files=["src/bar.py"],
            boundary_note="",
        )
        assert isinstance(result, PromptBuildResult)
        assert "Proj" in result.user

    def test_includes_existing_highlights(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: False)
        highlights = "**Roadmap-8**\n- existing bullet"
        result = build_readme_prompt(
            commits=[], classified={},
            existing_highlights=highlights,
            project_name="P", project_desc="",
            changed_files=[], boundary_note="",
        )
        assert "existing bullet" in result.user

    def test_includes_candidate_headers(self, monkeypatch):
        monkeypatch.setattr("helpers.doc_drafter_prompts.is_sparse", lambda _: False)
        highlights = "**Roadmap-8 — Shadow Links**\n- feature"
        result = build_readme_prompt(
            commits=[], classified={},
            existing_highlights=highlights,
            project_name="P", project_desc="",
            changed_files=[], boundary_note="",
        )
        assert "Roadmap-8" in result.user
