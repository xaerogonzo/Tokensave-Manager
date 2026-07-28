"""Tests for prompts.py — PROMPT_SNIPPETS structural + integration invariants.

prompts.py is a pure module-level constant (the Reference-tab snippet catalog),
so these tests validate its *shape* and a few *content invariants* tied to the
tokensave v6.3.0 integration sync: the renamed tool must not reappear, each new
v6.3 tool must have a covering snippet, and the integration-audit snippet must
no longer point STEP 1 at the wrong tool. No Tk, no fixtures — pure data checks.
"""
import re
import pytest

from prompts import PROMPT_SNIPPETS


# ── Structural invariants ─────────────────────────────────────────────────────

def test_snippets_is_nonempty_list():
    assert isinstance(PROMPT_SNIPPETS, list)
    assert len(PROMPT_SNIPPETS) >= 20      # floor — catches accidental truncation


def test_every_snippet_is_a_2tuple_of_nonempty_strings():
    for item in PROMPT_SNIPPETS:
        assert isinstance(item, tuple) and len(item) == 2, f"not a 2-tuple: {item!r}"
        title, body = item
        assert isinstance(title, str) and title.strip(), f"bad title: {item!r}"
        assert isinstance(body, str) and len(body.strip()) > 20, f"thin body: {title!r}"


def test_titles_are_unique():
    titles = [t for t, _ in PROMPT_SNIPPETS]
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    assert not dupes, f"duplicate titles: {dupes}"


def test_every_title_has_emoji_prefix():
    # Convention: each title starts with an emoji (non-ASCII) prefix + label.
    for title, _ in PROMPT_SNIPPETS:
        first = title.lstrip()[0]
        assert ord(first) > 127, f"title lacks emoji prefix: {title!r}"


def test_placeholders_are_balanced_double_brackets():
    # Placeholder syntax is [[token]]; single [brackets] are reserved for markdown.
    for title, body in PROMPT_SNIPPETS:
        assert body.count("[[") == body.count("]]"), \
            f"unbalanced [[ ]] in snippet {title!r}"


def test_no_unclosed_placeholder():
    for title, body in PROMPT_SNIPPETS:
        for m in re.finditer(r"\[\[", body):
            assert "]]" in body[m.start():], f"unclosed [[ in {title!r}"


# ── Integration-sync regression invariants (tokensave v6.3.0) ────────────────

def _all_bodies() -> str:
    return "\n".join(b for _, b in PROMPT_SNIPPETS)


def test_renamed_tool_outline_is_gone():
    # tokensave_outline → tokensave_entities (v6.2.0). The old name must not
    # appear anywhere — even as a descriptive mention.
    assert "tokensave_outline" not in _all_bodies()


@pytest.mark.parametrize("tool", [
    "tokensave_entities",
    "tokensave_blame",
    "tokensave_log",
    "tokensave_diff",
    "tokensave_dependencies",
    "tokensave_test_coverage",
    "tokensave_annotations",
])
def test_new_v63_tool_has_a_snippet(tool):
    # Word-boundary match so tokensave_diff does NOT match tokensave_diff_context.
    assert re.search(rf"\b{re.escape(tool)}\b", _all_bodies()), \
        f"no snippet references {tool}"


def test_tokensave_diff_is_standalone_not_just_diff_context():
    # Guards the word-boundary intent above: there must be a real `tokensave_diff`
    # token, not only occurrences of `tokensave_diff_context`.
    bodies = _all_bodies()
    standalone = re.findall(r"tokensave_diff(?!_context)", bodies)
    assert standalone, "tokensave_diff only appears as tokensave_diff_context"


def test_integration_audit_uses_release_notes_not_changelog_tool():
    # STEP 1 of the "Integration audit" snippet must NOT instruct calling
    # tokensave_changelog (which diffs the project, not tokensave's tools);
    # it should point at upstream release notes instead.
    audit = next((b for t, b in PROMPT_SNIPPETS if "Integration audit" in t), None)
    assert audit is not None, "Integration audit snippet not found"
    assert "gh release view" in audit
    assert "Call tokensave_changelog" not in audit
