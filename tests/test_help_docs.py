"""tests/test_help_docs.py — the help corpus and the engine that reads it.

Pure: no Tk anywhere in this file. The whole point of splitting `help_docs`
from the tab is that the corpus can be checked without a display, and CI runs
these outside xvfb.

Several of these guard the REAL corpus rather than a fixture, deliberately.
A test that only proves the parser works on invented input would have let the
25-topic Python help go stale in exactly the way it did.
"""
from __future__ import annotations

import os

import pytest

from helpers import help_docs


# ── The corpus itself ─────────────────────────────────────────────────────

def test_every_document_is_present_and_readable():
    """A missing document is the failure mode the `Problem` type exists for.

    Asserted on the real corpus: `build.ps1` has to ship each of these, and a
    document renamed in the repo would otherwise only show up as a quietly
    shorter topic list.
    """
    missing = [d for d in help_docs.HELP_DOCUMENTS
               if not os.path.isfile(help_docs.document_path(d))]
    assert missing == []


def test_the_corpus_scans_without_problems():
    _topics, problems = help_docs.scan()
    assert [(p.document, p.detail) for p in problems] == []


def test_the_corpus_has_a_useful_number_of_topics():
    """A floor, not a fixed count -- a fixed one would fight every doc edit.

    The Python help it replaced had 25 topics and covered 9 of 19 features.
    Dropping back under that many means something stopped being anchored.
    """
    assert len(help_docs.topics()) >= 25


def test_keys_are_unique_across_the_whole_corpus():
    """Globally unique, not per document.

    At run time the first wins and `scan` records a problem; here a duplicate
    is a failure, because two documents claiming `help:git-workflow` is an
    editing mistake with a silent outcome -- one of them simply never opens.
    """
    keys = [t.key for t in help_docs.topics()]
    assert len(keys) == len(set(keys))


def test_no_topic_uses_a_placeholder_outside_the_allowlist():
    """`{{template_dri}}` would otherwise ship as visibly broken help."""
    bad = {}
    for item in help_docs.topics():
        unknown = help_docs.unknown_placeholders(
            help_docs.topic_markdown(item.key))
        if unknown:
            bad[item.key] = unknown
    assert bad == {}


def test_every_topic_renders_a_non_empty_body():
    """The guarantee the old `test_help_tab` made, kept across the move."""
    empty = [t.key for t in help_docs.topics()
             if not help_docs.topic_markdown(t.key).strip()]
    assert empty == []


# ── Ordering and identity ─────────────────────────────────────────────────

def test_topics_come_back_in_document_then_anchor_order():
    """The sidebar's order is a contract; without one it is arbitrary."""
    found = help_docs.topics()
    positions = [help_docs.HELP_DOCUMENTS.index(t.document) for t in found]
    assert positions == sorted(positions)


def test_a_title_comes_from_the_heading_not_from_python():
    """The whole reason the key lives in the markdown.

    If titles were a table here, rewording a heading would leave the sidebar
    saying the old thing -- which is the edit a documentation sweep makes.
    """
    item = help_docs.topic("switching-projects")
    assert item.title == "Switching Projects"
    assert help_docs.topic_markdown(item.key).startswith("## Switching")


def test_an_unknown_key_raises_rather_than_returning_empty():
    with pytest.raises(help_docs.HelpUnavailable):
        help_docs.topic_markdown("no-such-topic")


# ── Section boundaries ────────────────────────────────────────────────────

FIXTURE = "\n".join([
    "# Doc",
    "",
    "<!-- help:parent -->",
    "## Parent",
    "parent body",
    "",
    "<!-- help:child -->",
    "### Child",
    "child body",
    "",
    "## Sibling",
    "sibling body",
])


def _fixture_corpus(tmp_path, monkeypatch, text=FIXTURE, name="F.md"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(help_docs, "docs_directory", lambda: str(tmp_path))
    monkeypatch.setattr(help_docs, "HELP_DOCUMENTS", (name,))


def test_a_parent_section_carries_its_children(tmp_path, monkeypatch):
    _fixture_corpus(tmp_path, monkeypatch)
    body = help_docs.topic_markdown("parent")
    assert "parent body" in body
    assert "### Child" in body and "child body" in body
    assert "sibling body" not in body


def test_a_child_section_does_not_swallow_the_next_one(tmp_path, monkeypatch):
    _fixture_corpus(tmp_path, monkeypatch)
    body = help_docs.topic_markdown("child")
    assert "child body" in body
    assert "sibling body" not in body


def test_anchors_are_stripped_from_the_rendered_body(tmp_path, monkeypatch):
    _fixture_corpus(tmp_path, monkeypatch)
    assert "help:child" not in help_docs.topic_markdown("parent")


def test_an_anchor_not_above_a_heading_is_a_problem(tmp_path, monkeypatch):
    _fixture_corpus(tmp_path, monkeypatch,
                    text="<!-- help:orphan -->\nnot a heading\n")
    topics, problems = help_docs.scan()
    assert topics == ()
    assert len(problems) == 1 and "orphan" in problems[0].detail


def test_a_duplicate_key_keeps_the_first_and_reports_it(tmp_path, monkeypatch):
    text = "\n".join(["<!-- help:dup -->", "## First", "a", "",
                      "<!-- help:dup -->", "## Second", "b"])
    _fixture_corpus(tmp_path, monkeypatch, text=text)
    topics, problems = help_docs.scan()
    assert [t.title for t in topics] == ["First"]
    assert len(problems) == 1 and "duplicate" in problems[0].detail


# ── Failing closed ────────────────────────────────────────────────────────

def test_a_missing_document_is_reported_not_silently_dropped(
        tmp_path, monkeypatch):
    """"Unknown is never false", in a new place.

    A shorter topic list is indistinguishable from a document that had
    nothing to say, so the absence has to arrive as a statement.
    """
    monkeypatch.setattr(help_docs, "docs_directory", lambda: str(tmp_path))
    monkeypatch.setattr(help_docs, "HELP_DOCUMENTS", ("GONE.md",))
    topics, problems = help_docs.scan()
    assert topics == ()
    assert len(problems) == 1
    assert problems[0].document == "GONE.md"
    assert "could not be read" in problems[0].detail


# ── Placeholders ──────────────────────────────────────────────────────────

def test_an_allowlisted_placeholder_is_substituted():
    out = help_docs.substitute("at {{template_dir}}.", {"template_dir": "D:/t"})
    assert out == "at D:/t."


def test_a_known_placeholder_with_no_value_says_unknown():
    """Never an empty string, which reads as though there were nothing there."""
    assert help_docs.substitute("{{template_dir}}", {}) == "unknown"
    assert help_docs.substitute("{{template_dir}}",
                                {"template_dir": ""}) == "unknown"


def test_an_unknown_placeholder_is_left_visible():
    """Loud at run time as well as at test time."""
    assert help_docs.substitute("{{nope}}", {"nope": "x"}) == "{{nope}}"


def test_substitution_applies_inside_a_fenced_block():
    """Deliberate: the file-locations topic is paths shown in monospace.

    Excluding fences would break the main reason placeholders exist here.
    """
    text = "```\n{{template_dir}}\\project-baseline.md\n```"
    out = help_docs.substitute(text, {"template_dir": "D:/t"})
    assert "D:/t\\project-baseline.md" in out


def test_substitution_is_scoped_to_the_text_it_is_given():
    """It runs on an extracted SECTION, never over a whole document."""
    body = help_docs.topic_markdown("file-locations")
    assert "{{template_dir}}" in body          # the raw section still has it
    filled = help_docs.substitute(body, {"template_dir": "D:/t"})
    assert "{{template_dir}}" not in filled


# ── Search ────────────────────────────────────────────────────────────────

def test_search_reads_bodies_not_just_titles():
    """The question a reader actually asks is about a word in the text."""
    hits = help_docs.search("strict_tree")
    assert hits, "a term that appears only in body text found nothing"
    assert all("strict_tree" not in h.topic.title.lower() or h.in_title
               for h in hits)


def test_a_title_match_outranks_a_busier_body_match(tmp_path, monkeypatch):
    text = "\n".join([
        "<!-- help:a -->", "## Widgets", "nothing much here", "",
        "<!-- help:b -->", "## Other", "widgets widgets widgets widgets",
    ])
    _fixture_corpus(tmp_path, monkeypatch, text=text)
    hits = help_docs.search("widgets")
    assert [h.topic.key for h in hits] == ["a", "b"]


def test_body_matches_rank_by_occurrence_then_corpus_order(
        tmp_path, monkeypatch):
    text = "\n".join([
        "<!-- help:one -->", "## One", "term", "",
        "<!-- help:two -->", "## Two", "term term term", "",
        "<!-- help:three -->", "## Three", "term",
    ])
    _fixture_corpus(tmp_path, monkeypatch, text=text)
    hits = help_docs.search("term")
    assert [h.topic.key for h in hits] == ["two", "one", "three"]


def test_an_empty_query_returns_nothing():
    assert help_docs.search("") == ()
    assert help_docs.search("   ") == ()


def test_a_hit_carries_a_snippet_for_a_title_that_looks_unrelated():
    hits = help_docs.search("strict_tree")
    assert any(h.snippet for h in hits)
