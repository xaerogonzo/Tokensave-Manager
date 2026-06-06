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
