"""tests/test_help_tab.py — HelpTabController (Tk-marked).

Safety net for the Roadmap-8 god-file split: every help section must
render non-empty content into the text pane. Written against the
pre-split controller; must pass unchanged after the topic methods move
to the help_topics_* modules.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

from controllers.help_tab import HelpTabController

pytestmark = pytest.mark.tk


def _build_controller(tk_root, mock_config):
    notebook = ttk.Notebook(tk_root)
    # _help_file_locations reads cfg.template_dir at display time.
    mock_config.template_dir = ""
    return HelpTabController(notebook, mock_config)


def test_constructs_and_renders_first_section(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    assert ctl._help_txt.get("1.0", "end").strip()


def test_section_count_matches_listbox(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    assert ctl._help_lb.size() == len(ctl._help_sections)


def test_every_section_renders_nonempty(tk_root, mock_config):
    """Drive each section renderer exactly as _on_help_select would."""
    ctl = _build_controller(tk_root, mock_config)
    for title, fn in ctl._help_sections:
        fn()
        content = ctl._help_txt.get("1.0", "end").strip()
        assert content, f"help section {title!r} rendered empty"


def test_shadow_links_blurb_is_accurate(tk_root, mock_config):
    """R9-SL6: the Shadow Links help line must describe hardlink-based
    extension indexing, NOT the old (wrong) 'symlink mirrors' wording."""
    ctl = _build_controller(tk_root, mock_config)
    section = dict(ctl._help_sections)["  Right-click Menu"]
    section()
    text = ctl._help_txt.get("1.0", "end").lower()
    assert "shadow links" in text
    assert "hardlink" in text and "index" in text
    assert "symlink" not in text
