"""tests/test_pr_gap_body_refresh.py — Draft PR body coverage-gap checkbox refresh.

Covers the Tk glue `TestGapCtrl._apply_gap_progress_to_body` (Phase C2 moved it
from GitTabController): after the embedded panel writes a passing test, the body's
`### Coverage gaps` line flips `- [ ]` → `- [x]` WITHOUT tripping the dirty flag
(the edit is guarded by ctx["prog"]). The method does not reference `self`, so we
call it unbound with a throwaway instance.

Tk-marked: needs a real Text widget. Uses the session-scoped `tk_root` fixture.
"""
import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

pytestmark = pytest.mark.tk

from controllers.test_gap_ctrl import TestGapCtrl  # Phase C2 home of gap methods
from helpers.pr_draft import _render_coverage_gaps


class _FakeSuggestion:
    def __init__(self, rel_path, template):
        self.rel_path = rel_path
        self.template = template

    @property
    def requires_automation(self):
        return self.template in ("pure_helper", "subprocess_helper")


def _text_with_body(tk_root, body):
    txt = tk.Text(tk_root)
    txt.insert("1.0", body)
    txt.configure(state=tk.DISABLED)
    txt.edit_modified(False)
    return txt


def test_body_gap_flips_to_x_without_dirty(tk_root):
    body = _render_coverage_gaps([
        _FakeSuggestion("src/helpers/foo.py", "pure_helper"),
        _FakeSuggestion("src/helpers/bar.py", "subprocess_helper"),
    ])
    txt = _text_with_body(tk_root, body)
    ctx = {"txt": txt, "prog": [False]}

    TestGapCtrl._apply_gap_progress_to_body(object(), ctx, ["src/helpers/foo.py"])

    out = txt.get("1.0", tk.END)
    assert "- [x] `src/helpers/foo.py`" in out      # closed gap flipped
    assert "- [ ] `src/helpers/bar.py`" in out      # other gap still open
    assert not txt.edit_modified()                  # not flagged as a user edit
    assert ctx["prog"][0] is False                  # guard was reset
    txt.destroy()


def test_body_refresh_noop_when_no_match(tk_root):
    body = _render_coverage_gaps([_FakeSuggestion("src/helpers/foo.py", "pure_helper")])
    txt = _text_with_body(tk_root, body)
    ctx = {"txt": txt, "prog": [False]}

    TestGapCtrl._apply_gap_progress_to_body(object(), ctx, ["src/helpers/other.py"])

    assert "- [x]" not in txt.get("1.0", tk.END)
    txt.destroy()


def test_body_refresh_survives_missing_widget():
    # A destroyed/closed dialog (txt is None) must be a silent no-op, not a crash.
    TestGapCtrl._apply_gap_progress_to_body(object(), {"txt": None, "prog": [False]},
                                                 ["src/helpers/foo.py"])


# ── _gap_copy_claude_prompt (agentic handoff → clipboard) ────────────────────

def test_gap_copy_claude_prompt_populates_clipboard(tk_root):
    """Checked gaps → a Claude Code handoff prompt lands on the clipboard."""
    sg = type("S", (), {})()
    sg.source_path = "/p/src/helpers/foo.py"
    sg.rel_path = "src/helpers/foo.py"
    sg.template = "pure_helper"
    var = tk.BooleanVar(master=tk_root, value=True)
    status = tk.StringVar(master=tk_root)
    tk_root.clipboard_clear()

    TestGapCtrl._gap_copy_claude_prompt(
        object(), [sg], [var], "/p", status, tk_root)

    clip = tk_root.clipboard_get()
    assert "src/helpers/foo.py" in clip
    assert "Only CREATE files under `tests/`" in clip
    assert "Copied a Claude Code prompt for 1 file" in status.get()


def test_gap_copy_claude_prompt_nothing_selected(tk_root):
    sg = type("S", (), {})()
    sg.source_path = "/p/src/helpers/foo.py"; sg.rel_path = "src/helpers/foo.py"
    sg.template = "pure_helper"
    var = tk.BooleanVar(master=tk_root, value=False)   # unchecked
    status = tk.StringVar(master=tk_root)
    TestGapCtrl._gap_copy_claude_prompt(
        object(), [sg], [var], "/p", status, tk_root)
    assert "Nothing selected" in status.get()


# ── _show_ai_failures: written-partials vs discarded, two sections ────────────

def _descendants(w):
    out = []
    for c in w.winfo_children():
        out.append(c)
        out.extend(_descendants(c))
    return out


def _failures_window_text(tk_root, obj):
    """Open _show_ai_failures and return the ScrolledText's content (the Text is
    nested inside ScrolledText's internal Frame, so search descendants)."""
    before = set(tk_root.winfo_children())
    TestGapCtrl._show_ai_failures(obj, tk_root)
    wins = [w for w in tk_root.winfo_children()
            if w not in before and isinstance(w, tk.Toplevel)]
    assert wins, "no results window created"
    win = wins[0]
    texts = [w for w in _descendants(win) if isinstance(w, tk.Text)]
    assert texts, "no text widget"
    return win, texts[0].get("1.0", tk.END)


def test_show_ai_failures_splits_partials_and_discards(tk_root):
    obj = type("X", (), {})()
    obj._last_ai_partials = {"src/a.py": "[prune-verify] kept 3/4; dropped: T.test_x"}
    obj._last_ai_fail_reports = {"src/b.py": "passed alone but FAILED in the suite"}
    win, content = _failures_window_text(tk_root, obj)
    assert "WRITTEN" in content and "src/a.py" in content        # partial section
    assert "DISCARDED" in content and "src/b.py" in content      # failure section
    assert content.index("WRITTEN") < content.index("DISCARDED")  # not framed as discard
    win.destroy()


def test_show_ai_failures_empty_state(tk_root):
    obj = type("X", (), {})()
    obj._last_ai_partials = {}
    obj._last_ai_fail_reports = {}
    win, content = _failures_window_text(tk_root, obj)
    assert "No failures or dropped tests" in content
    win.destroy()


# ── Full panel render: header + suggestions + actions all build ──────────────

def _make_suggestion(rel_path="src/helpers/foo.py", template="pure_helper",
                     requires_automation=True, test_exists=False):
    sg = type("S", (), {})()
    sg.rel_path = rel_path
    sg.template = template
    sg.requires_automation = requires_automation
    sg.test_exists = test_exists
    sg.source_path = "/proj/" + rel_path
    sg.test_path = ""
    return sg


def test_gap_panel_builds_all_sections(tk_root, mock_config):
    """Regression for the _GapPanelCtx __slots__ bug (Phase B1).

    `_gap_panel_header` ended with `ctx._ai_available = ai_available`, but
    `_ai_available` was missing from `_GapPanelCtx.__slots__` — so the
    assignment raised AttributeError on the header's LAST line. The header
    rendered fully (count label, backend radios, nudge) but the suggestions
    list and the action-button row never built, leaving a half-drawn panel.

    This drives a full synchronous render (suggestions passed in → no
    background scan) and asserts all three sections are present.
    """
    ctrl = TestGapCtrl(
        tab=tk_root, cfg=mock_config, on_log=lambda *a, **k: None,
        get_path=lambda: "/proj", get_test_manager_ref=lambda: None)

    dlg = tk.Frame(tk_root)
    ctrl._build_test_gap_panel(
        dlg, "/proj", "master", suggestions=[_make_suggestion()])

    labels = [str(w.cget("text"))
              for w in _descendants(dlg)
              if isinstance(w, ttk.Button)]
    # Actions section (the part that vanished when the header crashed).
    assert any("AI generate" in lbl for lbl in labels), \
        f"AI-generate button missing — panel built only partway: {labels}"
    assert any("Generate stubs" in lbl for lbl in labels), \
        f"Generate-stubs button missing: {labels}"
    assert any("Recommend" in lbl for lbl in labels), \
        f"quick-select row missing: {labels}"

    # Suggestions section — the changed file's checkbox row rendered.
    all_text = " ".join(
        str(w.cget("text")) for w in _descendants(dlg)
        if "text" in (w.keys() if hasattr(w, "keys") else []))
    assert "foo.py" in all_text, "suggestion checkbox row missing"
    dlg.destroy()


def test_gap_panel_ai_disabled_when_no_backend(tk_root, mock_config):
    """When no AI backend is configured, the panel still builds fully and the
    AI-generate button is DISABLED (exercises the `_ai_available` False path)."""
    # mock_config defaults: claude_cli_exe="" and no commit_message_llm provider.
    ctrl = TestGapCtrl(
        tab=tk_root, cfg=mock_config, on_log=lambda *a, **k: None,
        get_path=lambda: "/proj", get_test_manager_ref=lambda: None)

    dlg = tk.Frame(tk_root)
    ctrl._build_test_gap_panel(
        dlg, "/proj", "master", suggestions=[_make_suggestion()])

    ai_btns = [w for w in _descendants(dlg)
               if isinstance(w, ttk.Button)
               and "AI generate" in str(w.cget("text"))]
    assert ai_btns, "panel built only partway — AI button missing"
    assert str(ai_btns[0].cget("state")) == "disabled"
    dlg.destroy()
