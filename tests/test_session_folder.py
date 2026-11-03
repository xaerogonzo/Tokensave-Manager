"""Where a session can be continued: a removed worktree is not "present".

Claude removes a finished worktree's checkout and leaves the empty directory, and
git stops listing it. `os.path.isdir` called that folder present, so "Continue in
Cursor" opened an empty folder. Measured on this machine: the session "OpenChem
Studio open-source tools inventory" recorded
`.../OpenChem Studio/.claude/worktrees/fervent-rhodes-5823fa`, a folder with 0 files.
"""
import os

import pytest

from helpers import claude_tasks as ct
from helpers.claude_tasks import (FOLDER_MISSING, FOLDER_OK, FOLDER_REMOVED,
                                  resolve_session_folder, worktree_label)


def _repo(tmp_path, name="Repo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _worktree(repo, name="wt", *, live):
    wt = repo / ".claude" / "worktrees" / name
    wt.mkdir(parents=True)
    if live:
        (wt / ".git").write_text("gitdir: elsewhere\n")      # a worktree's .git is a file
    return wt


def test_a_plain_folder_is_ok_and_a_missing_one_is_missing(tmp_path):
    assert resolve_session_folder(str(tmp_path)) == ct.SessionFolder(FOLDER_OK, str(tmp_path), str(tmp_path))
    gone = str(tmp_path / "gone")
    assert resolve_session_folder(gone).state == FOLDER_MISSING
    assert resolve_session_folder("").state == FOLDER_MISSING


def test_a_folder_that_is_not_a_repository_is_never_second_guessed(tmp_path):
    plain = tmp_path / "Notes"
    plain.mkdir()
    assert resolve_session_folder(str(plain)).state == FOLDER_OK


def test_a_live_worktree_is_ok(tmp_path):
    wt = _worktree(_repo(tmp_path), live=True)
    found = resolve_session_folder(str(wt))
    assert found.state == FOLDER_OK and found.path == str(wt)


def test_a_subfolder_of_a_live_worktree_is_ok_not_removed(tmp_path):
    wt = _worktree(_repo(tmp_path), live=True)
    sub = wt / "src"
    sub.mkdir()
    assert resolve_session_folder(str(sub)).state == FOLDER_OK


def test_the_empty_leftover_of_a_removed_worktree_falls_back_to_the_repository(tmp_path):
    repo = _repo(tmp_path)
    wt = _worktree(repo, live=False)
    assert os.path.isdir(wt)                              # the trap: it still "exists"
    found = resolve_session_folder(str(wt))
    assert found == ct.SessionFolder(FOLDER_REMOVED, str(repo), str(wt))


def test_a_worktree_whose_directory_is_gone_entirely_falls_back_too(tmp_path):
    repo = _repo(tmp_path)
    found = resolve_session_folder(str(repo / ".claude" / "worktrees" / "wt"))
    assert found.state == FOLDER_REMOVED and found.path == str(repo)


def test_no_fallback_is_invented_when_the_repository_is_not_a_checkout_either(tmp_path):
    notrepo = tmp_path / "Repo"
    (notrepo / ".claude" / "worktrees" / "wt").mkdir(parents=True)
    found = resolve_session_folder(str(notrepo / ".claude" / "worktrees" / "wt"))
    assert found.state == FOLDER_MISSING and found.path == ""
    assert resolve_session_folder(str(tmp_path / "x" / ".claude" / "worktrees" / "wt")).state == FOLDER_MISSING


@pytest.mark.parametrize("sep", ["\\", "/"])
def test_either_separator_is_read(tmp_path, sep):
    repo = _repo(tmp_path)
    cwd = sep.join([str(repo), ".claude", "worktrees", "wt"])
    assert resolve_session_folder(cwd).state == FOLDER_REMOVED


def test_the_session_list_says_when_a_checkout_was_removed(tmp_path):
    repo = _repo(tmp_path)
    removed = str(_worktree(repo, "gone", live=False))
    live = str(_worktree(repo, "here", live=True))
    assert ct._display_for("enc", removed, []).endswith("(worktree gone) - checkout removed")
    assert ct._display_for("enc", live, []) == worktree_label(live)
    assert "removed" not in ct._display_for("enc", str(tmp_path / "x" / ".claude" / "worktrees" / "y"), [])
