"""tests/test_help_covers_the_ui.py — every surface the app offers is documented.

THE LIST IS DERIVED FROM THE CODE, NEVER HAND-KEPT. That is the whole point.
The Help tab went stale by covering 9 of 19 features, and a guard carrying its
own list of feature names would have gone stale in exactly the same way — the
failure recorded in `templates/gotchas/moving-content-moves-its-guards.md`.

So this reads the notebook tab labels and the right-click menu labels straight
out of the controllers and asserts each appears somewhere in the help corpus.
Add a menu entry and this fails until a document mentions it.

It is a COVERAGE floor, not a quality check: "mentioned somewhere" is a low bar
on purpose, because the alternative is a test with opinions about prose.

TWO NORMALISERS, NOT ONE. `_label_core` drops a parenthetical suffix, because
"Doc Updates… (CHANGELOG + README)" is one action named *Doc Updates* with an
inline hint — requiring the documentation to repeat the hint verbatim would
test the wording rather than the coverage. `_flatten` must NOT do that, because
running it over the corpus would discard everything after the first bracket in
60 KB of prose. Sharing one function between a short label and a whole document
looked tidy and deleted most of the corpus.
"""
from __future__ import annotations

import io
import os
import re

from helpers import help_docs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Labels whose words are too generic for "appears in the corpus" to mean
#: anything. Each is reachable only inside a submenu that IS checked, so the
#: submenu's own coverage is what carries them.
_TOO_GENERIC = {
    "Init", "Sync", "Status", "Analyze", "Clear selection",
    "Open Folder", "Open in Editor", "Copy Path", "Commit", "Open",
    "Merge into current branch",
}


def _flatten(text: str) -> str:
    """Collapse punctuation and whitespace so a match survives a line wrap."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w &./-]+", " ", text)).strip().lower()


def _label_core(label: str) -> str:
    """A menu label's NAME: no glyphs, no padding, no parenthetical hint."""
    return _flatten(label.split("(", 1)[0])


def _corpus_text() -> str:
    return _flatten("\n".join(help_docs.topic_markdown(t.key)
                              for t in help_docs.topics()))


def _labels_from(relative: str) -> set:
    """Every literal menu / tab label in a controller."""
    with io.open(os.path.join(_ROOT, relative), encoding="utf-8") as handle:
        source = handle.read()
    found = set()
    for pattern in (r'add_command\(label=f?"([^"]*)"',
                    r'add_cascade\(label="([^"]*)"',
                    r'notebook\.add\([^,]*, text="([^"]*)"'):
        found.update(re.findall(pattern, source))
    return found


# ── Sanity: a regex that matches nothing makes everything below vacuous ──

def test_the_extractor_found_the_menu():
    labels = _labels_from(os.path.join("src", "controllers", "projects_tab.py"))
    assert len(labels) >= 30, sorted(labels)
    assert any("Housekeeping" in x for x in labels)


def test_the_corpus_normaliser_keeps_the_corpus():
    """The bug this file already had once: `_flatten` must not truncate."""
    corpus = _corpus_text()
    assert len(corpus) > 50_000, len(corpus)
    assert "housekeeping" in corpus


# ── Coverage ─────────────────────────────────────────────────────────────

def test_every_notebook_tab_is_documented():
    corpus = _corpus_text()
    tabs = set()
    for name in ("projects_tab", "git_tab", "ask_tab", "snippets", "help_tab",
                 "tasks_tab", "settings_tab"):
        tabs |= _labels_from(os.path.join("src", "controllers", "%s.py" % name))
    wanted = {"Projects", "Git", "Ask", "Reference", "Help", "Tasks",
              "Settings"}
    present = {_label_core(x) for x in tabs}
    undocumented = sorted(t for t in wanted
                          if t.lower() in {p for p in present}
                          and t.lower() not in corpus)
    assert undocumented == [], (
        "these tabs exist but no help topic names them: %s" % undocumented)


def test_every_right_click_action_is_documented():
    """Add an entry to the menu and this fails until a document mentions it."""
    corpus = _corpus_text()
    generic = {_flatten(x) for x in _TOO_GENERIC}
    undocumented = []
    for label in sorted(_labels_from(os.path.join("src", "controllers",
                                                  "projects_tab.py"))):
        if "{" in label:                      # f-string, multi-select counts
            continue
        core = _label_core(label)
        if not core or core in generic:
            continue
        if core not in corpus:
            undocumented.append(core)
    assert undocumented == [], (
        "these right-click actions are not mentioned in any help topic: %s"
        % undocumented)
