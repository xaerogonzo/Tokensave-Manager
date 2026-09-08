"""tests/test_integration_issue_doc_match.py — upstream-issue doc matching.

`_find_issue_doc` decides whether an upstream issue already has a hand-written
doc in ``docs/upstream-issues/``. Two lifecycle actions hang off that answer:

  * ``--fix`` writes an AUTO_GENERATED stub when it finds no doc — so a false
    negative CLOBBERS a hand-authored file with a TODO placeholder;
  * ``_auto_archive_resolved`` only archives a doc it can find — so a false
    negative leaves a resolved issue's doc flagged ⚠ forever.

The original pattern only accepted ``ISSUE: #NNN`` / ``issue #NNN``. Docs that
cited their issue purely as a URL (the natural thing to write in a STATUS line)
or as ``issue NNN`` without the hash were invisible to both paths. These tests
pin every citation form that appears in the real docs directory.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "check_tokensave_integration.py"


def _load_script_module():
    """Import the checker by path — scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "_check_tokensave_integration", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def checker(tmp_path, monkeypatch):
    """The checker module with its issues dir pointed at a temp directory."""
    mod = _load_script_module()
    issues = tmp_path / "upstream-issues"
    issues.mkdir()
    (issues / "archived").mkdir()
    monkeypatch.setattr(mod, "_ISSUES", issues)
    return mod, issues


# ── citation forms that must be recognised ────────────────────────────────────

@pytest.mark.parametrize("body,label", [
    ("ISSUE: #419\nsome analysis\n",                      "frontmatter marker"),
    ("Tracked as issue #419 upstream.\n",                 "inline with hash"),
    ("Still OPEN upstream; issue 419 is registered.\n",   "inline without hash"),
    ("STATUS: FILED — https://github.com/o/r/issues/419", "bare GitHub URL"),
])
def test_recognises_citation_form(checker, body, label):
    mod, issues = checker
    doc = issues / "some-hand-written-doc.md"
    doc.write_text(body, encoding="utf-8")
    assert mod._find_issue_doc(419) == doc, f"failed to match {label}"


def test_url_form_is_the_regression_case(checker):
    """The exact shape that shipped in tokensave-settings-write-on-readonly-query.md.

    A STATUS line citing only the URL. Before the fix this returned None, so
    --fix would have written issue-419.md over a real analysis document.
    """
    mod, issues = checker
    doc = issues / "tokensave-settings-write-on-readonly-query.md"
    doc.write_text(
        "<!--\n"
        "STATUS: FILED 2026-08-19 — "
        "https://github.com/aovestdipaperino/tokensave/issues/419\n"
        "-->\n\n# A real hand-written analysis\n",
        encoding="utf-8")
    assert mod._find_issue_doc(419) == doc


# ── things that must NOT match ────────────────────────────────────────────────

def test_unrelated_number_does_not_match(checker):
    mod, issues = checker
    (issues / "other.md").write_text("ISSUE: #123\n", encoding="utf-8")
    assert mod._find_issue_doc(419) is None


def test_archived_docs_are_not_matched(checker):
    """archived/ is out of scope — a re-opened issue should get a fresh doc."""
    mod, issues = checker
    (issues / "archived" / "old.md").write_text(
        "ISSUE: #419\n", encoding="utf-8")
    assert mod._find_issue_doc(419) is None


def test_substring_of_longer_number_does_not_match(checker):
    """`/issues/4190` must not satisfy a lookup for 419."""
    mod, issues = checker
    (issues / "other.md").write_text(
        "https://github.com/o/r/issues/4190\n", encoding="utf-8")
    assert mod._find_issue_doc(419) is None


def test_missing_issues_dir_returns_none(tmp_path, monkeypatch):
    mod = _load_script_module()
    monkeypatch.setattr(mod, "_ISSUES", tmp_path / "does-not-exist")
    assert mod._find_issue_doc(419) is None


# ── the real repo docs stay matchable ─────────────────────────────────────────

#: Docs whose citation form broke the original pattern, pinned by NAME rather
#: than by path. Archiving moves a file between two directories and changes
#: nothing about how it names its issue — an earlier version of this list
#: carried `archived/` prefixes and had to be edited every time an issue
#: closed, which is churn that teaches nothing. `_real_doc` resolves either
#: location.
CITATION_FORMS = [
    ("tokensave-worktree-index-resolution.md", 372, "issue #NNN"),
    ("tokensave-glob-matcher-coverage.md",     389, "issue NNN, no hash"),
    ("tokensave-settings-write-on-readonly-query.md", 419, "bare URL only"),
]


def _real_doc(name):
    """A doc from the real repo, active or archived. None if it is neither."""
    base = _REPO / "docs" / "upstream-issues"
    for candidate in (base / name, base / "archived" / name):
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.parametrize("name,number,form", CITATION_FORMS)
def test_real_repo_docs_cite_a_matchable_issue_number(name, number, form):
    """Every real doc must cite its issue in a form the pattern recognises.

    Deliberately checks the PATTERN against file content rather than calling
    `_find_issue_doc`, so it is independent of whether a doc has since been
    archived — archiving is a lifecycle move, not a change to how the doc
    names its issue. The lookup is by name across both directories so that
    independence is real rather than merely claimed; it was claimed here
    while the parameters carried hardcoded `archived/` prefixes, and the
    test duly broke the first time one of these was archived.

    #389 cites its issue as "issue 389" (no hash) and #419 only as a
    `.../issues/419` URL. Both were invisible to the original pattern, which
    is what this file exists to prevent recurring.
    """
    mod = _load_script_module()
    doc = _real_doc(name)
    assert doc is not None, f"{name} is in neither the active nor archived dir"
    found = {int(m.group(1)) for m in mod._ISSUE_DOC_RE.finditer(
        doc.read_text(encoding="utf-8", errors="replace"))}
    assert number in found, (
        f"{name} tracks issue #{number} as {form} but cites it in a form "
        f"_ISSUE_DOC_RE cannot see (found: {sorted(found) or 'nothing'})")


def test_every_active_doc_resolves_through_the_real_lookup():
    """Whatever is currently un-archived must be findable.

    Derived from the directory rather than naming an issue number, because
    naming one makes this a snapshot of which issues happened to be open on
    the day it was written — and the previous version, pinned to #419, failed
    the moment #419 was archived, reporting a lifecycle event as a defect.

    The stake is real: `--fix` writes an AUTO_GENERATED stub over any issue it
    cannot find a doc for, so a doc that exists but does not resolve gets
    clobbered by a TODO placeholder.
    """
    mod = _load_script_module()
    active = sorted(p for p in mod._ISSUES.glob("*.md"))
    assert active, "no active upstream-issue docs — nothing to check"

    unresolvable, checked = [], 0
    for doc in active:
        numbers = {int(m.group(1)) for m in mod._ISSUE_DOC_RE.finditer(
            doc.read_text(encoding="utf-8", errors="replace"))}
        if not numbers:
            continue          # a MOOT doc that never got an issue number
        checked += 1
        if not any(mod._find_issue_doc(n) == doc for n in numbers):
            unresolvable.append((doc.name, sorted(numbers)))
    assert not unresolvable, (
        "these active docs cite an issue number that does not resolve back to "
        "them, so `--fix` would stub over them: %r" % unresolvable)
    # Report the population, not just the verdict. Every active doc could hit
    # the `continue` above and this would pass having checked nothing — the
    # same shape as the guard that scanned 71% of src/ and never said so.
    assert checked >= 2, (
        f"only {checked} of {len(active)} active docs cited an issue number; "
        f"this assertion is close to vacuous")


def test_archived_issue_is_not_returned_by_lookup():
    """#389 is archived, so the active-dir lookup must NOT return it.

    Pins the archive boundary: were this to regress, `--fix` would try to
    re-archive an already-archived doc.
    """
    mod = _load_script_module()
    assert mod._find_issue_doc(389) is None


# ── Ownership vs citation ────────────────────────────────────────────────
#
# The regex cannot tell a doc's SUBJECT from an issue it merely cites, and
# these drafts cite freely: a duplicate search names the issues it ruled
# out, a "same class as #457" aside names a third. Only POSITION separates
# them, so ownership is read from the STATUS stanza and nothing else.
#
# The case that forced this: the #513 report keeps its duplicate search
# inside the SAME html comment as STATUS and cites #512 and #136 there. A
# whole-file scan resolved #512 to it, so `--fix` would have archived our
# report under someone else's packaging issue and then found nothing for
# #513, because the file had already moved.

_CITING_DOC = """<!--
STATUS: FILED 2026-09-08 as issue #513 — awaiting maintainer response.
  https://github.com/o/r/issues/513

DUPLICATE SEARCH:
  #512 Windows asset missing from the release — OPEN, filed by someone else.
       https://github.com/o/r/issues/512
  #136 the same packaging failure, previous occurrence — CLOSED.
-->

# The report body, which also mentions issue #512 in passing.
"""


def test_status_stanza_declares_ownership(checker):
    mod, issues = checker
    (issues / "report.md").write_text(_CITING_DOC, encoding="utf-8")
    assert mod._find_issue_doc(513).name == "report.md"


@pytest.mark.parametrize("cited", [512, 136])
def test_a_cited_issue_does_not_claim_the_doc(checker, cited):
    mod, issues = checker
    (issues / "report.md").write_text(_CITING_DOC, encoding="utf-8")
    assert mod._find_issue_doc(cited) is None


def test_a_doc_with_no_status_still_resolves_when_unambiguous(checker):
    """Hand-written docs predating the STATUS convention must still match."""
    mod, issues = checker
    (issues / "old.md").write_text(
        "Some analysis of issue #419 and nothing else.\n", encoding="utf-8")
    assert mod._find_issue_doc(419).name == "old.md"


def test_a_statusless_doc_naming_several_issues_is_ambiguous(checker):
    """None rather than a guess, on graph_trust's `insufficient` reasoning."""
    mod, issues = checker
    (issues / "old.md").write_text(
        "Compares issue #419 with issue #421 at length.\n", encoding="utf-8")
    assert mod._find_issue_doc(419) is None
    assert mod._find_issue_doc(421) is None


# ── Archiving preserves what resolved the issue ──────────────────────────

def test_archive_keeps_the_hand_written_status(checker):
    """The script knows it is closed and when it looked. It does NOT know
    what fixed it, and that is the half a reader needs."""
    mod, issues = checker
    (issues / "report.md").write_text(
        "<!--\nSTATUS: FIXED upstream in PR #519 (merged) — unreleased.\n"
        "  https://github.com/o/r/issues/513\n-->\n", encoding="utf-8")
    line = mod._auto_archive_resolved(513, "some title")
    assert line
    moved = issues / "archived" / "report.md"
    text = moved.read_text(encoding="utf-8")
    assert "STATUS: CLOSED" in text
    assert "PRIOR: FIXED upstream in PR #519 (merged) — unreleased." in text


def test_archive_replaces_a_placeholder_status_outright(checker):
    """A stub's STATUS carries nothing, so there is nothing to preserve."""
    mod, issues = checker
    (issues / "issue-999.md").write_text(
        "AUTO_GENERATED: true\nISSUE: #999\nSTATUS: OPEN\n",
        encoding="utf-8")
    mod._auto_archive_resolved(999, "some title")
    text = (issues / "archived" / "issue-999.md").read_text(encoding="utf-8")
    assert "STATUS: CLOSED" in text
    assert "PRIOR:" not in text
