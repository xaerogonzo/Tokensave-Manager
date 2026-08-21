"""tests/test_worktree_cleanup.py — the half-state, and refusing to destroy.

`git worktree remove` deregisters the worktree even when it cannot delete the
directory, so the interesting states are not "worked" and "failed" but:

    registered? x directory exists?

The old Tasks-tab code collapsed that into "failed -> tell the user to close
things and try again", which is advice that cannot work: git has nothing left
to do, and the directory that remains holds the uncommitted work which caused
the delete to fail.

Filesystem facts are real here (tmp_path); only the git boundary is mocked,
because the behaviour under test is precisely how this module reacts to what
git reports versus what it independently observes.
"""
from __future__ import annotations

import os
import subprocess

from helpers import worktree_cleanup as wc
from helpers.worktree_cleanup import (
    LOCK_NONE,
    LOCK_TOKENSAVE_DB,
    LOCK_UNKNOWN,
    LOCK_WORKTREE_DIRECTORY,
    classify_lock,
    delete_orphan_directory,
    directory_signature,
    remove_worktree,
)

GIT = "git"
REPO = "C:/repo" if os.name == "nt" else "/repo"


def _run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _worktree(tmp_path, name="wt", files=("a.txt",)):
    wt = tmp_path / name
    wt.mkdir()
    for f in files:
        (wt / f).write_text("content", encoding="utf-8")
    return str(wt)


def _mock_git(mocker, remove_result, listing=""):
    """Mock the two git calls this module makes, by argv."""
    def _dispatch(cmd, *a, **kw):
        if "remove" in cmd:
            return remove_result
        return _run(0, stdout=listing)
    return mocker.patch.object(wc.subprocess, "run", side_effect=_dispatch)


# ── the happy path ───────────────────────────────────────────────────────

def test_a_clean_removal_reports_success(mocker, tmp_path):
    path = str(tmp_path / "gone")           # never created
    _mock_git(mocker, _run(0))
    res = remove_worktree(GIT, REPO, path)
    assert res.success and res.deregistered
    assert not res.directory_exists
    assert not res.is_half_state


# ── the half-state: git let go, the filesystem did not ───────────────────

def test_a_failed_delete_that_still_deregistered_is_the_half_state(mocker, tmp_path):
    """The case the previous implementation had no representation for."""
    path = _worktree(tmp_path)
    _mock_git(mocker,
              _run(1, stderr="error: failed to delete '%s': Permission denied" % path),
              listing="worktree %s\n" % REPO)      # our worktree is NOT listed
    res = remove_worktree(GIT, REPO, path)

    assert res.success is False
    assert res.deregistered is True, "git prunes its metadata even on failure"
    assert res.directory_exists is True
    assert res.is_half_state is True


def test_the_half_state_must_not_advise_retrying(mocker, tmp_path):
    """`retry_would_help` is what the dialog branches on.

    Retrying is the advice the old code gave, and git has nothing left to do:
    it already deregistered. Saying otherwise sends the user round a loop that
    cannot terminate.
    """
    path = _worktree(tmp_path)
    _mock_git(mocker, _run(1, stderr="failed to delete: Permission denied"),
              listing="worktree %s\n" % REPO)
    res = remove_worktree(GIT, REPO, path)
    assert res.retry_would_help is False


def test_a_failure_that_left_it_registered_is_worth_retrying(mocker, tmp_path):
    """Git refused outright (e.g. dirty worktree, no --force) — nothing pruned."""
    path = _worktree(tmp_path)
    _mock_git(mocker,
              _run(1, stderr="fatal: '%s' contains modified files" % path),
              listing="worktree %s\nworktree %s\n" % (REPO, path))
    res = remove_worktree(GIT, REPO, path)
    assert res.deregistered is False
    assert res.retry_would_help is True
    assert res.is_half_state is False


def test_registration_is_observed_not_inferred_from_the_exit_code(mocker, tmp_path):
    """A zero exit with the directory still present is still not success.

    The exit code describes what git attempted. Whether the worktree is
    registered, and whether the directory exists, are facts — and they are
    what the result reports.
    """
    path = _worktree(tmp_path)
    _mock_git(mocker, _run(0), listing="worktree %s\n" % REPO)
    res = remove_worktree(GIT, REPO, path)
    assert res.success is False, "the directory is still there"
    assert res.deregistered is True
    assert res.is_half_state is True


# ── lock classification is evidence, and only evidence ───────────────────

def test_a_tokensave_database_lock_is_named_as_such():
    stderr = r"error: failed to delete 'D:\wt\.tokensave\tokensave.db': Permission denied"
    assert classify_lock(stderr) == LOCK_TOKENSAVE_DB


def test_a_bare_directory_lock_is_distinguished_from_the_database():
    """Different holder, different remedy — one is fixable, one is not.

    A daemon holding the database can be stopped. A live session's own working
    directory cannot be released from inside that session.
    """
    assert classify_lock(r"error: failed to delete 'D:\wt': Permission denied") \
        == LOCK_WORKTREE_DIRECTORY


def test_an_empty_stderr_classifies_as_no_lock():
    assert classify_lock("") == LOCK_NONE


def test_an_unrecognised_error_is_unknown_rather_than_guessed():
    assert classify_lock("fatal: something else entirely") == LOCK_UNKNOWN


# ── the change detector ──────────────────────────────────────────────────

def test_a_signature_matches_itself(tmp_path):
    path = _worktree(tmp_path, files=("a.txt", "b.txt"))
    assert directory_signature(path).matches(directory_signature(path))


def test_a_new_file_breaks_the_signature(tmp_path):
    """The case this exists for: the user kept working in the leftover dir."""
    path = _worktree(tmp_path)
    before = directory_signature(path)
    (tmp_path / "wt" / "new_work.txt").write_text("important", encoding="utf-8")
    assert not directory_signature(path).matches(before)


def test_edited_content_breaks_the_signature(tmp_path):
    path = _worktree(tmp_path)
    before = directory_signature(path)
    (tmp_path / "wt" / "a.txt").write_text("content plus rather more",
                                           encoding="utf-8")
    assert not directory_signature(path).matches(before)


def test_index_churn_does_not_break_the_signature(tmp_path):
    """A background indexer writing its own database is not the user working.

    Without this the delete would refuse on any project with tokensave
    running, which would make the whole guard useless noise.
    """
    path = _worktree(tmp_path)
    before = directory_signature(path)
    ts = tmp_path / "wt" / ".tokensave"
    ts.mkdir()
    (ts / "tokensave.db").write_text("x" * 5000, encoding="utf-8")
    assert directory_signature(path).matches(before)


def test_a_missing_directory_has_no_signature(tmp_path):
    assert directory_signature(str(tmp_path / "nope")) is None


# ── deletion refuses more readily than it acts ───────────────────────────

def test_deletion_refuses_while_git_still_lists_it(mocker, tmp_path):
    """rmtree is not the tool for a registered worktree."""
    path = _worktree(tmp_path)
    mocker.patch.object(wc.subprocess, "run",
                        return_value=_run(0, stdout="worktree %s\n" % path))
    ok, detail = delete_orphan_directory(GIT, REPO, path, None)
    assert ok is False
    assert "still lists this as a worktree" in detail
    assert os.path.isdir(path), "nothing may be deleted after a refusal"


def test_deletion_refuses_when_the_directory_changed_since_inspection(mocker, tmp_path):
    path = _worktree(tmp_path)
    inspected = directory_signature(path)
    (tmp_path / "wt" / "written_since.txt").write_text("new", encoding="utf-8")
    mocker.patch.object(wc.subprocess, "run",
                        return_value=_run(0, stdout="worktree %s\n" % REPO))
    ok, detail = delete_orphan_directory(GIT, REPO, path, inspected)
    assert ok is False
    assert "changed since it was inspected" in detail
    assert os.path.isdir(path)


def test_deletion_proceeds_when_everything_still_checks_out(mocker, tmp_path):
    path = _worktree(tmp_path)
    inspected = directory_signature(path)
    mocker.patch.object(wc.subprocess, "run",
                        return_value=_run(0, stdout="worktree %s\n" % REPO))
    ok, detail = delete_orphan_directory(GIT, REPO, path, inspected)
    assert ok is True, detail
    assert not os.path.isdir(path)


def test_deleting_an_already_gone_directory_is_success_not_an_error(mocker, tmp_path):
    mocker.patch.object(wc.subprocess, "run",
                        return_value=_run(0, stdout="worktree %s\n" % REPO))
    ok, detail = delete_orphan_directory(GIT, REPO, str(tmp_path / "nope"), None)
    assert ok is True
    assert "already gone" in detail


# ── failure to ask git is not permission to proceed ──────────────────────

def test_an_unreadable_worktree_list_counts_as_still_registered(mocker):
    """Cautious default: a failed lookup must not unlock the delete path."""
    mocker.patch.object(wc.subprocess, "run", side_effect=OSError("git missing"))
    assert wc.is_registered(GIT, REPO, "/anything") is True


def test_deletion_refuses_when_git_cannot_be_consulted(mocker, tmp_path):
    path = _worktree(tmp_path)
    mocker.patch.object(wc.subprocess, "run", side_effect=OSError("git missing"))
    ok, _detail = delete_orphan_directory(GIT, REPO, path, None)
    assert ok is False
    assert os.path.isdir(path)
