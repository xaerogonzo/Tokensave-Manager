"""Tests for pr_draft.py pure-function helpers.

Covers:
  _clean_local_artifacts  — regex fence-stripping, structural-whitespace preservation
  _inject_automated_block — insertion ordering, fallback structure, marker robustness
"""
from helpers.pr_draft import _clean_local_artifacts, _inject_automated_block

# ── _clean_local_artifacts ────────────────────────────────────────────────────

def test_clean_strips_fenced_block():
    raw = "Prose.\n```python\ncode here\n```\nMore prose."
    out = _clean_local_artifacts(raw)
    assert "```" not in out
    assert "Prose." in out and "More prose." in out


def test_clean_preserves_inline_backticks():
    text = "Use `pytest` to run tests."
    assert _clean_local_artifacts(text) == text


def test_clean_noop_on_plain_prose():
    assert "```" not in _clean_local_artifacts("## Summary\n\nSome prose.\n")


def test_clean_strips_multiple_blocks():
    raw = "A.\n```\nb1\n```\nB.\n```python\nb2\n```\nC."
    out = _clean_local_artifacts(raw)
    assert "```" not in out
    assert all(x in out for x in ("A.", "B.", "C."))


def test_clean_handles_padded_fence_marker():
    """Trailing spaces after language tag AND after the closing fence.

    Requires regex r'```[^\n]*\n.*?```[ \t]*\n?' — Fix B1 in production code.
    Without [ \t]* the closing ` ``` ` followed by spaces+newline is not matched
    and the code block content leaks into the output.
    """
    raw = "Text.\n```python   \ncode line\n```  \nAfter."
    out = _clean_local_artifacts(raw)
    assert "code line" not in out   # fence content stripped
    assert "After." in out          # prose after fence preserved


def test_clean_preserves_structural_whitespace():
    """.strip() must NOT be called globally — it destroys section spacing.

    Since re.sub with no code-fence matches performs a clean no-op, the
    returned string must be byte-identical to the input.
    """
    text = "\n## Summary\n\nSome prose.\n\n## Details\n\nMore.\n"
    out = _clean_local_artifacts(text)
    assert out == text


# ── _inject_automated_block ───────────────────────────────────────────────────

_AUTO = "### Automated (verified by `pytest -m \"not tk\"`)\n- [x] pass\n"
_MARKER = "<!-- tokensave-manager:testing-checklist v1 -->"


def test_inject_places_automated_before_manual():
    text = f"## Testing checklist\n{_MARKER}\n### Manual\n- [ ] check"
    result = _inject_automated_block(text, _AUTO)
    # Pre-assert presence — avoids ValueError masking the real injection failure
    assert "### Automated" in result
    assert "### Manual" in result
    assert result.index("### Automated") < result.index("### Manual")


def test_inject_preserves_manual_bullets():
    text = f"## Testing checklist\n{_MARKER}\n### Manual\n- [ ] smoke the UI"
    assert "- [ ] smoke the UI" in _inject_automated_block(text, _AUTO)


def test_inject_preserves_marker_comment():
    text = f"## Testing checklist\n{_MARKER}\n### Manual\n- [ ] x"
    assert _MARKER in _inject_automated_block(text, _AUTO)


def test_inject_fallback_structure_when_no_manual_section():
    """Fallback must emit the full two-subsection structure (Automated + Manual).

    Fix B2 adds a ### Manual stub to the fallback path so the Sync PR Checklist
    template contract is met even when the model omits the checklist entirely.
    """
    text = "## Summary\n\nsome prose"
    result = _inject_automated_block(text, _AUTO)
    assert "## Testing checklist" in result
    assert "### Automated" in result
    assert "### Manual" in result        # Fix B2 ensures fallback emits both
    # Verify structural ordering
    assert result.index("## Testing checklist") < result.index("### Automated")
    assert result.index("### Automated") < result.index("### Manual")


def test_inject_automated_appears_exactly_once():
    text = f"## Testing checklist\n{_MARKER}\n### Manual\n- [ ] x"
    assert _inject_automated_block(text, _AUTO).count("### Automated") == 1


def test_inject_no_crash_when_marker_absent():
    """Injection anchors on '### Manual', not the HTML comment marker.

    The function must work correctly even when the
    <!-- tokensave-manager:testing-checklist v1 --> comment is absent —
    for example if a user edited the PR body and deleted it.
    """
    # No _MARKER in input — only ### Manual
    text = "## Testing checklist\n### Manual\n- [ ] smoke check"
    result = _inject_automated_block(text, _AUTO)
    assert "### Automated" in result
    assert "### Manual" in result
    assert result.index("### Automated") < result.index("### Manual")
