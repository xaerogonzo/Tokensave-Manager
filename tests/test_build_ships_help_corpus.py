"""tests/test_build_ships_help_corpus.py — the build must ship what Help reads.

`helpers/help_docs.HELP_DOCUMENTS` is read at RUN TIME from `_BASE_DIR`, which
in a Nuitka build is the folder beside the exe. A document `build.ps1` forgets
to copy is therefore a help topic that silently disappears from the shipped
application while every test on this machine still passes, because a source
checkout has the file sitting right there.

WHY NOT JUST COPY `docs\\` RECURSIVELY. It was the obvious fix and it is the
wrong one: `docs/` also holds ROADMAP, VERIFICATION, the upstream-issue files
and the Windows findings notes, none of which belong in a user's download. So
the list stays explicit and deliberate, and this test makes it
self-maintaining -- adding a help document without shipping it fails CI, which
is the property the recursive copy was really reaching for.

This is the failure mode in `templates/gotchas/moving-content-moves-its-guards.md`:
a hand-kept list only grows by hand.
"""
from __future__ import annotations

import os
import re

from helpers import help_docs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILD = os.path.join(_ROOT, "build.ps1")


def _copied_names() -> set:
    """Every markdown filename `build.ps1` copies into `dist\\`.

    Parsed from the two loops that do it -- the root-markdown `foreach` and
    the `$docsInclude` array -- rather than by running PowerShell, so this
    works on the Linux CI runner too.
    """
    with open(_BUILD, encoding="utf-8-sig") as handle:
        script = handle.read()

    names = set()
    for block in re.findall(r"foreach \(\$md in @\(([^)]*)\)\)", script):
        names.update(re.findall(r'"([^"]+\.md)"', block))
    for block in re.findall(r"\$docsInclude\s*=\s*@\(([^)]*)\)", script,
                            re.S):
        names.update(re.findall(r'"([^"]+\.md)"', block))
    return names


def test_the_parser_found_something():
    """A regex that matches nothing would make every assertion below vacuous."""
    names = _copied_names()
    assert len(names) >= 5, names
    assert "CHANGELOG.md" in names


def test_every_help_document_is_shipped():
    copied = _copied_names()
    missing = [d for d in help_docs.HELP_DOCUMENTS
               if os.path.basename(d) not in copied]
    assert missing == [], (
        "build.ps1 does not copy %s -- the Help tab reads these at run time, "
        "so a build without them loses those topics silently" % missing)


def test_readme_is_shipped_because_help_reads_it():
    """Called out on its own: README moved from "nice to include" to load
    bearing when it became the largest help document."""
    assert "README.md" in _copied_names()


def test_a_docs_corpus_file_lands_under_docs():
    """`HELP_DOCUMENTS` spells the path Help will look for, and `build.ps1`
    copies `docs\\<name>` into `dist\\docs\\`. This pins the two shapes
    agreeing: a corpus entry under `docs/` must be copied by the docs loop,
    not the root one."""
    with open(_BUILD, encoding="utf-8-sig") as handle:
        script = handle.read()
    docs_block = re.search(r"\$docsInclude\s*=\s*@\(([^)]*)\)", script, re.S)
    assert docs_block is not None
    in_docs_loop = set(re.findall(r'"([^"]+\.md)"', docs_block.group(1)))
    for document in help_docs.HELP_DOCUMENTS:
        head, name = os.path.split(document)
        if head:
            assert name in in_docs_loop, (
                "%s lives under docs/ but is not in $docsInclude" % document)
