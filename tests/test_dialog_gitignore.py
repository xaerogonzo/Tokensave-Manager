"""tests/test_dialog_gitignore.py — pattern merging, and what gets written.

The largest untested dialog, and one that writes into the user's repository.
The interesting logic is not the widgets but the merge: what counts as
"already ignored", and therefore what a template or an AI suggestion is
allowed to append.

Two rules carry most of the weight, and both are easy to get subtly wrong:

* **trailing slashes are noise** — `__pycache__/` and `__pycache__` are the
  same rule, so treating them as different appends a duplicate every time;
* **a pattern with no `/` matches at any depth** — so once `__pycache__/` is
  ignored, suggesting `src/__pycache__/` adds a line that changes nothing.
  This is where AI suggestions used to pile up noise.

Built with `object.__new__` and the handful of attributes the merge reads:
the alternative is standing up a Toplevel that needs an App parent, which
would test Tk rather than the merge.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.gitignore import GitignoreDialog


def _dialog(original=(), additions=(), removed=()):
    dialog = object.__new__(GitignoreDialog)
    dialog._original_lines = list(original)
    dialog._additions = list(additions)
    dialog._removed_indices = set(removed)
    # Reverting a pending removal repaints the panel; the merge only needs
    # the bookkeeping half.
    dialog._toggle_removal = lambda idx: dialog._removed_indices.discard(idx)
    return dialog


# ── what counts as already ignored ──────────────────────────────────────

def test_pattern_state_ignores_comments_and_blanks():
    dialog = _dialog(original=["# a comment", "", "  ", "dist/"])
    assert dialog._current_pattern_state() == {"dist/"}


def test_pattern_state_excludes_lines_marked_for_removal():
    dialog = _dialog(original=["dist/", "build/"], removed=[1])
    assert dialog._current_pattern_state() == {"dist/"}


def test_pattern_state_includes_pending_additions():
    """Final state, not on-disk state — additions have not been saved yet."""
    dialog = _dialog(original=["dist/"], additions=["*.log"])
    assert dialog._current_pattern_state() == {"dist/", "*.log"}


# ── the merge ───────────────────────────────────────────────────────────

def test_a_genuinely_new_pattern_is_added():
    dialog = _dialog(original=["dist/"])
    assert dialog._inject_patterns_list(["*.log"]) == 1
    assert "*.log" in dialog._additions


def test_an_exact_duplicate_is_not_added():
    dialog = _dialog(original=["dist/"])
    assert dialog._inject_patterns_list(["dist/"]) == 0
    assert dialog._additions == []


def test_a_trailing_slash_difference_is_not_a_new_pattern():
    """`__pycache__` and `__pycache__/` are one rule, not two."""
    dialog = _dialog(original=["__pycache__/"])
    assert dialog._inject_patterns_list(["__pycache__"]) == 0
    dialog = _dialog(original=["__pycache__"])
    assert dialog._inject_patterns_list(["__pycache__/"]) == 0


def test_a_path_scoped_pattern_is_suppressed_by_a_broader_one():
    """The AI-noise case.

    A gitignore pattern with no `/` matches at every depth, so once
    `__pycache__/` is ignored, `src/__pycache__/` is a line that changes
    nothing — and suggestions used to accumulate several of them.
    """
    dialog = _dialog(original=["__pycache__/"])
    assert dialog._inject_patterns_list(
        ["src/__pycache__/", "tests/__pycache__/"]) == 0
    assert dialog._additions == []


def test_a_path_scoped_pattern_survives_when_nothing_broader_exists():
    """The suppression must not swallow a rule that really is needed."""
    dialog = _dialog(original=["dist/"])
    assert dialog._inject_patterns_list(["src/generated/"]) == 1


def test_an_in_flight_addition_blocks_a_later_duplicate():
    """Seeded from pending additions too, so a suggestion cannot re-add
    something the user typed manually a moment ago."""
    dialog = _dialog(original=[], additions=["*.log"])
    assert dialog._inject_patterns_list(["*.log"]) == 0


def test_duplicates_within_one_suggestion_are_collapsed():
    dialog = _dialog(original=[])
    assert dialog._inject_patterns_list(["*.log", "*.log/", "*.log"]) == 1


def test_new_patterns_are_labelled_in_the_pending_diff():
    """So the review panel says where the lines came from."""
    dialog = _dialog(original=[])
    dialog._inject_patterns_list(["*.log"])
    assert dialog._additions[0] == "# AI suggested patterns"


def test_the_label_is_added_only_once():
    dialog = _dialog(original=[])
    dialog._inject_patterns_list(["*.log"])
    dialog._inject_patterns_list(["*.tmp"])
    assert dialog._additions.count("# AI suggested patterns") == 1


def test_nothing_is_labelled_when_nothing_is_new():
    """An empty suggestion must not leave a stray header behind."""
    dialog = _dialog(original=["*.log"])
    dialog._inject_patterns_list(["*.log"])
    assert dialog._additions == []


# ── pending removals are reverted, not duplicated ───────────────────────

def test_a_suggestion_reverts_a_pending_removal_instead_of_re_adding():
    """The user marked `dist/` for removal, then a suggestion wants it kept.

    Appending it again would leave the file with the line removed from one
    place and added in another — a no-op diff that looks like a change.
    """
    dialog = _dialog(original=["dist/"], removed=[0])
    added = dialog._inject_patterns_list(["dist/"])
    assert added == 0
    assert dialog._removed_indices == set()
    assert dialog._additions == []


def test_reverting_a_removal_matches_across_trailing_slashes():
    dialog = _dialog(original=["dist"], removed=[0])
    assert dialog._inject_patterns_list(["dist/"]) == 0
    assert dialog._removed_indices == set()


def test_an_unrelated_removal_is_left_alone():
    dialog = _dialog(original=["dist/", "build/"], removed=[1])
    dialog._inject_patterns_list(["*.log"])
    assert dialog._removed_indices == {1}, "build/ was not in the suggestion"


# ── the merge never edits the file ──────────────────────────────────────

def test_merging_touches_only_pending_state(tmp_path):
    """Everything here is staged; the write happens on Save.

    Worth pinning because the dialog holds the original lines in memory and
    an accidental write during a merge would bypass the pending-changes
    review entirely.
    """
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist/\n", encoding="utf-8")
    dialog = _dialog(original=["dist/"])
    dialog._path = str(tmp_path)

    dialog._inject_patterns_list(["*.log", "src/__pycache__/"])

    assert gitignore.read_text(encoding="utf-8") == "dist/\n"


# ── the template-button reflow ──────────────────────────────────────────
# The shipped defect: a fixed six columns of variable-width buttons inside a
# hard-coded 640px window put "+ .NET / Visual Studio" entirely past the right
# edge — invisible, with no way to discover the template existed. Found by the
# geometry oracle (docs/VERIFICATION.md), fixed by measuring the column count.
#
# Stubbed rather than realised: the reflow only reads winfo_reqwidth() and
# calls grid(), so a Toplevel would be testing Tk rather than the arithmetic
# and the re-entrancy guard, which are the parts that can be wrong.


class _StubButton:
    def __init__(self, width: int) -> None:
        self._w = width
        self.grid_calls: list = []

    def winfo_reqwidth(self) -> int:
        return self._w

    def grid(self, **kw) -> None:
        self.grid_calls.append(kw)


class _StubWrap:
    def __init__(self, width: int) -> None:
        self._w = width

    def winfo_width(self) -> int:
        return self._w


def _reflow_dialog(widths, wrap_width=604):
    dialog = object.__new__(GitignoreDialog)
    dialog._tmpl_btns = [_StubButton(w) for w in widths]
    dialog._tmpl_wrap = _StubWrap(wrap_width)
    dialog._tmpl_last_width = -1
    return dialog


def _placements(dialog):
    """(row, column) per button, in button order."""
    return [(b.grid_calls[-1]["row"], b.grid_calls[-1]["column"])
            for b in dialog._tmpl_btns]


def test_the_reflow_lays_out_every_button():
    """No button may be dropped — one of them being unreachable is the bug."""
    dialog = _reflow_dialog([100] * 11)
    dialog._reflow_template_buttons()
    assert all(b.grid_calls for b in dialog._tmpl_btns)


def test_the_reflow_is_row_major():
    dialog = _reflow_dialog([100] * 6, wrap_width=320)   # 3 per row at 100px
    dialog._reflow_template_buttons()
    rows = [r for r, _ in _placements(dialog)]
    assert rows == sorted(rows), "buttons must fill left-to-right, top-to-bottom"


def test_a_narrow_frame_uses_fewer_columns_than_a_wide_one():
    narrow = _reflow_dialog([100] * 11, wrap_width=320)
    narrow._reflow_template_buttons()
    wide = _reflow_dialog([100] * 11, wrap_width=2000)
    wide._reflow_template_buttons()
    cols_narrow = max(c for _, c in _placements(narrow)) + 1
    cols_wide = max(c for _, c in _placements(wide)) + 1
    assert cols_narrow < cols_wide


def test_nothing_is_placed_past_the_available_width():
    """The whole point: the laid-out grid must fit the room it was given."""
    widths = [188, 78, 88, 66, 99, 150, 89, 118, 82, 92, 99]
    dialog = _reflow_dialog(widths, wrap_width=604)
    dialog._reflow_template_buttons()
    cols = max(c for _, c in _placements(dialog)) + 1
    col_w = [0] * cols
    for i, w in enumerate(widths):
        col_w[i % cols] = max(col_w[i % cols], w)
    total = sum(col_w) + dialog._TMPL_GAP_PX * (cols - 1)
    assert total <= 604, f"grid needs {total}px in 604px of room"


def test_a_repeat_at_the_same_width_does_not_re_grid():
    """Re-gridding fires <Configure> again; without the guard it re-enters."""
    dialog = _reflow_dialog([100] * 4)
    dialog._reflow_template_buttons()
    before = [len(b.grid_calls) for b in dialog._tmpl_btns]
    dialog._reflow_template_buttons()
    assert [len(b.grid_calls) for b in dialog._tmpl_btns] == before


def test_a_changed_width_does_re_grid():
    """The guard must not be so eager that a real resize is ignored."""
    dialog = _reflow_dialog([100] * 4, wrap_width=320)
    dialog._reflow_template_buttons()
    before = [len(b.grid_calls) for b in dialog._tmpl_btns]
    dialog._tmpl_wrap = _StubWrap(2000)
    dialog._reflow_template_buttons()
    assert all(n > b for n, b in
               zip([len(x.grid_calls) for x in dialog._tmpl_btns], before))


def test_an_unlaid_out_frame_falls_back_to_the_minimum_width():
    """Before the first <Configure> the frame reports 1px.

    Laying out against that would put every button on its own row; laying out
    against nothing at all would leave them ungridded and invisible. The
    fallback is the narrowest width the dialog claims to support.
    """
    dialog = _reflow_dialog([100] * 11, wrap_width=1)
    dialog._reflow_template_buttons()
    assert all(b.grid_calls for b in dialog._tmpl_btns)
    assert dialog._tmpl_last_width == GitignoreDialog._TMPL_FALLBACK_W


def test_the_reflow_survives_being_called_before_the_buttons_exist():
    """<Configure> can arrive during construction."""
    dialog = object.__new__(GitignoreDialog)
    dialog._tmpl_btns = []
    dialog._tmpl_wrap = _StubWrap(600)
    dialog._tmpl_last_width = -1
    dialog._reflow_template_buttons()          # must not raise
