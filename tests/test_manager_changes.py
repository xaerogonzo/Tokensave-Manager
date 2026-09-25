"""tests/test_manager_changes.py — ownership is proven, and the message is data.

Pure. The two properties worth holding hard: a path's NAME never confers
eligibility, and the same eligible set always yields the same message, built
from the files actually going into the commit.
"""
from __future__ import annotations

import itertools

import pytest

from helpers import manager_changes as mch

EV = mch.OperationEvidence


def cur(sha="h", differs=True, ignored=False):
    return mch.CurrentPath(sha, differs, ignored)


def one(path="project-baseline.md", written="h", dirty=None, now=None):
    ev = EV(written={path: written}, dirty_before=dirty or {}, complete=True)
    (f,) = mch.assess(ev, {path: now or cur()})
    return f


# ── ownership ────────────────────────────────────────────────────────────

def test_a_clean_unchanged_write_is_full():
    assert one().ownership == mch.FULL


def test_dirty_before_the_write_is_mixed():
    f = one(dirty={"project-baseline.md": "old"})
    assert (f.ownership, f.reason) == (mch.MIXED, mch.REASON_DIRTY_BEFORE)


def test_changed_since_the_write_is_mixed():
    f = one(now=cur(sha="someone-else"))
    assert (f.ownership, f.reason) == (mch.MIXED, mch.REASON_CHANGED_SINCE)


def test_no_longer_differing_is_none():
    assert one(now=cur(differs=False)).ownership == mch.NONE


def test_tracked_but_ignored_is_blocked():
    assert one(now=cur(ignored=True)).ownership == mch.BLOCKED


@pytest.mark.parametrize("evidence", [None, EV(written={"a.md": "h"}, complete=False)])
def test_without_complete_evidence_nothing_is_provable(evidence):
    files = mch.assess(evidence, {"a.md": cur()})
    assert files and {f.ownership for f in files} == {mch.UNKNOWN}
    assert mch.eligible(files) == ()


def test_a_candidate_git_could_not_read_is_unknown_never_clean():
    ev = EV(written={"a.md": "h"}, complete=True)
    (f,) = mch.assess(ev, {})
    assert f.ownership == mch.UNKNOWN


def test_a_matching_name_confers_nothing():
    """The whole point: `.gitignore` matches a group and is still mixed."""
    f = one(".gitignore", dirty={".gitignore": "theirs"})
    assert f.group == mch.G_IGNORE and f.ownership == mch.MIXED
    assert mch.eligible([f]) == ()


# ── grouping is a label ──────────────────────────────────────────────────

@pytest.mark.parametrize("path,group", [
    ("project-baseline.md", mch.G_INSTRUCTIONS),
    ("CLAUDE.md", mch.G_INSTRUCTIONS),
    ("BASIC_INSTRUCTIONS.md", mch.G_INSTRUCTIONS),
    ("docs/gotchas/x.md", mch.G_LESSONS),
    ("docs/gotchas/NUITKA_GOTCHAS.md", mch.G_LESSONS),
    ("docs/LESSONS.md", mch.G_LESSONS),
    (".gitignore", mch.G_IGNORE),
    ("AGENTS.md", mch.G_AGENTS),
    (".cursor/rules/tokensave.mdc", mch.G_AGENTS),
    ("src/app.py", mch.G_OTHER),
    ("docs/gotchas", mch.G_OTHER),
])
def test_group_of(path, group):
    assert mch.group_of(path) == group
    assert mch.group_of(path.replace("/", "\\")) == group


# ── wording: exact fixtures, one per combination ─────────────────────────

FIXTURES = {
    "I": "chore(instructions): refresh project instructions",
    "L": "chore(instructions): deliver shared lessons",
    "G": "chore(gitignore): align ignore rules with local-only files",
    "A": "chore(agents): update agent rules",
    "IL": "chore(instructions): refresh instructions and deliver shared lessons",
    "IG": "chore(instructions): refresh instructions and align ignore rules",
    "IA": "chore(instructions): refresh instructions and update agent rules",
    "LG": "chore(instructions): deliver shared lessons and align ignore rules",
    "LA": "chore(instructions): deliver shared lessons and update agent rules",
    "GA": "chore(agents): update agent rules and align ignore rules",
    "ILG": "chore(instructions): update instructions, lessons and ignore rules",
    "ILA": "chore(instructions): update instructions, lessons and agent rules",
    "IGA": "chore(instructions): update instructions, agent rules and ignore rules",
    "LGA": "chore(instructions): update lessons, agent rules and ignore rules",
    "ILGA": "chore(instructions): update instructions, lessons and all rules",
}
_PATH = {"I": "project-baseline.md", "L": "docs/gotchas/a.md",
         "G": ".gitignore", "A": "AGENTS.md"}


def full(*paths):
    return [mch.ChangedFile(p, mch.FULL, "", mch.group_of(p)) for p in paths]


@pytest.mark.parametrize("key,expected", sorted(FIXTURES.items()))
def test_every_combination_has_its_exact_subject(key, expected):
    files = full(*[_PATH[c] for c in key])
    assert mch.compose(files, "1.0").subject == expected


def test_the_table_is_complete_for_every_combination_of_the_four_groups():
    groups = [mch.G_INSTRUCTIONS, mch.G_LESSONS, mch.G_IGNORE, mch.G_AGENTS]
    for size in range(1, 5):
        for combo in itertools.combinations(groups, size):
            assert frozenset(combo) in mch.COMBINATIONS, combo
    assert len(mch.COMBINATIONS) == 15 == len(FIXTURES)


def test_every_subject_fits_and_starts_with_an_approved_verb():
    for scope_phrase in list(mch.COMBINATIONS.values()) + [mch.GENERIC]:
        subject = "chore(%s): %s" % scope_phrase
        assert len(subject) <= mch.SUBJECT_MAX, subject
        assert subject.split(": ", 1)[1].split()[0] in mch.APPROVED_VERBS, subject


def test_a_file_in_no_group_falls_back_to_the_generic_subject():
    files = full("src/app.py")
    assert mch.compose(files, "1.0").subject == \
        "chore(manager): refresh generated project files"
    mixed = full("src/app.py", "project-baseline.md")
    assert mch.compose(mixed, "1.0").subject == \
        "chore(manager): refresh generated project files"


# ── the body ─────────────────────────────────────────────────────────────

def test_the_same_set_gives_the_same_message_whatever_the_input_order():
    files = full("docs/gotchas/b.md", "project-baseline.md", "docs/gotchas/a.md")
    forward = mch.compose(files, "1.0")
    assert forward == mch.compose(list(reversed(files)), "1.0")
    assert forward == mch.compose(sorted(files, key=lambda f: f.path), "1.0")


def test_only_eligible_files_are_described():
    files = full("project-baseline.md") + [
        mch.ChangedFile("docs/gotchas/a.md", mch.MIXED, "x", mch.G_LESSONS)]
    msg = mch.compose(files, "1.0")
    assert "docs/gotchas/a.md" not in msg.text()
    assert msg.subject == "chore(instructions): refresh project instructions"


def test_names_are_listed_up_to_six_then_a_count():
    six = full(*["docs/gotchas/%d.md" % i for i in range(6)])
    seven = full(*["docs/gotchas/%d.md" % i for i in range(7)])
    assert "docs/gotchas/0.md" in mch.compose(six, "1.0").body
    body = mch.compose(seven, "1.0").body
    assert "shared lessons: 7 files" in body and "docs/gotchas/0.md" not in body


def test_the_body_states_why_in_user_facing_words():
    body = mch.compose(full("project-baseline.md", "docs/gotchas/a.md"), "2.5").body
    assert "loads in every session" in body
    assert "indexes these lessons" in body
    assert "Written-By: TokenSave Manager 2.5" in body
    for internal in ("G_INSTRUCTIONS", "baseline_copy", "lessons_delivery", "ManagedCopy"):
        assert internal not in body


def test_nothing_eligible_composes_nothing():
    assert mch.compose([], "1.0") == mch.Message("", "")
    assert mch.compose([mch.ChangedFile("a", mch.MIXED)], "1.0").text() == ""


def test_the_sentence_is_stable_across_the_version_only_in_its_trailer():
    a = mch.compose(full("AGENTS.md"), "1.0")
    b = mch.compose(full("AGENTS.md"), "2.0")
    assert a.subject == b.subject
    assert a.body.replace("1.0", "X") == b.body.replace("2.0", "X")
