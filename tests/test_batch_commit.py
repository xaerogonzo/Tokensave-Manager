"""tests/test_batch_commit.py — committing what the Manager wrote, and only that.

Every test here is a way a convenient batch commit would damage somebody's work
without raising: sweeping in a file they had staged, staging a whole `.gitignore`
that holds their edit, committing a file changed while a dialog sat open, or
leaving the Manager's staging behind after a failed hook. They run against real
scratch repositories, because the property being claimed is what git does.
"""
from __future__ import annotations

import shutil
import stat
import subprocess

import pytest

from helpers import batch_commit as bc
from helpers import manager_changes as mch

GIT = shutil.which("git") or ""
pytestmark = pytest.mark.skipif(not GIT, reason="git not installed")


def run(root, *args, check=True):
    return subprocess.run([GIT, "-C", str(root)] + list(args), capture_output=True,
                          text=True, check=check)


def repo(tmp_path, name="proj", files=None):
    root = tmp_path / name
    root.mkdir()
    run(root, "init", "-q")
    run(root, "config", "user.email", "t@example.com")
    run(root, "config", "user.name", "t")
    run(root, "config", "commit.gpgsign", "false")
    for rel, text in (files or {"README.md": "hello\n"}).items():
        write(root, rel, text)
    run(root, "add", "-A")
    run(root, "commit", "-q", "-m", "init")
    return root


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def manager_writes(root, files):
    """What a bulk operation does: capture, write, record evidence."""
    dirty = bc.capture_dirty(GIT, str(root))
    for rel, text in files.items():
        write(root, rel, text)
    evidence = bc.make_evidence(GIT, str(root), dirty, list(files))
    return bc.BatchItem(str(root), root.name, tuple(files), evidence)


LESSONS = {"project-baseline.md": "# baseline\n",
           "docs/gotchas/a.md": "a\n", "docs/gotchas/b.md": "b\n"}


def head_files(root):
    return set(run(root, "show", "--name-only", "--format=", "HEAD").stdout.split())


def staged(root):
    return set(run(root, "diff", "--cached", "--name-only").stdout.split())


def msg(item):
    ins = bc.inspect_project(item, GIT)
    return mch.compose(ins.files, "9.9.9").text()


# ── the happy path, and what a commit is verified against ─────────────────

def test_a_clean_write_is_ready_and_commits_exactly_those_files(tmp_path):
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)

    ins = bc.inspect_project(item, GIT)
    assert ins.state == bc.READY
    assert {f.path for f in ins.eligible} == set(LESSONS)

    result = bc.commit_project(item, msg(item), GIT)

    assert result.outcome == bc.COMMITTED, result.detail
    assert head_files(root) == set(LESSONS)
    assert set(result.committed) == set(LESSONS)
    assert result.sha == run(root, "rev-parse", "HEAD").stdout.strip()
    assert result.remaining_manager == () and result.remaining_unrelated == 0
    assert "Written-By: TokenSave Manager 9.9.9" in \
        run(root, "log", "-1", "--format=%B").stdout


def test_a_second_run_finds_nothing_to_do(tmp_path):
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)
    bc.commit_project(item, msg(item), GIT)
    assert bc.inspect_project(item, GIT).state == bc.NOTHING_CHANGED
    assert bc.commit_project(item, "x", GIT).outcome == bc.NOTHING


# ── adversarial: the user's own work ──────────────────────────────────────

def test_an_unrelated_unstaged_file_stays_out_of_the_commit(tmp_path):
    root = repo(tmp_path)
    write(root, "README.md", "hello\nmy edit\n")
    item = manager_writes(root, LESSONS)

    result = bc.commit_project(item, msg(item), GIT)

    assert result.outcome == bc.COMMITTED
    assert "README.md" not in head_files(root)
    assert "my edit" in (root / "README.md").read_text(encoding="utf-8")
    assert result.remaining_unrelated == 1


def test_an_unrelated_STAGED_file_stays_staged_and_out_of_the_commit(tmp_path):
    """The dangerous one: `git commit -- paths` alone can sweep it in."""
    root = repo(tmp_path)
    write(root, "README.md", "hello\nstaged edit\n")
    write(root, "notes.txt", "new file the user staged\n")
    run(root, "add", "README.md", "notes.txt")
    item = manager_writes(root, LESSONS)

    result = bc.commit_project(item, msg(item), GIT)

    assert result.outcome == bc.COMMITTED, result.detail
    assert head_files(root) == set(LESSONS), "the user's staged files were committed"
    assert staged(root) == {"README.md", "notes.txt"}, "their staging was consumed"


def test_a_file_the_user_had_already_edited_is_never_eligible(tmp_path):
    """A `.gitignore` holding their edit beside ours: not partially staged."""
    root = repo(tmp_path, files={".gitignore": "*.bak\n", "README.md": "x\n"})
    write(root, ".gitignore", "*.bak\n*.tmp\n")            # theirs, uncommitted
    item = manager_writes(root, {".gitignore": "*.bak\n*.tmp\n/local-only.md\n",
                                 "docs/gotchas/a.md": "a\n"})

    by = {f.path: f for f in bc.inspect_project(item, GIT).files}
    assert by[".gitignore"].ownership == mch.MIXED
    assert by[".gitignore"].reason == mch.REASON_DIRTY_BEFORE
    assert by["docs/gotchas/a.md"].ownership == mch.FULL

    result = bc.commit_project(item, "chore: x", GIT)

    assert result.outcome == bc.COMMITTED
    assert head_files(root) == {"docs/gotchas/a.md"}, \
        "a .gitignore holding the user's edit was staged wholesale"
    assert ".gitignore" in result.remaining_manager
    assert "*.tmp" in (root / ".gitignore").read_text(encoding="utf-8")


def test_a_file_changed_after_the_write_is_excluded_on_reinspection(tmp_path):
    """The dialog stayed open; the earlier inspection is not an authorization."""
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)
    assert bc.inspect_project(item, GIT).state == bc.READY

    write(root, "docs/gotchas/a.md", "a\nsomeone edited this while the dialog was open\n")
    result = bc.commit_project(item, "chore: x", GIT)

    assert result.outcome == bc.COMMITTED
    assert head_files(root) == {"project-baseline.md", "docs/gotchas/b.md"}
    assert "docs/gotchas/a.md" in result.remaining_manager


def test_the_message_describes_the_files_actually_committed(tmp_path):
    root = repo(tmp_path)
    many = {"docs/gotchas/g%02d.md" % i: "g\n" for i in range(8)}
    many["project-baseline.md"] = "# b\n"
    item = manager_writes(root, many)
    write(root, "docs/gotchas/g00.md", "edited\n")

    ins = bc.inspect_project(item, GIT)
    text = mch.compose(ins.files, "1.0").body
    assert "shared lessons: 7 files" in text, "the count must be the final set"


def test_no_evidence_means_nothing_is_eligible(tmp_path):
    root = repo(tmp_path)
    for rel, text in LESSONS.items():
        write(root, rel, text)
    item = bc.BatchItem(str(root), "proj", tuple(LESSONS), None)

    ins = bc.inspect_project(item, GIT)
    assert ins.state == bc.NO_ELIGIBLE
    assert {f.ownership for f in ins.files} == {mch.UNKNOWN}
    assert bc.commit_project(item, "chore: x", GIT).outcome == bc.REFUSED
    assert head_files(root) == {"README.md"}


def test_a_local_only_project_has_nothing_to_offer(tmp_path):
    root = repo(tmp_path, files={".gitignore": "project-baseline.md\ndocs/\n"})
    item = manager_writes(root, LESSONS)
    assert bc.inspect_project(item, GIT).state == bc.NOTHING_CHANGED


def test_a_tracked_but_ignored_file_is_blocked_not_forced(tmp_path):
    root = repo(tmp_path, files={"project-baseline.md": "old\n"})
    write(root, ".gitignore", "project-baseline.md\n")
    run(root, "add", ".gitignore")
    run(root, "commit", "-q", "-m", "ignore it")
    item = manager_writes(root, {"project-baseline.md": "new\n"})

    by = {f.path: f for f in bc.inspect_project(item, GIT).files}
    assert by["project-baseline.md"].ownership == mch.BLOCKED


# ── repository state ──────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"])
def test_an_operation_in_progress_blocks_the_project(tmp_path, marker):
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)
    (root / ".git" / marker).write_text(run(root, "rev-parse", "HEAD").stdout)
    ins = bc.inspect_project(item, GIT)
    assert ins.state == bc.BLOCKED and "in progress" in ins.reason
    assert bc.commit_project(item, "chore: x", GIT).outcome == bc.REFUSED


@pytest.mark.parametrize("marker", ["rebase-merge", "rebase-apply"])
def test_a_rebase_in_progress_blocks_the_project(tmp_path, marker):
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)
    (root / ".git" / marker).mkdir()
    assert bc.inspect_project(item, GIT).state == bc.BLOCKED


def test_detached_head_is_its_own_flagged_state_not_a_branch_warning(tmp_path):
    root = repo(tmp_path)
    run(root, "checkout", "-q", "--detach")
    ins = bc.inspect_project(manager_writes(root, LESSONS), GIT)
    assert ins.detached is True and ins.branch == ""
    assert ins.main_branch is False and ins.state == bc.READY


def test_the_default_branch_is_metadata_and_changes_nothing(tmp_path):
    root = repo(tmp_path)
    run(root, "branch", "-M", "master")
    item = manager_writes(root, LESSONS)
    ins = bc.inspect_project(item, GIT)
    assert ins.main_branch is True and ins.state == bc.READY
    assert bc.commit_project(item, msg(item), GIT).outcome == bc.COMMITTED


def test_a_folder_that_is_not_its_own_repository_is_blocked(tmp_path):
    outer = repo(tmp_path, "outer")
    inner = outer / "child"
    inner.mkdir()
    item = bc.BatchItem(str(inner), "child", ("x.md",),
                        mch.OperationEvidence(written={"x.md": "h"}, complete=True))
    assert bc.inspect_project(item, GIT).state == bc.BLOCKED


def test_a_repository_with_no_commits_is_blocked(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    run(root, "init", "-q")
    item = bc.BatchItem(str(root), "fresh", (), None)
    ins = bc.inspect_project(item, GIT)
    assert ins.state == bc.BLOCKED and "no commits" in ins.reason


# ── a failure undoes only what this batch did ─────────────────────────────

def _failing_hook(root):
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'review says no' >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def test_a_failed_hook_leaves_the_users_staging_and_none_of_ours(tmp_path):
    root = repo(tmp_path)
    write(root, "README.md", "hello\nstaged edit\n")
    run(root, "add", "README.md")
    item = manager_writes(root, LESSONS)
    _failing_hook(root)

    result = bc.commit_project(item, "chore: x", GIT)

    assert result.outcome == bc.FAILED and "review says no" in result.detail
    assert staged(root) == {"README.md"}, "the Manager's staging was left behind"
    assert run(root, "log", "--oneline").stdout.count("\n") == 1


def test_one_failing_project_does_not_stop_the_next(tmp_path):
    bad, good = repo(tmp_path, "bad"), repo(tmp_path, "good")
    items = [manager_writes(bad, LESSONS), manager_writes(good, LESSONS)]
    _failing_hook(bad)
    results = [bc.commit_project(i, "chore: x", GIT) for i in items]
    assert [r.outcome for r in results] == [bc.FAILED, bc.COMMITTED]


def test_an_empty_message_is_refused_before_anything_is_staged(tmp_path):
    root = repo(tmp_path)
    item = manager_writes(root, LESSONS)
    assert bc.commit_project(item, "   ", GIT).outcome == bc.REFUSED
    assert staged(root) == set()


# ── evidence ──────────────────────────────────────────────────────────────

def test_capture_dirty_sees_the_index_as_well_as_the_working_tree(tmp_path):
    root = repo(tmp_path)
    write(root, "a.txt", "staged only\n")
    run(root, "add", "a.txt")
    write(root, "README.md", "hello\nunstaged\n")
    dirty = bc.capture_dirty(GIT, str(root))
    assert set(dirty) == {"a.txt", "README.md"}
    assert all(dirty.values())


def test_no_git_means_no_evidence_not_an_empty_clean_record(tmp_path):
    assert bc.capture_dirty("", str(tmp_path)) is None
    assert bc.capture_dirty(GIT, str(tmp_path)) is None       # no .git here
    ev = bc.make_evidence(GIT, str(tmp_path), None, ["x.md"])
    assert ev.complete is False and ev.dirty_before == {}


# ── end to end: the evidence apply_to_project records is what proves ownership ─

def test_apply_to_project_records_evidence_that_lets_only_our_files_commit(tmp_path):
    from helpers import baseline_copy as bcopy
    from helpers import instructions_posture as ip
    from helpers import instructions_wiring as iw
    from helpers import lessons_delivery as ld

    templates = tmp_path / "templates"
    templates.mkdir()
    base = "# Project Baseline Rules\n"
    (templates / "project-baseline.md").write_text(base, encoding="utf-8")
    root = repo(tmp_path, "wired", files={"README.md": "x\n"})
    write(root, "CLAUDE.md", "@BASIC_INSTRUCTIONS.md\n")
    write(root, "BASIC_INSTRUCTIONS.md", bcopy.LOCAL_INCLUDE_LINE + "\n")
    (root / "project-baseline.md").write_bytes(bcopy.render_copy(base).encode("utf-8"))
    run(root, "add", "-A")
    run(root, "commit", "-q", "-m", "wired")
    write(root, "README.md", "x\nuser is midway through an edit\n")     # not ours

    corpus = tuple(ld.Lesson(s, "# %s\n" % s.source_rel) for s in ld.LESSONS)
    baseline = ip.canonical(str(templates / "project-baseline.md"))
    posture = ip.read_project(str(root), "wired", str(templates), baseline, base, "", {})
    outcome = iw.apply_to_project(posture, baseline, str(templates), "", False, base,
                                  claude_projects={}, git_exe=GIT, lessons=corpus)

    ev = outcome.evidence
    assert ev is not None and ev.complete
    assert set(ev.written) == set(ld.dest_rels())
    assert "README.md" in ev.dirty_before, "the user's edit must be on the record"

    item = bc.BatchItem(str(root), "wired", tuple(outcome.all_files), ev)
    result = bc.commit_project(item, lambda ins: mch.compose(ins.files, "1.0").text(), GIT)

    assert result.outcome == bc.COMMITTED, result.detail
    assert head_files(root) == set(ld.dest_rels())
    assert "README.md" not in head_files(root)
    assert result.remaining_unrelated == 1
    subject = run(root, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "chore(instructions): deliver shared lessons"


def test_an_outcome_without_a_git_exe_carries_no_evidence():
    from helpers import instructions_wiring as iw
    assert iw.ApplyOutcome(iw.OUTCOME_WIRED).evidence is None
