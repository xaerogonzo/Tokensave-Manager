"""Tests for pr_draft.py pure-function helpers.

Covers:
  _clean_local_artifacts  — regex fence-stripping, structural-whitespace preservation
  _inject_automated_block — insertion ordering, fallback structure, marker robustness
  _render_coverage_gaps   — requires_automation filtering, backtick escaping
  _inject_coverage_gaps   — ordering (Automated < Coverage gaps < Manual)
"""
from dataclasses import dataclass

from helpers.pr_draft import (
    _PR_SYSTEM_PROMPT,
    _PR_SYSTEM_PROMPT_LOCAL,
    _branch_commit_log,
    _clean_local_artifacts,
    _inject_automated_block,
    _inject_coverage_gaps,
    _mark_gaps_addressed,
    _render_coverage_gaps,
)


@dataclass
class _FakeSuggestion:
    """Stand-in for test_gap_report.SuggestedTest with the attrs the renderer reads."""
    rel_path: str
    template: str

    @property
    def requires_automation(self) -> bool:
        return self.template in ("pure_helper", "subprocess_helper")

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


# ── _render_coverage_gaps ───────────────────────────────────────────────────────

def test_render_gaps_empty_list_returns_empty():
    assert _render_coverage_gaps([]) == ""
    assert _render_coverage_gaps(None) == ""


def test_render_gaps_filters_to_automatable_only():
    """dialog_tk and blank are excluded; pure/subprocess kept."""
    sugg = [
        _FakeSuggestion("src/helpers/a.py", "pure_helper"),
        _FakeSuggestion("src/controllers/b.py", "subprocess_helper"),
        _FakeSuggestion("src/dialogs/c.py", "dialog_tk"),     # excluded
        _FakeSuggestion("src/d.py", "blank"),                 # excluded
    ]
    out = _render_coverage_gaps(sugg)
    assert "src/helpers/a.py" in out
    assert "src/controllers/b.py" in out
    assert "src/dialogs/c.py" not in out
    assert "src/d.py" not in out


def test_render_gaps_all_excluded_returns_empty():
    sugg = [_FakeSuggestion("src/dialogs/c.py", "dialog_tk")]
    assert _render_coverage_gaps(sugg) == ""


def test_render_gaps_has_header_and_checkboxes():
    out = _render_coverage_gaps([_FakeSuggestion("src/helpers/a.py", "pure_helper")])
    assert out.startswith("### Coverage gaps")
    assert "- [ ] `src/helpers/a.py`" in out
    assert "pure_helper" in out


def test_render_gaps_escapes_backticks_in_path():
    """A backtick in a path must not break the surrounding markdown."""
    out = _render_coverage_gaps([_FakeSuggestion("src/we`ird.py", "pure_helper")])
    assert "`" in out                      # the wrapping backticks remain
    assert "we`ird" not in out             # raw backtick neutralised
    assert "we'ird" in out                 # replaced with a safe quote


# ── _inject_coverage_gaps ───────────────────────────────────────────────────────

_GAPS = "### Coverage gaps (changed files with no test file)\n- [ ] `src/a.py` — no test yet (pure_helper)\n"


def test_inject_gaps_orders_automated_gaps_manual():
    text = "### Automated\n- [x] suite\n### Manual\n- [ ] smoke"
    result = _inject_coverage_gaps(text, _GAPS)
    assert result.index("### Automated") < result.index("### Coverage gaps")
    assert result.index("### Coverage gaps") < result.index("### Manual")


def test_inject_gaps_empty_block_is_noop():
    text = "### Manual\n- [ ] smoke"
    assert _inject_coverage_gaps(text, "") == text


def test_inject_gaps_fallback_appends_when_no_manual():
    text = "## Summary\n\nprose only"
    result = _inject_coverage_gaps(text, _GAPS)
    assert "### Coverage gaps" in result
    assert "src/a.py" in result
    # Original prose preserved and the gaps block follows it.
    assert result.index("## Summary") < result.index("### Coverage gaps")


def test_inject_gaps_preserves_manual_bullets():
    text = "### Manual\n- [ ] keep me"
    result = _inject_coverage_gaps(text, _GAPS)
    assert "- [ ] keep me" in result


# ── _mark_gaps_addressed ──────────────────────────────────────────────────────

_GAP_BLOCK = _render_coverage_gaps([
    _FakeSuggestion("src/helpers/foo.py", "pure_helper"),
    _FakeSuggestion("src/helpers/bar.py", "subprocess_helper"),
])


def test_mark_flips_matching_line_only():
    out = _mark_gaps_addressed(_GAP_BLOCK, ["src/helpers/foo.py"])
    assert "- [x] `src/helpers/foo.py`" in out
    assert "- [ ] `src/helpers/bar.py`" in out          # untouched gap stays open


def test_mark_empty_list_is_noop():
    assert _mark_gaps_addressed(_GAP_BLOCK, []) == _GAP_BLOCK


def test_mark_unknown_path_is_noop():
    assert _mark_gaps_addressed(_GAP_BLOCK, ["src/helpers/nope.py"]) == _GAP_BLOCK


def test_mark_is_idempotent():
    once = _mark_gaps_addressed(_GAP_BLOCK, ["src/helpers/foo.py"])
    twice = _mark_gaps_addressed(once, ["src/helpers/foo.py"])
    assert once == twice
    assert once.count("- [x] `src/helpers/foo.py`") == 1


def test_mark_escapes_backticks_like_renderer():
    # A path with a backtick is rendered with the backtick → apostrophe; the marker
    # must apply the same escape to match.
    block = _render_coverage_gaps([_FakeSuggestion("src/a`b.py", "pure_helper")])
    out = _mark_gaps_addressed(block, ["src/a`b.py"])
    assert "- [x] `src/a'b.py`" in out


def test_mark_does_not_touch_manual_bullet_with_same_path():
    body = (_GAP_BLOCK
            + "\n### Manual (please verify before merge)\n"
            + "- [ ] manually re-check `src/helpers/foo.py` behaviour\n")
    out = _mark_gaps_addressed(body, ["src/helpers/foo.py"])
    # the gap line flips...
    assert "- [x] `src/helpers/foo.py` — no test yet" in out
    # ...but the manual bullet (path not immediately after "- [ ] `") stays open
    assert "- [ ] manually re-check `src/helpers/foo.py` behaviour" in out


def test_mark_round_trip_with_renderer():
    block = _render_coverage_gaps([_FakeSuggestion("src/helpers/foo.py", "pure_helper")])
    out = _mark_gaps_addressed(block, ["src/helpers/foo.py"])
    assert out == block.replace("- [ ] ", "- [x] ", 1)


# ── PR system prompts: positive anti-fabrication guardrails ──────────────────

import pytest as _pytest


@_pytest.mark.parametrize("prompt", [_PR_SYSTEM_PROMPT, _PR_SYSTEM_PROMPT_LOCAL])
def test_pr_prompt_has_positive_guardrails(prompt):
    assert "LITERALLY appear in the git diff" in prompt
    assert "Generate ONLY these exact" in prompt


@_pytest.mark.parametrize("prompt", [_PR_SYSTEM_PROMPT, _PR_SYSTEM_PROMPT_LOCAL])
def test_pr_prompt_avoids_negative_constraint_echo(prompt):
    # Must NOT print the forbidden section names — that re-introduces the
    # negative-constraint antipattern (a strained 14B echoes printed strings).
    for banned in ("Documentation Updates", "Deployment", "Reviewers"):
        assert banned not in prompt


# ── _branch_commit_log ───────────────────────────────────────────────────────

def test_branch_commit_log_builds_reverse_bullets(monkeypatch):
    captured = {}
    def fake_co(args, **k):
        captured["args"] = args
        return "- first commit\n- second commit\n"
    monkeypatch.setattr("helpers.pr_draft.subprocess.check_output", fake_co)
    out = _branch_commit_log("/repo", "origin/master", git_exe="git")
    assert "--reverse" in captured["args"]
    assert "master..HEAD" in " ".join(captured["args"]) or \
           "origin/master..HEAD" in captured["args"]
    assert out == "- first commit\n- second commit"


def test_branch_commit_log_caps_and_notes_overflow(monkeypatch):
    many = "\n".join(f"- commit {i}" for i in range(250))
    monkeypatch.setattr("helpers.pr_draft.subprocess.check_output",
                        lambda *a, **k: many)
    out = _branch_commit_log("/repo", "master", max_commits=200)
    lines = out.splitlines()
    assert len(lines) == 201                       # 200 kept + 1 overflow note
    assert lines[0] == "- commit 0"                # oldest kept (--reverse semantics)
    assert "later commits omitted" in lines[-1]
    assert "Claude CLI" in lines[-1]


def test_branch_commit_log_empty_on_git_failure(monkeypatch):
    monkeypatch.setattr("helpers.pr_draft.subprocess.check_output",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert _branch_commit_log("/repo", "master") == ""
