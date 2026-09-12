"""tests/test_markdown_tk.py — the Help renderer's markdown subset.

Pure: `render()` takes a string and returns `(text, tag)` spans, so every rule
is checked here without a display. That is the property that let the ambiguous
inline cases be PINNED rather than discovered later in a screenshot.

The unsupported-syntax cases matter as much as the supported ones. Without
them the module is one "just add links" away from being a markdown parser
nobody trusts, which is what its docstring exists to prevent.
"""
from __future__ import annotations

from helpers import markdown_tk
from helpers.markdown_tk import plain_text, render


def _tags_for(text, markdown):
    """Every tag applied to spans whose text contains `text`."""
    return [tag for chunk, tag in render(markdown) if text in chunk]


# ── Blocks ────────────────────────────────────────────────────────────────

def test_headings_map_to_three_levels():
    assert _tags_for("One", "# One") == ["h1"]
    assert _tags_for("Two", "## Two") == ["h2"]
    assert _tags_for("Three", "### Three") == ["h3"]


def test_a_deeper_heading_still_renders_as_h3():
    """Rather than emitting an h4 tag the widget never configured."""
    assert _tags_for("Deep", "##### Deep") == ["h3"]


def test_a_heading_loses_its_hashes():
    assert "#" not in plain_text("## Title")


def test_a_fenced_block_is_monospace_and_keeps_its_lines():
    out = plain_text("```\nline one\nline two\n```")
    assert "line one" in out and "line two" in out
    assert "```" not in out
    assert _tags_for("line one", "```\nline one\n```") == ["code"]


def test_markdown_inside_a_fence_is_not_interpreted():
    """A fenced example of markdown has to survive being shown."""
    out = render("```\n## not a heading **not bold**\n```")
    assert out == [("## not a heading **not bold**\n", "code")]


def test_a_table_row_is_monospace_and_verbatim():
    """Proportional fonts destroy column alignment, so the row goes through
    exactly as written."""
    row = "| a | b |"
    assert render(row) == [(row + "\n", "code")]


def test_a_horizontal_rule_becomes_a_dim_line():
    out = render("---")
    assert len(out) == 1 and out[0][1] == "dim"
    assert "-" not in out[0][0]          # drawn, not repeated


def test_a_block_quote_is_dim_and_loses_its_marker():
    out = plain_text("> quoted")
    assert "quoted" in out and ">" not in out
    assert _tags_for("quoted", "> quoted") == ["dim"]


def test_bullets_get_a_glyph_and_keep_their_text():
    out = plain_text("- first\n- second")
    assert "• first" in out and "• second" in out
    assert "- first" not in out


def test_a_nested_bullet_indents_and_changes_glyph():
    out = plain_text("- top\n  - nested")
    assert "• top" in out
    assert "◦ nested" in out


def test_a_numbered_item_keeps_its_number():
    assert "1. first" in plain_text("1. first")


# ── Inline: the three precedence rules ───────────────────────────────────

def test_code_wins_over_asterisks_inside_it():
    """`**not bold**` -- the asterisks are literal."""
    out = render("`**not bold**`")
    assert out == [("**not bold**", "code"), ("\n", "body")]


def test_bold_can_contain_a_code_span():
    """**bold `code`** -- both, with the code span rendered as code."""
    out = [(t, g) for t, g in render("**bold `code`**") if t.strip()]
    assert out == [("bold ", "bold"), ("code", "code")]


def test_triple_asterisks_split_deterministically():
    """***both*** -- ugly, defined, and not used by the corpus.

    `**` toggles and the leftover `*` is ordinary text, so the closing run
    splits the same way the opening one did. Asserted because "deterministic"
    is the property being claimed; measured separately, the corpus contains
    no `***` at all, which is why this is not worth special-casing.
    """
    out = [(t, g) for t, g in render("***both***") if t.strip()]
    assert out == [("*both", "bold"), ("*", "body")]


def test_a_lone_asterisk_is_never_emphasis():
    """Otherwise `2 * 3 * 4` turns half a paragraph italic."""
    assert "2 * 3 * 4" in plain_text("2 * 3 * 4")


def test_bold_tags_only_the_text_between_the_markers():
    out = render("plain **loud** plain")
    assert ("loud", "bold") in out
    assert "**" not in plain_text("plain **loud** plain")


# ── Inline: links and images ─────────────────────────────────────────────

def test_a_link_renders_as_its_text_and_drops_the_url():
    out = plain_text("see [the guide](docs/GUIDE.md) first")
    assert out.strip() == "see the guide first"


def test_an_image_is_dropped_entirely():
    """A badge row is not help."""
    assert plain_text("![build](https://x/y.svg)").strip() == ""


def test_an_image_is_removed_before_a_link_is_rewritten():
    """They share a shape; the wrong order leaves a stray `!`."""
    assert "!" not in plain_text("![alt](u)")


# ── Unsupported syntax stays literal ─────────────────────────────────────

def test_underscore_emphasis_is_literal():
    assert "_not italic_" in plain_text("_not italic_")


def test_strikethrough_is_literal():
    assert "~~struck~~" in plain_text("~~struck~~")


def test_inline_html_is_literal():
    assert "<b>x</b>" in plain_text("<b>x</b>")


def test_a_task_list_renders_as_a_bullet_with_its_brackets():
    out = plain_text("- [ ] todo")
    assert "[ ] todo" in out


def test_a_setext_heading_is_not_a_heading():
    tags = {tag for _t, tag in render("Title\n=====")}
    assert "h1" not in tags


# ── The contract with the widget ─────────────────────────────────────────

def test_every_emitted_tag_is_declared_in_TAGS():
    """A span carrying an undeclared tag renders unstyled and silently.

    Run over the REAL corpus rather than a fixture: the documents are what
    actually reaches the renderer.
    """
    from helpers import help_docs

    emitted = set()
    for item in help_docs.topics():
        emitted.update(tag for _t, tag in
                       render(help_docs.topic_markdown(item.key)))
    assert emitted <= set(markdown_tk.TAGS), (
        "renderer emitted tags outside TAGS: %s"
        % sorted(emitted - set(markdown_tk.TAGS)))


def test_render_is_pure_and_repeatable():
    from helpers import help_docs

    body = help_docs.topic_markdown("window-tray")
    assert render(body) == render(body)


def test_empty_input_is_not_an_error():
    assert render("") == []
    assert render(None) == []
