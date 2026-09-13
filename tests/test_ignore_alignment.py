"""tests/test_ignore_alignment.py — companions follow their referrer into git.

The case that paid for it: KicomAI ignores `CLAUDE.md` and
`BASIC_INSTRUCTIONS.md`, and the Manager then wrote `project-baseline.md` and
`docs/LESSONS.md` where git would commit them. Every decision here is taken
from what git says, and nothing is done on a fact that is missing or unknown.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import types

import pytest

from helpers import ignore_alignment as ia
from helpers.instructions_posture import canonical

GIT = shutil.which("git") or ""
needs_git = pytest.mark.skipif(not GIT, reason="git not installed")

COPY = "project-baseline.md"
LESSONS = "docs/LESSONS.md"
BASIC = "BASIC_INSTRUCTIONS.md"
CLAUDE = "CLAUDE.md"


def _pair(companion, referrer, kind=ia.KIND_BASELINE_COPY):
    return ia.CompanionPair(companion, referrer, kind)


def _facts(repo=ia.REPO, **states):
    """`name=(fs, git)` with the dotted names spelled as keys below."""
    return ia.Facts(repo, {rel: ia.PathFact(fs, git)
                           for rel, (fs, git) in states.items()})


def _both(ref_git, comp_git, comp_fs=ia.FS_EXISTS, repo=ia.REPO):
    return ia.Facts(repo, {
        CLAUDE: ia.PathFact(ia.FS_EXISTS, ref_git),
        COPY: ia.PathFact(comp_fs, comp_git),
    })


# ── decide: the table ────────────────────────────────────────────────────

def test_local_referrer_open_companion_is_pending():
    result = ia.decide([_pair(COPY, CLAUDE)], _both(ia.LOCAL, ia.OPEN))
    assert result.patterns == ("/" + COPY,)
    assert not result.notes


def test_local_referrer_tracked_companion_is_a_note_never_an_untrack():
    result = ia.decide([_pair(COPY, CLAUDE)], _both(ia.LOCAL, ia.TRACKED))
    assert result.patterns == ()
    assert "git rm --cached" in result.notes[0]


@pytest.mark.parametrize("ref", [ia.TRACKED, ia.OPEN])
def test_ignored_companion_under_a_visible_referrer_is_only_reported(ref):
    result = ia.decide([_pair(COPY, CLAUDE)], _both(ref, ia.LOCAL))
    assert result.patterns == ()
    assert "existing rule" in result.notes[0]


@pytest.mark.parametrize("ref,comp", [(ia.TRACKED, ia.TRACKED),
                                      (ia.OPEN, ia.OPEN),
                                      (ia.LOCAL, ia.LOCAL),
                                      (ia.TRACKED, ia.OPEN)])
def test_aligned_states_do_nothing(ref, comp):
    result = ia.decide([_pair(COPY, CLAUDE)], _both(ref, comp))
    assert result.patterns == () and not result.notes and not result.unknown


def test_a_missing_companion_is_never_acted_on():
    result = ia.decide([_pair(COPY, CLAUDE)],
                       _both(ia.LOCAL, ia.OPEN, comp_fs=ia.FS_MISSING))
    assert result.patterns == ()


@pytest.mark.parametrize("ref,comp", [(ia.UNKNOWN, ia.OPEN),
                                      (ia.LOCAL, ia.UNKNOWN)])
def test_unknown_on_either_side_is_never_acted_on(ref, comp):
    result = ia.decide([_pair(COPY, CLAUDE)], _both(ref, comp))
    assert result.patterns == ()
    assert result.unknown


@pytest.mark.parametrize("repo", [ia.NO_REPO, ia.NOT_OWN_REPO, ia.UNKNOWN])
def test_no_own_repository_is_never_acted_on(repo):
    result = ia.decide([_pair(COPY, CLAUDE)],
                       _both(ia.LOCAL, ia.OPEN, repo=repo))
    assert result.patterns == ()


# ── paths that escape ────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", ["../x.md", "sub/../../x.md", "C:x.md",
                                 "/abs.md", os.path.abspath("x.md")])
def test_escaping_paths_are_rejected_before_git_runs(tmp_path, mocker, rel):
    spy = mocker.patch.object(ia, "_git_states", return_value=({}, ""))
    mocker.patch.object(ia, "_repo_state", return_value=(ia.REPO, ""))
    facts = ia.read_facts(str(tmp_path), "git", [rel])
    assert facts.of(rel).fs == ia.FS_ESCAPES
    assert spy.call_args is None or rel not in spy.call_args.args[2]


# ── companions_of ────────────────────────────────────────────────────────

def _posture(root, chain_files, directives):
    return types.SimpleNamespace(
        display_root=str(root),
        chain=tuple(canonical(os.path.join(str(root), f))
                    for f in chain_files),
        baseline_directives=tuple(directives))


def test_the_copys_referrer_is_the_file_with_the_live_directive(tmp_path):
    posture = _posture(tmp_path, [CLAUDE, BASIC, COPY],
                       [(BASIC, 3, COPY)])
    pairs = ia.companions_of(posture)
    assert _pair(BASIC, CLAUDE, ia.KIND_BASIC) in pairs
    assert _pair(COPY, BASIC) in pairs


def test_a_one_hop_chain_refers_the_copy_to_claude_md(tmp_path):
    posture = _posture(tmp_path, [CLAUDE, COPY], [(CLAUDE, 1, COPY),
                                                  (BASIC, 1, COPY)])
    assert ia.companions_of(posture) == (_pair(COPY, CLAUDE),)


def test_lessons_is_a_companion_only_when_it_exists(tmp_path):
    posture = _posture(tmp_path, [CLAUDE], [])
    assert ia.companions_of(posture) == ()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "LESSONS.md").write_text("# Lessons\n")
    assert ia.companions_of(posture) == (
        _pair(LESSONS, CLAUDE, ia.KIND_LESSONS),)


# ── real repositories ────────────────────────────────────────────────────

def _run(root, *args):
    return subprocess.run([GIT, "-C", str(root)] + list(args), check=True,
                          capture_output=True, text=True)


def _repo(tmp_path, gitignore="", tracked=(), files=()):
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "t@example.com")
    _run(root, "config", "user.name", "t")
    if gitignore:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    for rel in set(tracked) | set(files):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# %s\n" % rel, encoding="utf-8")
    for rel in tracked:
        _run(root, "add", "-f", rel)
    _run(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _porcelain(root):
    return _run(root, "status", "--porcelain", "--untracked-files=all").stdout


@needs_git
def test_kicomai_shape_is_aligned_and_only_gitignore_changes(tmp_path):
    root = _repo(tmp_path, gitignore="CLAUDE.md\n",
                 files=[CLAUDE, COPY, LESSONS])
    _run(root, "add", ".gitignore")
    _run(root, "commit", "-q", "-m", "ignore")
    before = {rel: (root / rel).read_bytes() for rel in (COPY, LESSONS)}
    assert "project-baseline.md" in _porcelain(root)

    pairs = [_pair(COPY, CLAUDE), _pair(LESSONS, CLAUDE, ia.KIND_LESSONS)]
    result = ia.align(str(root), GIT, pairs)

    assert {p.companion_rel for p in result.confirmed} == {COPY, LESSONS}
    assert result.changed_files == (".gitignore",)
    assert _porcelain(root).strip() == "M .gitignore"
    # Ignored, not removed: the files are exactly as they were.
    assert {rel: (root / rel).read_bytes() for rel in before} == before
    assert "is now ignored to match local-only CLAUDE.md" in result.render()

    gitignore = (root / ".gitignore").read_bytes()
    again = ia.align(str(root), GIT, pairs)
    assert again.changed_files == () and again.confirmed == ()
    assert (root / ".gitignore").read_bytes() == gitignore


@needs_git
def test_a_two_deep_chain_is_aligned_in_one_call(tmp_path):
    root = _repo(tmp_path, gitignore="CLAUDE.md\n", files=[CLAUDE, BASIC, COPY])
    result = ia.align(str(root), GIT, [_pair(BASIC, CLAUDE, ia.KIND_BASIC),
                                       _pair(COPY, BASIC)])
    assert {p.companion_rel for p in result.confirmed} == {BASIC, COPY}


@needs_git
def test_a_tracked_referrer_adds_nothing(tmp_path):
    root = _repo(tmp_path, tracked=[CLAUDE], files=[COPY])
    result = ia.align(str(root), GIT, [_pair(COPY, CLAUDE)])
    assert result.changed_files == ()
    assert not (root / ".gitignore").exists()


@needs_git
def test_a_tracked_companion_is_reported_and_the_index_untouched(tmp_path):
    root = _repo(tmp_path, gitignore="CLAUDE.md\n", tracked=[COPY],
                 files=[CLAUDE])
    index = (root / ".git" / "index").read_bytes()
    result = ia.align(str(root), GIT, [_pair(COPY, CLAUDE)])
    assert result.changed_files == ()
    assert any("git rm --cached" in n for n in result.notes)
    assert (root / ".git" / "index").read_bytes() == index


@needs_git
def test_a_broad_user_rule_is_reported_not_duplicated(tmp_path):
    root = _repo(tmp_path, gitignore="*.md\n!CLAUDE.md\n", tracked=[CLAUDE],
                 files=[COPY])
    before = (root / ".gitignore").read_bytes()
    result = ia.align(str(root), GIT, [_pair(COPY, CLAUDE)])
    assert result.changed_files == ()
    assert any("existing rule" in n for n in result.notes)
    assert (root / ".gitignore").read_bytes() == before


@needs_git
def test_a_negation_is_judged_by_git_not_by_reading_patterns(tmp_path):
    root = _repo(tmp_path, gitignore="*.md\n!project-baseline.md\n",
                 files=[CLAUDE, COPY])
    facts = ia.read_facts(str(root), GIT, [CLAUDE, COPY])
    assert facts.of(COPY).git == ia.OPEN
    result = ia.align(str(root), GIT, [_pair(COPY, CLAUDE)])
    # The later, anchored rule wins, and git is what says so.
    assert [p.companion_rel for p in result.confirmed] == [COPY]
    assert ia.read_facts(str(root), GIT, [COPY]).of(COPY).git == ia.LOCAL


@needs_git
def test_align_rereads_instead_of_trusting_the_scan(tmp_path):
    root = _repo(tmp_path, gitignore="CLAUDE.md\n", files=[CLAUDE, COPY])
    scan = ia.decide([_pair(COPY, CLAUDE)],
                     ia.read_facts(str(root), GIT, [CLAUDE, COPY]))
    assert scan.pending
    # Fixed by hand between the scan and the click.
    with open(root / ".gitignore", "a", encoding="utf-8") as handle:
        handle.write("project-baseline.md\n")
    before = (root / ".gitignore").read_bytes()
    result = ia.align(str(root), GIT, [_pair(COPY, CLAUDE)])
    assert result.changed_files == () and result.confirmed == ()
    assert (root / ".gitignore").read_bytes() == before


@needs_git
def test_every_requested_path_gets_a_state_with_literal_names(tmp_path):
    # `[ab]` is a glob that matches the tracked `wad.md`. Read as a pathspec,
    # the untracked bracket file would be answered for by its neighbour.
    odd = "w[ab]d.md"
    root = _repo(tmp_path, gitignore="CLAUDE.md\n", tracked=["wad.md"],
                 files=[CLAUDE, odd])
    rels = [CLAUDE, odd, "wad.md", "missing.md"]
    facts = ia.read_facts(str(root), GIT, rels)
    assert set(facts.paths) == set(rels)
    assert facts.of(CLAUDE).git == ia.LOCAL
    assert facts.of("wad.md").git == ia.TRACKED
    assert facts.of(odd).git == ia.OPEN
    assert facts.of("missing.md").fs == ia.FS_MISSING


@needs_git
def test_a_folder_inside_another_repo_is_not_its_own(tmp_path):
    root = _repo(tmp_path, files=["sub/CLAUDE.md"])
    facts = ia.read_facts(str(root / "sub"), GIT, [CLAUDE])
    assert facts.repo == ia.NOT_OWN_REPO


def test_no_repository_is_not_unknown(tmp_path):
    if not GIT:
        pytest.skip("git not installed")
    (tmp_path / CLAUDE).write_text("x")
    facts = ia.read_facts(str(tmp_path), GIT, [CLAUDE])
    # tmp_path may sit inside some other checkout on a dev machine.
    assert facts.repo in (ia.NO_REPO, ia.NOT_OWN_REPO)


def test_a_failing_check_ignore_is_unknown_never_open(tmp_path, mocker):
    """The shipped shape of this bug: exit 128 with no output, which a parser
    that only reads stdout turns into "nothing is ignored"."""
    mocker.patch.object(ia, "_repo_state", return_value=(ia.REPO, ""))

    def fake(git_exe, root, args, stdin="", literal=False):
        code = 128 if args[0] == "check-ignore" else 0
        return types.SimpleNamespace(returncode=code, stdout=b"",
                                     stderr=b"fatal: nope")

    mocker.patch.object(ia, "_git", side_effect=fake)
    (tmp_path / COPY).write_text("x")
    facts = ia.read_facts(str(tmp_path), "git", [CLAUDE, COPY])
    assert facts.of(COPY).git == ia.UNKNOWN
    assert "check-ignore exit 128" in facts.of(COPY).reason


def test_a_broken_git_is_unknown_with_a_reason_and_writes_nothing(tmp_path):
    (tmp_path / CLAUDE).write_text("x")
    (tmp_path / COPY).write_text("x")
    (tmp_path / ".git").mkdir()
    bogus = str(tmp_path / "no-such-git.exe")
    facts = ia.read_facts(str(tmp_path), bogus, [CLAUDE, COPY])
    assert facts.repo == ia.UNKNOWN and facts.reason
    result = ia.align(str(tmp_path), bogus, [_pair(COPY, CLAUDE)])
    assert result.changed_files == ()
    assert not (tmp_path / ".gitignore").exists()


@needs_git
def test_a_linked_worktree_is_aligned_end_to_end(tmp_path):
    root = _repo(tmp_path, gitignore="CLAUDE.md\n")
    _run(root, "add", ".gitignore")
    _run(root, "commit", "-q", "-m", "ignore")
    wt = tmp_path / "wt"
    _run(root, "worktree", "add", "-q", str(wt))
    assert (wt / ".git").is_file()
    (wt / CLAUDE).write_text("x")
    (wt / COPY).write_text("x")

    result = ia.align(str(wt), GIT, [_pair(COPY, CLAUDE)])

    assert [p.companion_rel for p in result.confirmed] == [COPY]
    assert "/project-baseline.md" in (wt / ".gitignore").read_text()
    assert ia.align(str(wt), GIT, [_pair(COPY, CLAUDE)]).changed_files == ()


# ── the split confirmation says what is true ─────────────────────────────

def _split_facts(src, tgt, tgt_fs=ia.FS_EXISTS, repo=ia.REPO):
    return ia.Facts(repo, {CLAUDE: ia.PathFact(ia.FS_EXISTS, src),
                           LESSONS: ia.PathFact(tgt_fs, tgt)})


def test_split_text_tracked_source_open_target():
    text = ia.split_visibility_text(_split_facts(ia.TRACKED, ia.OPEN,
                                                 ia.FS_MISSING),
                                    CLAUDE, LESSONS)
    assert "CLAUDE.md is in git" in text and "revertible" in text


def test_split_text_local_source_never_claims_git():
    text = ia.split_visibility_text(_split_facts(ia.LOCAL, ia.OPEN,
                                                 ia.FS_MISSING),
                                    CLAUDE, LESSONS)
    assert "not revertible through git" in text
    assert "ignored to match" in text


def test_split_text_both_local():
    text = ia.split_visibility_text(_split_facts(ia.LOCAL, ia.LOCAL),
                                    CLAUDE, LESSONS)
    assert "both local-only" in text


def test_split_text_open_source_ignored_target():
    text = ia.split_visibility_text(_split_facts(ia.OPEN, ia.LOCAL),
                                    CLAUDE, LESSONS)
    assert "will not be committed" in text


def test_split_text_unknown():
    text = ia.split_visibility_text(
        ia.Facts(ia.UNKNOWN, {}, "git rev-parse exit 5: boom"),
        CLAUDE, LESSONS)
    assert "Could not determine" in text and "boom" in text
