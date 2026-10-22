"""The Release Wizard consumes changelog fragments (Phase 1B).

The acceptance test runs the REAL patch and stage/commit steps against a real git repository:
two fragments in, one release-prep commit out, and the commit must hold every path the
operation changed (CHANGELOG.md and both deletions), leave the tree clean, keep CRLF, and put
each entry exactly once in the versioned section. A fragment consumed but left tracked would
come back in the next release, which is the failure this exists to prevent.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytest.importorskip("tkinter")

from dialogs.release_wizard import ReleaseWizardDialog, _ReleaseCtx
from helpers import changelog_fragments as cf

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

CHANGELOG = (
    "# Changelog\r\n\r\n## [Unreleased]\r\n\r\n### Added\r\n- (old) **Legacy.** stays\r\n\r\n"
    "## [1.0.0] — 2026-01-01\r\n\r\n### Added\r\n- shipped\r\n"
)


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


def _repo(tmp_path, *, commit_second=True):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG.encode("utf-8"))
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "202610221200-a-aaaa.md").write_text("### Added\n- (x) **First.** one\n", newline="\n")
    second = tmp_path / "changelog.d" / "202610221300-b-bbbb.md"
    second.write_text("### Fixed\n- (y) **Second.** two\n  wraps\n", newline="\n")
    _git(tmp_path, "add", "-A" if commit_second else "CHANGELOG.md")
    if not commit_second:
        _git(tmp_path, "add", "changelog.d/202610221200-a-aaaa.md")
    assert _git(tmp_path, "commit", "-qm", "base")[1] == 0
    return tmp_path


def _dialog(repo, mocker, sync=True):
    d = object.__new__(ReleaseWizardDialog)
    d._path = str(repo)
    d._changelog_path = str(repo / "CHANGELOG.md")
    d._has_changelog = True
    d._sync_cl_var = _Var(sync)
    d._frag = cf.collect(repo)
    d._app = _App()
    d._cfg = mocker.MagicMock(git_exe="git")
    d._failures = []
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    d._fail = lambda message, **kw: d._failures.append(message)
    return d


def _ctx(dialog):
    notes = cf.render_section("1.1.0", "2026-10-23", dialog._frag.sections)
    return _ReleaseCtx(tag="v1.1.0", title="t", notes=notes)


def test_two_fragments_become_one_release_commit_holding_every_changed_path(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker)
    ctx = _ctx(d)

    assert d._pub_patch_changelog(ctx) and d._pub_stage_commit(ctx), d._failures

    text = (repo / "CHANGELOG.md").read_bytes().decode("utf-8")
    section = text.split("## [1.1.0]")[1].split("## [1.0.0]")[0]
    assert section.count("**First.**") == 1 and section.count("**Second.**") == 1
    assert "wraps" in section  # continuation line travelled with its bullet
    assert text.count("**First.**") == 1 and text.count("**Second.**") == 1  # nowhere else
    # The patcher puts the new heading directly under [Unreleased], so hand-written
    # [Unreleased] entries are promoted with the release rather than stranded above it.
    assert text.split("## [1.1.0]")[0].rstrip().endswith("## [Unreleased]")
    assert "**Legacy.**" in section
    assert text.count("\r\n") == text.count("\n")  # CRLF kept, no mixed endings

    assert not list((repo / "changelog.d").glob("2*.md"))  # gone from disk
    assert _git(repo, "ls-files", "changelog.d")[0].strip() == ""  # and from the index
    changed = set(_git(repo, "show", "--name-only", "--format=", "HEAD")[0].split())
    assert changed == {"CHANGELOG.md", "changelog.d/202610221200-a-aaaa.md", "changelog.d/202610221300-b-bbbb.md"}
    assert _git(repo, "status", "--porcelain")[0].strip() == ""  # tree clean afterwards
    assert _git(repo, "diff", "--numstat", "HEAD~1", "HEAD", "--", "CHANGELOG.md")[0].split()[1] != str(text.count("\n"))


def test_a_git_rm_failure_stops_before_the_commit_and_a_rerun_gives_identical_notes(tmp_path, mocker):
    repo = _repo(tmp_path, commit_second=False)  # second fragment is untracked: `git rm` will refuse
    d = _dialog(repo, mocker)
    ctx = _ctx(d)

    assert d._pub_patch_changelog(ctx)
    first_changelog = (repo / "CHANGELOG.md").read_bytes()
    assert d._pub_stage_commit(ctx) is False
    assert d._failures and "git restore" in d._failures[0]
    assert len(list((repo / "changelog.d").glob("2*.md"))) == 2  # nothing consumed
    assert _git(repo, "log", "--oneline")[0].count("\n") == 1  # no release commit

    # Follow the recovery the failure message gives, then re-run: same notes, same file.
    assert _git(repo, "restore", "--staged", "--worktree", "CHANGELOG.md", "changelog.d")[1] == 0
    d2 = _dialog(repo, mocker)
    again = _ctx(d2)
    assert again.notes == ctx.notes
    assert d2._pub_patch_changelog(again)
    assert (repo / "CHANGELOG.md").read_bytes() == first_changelog


def test_with_sync_off_nothing_is_staged_or_removed(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker, sync=False)
    ctx = _ctx(d)
    assert d._pub_patch_changelog(ctx) and ctx.remove_files == [] and ctx.staged_files == []
    assert d._pub_stage_commit(ctx)
    assert len(list((repo / "changelog.d").glob("2*.md"))) == 2
    assert "leave 2 changelog fragment(s) in place" in d._fragment_consumption_note()


def test_notes_source_text_names_where_the_draft_came_from(tmp_path, mocker):
    repo = _repo(tmp_path)
    d = _dialog(repo, mocker)
    assert "2 changelog fragment(s)" in d._notes_source_text()
    assert "remove 2 changelog fragment(s)" in d._fragment_consumption_note()
    d._frag = cf.Collected()
    assert d._notes_source_text() == "Source: commits since the last tag."
    assert d._fragment_consumption_note() == ""
    d._frag = cf.Collected(errors=["changelog.d/x.md: empty"])
    assert "Publish is blocked" in d._notes_source_text()


def test_invalid_fragments_block_publish_before_anything_changes(tmp_path, mocker):
    repo = _repo(tmp_path)
    (repo / "changelog.d" / "202610221400-c-cccc.md").write_text("### Added\n", newline="\n")
    d = _dialog(repo, mocker)
    assert d._frag.errors
    warn = mocker.patch("dialogs.release_wizard.messagebox")
    d._publishing = False
    d._next_tag = lambda: "v1.1.0"
    d._title_var = _Var("t")
    d._notes_txt = mocker.MagicMock(get=lambda *a: "notes")
    d._on_publish()
    warn.showwarning.assert_called_once()
    assert not warn.askyesno.called and d._publishing is False
    assert (repo / "CHANGELOG.md").read_bytes().decode("utf-8") == CHANGELOG
