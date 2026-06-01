"""tests/test_recommend_selection.py — test-gap panel "✓ Recommend" logic.

`_recommended_test_selection` drives which rows the Recommend quick-select checks.
It must pick only the high-ROI files (pure/subprocess helpers) and skip Tk-dialog
/ unclassified ones. Pure function — no Tk root needed (importing the controller
module is import-safe; it never builds widgets at import time).
"""
from __future__ import annotations

from controllers.git_tab import _recommended_test_selection
from helpers.test_gap_report import SuggestedTest


def _sug(template: str) -> SuggestedTest:
    return SuggestedTest(source_path="s.py", rel_path="src/s.py",
                         template=template, test_exists=False)


def test_recommends_only_automatable_templates():
    sugg = [
        _sug("pure_helper"),
        _sug("subprocess_helper"),
        _sug("dialog_tk"),       # skipped
        _sug("blank"),           # skipped
    ]
    assert _recommended_test_selection(sugg) == [True, True, False, False]


def test_empty_list():
    assert _recommended_test_selection([]) == []


def test_all_dialog_tk_recommends_nothing():
    assert _recommended_test_selection([_sug("dialog_tk"), _sug("dialog_tk")]) == [False, False]
