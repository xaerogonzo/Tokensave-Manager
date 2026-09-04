"""tests/test_no_shadowed_tests.py — two `def`s, one name, one silent loss.

Python does not complain when a module defines the same function twice: the
second binding simply replaces the first. In a test file that means the first
test **stops existing** — pytest never collects it, it never passes, it never
fails, and the suite's own count goes down by one without anything going red.
A dead assertion is worse than a missing one, because the file still reads as
though the case is covered.

This is not hypothetical. Roadmap-17's per-test discovery found exactly one
such pair on its first run over this repository:
`tests/test_cli.py` defined `test_status_reports_an_unreadable_repo_as_
unknown_not_clean` at line 776 and again at line 843, and the first — which
uniquely asserted `changed_files is None` and `ahead is None` — had never once
executed.

**Why nothing caught it.** pyflakes reports it correctly (`redefinition of
unused '...' from line 776`), but the CI `check` job runs `python -m pyflakes
src/`, and `tests/` has never been linted. Turning pyflakes loose on the whole
test tree surfaces 83 findings, 78 of which are unused imports and unused
locals — noise that would have to be cleaned before the signal could be gated
on. This guard takes the one class that silently disables a test and leaves
the noise alone.

It is built on `helpers.test_discovery.list_test_cases`, which is the same
walker the VS Code Test Explorer discovers from. That is deliberate: a
collision here is also the case where a run result cannot be attributed to a
single test item, so the guard and the Explorer's ambiguity rule are checking
one property, not two similar ones.

Dual-mode, like the other `test_no_*` guards: runs under pytest, and
standalone via `python tests/test_no_shadowed_tests.py` so CI can run it in an
import-free job.
"""
from __future__ import annotations

import collections
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from helpers.test_discovery import list_test_cases  # noqa: E402


def _shadowed() -> list:
    """Every nodeid defined more than once, with the lines involved.

    Returns a list of `(nodeid, [line, ...])`, sorted, so the report is
    stable and names both ends of the collision rather than only the survivor.
    """
    cases = list_test_cases(str(_ROOT))
    by_id = collections.defaultdict(list)
    for case in cases:
        by_id[case.nodeid].append(case.line)
    return sorted((nodeid, sorted(lines))
                  for nodeid, lines in by_id.items() if len(lines) > 1)


def test_no_test_is_shadowed_by_a_later_definition():
    """A duplicated test name means the earlier test does not run at all."""
    bad = _shadowed()
    assert not bad, (
        "these test names are defined more than once; every definition but "
        "the last is dead code that pytest never collects:\n"
        + "\n".join(f"  {nodeid} at lines {lines}" for nodeid, lines in bad))


def test_the_guard_scans_a_population_it_reports():
    """A scan that finds nothing must prove it looked at something.

    The lesson is this project's own: an earlier import-time guard skipped 27%
    of `src/` and reported a clean result for the part it did read. "No
    findings" over an unstated population is not a result, so the count is
    asserted rather than trusted.
    """
    cases = list_test_cases(str(_ROOT))
    assert len(cases) > 1000, (
        f"only {len(cases)} test definitions discovered under {_ROOT}; "
        "this suite has thousands, so the walker is not reaching them")


if __name__ == "__main__":
    shadowed = _shadowed()
    if shadowed:
        print("FAIL: test names defined more than once (all but the last "
              "definition are never collected):")
        for nodeid, lines in shadowed:
            print(f"  {nodeid} at lines {lines}")
        raise SystemExit(1)
    total = len(list_test_cases(str(_ROOT)))
    print(f"OK: {total} test definitions, no shadowed names.")
