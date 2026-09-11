"""tests/test_architecture_doc.py — the module map cannot go stale silently.

`docs/ARCHITECTURE.md` became the canonical per-module map on 2026-09-11, when
`BASIC_INSTRUCTIONS.md`'s own copy of the tree was deleted: it had drifted to
naming **66 of 187** modules while costing **16,722 B on every message**, because
a hand-kept list only grows by hand.

Moving that list did not fix the failure mode — it moved it. Within a day the
new home was itself missing 14 modules and stating three counts that were all
wrong (96 helpers against 108 on disk, 47 dialogs against 53, and a legacy
summary still claiming 22 dialogs and 4 controllers). These two tests are what
actually fixes it: correcting the numbers by hand only resets a clock.

Deliberately NOT asserted: that each entry's *description* is accurate. No test
can check that, and pretending otherwise would be the degeneracy
`verifying-a-gui.md` warns about — the invariant here is "is named", never "is
described well".
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARCH = os.path.join(_ROOT, "docs", "ARCHITECTURE.md")
_PACKAGES = ("helpers", "dialogs", "controllers")


def _doc() -> str:
    with open(_ARCH, encoding="utf-8") as handle:
        return handle.read()


def _repository_layout(text: str) -> str:
    """Just the `## Repository Layout` section, not the whole document.

    Scoping matters: a bare filename search over the whole file also matches
    the *test* listing and ordinary prose, which is how a first attempt at this
    check reported 106 of 107 covered when the real figure was 175 of 187.
    """
    start = text.index("## Repository Layout")
    nxt = text.index("\n## ", start + 1)
    return text[start:nxt]


def _modules_on_disk(package: str) -> list:
    path = os.path.join(_ROOT, "src", package)
    return sorted(f for f in os.listdir(path)
                  if f.endswith(".py") and f != "__init__.py")


def test_every_module_is_named_in_the_repository_layout():
    """A module nobody is pointed at is a module nobody finds."""
    layout = _repository_layout(_doc())
    missing = []
    for package in _PACKAGES:
        missing += ["src/%s/%s" % (package, name)
                    for name in _modules_on_disk(package)
                    if name not in layout]
    assert missing == [], (
        "these modules exist but are not named in docs/ARCHITECTURE.md's "
        "`## Repository Layout`, which is the canonical map since "
        "BASIC_INSTRUCTIONS.md stopped carrying one: %s" % missing)


def test_the_stated_module_counts_match_the_filesystem():
    """The counts in the section headers are claims, so they are checked.

    Each of these was wrong when the test was written, and one of them twice
    over: the tree header said 47 dialogs while a summary block further down
    still said 22.
    """
    text = _doc()
    claims = {
        "helpers": re.findall(r"helpers/\s+(\d+) modules", text),
        "dialogs": re.findall(r"dialogs/\s+(\d+) dialog", text)
                   + re.findall(r"dialogs/\*\s+— (\d+) ", text),
        "controllers": re.findall(r"controllers/\*\s+— (\d+) ", text),
    }
    wrong = []
    for package, stated in claims.items():
        actual = len(_modules_on_disk(package))
        assert stated, "no count stated for src/%s — did a header change?" % package
        for value in stated:
            if int(value) != actual:
                wrong.append("%s: doc says %s, disk has %d" % (package, value, actual))
    assert wrong == [], (
        "docs/ARCHITECTURE.md states module counts that no longer match "
        "src/: %s" % wrong)
