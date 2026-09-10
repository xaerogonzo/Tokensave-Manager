"""Guards on `templates/project-baseline.md`, the file every project loads.

The baseline is `@include`d by every wired project, so a byte added here is a
byte multiplied by the fleet. These tests exist because the obvious way to
"improve" it is to add to it, and the obvious way to make the gotchas library
more discoverable is to include it — which would undo the shrink that created
the library in the first place.
"""

import os
import re

import pytest

from helpers.instructions_posture import scan_text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO, "templates")
BASELINE = os.path.join(TEMPLATES, "project-baseline.md")
GOTCHAS = os.path.join(TEMPLATES, "gotchas")


def read(path):
    with open(path, encoding="utf-8-sig") as handle:
        return handle.read()


def test_the_baseline_never_includes_a_gotcha_file():
    """The load-bearing invariant of the whole gotchas design.

    The index is the cheap part; the files are the expensive part. Include one
    and the optimisation recreates the problem it was written to fix: the
    baseline shrinks, the gotchas get pulled in, the baseline grows.
    """
    directives, _indented, _fence = scan_text(read(BASELINE))
    included = [raw for _lineno, raw in directives]
    assert included == [], (
        "project-baseline.md must not @include anything, least of all a gotcha "
        "file. Found: %s" % included)


def test_every_gotcha_file_is_reachable_from_the_index():
    """A file nobody is pointed at is a file nobody reads.

    The index is how these are found at all, since they are deliberately not
    loaded. An unindexed file is invisible rather than merely cheap.
    """
    baseline = read(BASELINE)
    missing = [name for name in sorted(os.listdir(GOTCHAS))
               if name.endswith(".md") and name not in baseline]
    assert missing == [], (
        "gotcha files with no row in the baseline index: %s" % missing)


def test_the_index_points_only_at_files_that_exist():
    """A dangling row is worse than no row: it reads as coverage."""
    baseline = read(BASELINE)
    referenced = set(re.findall(r"`gotchas/([A-Za-z0-9_.-]+\.md)`", baseline))
    absent = sorted(n for n in referenced
                    if not os.path.isfile(os.path.join(GOTCHAS, n)))
    assert absent == [], "index rows naming files that do not exist: %s" % absent


def test_the_baseline_stays_within_its_review_budget():
    """A ratchet, so growth is a visible act rather than a drift.

    6,623 B after the Phase 1b shrink and the Phase 3 index rows. (The 
    figure recorded here after 1b said 6,348; the file was 6,260. Measure it, 
    do not carry it forward from a report.) The ceiling is deliberately close to
    that: this file is multiplied by every project that resolves it, so an
    addition should have to argue for itself. Raising the number is a fine
    thing to do on purpose — the point is that it cannot happen by accident.
    """
    size = len(read(BASELINE).encode("utf-8"))
    assert size <= 7000, (
        "project-baseline.md is %d B, over the 7,000 B review budget. It loads "
        "in every wired project on every message. Consider moving the new "
        "material to templates/gotchas/ with one index row, which is the "
        "pattern the Nuitka and Tkinter sections already followed." % size)


# A test asserting that each gotcha file contains the word "silent" was written
# here and deleted the same minute. `customtkinter.md` states the bar perfectly
# well as "the app still renders, and the defect is invisible" — and grepping
# prose for a keyword is precisely the degeneracy `verifying-a-gui.md` warns
# about two files away: the invariant is "has a documented contract", never "has
# the expected string in it". The admission bar lives in the baseline's gotchas
# section, which is the right place for a rule addressed to a human.
