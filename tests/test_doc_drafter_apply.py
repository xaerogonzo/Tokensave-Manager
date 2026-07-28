"""tests/test_doc_drafter_apply.py — helpers/doc_drafter_apply.py."""
from __future__ import annotations

import pytest

from helpers.doc_drafter_apply import (
    _apply_sections,
    changelog_io_apply,
    memory_compute_apply,
)


# ── _apply_sections (pure helper) ─────────────────────────────────────────────

class TestApplySections:
    def test_empty_sections_returns_false(self):
        _, ok, msg, stats = _apply_sections("text", [], lambda t, *a: ("", True, "ok"), set())
        assert not ok
        assert stats["applied"] == []

    def test_unknown_title_skipped_when_allow_new_false(self):
        sections = [("Unknown Section", "body")]

        def compute_fn(text, title, body):
            return text + body, True, "ok"

        _, ok, msg, stats = _apply_sections(
            "original", sections, compute_fn, known_titles=set(),
            allow_new=False, title_for=lambda args: args[0],
        )
        assert not ok
        assert len(stats["skipped"]) == 1
        assert "hallucinated" in stats["skipped"][0][1]

    def test_unknown_title_accepted_when_allow_new_true(self):
        sections = [("New Section", "body")]

        def compute_fn(text, title, body):
            return text + body, True, "ok"

        _, ok, msg, stats = _apply_sections(
            "original", sections, compute_fn, known_titles=set(),
            allow_new=True, title_for=lambda args: args[0],
        )
        assert ok
        assert stats["applied"] == ["New Section"]

    def test_known_section_applied(self):
        sections = [("Overview", "new content")]
        applied_texts = []

        def compute_fn(text, title, body):
            applied_texts.append((title, body))
            return text + "\n" + body, True, "ok"

        result_text, ok, msg, stats = _apply_sections(
            "start", sections, compute_fn, known_titles={"overview"},
            allow_new=False, title_for=lambda args: args[0],
        )
        assert ok
        assert "new content" in result_text
        assert stats["applied"] == ["Overview"]

    def test_compute_fn_failure_causes_skip(self):
        sections = [("Section A", "body")]

        def compute_fn(text, title, body):
            return text, False, "parse error"

        _, ok, msg, stats = _apply_sections(
            "original", sections, compute_fn, known_titles={"section a"},
            allow_new=False, title_for=lambda args: args[0],
        )
        assert not ok
        assert len(stats["skipped"]) == 1

    def test_partial_apply_continues(self):
        sections = [("Good", "body1"), ("Bad", "body2")]
        calls = []

        def compute_fn(text, title, body):
            calls.append(title)
            if title == "Bad":
                return text, False, "rejected"
            return text + body, True, "ok"

        result_text, ok, msg, stats = _apply_sections(
            "start", sections, compute_fn,
            known_titles={"good", "bad"},
            allow_new=False, title_for=lambda args: args[0],
        )
        assert ok  # at least one applied
        assert "Good" in stats["applied"]
        assert any(t == "Bad" for t, _ in stats["skipped"])

    def test_custom_title_for(self):
        sections = [(9, "Shadow Links", "content")]

        def compute_fn(text, n, theme, body):
            return text + body, True, "ok"

        _, ok, msg, stats = _apply_sections(
            "base", sections, compute_fn,
            known_titles={"roadmap 9 — shadow links"},
            allow_new=False,
            title_for=lambda args: f"Roadmap {args[0]} — {args[1]}",
        )
        assert ok


# ── changelog_io_apply ────────────────────────────────────────────────────────

class TestChangelogIoApply:
    def test_missing_section_headers_fails(self, tmp_path, monkeypatch):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [Unreleased]\n")
        # Draft with no ### headers
        ok, msg, stats = changelog_io_apply(str(path), "- orphan bullet with no header")
        assert not ok
        assert "###" in msg or "Section" in msg

    def test_all_bullets_filtered_returns_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [Unreleased]\n### Added\n")

        # Draft with only noop bullets
        draft = "### Added\n- none\n- n/a\n"
        monkeypatch.setattr("helpers.changelog_patch.read_section_bullets",
                            lambda *a, **k: [])
        monkeypatch.setattr("helpers.changelog_patch.insert_unreleased_bullets",
                            lambda *a, **k: (True, "ok"))
        ok, msg, stats = changelog_io_apply(str(path), draft)
        assert not ok
        assert "filtered" in msg.lower()

    def test_valid_draft_applied(self, tmp_path, monkeypatch):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [Unreleased]\n### Added\n")

        draft = "### Added\n- added dark mode support.\n"
        monkeypatch.setattr("helpers.changelog_patch.read_section_bullets",
                            lambda *a, **k: [])
        monkeypatch.setattr("helpers.changelog_patch.insert_unreleased_bullets",
                            lambda *a, **k: (True, "ok"))
        ok, msg, stats = changelog_io_apply(str(path), draft)
        assert ok
        assert "Added" in msg


# ── memory_compute_apply ──────────────────────────────────────────────────────

class TestMemoryComputeApply:
    def test_none_body_fails(self, monkeypatch):
        monkeypatch.setattr(
            "helpers.memory_patch._compute_insert_memory_body",
            lambda full, body: (full + body, True, "ok"),
        )
        _, ok, msg = memory_compute_apply("original", None)
        assert not ok
        assert "empty" in msg.lower()

    def test_delegates_to_patch(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "helpers.memory_patch._compute_insert_memory_body",
            lambda full, body: (full + "\n" + body, True, "ok"),
        )
        result, ok, msg = memory_compute_apply("existing", "new body")
        assert ok
        assert "new body" in result
