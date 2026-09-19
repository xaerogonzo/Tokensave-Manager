"""Doctor's extra-checkouts rule: worktrees nothing tracks or cleans up.

Runs against a real repository and real `git worktree add`, because the
rule's whole value is that `git worktree list` is authoritative -- a mocked
listing would only test our parsing of our own fixture.
"""
import os
import shutil
import subprocess

import pytest

from helpers import stray_checkouts as sc
from helpers.doctor_rules import audit_stray_checkouts

GIT = shutil.which("git") or ""
pytestmark = pytest.mark.skipif(not GIT, reason="git not installed")


def _git(cwd, *args):
    return subprocess.run(
        [GIT, "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
         *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("x")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "x")
    return root


def _add(repo, path):
    _git(repo, "worktree", "add", "-q", "--detach", str(path))
    return path


def _scan(repo, tmp_path):
    return sc.scan(str(repo), GIT, scratch_root=str(tmp_path / "_scratch"))


def test_a_worktree_loose_beside_the_repo_is_a_stray(repo, tmp_path):
    _add(repo, tmp_path / "ocs-master")
    rep = _scan(repo, tmp_path)
    assert rep.state == sc.STRAY
    assert [os.path.basename(c.path) for c in rep.strays] == ["ocs-master"]
    assert rep.strays[0].branch == ""            # detached
    assert rep.strays[0].exists


def test_claude_codes_own_worktree_folder_is_sanctioned(repo, tmp_path):
    _add(repo, repo / ".claude" / "worktrees" / "wt1")
    assert _scan(repo, tmp_path).state == sc.CLEAR


def test_the_scratch_folder_is_sanctioned(repo, tmp_path):
    _add(repo, tmp_path / "_scratch" / "proj-abc1234")
    assert _scan(repo, tmp_path).state == sc.CLEAR


def test_a_sibling_that_merely_starts_with_the_scratch_name_is_not_inside_it(
        repo, tmp_path):
    """`_scratch2` must not be mistaken for `_scratch` by a prefix test."""
    _add(repo, tmp_path / "_scratch2" / "proj-abc")
    assert _scan(repo, tmp_path).state == sc.STRAY


def test_has_index_reflects_the_folder_not_the_repo(repo, tmp_path):
    wt = _add(repo, tmp_path / "with-index")
    (wt / ".tokensave").mkdir()
    bare = _add(repo, tmp_path / "no-index")
    by_name = {os.path.basename(c.path): c for c in _scan(repo, tmp_path).strays}
    assert by_name["with-index"].has_index is True
    assert by_name["no-index"].has_index is False
    assert bare.exists()


def test_a_registered_worktree_whose_folder_is_gone_says_prune(repo, tmp_path):
    wt = _add(repo, tmp_path / "gone")
    shutil.rmtree(wt)
    joined = " ".join(audit_stray_checkouts(
        str(repo), GIT, str(tmp_path / "_scratch")))
    assert "folder is gone" in joined
    assert "git worktree prune" in joined


def test_the_project_itself_being_a_linked_worktree_is_not_a_stray(
        repo, tmp_path):
    me = _add(repo, tmp_path / "me")
    rep = sc.scan(str(me), GIT, scratch_root=str(tmp_path / "_scratch"))
    assert rep.state == sc.CLEAR      # main checkout is skipped, `me` is self


def test_an_unregistered_folder_named_for_the_project_is_listed(
        repo, tmp_path):
    scratch = tmp_path / "_scratch"
    (scratch / "proj-plain-copy").mkdir(parents=True)
    (scratch / "other-repo-abc").mkdir()
    registered = _add(repo, scratch / "proj-registered")
    rep = _scan(repo, tmp_path)
    assert [os.path.basename(p) for p in rep.unregistered] == ["proj-plain-copy"]
    assert registered.exists()          # registered ones are not "unregistered"


def test_a_clean_project_is_silent(repo, tmp_path):
    assert audit_stray_checkouts(
        str(repo), GIT, str(tmp_path / "_scratch")) == []


def test_not_a_git_repository_is_silent(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert audit_stray_checkouts(str(plain), GIT) == []
    assert audit_stray_checkouts("") == []


def test_git_that_cannot_be_asked_is_unknown_never_clean(repo, tmp_path):
    """The failure `scan_worktrees` hides: [] on error reads as 'none'."""
    for exe in ("", str(tmp_path / "no-such-git.exe")):
        rep = sc.scan(str(repo), exe, scratch_root=str(tmp_path / "_scratch"))
        assert rep.state == sc.UNKNOWN
        joined = " ".join(audit_stray_checkouts(
            str(repo), exe, str(tmp_path / "_scratch")))
        assert "unknown, not clean" in joined


def test_the_report_always_says_what_it_cannot_see(repo, tmp_path):
    _add(repo, tmp_path / "loose")
    joined = " ".join(audit_stray_checkouts(
        str(repo), GIT, str(tmp_path / "_scratch")))
    assert "cannot be listed from here" in joined
    assert "does not judge" in joined          # never calls a copy abandoned
    assert "git worktree remove" in joined


def test_default_scratch_root_is_on_the_projects_own_drive(tmp_path):
    root = sc.default_scratch_root(str(tmp_path / "proj"))
    assert os.path.basename(root) == "_scratch"
    assert os.path.splitdrive(root)[0] == os.path.splitdrive(str(tmp_path))[0]
