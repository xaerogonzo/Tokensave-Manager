"""The Release Wizard folds README fragments (readme.d/) in the same release-prep commit.

Same shape as ``test_release_fragments``: the REAL fold, stage and commit steps run against a real
git repository in a temp folder, and the commit must hold every path the operation changed. A fragment
folded into README.md but left tracked would be folded again next release; a README change left out of
the commit would leave the tree dirty for the next Wizard run.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytest.importorskip("tkinter")

from dialogs.release_wizard import ReleaseWizardDialog, _ReleaseCtx, unrelated_dirty_files
from helpers import changelog_fragments as cf
from helpers import readme_fragments as rf

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

README = (
    "# App\r\n\r\n<!-- help:tasks-tab -->\r\n## Tasks tab\r\n\r\nOld tasks text.\r\n\r\n---\r\n\r\n"
    "<!-- help:git-tab -->\r\n## Git tab\r\n\r\nGit text.\r\n"
)
CHANGELOG = "# Changelog\r\n\r\n## [Unreleased]\r\n\r\n## [1.0.0] — 2026-01-01\r\n\r\n### Added\r\n- shipped\r\n"
FIRST = "202610221200-a-aaaa.md"
SECOND = "202610221300-b-bbbb.md"


class _Var:
    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v


def _git(repo, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    return done.stdout + done.stderr, done.returncode


class _App:
    def __init__(self):
        self.logs = []

    def _log(self, text, _colour=None):
        self.logs.append(text)

    @staticmethod
    def _shell_capture(cmd, cwd):
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return done.stdout + done.stderr, done.returncode


def _repo(tmp_path, *, commit_second=True, with_changelog_fragment=False):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "user.email", "t@t")
    (tmp_path / "README.md").write_bytes(README.encode("utf-8"))
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG.encode("utf-8"))
    (tmp_path / "readme.d").mkdir()
    (tmp_path / "readme.d" / FIRST).write_text(
        "<!-- readme: tasks-tab -->\nFirst new paragraph.\n", newline="\n")
    (tmp_path / "readme.d" / SECOND).write_text(
        "<!-- readme: git-tab -->\nSecond new paragraph.\n", newline="\n")
    if with_changelog_fragment:
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "202610221400-c-cccc.md").write_text(
            "### Added\n- (x) **Third.** three\n", newline="\n")
    _git(tmp_path, "add", "-A" if commit_second else "README.md")
    if not commit_second:
        _git(tmp_path, "add", "CHANGELOG.md", f"readme.d/{FIRST}")
    assert _git(tmp_path, "commit", "-qm", "base")[1] == 0
    return tmp_path


def _dialog(repo, mocker, *, sync=True, has_changelog=True):
    d = object.__new__(ReleaseWizardDialog)
    d._path = str(repo)
    d._changelog_path = str(repo / "CHANGELOG.md")
    d._has_changelog = has_changelog
    d._sync_cl_var = _Var(sync)
    d._frag = cf.collect(repo) if (repo / "changelog.d").is_dir() else cf.Collected()
    d._readme = rf.inspect(repo)
    d._app = _App()
    d._cfg = mocker.MagicMock(git_exe="git")
    d._failures = []
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    d._fail = lambda message, **kw: d._failures.append(message)
    return d


def _ctx():
    return _ReleaseCtx(tag="v1.1.0", title="t", notes="## [1.1.0] — 2026-10-23\n\n### Added\n- (x) **Third.** three\n")


def _run_fold_and_commit(d, ctx, *, changelog=True):
    assert d._pub_fold_readme(ctx), d._failures
    if changelog:
        assert d._pub_patch_changelog(ctx), d._failures
    return d._pub_stage_commit(ctx)


def test_readme_fragments_are_folded_and_removed_in_one_release_commit(tmp_path, mocker):
    repo = _repo(tmp_path, with_changelog_fragment=True)
    d = _dialog(repo, mocker)
    ctx = _ctx()

    assert _run_fold_and_commit(d, ctx), d._failures

    text = (repo / "README.md").read_bytes().decode("utf-8")
    assert text.count("First new paragraph.") == 1 and text.count("Second new paragraph.") == 1
    assert text.index("First new paragraph.") < text.index("<!-- help:git-tab -->")  # inside its section
    assert text.count("\r\n") == text.count("\n")  # CRLF kept
    assert not list((repo / "readme.d").glob("2*.md"))
    assert _git(repo, "ls-files", "readme.d")[0].strip() == ""  # gone from the index too
    changed = set(_git(repo, "show", "--name-only", "--format=", "HEAD")[0].split())
    assert changed == {"README.md", "CHANGELOG.md", f"readme.d/{FIRST}", f"readme.d/{SECOND}",
                       "changelog.d/202610221400-c-cccc.md"}
    assert _git(repo, "status", "--porcelain")[0].strip() == ""
    assert _git(repo, "log", "--oneline")[0].count("\n") == 2  # base + exactly one release-prep commit


def test_readme_folds_even_when_the_changelog_sync_is_off(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker, sync=False)
    ctx = _ctx()

    assert _run_fold_and_commit(d, ctx), d._failures

    assert (repo / "CHANGELOG.md").read_bytes().decode("utf-8") == CHANGELOG  # untouched
    changed = set(_git(repo, "show", "--name-only", "--format=", "HEAD")[0].split())
    assert changed == {"README.md", f"readme.d/{FIRST}", f"readme.d/{SECOND}"}
    assert _git(repo, "status", "--porcelain")[0].strip() == ""


def test_readme_folds_in_a_project_with_no_changelog(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker, sync=False, has_changelog=False)
    assert _run_fold_and_commit(d, _ctx(), changelog=False), d._failures
    assert b"First new paragraph." in (repo / "README.md").read_bytes()


def test_a_git_rm_failure_leaves_readme_and_fragments_restorable_by_the_exact_command(tmp_path, mocker):
    repo = _repo(tmp_path, commit_second=False)  # the second fragment is untracked: `git rm` refuses
    d = _dialog(repo, mocker, sync=False)
    ctx = _ctx()

    assert d._pub_fold_readme(ctx)
    folded_readme = (repo / "README.md").read_bytes()
    assert d._pub_stage_commit(ctx) is False
    assert "Recover: git restore --staged --worktree README.md readme.d" in d._failures[0]
    assert len(list((repo / "readme.d").glob("2*.md"))) == 2  # nothing consumed
    assert _git(repo, "log", "--oneline")[0].count("\n") == 1  # no release commit

    # Follow the recovery exactly as the message gives it, then re-run: the same README results.
    assert _git(repo, "restore", "--staged", "--worktree", "README.md", "readme.d")[1] == 0
    assert (repo / "README.md").read_bytes().decode("utf-8") == README
    d2 = _dialog(repo, mocker, sync=False)
    assert d2._pub_fold_readme(_ctx())
    assert (repo / "README.md").read_bytes() == folded_readme


def test_a_rerun_after_a_later_failure_changes_nothing_and_still_removes_the_fragments(tmp_path, mocker):
    """The fold ran, then the build failed: README.md is modified, fragments are still there."""
    repo = _repo(tmp_path)
    assert _dialog(repo, mocker)._pub_fold_readme(_ctx())
    folded = (repo / "README.md").read_bytes()

    d = _dialog(repo, mocker, sync=False)
    assert d._readme.fold == [] and len(d._readme.stale) == 2  # recognised as already folded
    ctx = _ctx()
    assert _run_fold_and_commit(d, ctx, changelog=False), d._failures
    assert (repo / "README.md").read_bytes() == folded  # not folded twice
    changed = set(_git(repo, "show", "--name-only", "--format=", "HEAD")[0].split())
    assert changed == {"README.md", f"readme.d/{FIRST}", f"readme.d/{SECOND}"}


def test_an_invalid_readme_fragment_blocks_publish_before_anything_changes(tmp_path, mocker):
    repo = _repo(tmp_path)
    (repo / "readme.d" / "202610221500-d-dddd.md").write_text("<!-- readme: nowhere -->\ntext\n", newline="\n")
    d = _dialog(repo, mocker)
    assert d._readme.errors
    assert "Publish is blocked" in d._notes_source_text()
    warn = mocker.patch("dialogs.release_wizard.messagebox")
    d._publishing = False
    d._next_tag = lambda: "v1.1.0"
    d._title_var = _Var("t")
    d._notes_txt = mocker.MagicMock(get=lambda *a: "notes")
    d._release_workflow = False
    d._on_publish()
    warn.showwarning.assert_called_once()
    assert "readme.d" in warn.showwarning.call_args.args[1]
    assert not warn.askyesno.called and d._publishing is False
    assert (repo / "README.md").read_bytes().decode("utf-8") == README
    assert len(list((repo / "readme.d").glob("2*.md"))) == 3


def test_no_readme_fragments_means_no_step_no_note_and_no_change(tmp_path, mocker):
    repo = _repo(tmp_path)
    shutil.rmtree(repo / "readme.d")
    d = _dialog(repo, mocker)
    assert d._readme.pending == [] and d._readme.errors == []
    assert "_pub_fold_readme" not in [s.__name__ for s in d._release_steps()]
    assert d._readme_consumption_note() == ""
    ctx = _ctx()
    assert d._pub_fold_readme(ctx) and ctx.staged_files == [] and ctx.remove_files == []
    assert (repo / "README.md").read_bytes().decode("utf-8") == README
    assert rf.inspect(tmp_path / "nowhere").pending == []  # no readme.d at all is not an error


def test_the_fold_runs_before_the_build_and_the_tag_stays_before_the_push(tmp_path, mocker):
    repo = _repo(tmp_path)
    names = [s.__name__ for s in _dialog(repo, mocker)._release_steps()]
    assert names.index("_pub_fold_readme") < names.index("_pub_build")  # a build compiles README.md into Help
    assert names.index("_pub_stage_commit") < names.index("_pub_tag") < names.index("_pub_push")
    flow = _dialog(repo, mocker)
    flow._release_workflow = True
    names = [s.__name__ for s in flow._release_steps()]
    assert names[0] == "_pub_fold_readme" and names.index("_pub_tag") < names.index("_pub_push")


def test_the_confirmation_says_how_many_readme_fragments_are_folded(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker)
    assert d._readme_consumption_note() == "fold 2 README fragment(s) into README.md (in the release-prep commit), "
    assert _dialog(repo, mocker, sync=False)._readme_consumption_note().startswith("fold 2 README fragment(s)")
    d._readme = rf.Inspection(stale=[repo / "readme.d" / FIRST])
    assert d._readme_consumption_note().startswith("remove 1 README fragment(s) already folded")


def test_the_preflight_lets_readme_differ_only_while_it_is_being_folded():
    status = " M README.md\n M CHANGELOG.md\n M src/app.py\n?? notes.txt\n"
    assert unrelated_dirty_files(status, readme_fragments_pending=True) == ["src/app.py", "notes.txt"]
    # No pending fragments: a changed README is somebody's edit, so it is refused as it always was.
    assert unrelated_dirty_files(status, readme_fragments_pending=False) == ["README.md", "src/app.py", "notes.txt"]
    assert unrelated_dirty_files("", True) == []
