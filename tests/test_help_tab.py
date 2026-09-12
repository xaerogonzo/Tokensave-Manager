"""tests/test_help_tab.py — HelpTabController (Tk-marked).

The safety net the pre-markdown version carried is kept: every topic in the
list must render non-empty content into the pane. It just runs over 51 topics
read from the corpus now instead of 25 hard-coded Python renderers.

What is new guards the seam between the two pure modules and the widget --
notably the tag palette, where a mismatch is invisible: a span carrying a tag
the Text never configured renders as unstyled body text and nothing reports it.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

from controllers.help_tab import HelpTabController

pytestmark = pytest.mark.tk


def _build_controller(tk_root, mock_config):
    """Build the tab. Does NOT touch `template_dir`.

    The pre-markdown helper set it to "" unconditionally, which silently
    overwrote whatever a placeholder test had just configured.
    """
    notebook = ttk.Notebook(tk_root)
    if not hasattr(mock_config, "template_dir"):
        mock_config.template_dir = ""
    return HelpTabController(notebook, mock_config)


def test_constructs_and_renders_the_first_topic(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    assert ctl._help_txt.get("1.0", "end").strip()


def test_the_listbox_matches_the_corpus(tk_root, mock_config):
    """Every topic, plus one non-selectable header per document."""
    from helpers import help_docs

    ctl = _build_controller(tk_root, mock_config)
    found = help_docs.topics()
    documents = {t.document for t in found}
    assert ctl._help_lb.size() == len(found) + len(documents)
    assert len([k for k in ctl._rows if k]) == len(found)


def test_a_document_header_selects_nothing(tk_root, mock_config):
    """Headers exist to group a 51-item list; clicking one must be inert."""
    ctl = _build_controller(tk_root, mock_config)
    header = ctl._rows.index("")
    before = ctl._help_txt.get("1.0", "end")
    ctl._help_lb.selection_clear(0, tk.END)
    ctl._help_lb.selection_set(header)
    ctl._on_help_select()
    assert ctl._help_txt.get("1.0", "end") == before


def test_every_topic_renders_non_empty_content(tk_root, mock_config):
    """The guarantee carried over from the Python-topic version."""
    from helpers import help_docs

    ctl = _build_controller(tk_root, mock_config)
    for item in help_docs.topics():
        ctl._show(item.key)
        body = ctl._help_txt.get("1.0", "end").strip()
        assert body, "topic %r rendered nothing" % item.key


def test_the_tag_palette_covers_everything_the_renderer_emits(
        tk_root, mock_config):
    """A span with an unconfigured tag renders unstyled, and silently.

    Asserted against `markdown_tk.TAGS` rather than by reading the two lists
    side by side, because they live in different files and drift quietly.
    """
    from helpers import markdown_tk

    ctl = _build_controller(tk_root, mock_config)
    configured = set(ctl._help_txt.tag_names())
    missing = set(markdown_tk.TAGS) - configured
    assert missing == set(), "tags emitted but never configured: %s" % missing


def test_placeholders_are_resolved_before_rendering(tk_root, mock_config):
    """The file-locations topic is the reason substitution exists."""
    mock_config.template_dir = "D:/fixture-templates"
    ctl = _build_controller(tk_root, mock_config)
    ctl._show("file-locations")
    shown = ctl._help_txt.get("1.0", "end")
    assert "D:/fixture-templates" in shown
    assert "{{template_dir}}" not in shown


def test_a_placeholder_without_a_value_reads_unknown(tk_root, mock_config):
    mock_config.template_dir = ""
    ctl = _build_controller(tk_root, mock_config)
    ctl._show("file-locations")
    assert "unknown" in ctl._help_txt.get("1.0", "end")


def test_searching_narrows_the_list(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    everything = ctl._help_lb.size()
    ctl._query.set("strict_tree")
    assert 0 < ctl._help_lb.size() < everything


def test_clearing_the_search_restores_the_full_list(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    everything = ctl._help_lb.size()
    ctl._query.set("strict_tree")
    ctl._query.set("")
    assert ctl._help_lb.size() == everything


def test_a_search_with_no_hits_says_so_rather_than_emptying(
        tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    ctl._query.set("zzzz-no-such-term-zzzz")
    assert ctl._help_lb.size() == 1
    assert "nothing matches" in ctl._help_lb.get(0)


def test_a_corpus_problem_gets_its_own_row(tk_root, mock_config, monkeypatch):
    """A missing document must not just shorten the list.

    The row is how the reader can tell "this document had nothing to say"
    from "this document failed to load".
    """
    from helpers import help_docs

    ctl = _build_controller(tk_root, mock_config)
    monkeypatch.setattr(
        help_docs, "scan",
        lambda: ((), (help_docs.Problem("GONE.md", "could not be read"),)))
    ctl._refresh_list()
    assert ctl._help_lb.size() == 1
    assert "problem" in ctl._help_lb.get(0)

    ctl._help_lb.selection_set(0)
    ctl._on_help_select()
    shown = ctl._help_txt.get("1.0", "end")
    assert "GONE.md" in shown and "could not be read" in shown


def test_selecting_a_topic_shows_it(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    # The row index is the LISTBOX's, not the corpus's: document headers sit
    # between the topics, so the two numbering schemes stopped agreeing.
    index = ctl._rows.index("window-tray")
    ctl._help_lb.selection_clear(0, tk.END)
    ctl._help_lb.selection_set(index)
    ctl._on_help_select()
    assert "Window & Tray" in ctl._help_txt.get("1.0", "end")


def test_an_unknown_topic_reports_instead_of_raising(tk_root, mock_config):
    ctl = _build_controller(tk_root, mock_config)
    ctl._show("no-such-topic")
    assert "could not be loaded" in ctl._help_txt.get("1.0", "end")
