"""tests/test_pr_draft_streaming.py — generate_pr_draft streaming + status plumbing.

Verifies the Phase-A contract additions to `generate_pr_draft`:
  * `on_token` is forwarded to `_call_llm` (so the dialog can stream).
  * `on_status` is invoked at phase boundaries (so the dialog can show progress
    before the first token).
  * `coverage_gaps_md` is spliced into the returned body.

Hermetic: `_pending_diff` and `_call_llm` are monkeypatched at the
`helpers.pr_draft` import site (no real git / no real LLM).
"""
from __future__ import annotations

from types import SimpleNamespace

import helpers.pr_draft as prd
from helpers.pr_draft import generate_pr_draft

_BODY = (
    "## Summary of Changes\n\nDid things.\n\n"
    "## Testing checklist\n<!-- tokensave-manager:testing-checklist v1 -->\n"
    "### Automated\n- [x] suite\n"
    "### Manual (please verify before merge)\n- [ ] smoke\n"
)


def _cfg(provider="anthropic"):
    return SimpleNamespace(
        raw={"commit_message_llm": {
            "provider": provider, "model": "m", "enabled": True}},
        enable_pr_grounding=False,   # skip grounding → hermetic, no subprocess
        git_exe="git",
        tokensave_exe="",
        codegraph_exe="",
    )


def _patch(monkeypatch, capture):
    monkeypatch.setattr(prd, "_pending_diff",
                        lambda *a, **kw: "diff --git a/x b/x\n" + ("+line\n" * 40))

    def fake_call_llm(*, cfg, system_prompt, user_prompt, max_tokens, timeout,
                      on_token=None):
        capture["on_token"] = on_token
        if on_token is not None:
            for chunk in ("## Summary", " of Changes\n", "rest"):
                on_token(chunk)
        return _BODY

    monkeypatch.setattr(prd, "_call_llm", fake_call_llm)


def test_on_token_forwarded_to_call_llm(monkeypatch):
    capture: dict = {}
    received: list = []
    _patch(monkeypatch, capture)

    result = generate_pr_draft(_cfg(), ".", base="", on_token=received.append)

    assert capture["on_token"] is not None      # forwarded, not dropped
    assert received == ["## Summary", " of Changes\n", "rest"]
    assert result == _BODY


def test_on_status_called_for_generating(monkeypatch):
    capture: dict = {}
    statuses: list = []
    _patch(monkeypatch, capture)

    generate_pr_draft(_cfg(), ".", base="", on_status=statuses.append)

    assert "generating" in statuses             # ≥ 1 phase emitted


def test_on_status_grounding_when_enabled(monkeypatch):
    """When grounding is on, the 'grounding' phase fires before 'generating'."""
    capture: dict = {}
    statuses: list = []
    _patch(monkeypatch, capture)
    # Force grounding on but stub the builders so it stays hermetic.
    monkeypatch.setattr(prd, "_files_from_diff", lambda *a, **kw: [])
    cfg = _cfg()
    cfg.enable_pr_grounding = True

    generate_pr_draft(cfg, ".", base="", on_status=statuses.append)

    assert "grounding" in statuses
    assert statuses.index("grounding") < statuses.index("generating")


def test_coverage_gaps_md_spliced_into_body(monkeypatch):
    capture: dict = {}
    _patch(monkeypatch, capture)
    gaps = "### Coverage gaps (changed files with no test file)\n- [ ] `src/a.py`\n"

    result = generate_pr_draft(_cfg(), ".", base="", coverage_gaps_md=gaps)

    assert "### Coverage gaps" in result
    # Ordering: Automated < Coverage gaps < Manual
    assert result.index("### Automated") < result.index("### Coverage gaps")
    assert result.index("### Coverage gaps") < result.index("### Manual")


def test_no_callbacks_still_works(monkeypatch):
    """Existing callers (no kwargs) keep working unchanged."""
    capture: dict = {}
    _patch(monkeypatch, capture)
    assert generate_pr_draft(_cfg(), ".", base="") == _BODY
    assert capture["on_token"] is None


# ── Phase D: diff cap scales with local context window ──────────────────────────

def _capture_prompt(monkeypatch):
    """Patch _call_llm to record the user_prompt it receives; return the recorder."""
    seen: dict = {}

    def fake_call_llm(*, cfg, system_prompt, user_prompt, max_tokens, timeout,
                      on_token=None):
        seen["user_prompt"] = user_prompt
        return _BODY

    monkeypatch.setattr(prd, "_call_llm", fake_call_llm)
    return seen


def test_local_diff_cap_scales_past_old_8000(monkeypatch):
    """A local provider must now accept far more than the old 8000-char cap."""
    big = "diff --git a/x b/x\n" + ("+a line of changes\n" * 4000)  # ~76 KB
    monkeypatch.setattr(prd, "_pending_diff", lambda *a, **kw: big)
    monkeypatch.setattr(prd, "_files_from_diff", lambda *a, **kw: [])
    seen = _capture_prompt(monkeypatch)

    cfg = _cfg(provider="ollama")          # local → scaled cap (num_ctx 16384)
    generate_pr_draft(cfg, ".", base="")

    # Old behaviour truncated to 8000 chars; scaled cap is ~36 KB.
    assert len(seen["user_prompt"]) > 20000


def test_explicit_max_diff_chars_overrides(monkeypatch):
    big = "diff --git a/x b/x\n" + ("+a line\n" * 4000)
    monkeypatch.setattr(prd, "_pending_diff", lambda *a, **kw: big)
    monkeypatch.setattr(prd, "_files_from_diff", lambda *a, **kw: [])
    seen = _capture_prompt(monkeypatch)

    cfg = _cfg(provider="ollama")
    cfg.raw["commit_message_llm"]["max_diff_chars"] = 5000
    generate_pr_draft(cfg, ".", base="")

    # Diff portion capped at 5000; prompt is diff + a little scaffolding.
    assert len(seen["user_prompt"]) < 9000
