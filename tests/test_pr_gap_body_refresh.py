"""tests/test_pr_gap_body_refresh.py — Draft PR body coverage-gap checkbox refresh.

Covers the Tk glue `GitTabController._apply_gap_progress_to_body`: after the embedded
panel writes a passing test, the body's `### Coverage gaps` line flips `- [ ]` → `- [x]`
WITHOUT tripping the dirty flag (the edit is guarded by ctx["prog"]). The method does
not reference `self`, so we call it unbound with a throwaway instance.

Tk-marked: needs a real Text widget. Uses the session-scoped `tk_root` fixture.
"""
import pytest

tk = pytest.importorskip("tkinter")

pytestmark = pytest.mark.tk

from controllers.git_tab import GitTabController
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

    GitTabController._apply_gap_progress_to_body(object(), ctx, ["src/helpers/foo.py"])

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

    GitTabController._apply_gap_progress_to_body(object(), ctx, ["src/helpers/other.py"])

    assert "- [x]" not in txt.get("1.0", tk.END)
    txt.destroy()


def test_body_refresh_survives_missing_widget():
    # A destroyed/closed dialog (txt is None) must be a silent no-op, not a crash.
    GitTabController._apply_gap_progress_to_body(object(), {"txt": None, "prog": [False]},
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

    GitTabController._gap_copy_claude_prompt(
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
    GitTabController._gap_copy_claude_prompt(
        object(), [sg], [var], "/p", status, tk_root)
    assert "Nothing selected" in status.get()
