"""tests/test_release_wizard.py — tagging, and stopping when a step fails.

The Release Wizard was the largest untested dialog that performs irreversible
outward-facing actions: it tags, pushes, and creates a GitHub release. It is
also what the currently-deferred release will eventually be cut with.

Two things are worth pinning, and neither needs a window:

* **the tag it computes**, because everything downstream is named after it;
* **that the pipeline stops at the first failed step**. The steps run in a
  fixed order and each returns a bool. If a failure did not halt the rest, a
  CHANGELOG patch that failed would still be followed by a commit, a tag, a
  push and a published release — announcing a version whose notes never
  landed, with the tag already gone to the remote.

Instances are built with `object.__new__` and only the attributes the method
under test reads. Standing up the real dialog needs an App parent (it reaches
`self._app._git`), and that would test Tk plumbing rather than the release
logic.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.release_wizard import ReleaseWizardDialog, _ReleaseCtx
from helpers.changelog_fragments import Collected


class _Var:
    """Stand-in for a tk Variable — the methods under test only call get()."""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value


def _bare(**attrs) -> ReleaseWizardDialog:
    """An instance with no Tk behind it, carrying only what is needed."""
    dialog = object.__new__(ReleaseWizardDialog)
    for key, value in attrs.items():
        setattr(dialog, key, value)
    return dialog


# ── the tag everything else is named after ──────────────────────────────

@pytest.mark.parametrize("bump,expected", [
    ("major", "v2.0.0"),
    ("minor", "v1.3.0"),
    ("patch", "v1.2.4"),
])
def test_the_bump_radio_drives_the_next_tag(bump, expected):
    dialog = _bare(_override_var=_Var(""), _bump_var=_Var(bump),
                   _last_tag="v1.2.3")
    assert dialog._next_tag() == expected


def test_an_explicit_override_beats_the_radio():
    dialog = _bare(_override_var=_Var("v9.9.9"), _bump_var=_Var("patch"),
                   _last_tag="v1.2.3")
    assert dialog._next_tag() == "v9.9.9"


def test_an_override_without_the_v_prefix_gains_one():
    """Existing tags are `vX.Y.Z`; a bare number would sort and match oddly."""
    dialog = _bare(_override_var=_Var("2.0.0"), _bump_var=_Var("patch"),
                   _last_tag="v1.2.3")
    assert dialog._next_tag() == "v2.0.0"


def test_surrounding_whitespace_in_the_override_is_ignored():
    dialog = _bare(_override_var=_Var("  v3.1.0  "), _bump_var=_Var("patch"),
                   _last_tag="v1.2.3")
    assert dialog._next_tag() == "v3.1.0"


def test_a_repo_with_no_tags_starts_at_v0_1_0():
    """Not v0.0.1.

    A patch bump of the v0.0.0 placeholder would give v0.0.1, which reads as
    "the first release is a bugfix" — and leaves no room to describe the one
    after it as a feature.
    """
    dialog = _bare(_override_var=_Var(""), _bump_var=_Var("patch"),
                   _last_tag=None)
    assert dialog._next_tag() == "v0.1.0"


def test_an_untagged_repo_still_honours_an_override():
    dialog = _bare(_override_var=_Var("v1.0.0"), _bump_var=_Var("patch"),
                   _last_tag=None)
    assert dialog._next_tag() == "v1.0.0"


# ── the pipeline halts at the first failure ─────────────────────────────

def _pipeline_dialog(failing_step: "str | None", calls: list):
    """A dialog whose publish steps all record and succeed, bar one."""
    names = ["_pub_build", "_pub_zip", "_pub_write_notes",
             "_pub_patch_changelog", "_pub_stage_commit", "_pub_tag",
             "_pub_push", "_pub_gh_release"]
    dialog = _bare()
    for name in names:
        def _step(ctx, _n=name):
            calls.append(_n)
            return _n != failing_step
        setattr(dialog, name, _step)
    return dialog, names


def test_every_step_runs_when_none_fails(mocker):
    calls: list = []
    dialog, names = _pipeline_dialog(None, calls)
    # Steps 9 and 10 (cleanup + close) touch Tk and the App; stub them out.
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    mocker.patch.object(ReleaseWizardDialog, "_post_after")
    dialog._app = mocker.MagicMock()

    dialog._publish_worker("v1.0.0", "Title", "notes")

    assert calls == names


@pytest.mark.parametrize("failing", [
    "_pub_build", "_pub_zip", "_pub_write_notes", "_pub_patch_changelog",
    "_pub_stage_commit", "_pub_tag", "_pub_push",
])
def test_a_failed_step_stops_everything_after_it(failing, mocker):
    """The irreversibility ladder: each later step is harder to take back.

    A CHANGELOG patch that failed must not be followed by a commit, a tag, a
    push and a GitHub release — which would announce a version whose notes
    never landed, with the tag already on the remote.
    """
    calls: list = []
    dialog, names = _pipeline_dialog(failing, calls)
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    mocker.patch.object(ReleaseWizardDialog, "_post_after")
    dialog._app = mocker.MagicMock()

    dialog._publish_worker("v1.0.0", "Title", "notes")

    assert calls == names[:names.index(failing) + 1]


def test_a_failure_never_reaches_the_success_report(mocker):
    """No "published" message, and the wizard stays open on the failure."""
    calls: list = []
    dialog, _names = _pipeline_dialog("_pub_tag", calls)
    status = mocker.patch.object(ReleaseWizardDialog, "_set_status")
    close = mocker.patch.object(ReleaseWizardDialog, "_post_after")
    dialog._app = mocker.MagicMock()

    dialog._publish_worker("v1.0.0", "Title", "notes")

    assert not any("published" in str(c).lower()
                   for c in status.call_args_list)
    close.assert_not_called()


# ── the CHANGELOG step honours its checkbox ─────────────────────────────

def test_the_changelog_step_is_skipped_when_unticked(mocker):
    """Opting out must not silently patch the file anyway."""
    patcher = mocker.patch("dialogs.release_wizard.insert_changelog_release")
    dialog = _bare(_sync_cl_var=_Var(False), _has_changelog=True)
    ctx = _ReleaseCtx(tag="v1.0.0", title="T", notes="n")

    assert dialog._pub_patch_changelog(ctx) is True
    patcher.assert_not_called()
    assert ctx.staged_files == []


def test_the_changelog_step_is_skipped_when_there_is_no_changelog(mocker):
    patcher = mocker.patch("dialogs.release_wizard.insert_changelog_release")
    dialog = _bare(_sync_cl_var=_Var(True), _has_changelog=False)
    ctx = _ReleaseCtx(tag="v1.0.0", title="T", notes="n")

    assert dialog._pub_patch_changelog(ctx) is True
    patcher.assert_not_called()


def test_a_patched_changelog_is_staged_for_the_release_commit(mocker):
    """Otherwise the tag points at a commit without its own release notes."""
    mocker.patch("dialogs.release_wizard.insert_changelog_release",
                 return_value=(True, "inserted"))
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    dialog = _bare(_sync_cl_var=_Var(True), _has_changelog=True,
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock(),
                   _frag=Collected())
    ctx = _ReleaseCtx(tag="v1.0.0", title="T", notes="n")

    assert dialog._pub_patch_changelog(ctx) is True
    assert ctx.staged_files == ["CHANGELOG.md"]


def test_the_version_written_to_the_changelog_has_no_v_prefix(mocker):
    """`## [1.0.0]` — Keep-a-Changelog headings carry the bare number."""
    patcher = mocker.patch("dialogs.release_wizard.insert_changelog_release",
                           return_value=(True, "inserted"))
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    dialog = _bare(_sync_cl_var=_Var(True), _has_changelog=True,
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock(),
                   _frag=Collected())

    dialog._pub_patch_changelog(_ReleaseCtx(tag="v1.0.0", title="T",
                                            notes="n"))

    assert patcher.call_args[0][1] == "1.0.0"


def test_a_failed_changelog_patch_aborts_and_keeps_the_notes(mocker):
    """The notes temp file is the user's only copy if the wizard closes."""
    mocker.patch("dialogs.release_wizard.insert_changelog_release",
                 return_value=(False, "no [Unreleased] section"))
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    fail = mocker.patch.object(ReleaseWizardDialog, "_fail")
    dialog = _bare(_sync_cl_var=_Var(True), _has_changelog=True,
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock(),
                   _frag=Collected())
    ctx = _ReleaseCtx(tag="v1.0.0", title="T", notes="n")
    ctx.notes_file = "/tmp/notes.md"

    assert dialog._pub_patch_changelog(ctx) is False
    assert fail.call_args.kwargs["keep_temp"] is True
    assert fail.call_args.kwargs["temp_path"] == "/tmp/notes.md"


# ── build-script detection ──────────────────────────────────────────────

def test_powershell_build_script_wins_over_batch(tmp_path):
    (tmp_path / "build.ps1").write_text("", encoding="utf-8")
    (tmp_path / "build.bat").write_text("", encoding="utf-8")
    assert _bare(_path=str(tmp_path))._detect_build_script() == "build.ps1"


def test_batch_is_used_when_there_is_no_powershell_script(tmp_path):
    (tmp_path / "build.bat").write_text("", encoding="utf-8")
    assert _bare(_path=str(tmp_path))._detect_build_script() == "build.bat"


def test_no_build_script_is_reported_as_none(tmp_path):
    assert _bare(_path=str(tmp_path))._detect_build_script() is None


# ── the [Unreleased] probe ──────────────────────────────────────────────

def test_an_unreleased_section_is_detected(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n- thing\n",
                  encoding="utf-8")
    assert _bare(_changelog_path=str(cl))._changelog_has_unreleased() is True


def test_a_changelog_without_an_unreleased_section_is_reported_as_such(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [1.0.0]\n", encoding="utf-8")
    assert _bare(_changelog_path=str(cl))._changelog_has_unreleased() is False


def test_a_missing_changelog_is_not_an_error(tmp_path):
    """Plenty of projects have none; the wizard must still open."""
    missing = str(tmp_path / "nope.md")
    assert _bare(_changelog_path=missing)._changelog_has_unreleased() is False


# ── a project whose own workflow drafts the release ─────────────────────
#
# With `.github/workflows/release.yml` building and DRAFTING from a pushed tag, the Wizard doing
# the build, the zip and `gh release create` as well made two sources for one version's files, and
# `gh release create` fails on a tag the workflow has already drafted. In that mode it stops after
# the push; every claim below is about what it will and will not do then.

from pathlib import Path

from dialogs import release_wizard as rw

_ROOT = Path(__file__).resolve().parent.parent


def _project_with_workflow(tmp_path, text):
    path = tmp_path / ".github" / "workflows" / "release.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return str(tmp_path)


_DRAFTING = ("on:\n  push:\n    tags:\n      - 'v*'\njobs:\n  d:\n    steps:\n"
             "      - run: gh release create \"$T\" --draft\n")


def test_this_repositorys_own_release_workflow_is_one_the_wizard_hands_over_to():
    assert rw._release_workflow_drafts(str(_ROOT)) is True


def test_a_drafting_workflow_is_recognised(tmp_path):
    assert rw._release_workflow_drafts(_project_with_workflow(tmp_path, _DRAFTING)) is True


@pytest.mark.parametrize("text", [
    _DRAFTING.replace("--draft", ""),                                  # would publish by itself
    _DRAFTING.replace("tags:", "branches:"),                           # not triggered by a tag
    _DRAFTING.replace("gh release create", "gh release view"),         # creates no release
    "",
])
def test_a_workflow_that_does_not_draft_from_a_tag_is_not_handed_the_release(tmp_path, text):
    assert rw._release_workflow_drafts(_project_with_workflow(tmp_path, text)) is False


def test_no_workflow_or_an_unreadable_one_means_the_wizard_does_the_whole_job(tmp_path):
    assert rw._release_workflow_drafts(str(tmp_path)) is False
    path = tmp_path / ".github" / "workflows" / "release.yml"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    assert rw._release_workflow_drafts(str(tmp_path)) is False


def _names(dialog):
    return [step.__name__ for step in dialog._release_steps()]


def test_the_default_pipeline_is_unchanged_and_workflow_mode_stops_after_the_push():
    assert _names(_bare()) == ["_pub_build", "_pub_zip", "_pub_write_notes", "_pub_patch_changelog",
                               "_pub_stage_commit", "_pub_tag", "_pub_push", "_pub_gh_release"]
    assert _names(_bare(_release_workflow=True)) == [
        "_pub_check_tag", "_pub_patch_changelog", "_pub_stage_commit", "_pub_tag", "_pub_push"]


_WORKFLOW_ORDER = ["_pub_check_tag", "_pub_patch_changelog", "_pub_stage_commit", "_pub_tag", "_pub_push"]


def _workflow_pipeline(failing, calls):
    dialog = _bare(_release_workflow=True)
    for name in _WORKFLOW_ORDER + ["_pub_build", "_pub_zip", "_pub_write_notes", "_pub_gh_release"]:
        def _step(ctx, _n=name):
            calls.append(_n)
            return _n != failing
        _step.__name__ = name
        setattr(dialog, name, _step)
    return dialog


def _run_worker(dialog, mocker):
    status = mocker.patch.object(ReleaseWizardDialog, "_set_status")
    mocker.patch.object(ReleaseWizardDialog, "_post_after")
    dialog._app = mocker.MagicMock()
    dialog._publish_worker("v1.0.0", "T", "notes")
    return status


def test_workflow_mode_never_builds_zips_or_creates_a_release(mocker):
    calls: list = []
    _run_worker(_workflow_pipeline(None, calls), mocker)
    assert calls == _WORKFLOW_ORDER


@pytest.mark.parametrize("failing", _WORKFLOW_ORDER[:-1])
def test_in_workflow_mode_a_failure_still_stops_everything_after_it(failing, mocker):
    calls: list = []
    _run_worker(_workflow_pipeline(failing, calls), mocker)
    assert calls == _WORKFLOW_ORDER[:_WORKFLOW_ORDER.index(failing) + 1]


def test_workflow_mode_reports_a_pushed_tag_and_a_draft_never_a_published_release(mocker):
    status = _run_worker(_workflow_pipeline(None, []), mocker)
    said = " ".join(str(c) for c in status.call_args_list).lower()
    assert "drafting" in said and "published" not in said


# ── the check that keeps a doomed workflow run from starting ────────────

def _check_dialog(mocker, tmp_path, *, script, result):
    dialog = _bare(_path=str(tmp_path))
    dialog._app = mocker.MagicMock()
    dialog._app._shell_capture.return_value = result
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    fail = mocker.patch.object(ReleaseWizardDialog, "_fail")
    if script:
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "release_info.py").write_text("# stand-in", encoding="utf-8")
    return dialog, fail


def test_a_tag_that_matches_the_version_passes_and_asks_the_projects_own_script(mocker, tmp_path):
    dialog, fail = _check_dialog(mocker, tmp_path, script=True, result=("v2.7.0\n", 0))
    assert dialog._pub_check_tag(_ReleaseCtx("v2.7.0", "t", "n")) is True
    fail.assert_not_called()
    argv = dialog._app._shell_capture.call_args[0][0]
    assert argv[0] == "python" and argv[2:] == ["check-tag", "v2.7.0"] and argv[1].endswith("release_info.py")


def test_a_tag_that_is_not_the_version_stops_before_anything_is_changed(mocker, tmp_path):
    out = "release_info: tag v2.8.0 does not match the project version 2.7.0\n"
    dialog, fail = _check_dialog(mocker, tmp_path, script=True, result=(out, 1))
    assert dialog._pub_check_tag(_ReleaseCtx("v2.8.0", "t", "n")) is False
    message = fail.call_args[0][0]
    assert "not this project's version" in message and "Nothing was changed" in message


def test_a_check_that_could_not_run_is_a_failure_never_a_pass(mocker, tmp_path):
    dialog, fail = _check_dialog(mocker, tmp_path, script=True,
                                 result=("Error: 'python' not found on system PATH.", 1))
    assert dialog._pub_check_tag(_ReleaseCtx("v2.7.0", "t", "n")) is False
    assert "Could not check" in fail.call_args[0][0] and "check-tag v2.7.0" in fail.call_args[0][0]


def test_a_project_without_the_script_is_not_blocked_and_says_it_skipped(mocker, tmp_path):
    dialog, fail = _check_dialog(mocker, tmp_path, script=False, result=("", 0))
    assert dialog._pub_check_tag(_ReleaseCtx("v2.7.0", "t", "n")) is True
    fail.assert_not_called()
    dialog._app._shell_capture.assert_not_called()
    assert "not checking" in dialog._app._log.call_args[0][0]


# ── what Publish asks, and what it refuses ──────────────────────────────

class _Text:
    def get(self, *_args):
        return "release notes"


def _publish_dialog(mocker, *, workflow, sync, has_changelog=True):
    dialog = _bare(_publishing=False, _release_workflow=workflow, _sync_cl_var=_Var(sync),
                   _has_changelog=has_changelog, _frag=Collected(), _title_var=_Var(""), _notes_txt=_Text(),
                   _run_build_var=_Var(False))
    dialog._next_tag = lambda: "v2.7.0"
    dialog._publish_btn = mocker.MagicMock()
    dialog._app = mocker.MagicMock()
    return dialog


def test_workflow_mode_refuses_to_publish_without_the_changelog_step(mocker):
    box = mocker.patch.object(rw, "messagebox")
    thread = mocker.patch.object(rw.threading, "Thread")
    _publish_dialog(mocker, workflow=True, sync=False)._on_publish()
    assert "CHANGELOG sync is required" in box.showwarning.call_args[0][0]
    box.askyesno.assert_not_called()
    thread.assert_not_called()


def test_workflow_mode_confirms_in_terms_of_a_draft_and_starts_the_worker_only_on_yes(mocker):
    box = mocker.patch.object(rw, "messagebox")
    thread = mocker.patch.object(rw.threading, "Thread")
    box.askyesno.return_value = False
    dialog = _publish_dialog(mocker, workflow=True, sync=True)
    dialog._on_publish()
    text = box.askyesno.call_args[0][1]
    assert "DRAFT" in text and "builds nothing" in text and "gh release create" not in text
    thread.assert_not_called()

    box.askyesno.return_value = True
    dialog._on_publish()
    thread.assert_called_once()
    assert thread.call_args.kwargs["target"] == dialog._publish_worker


def test_the_ordinary_confirmation_is_unchanged_for_a_project_without_the_workflow(mocker):
    box = mocker.patch.object(rw, "messagebox")
    mocker.patch.object(rw.threading, "Thread")
    box.askyesno.return_value = False
    _publish_dialog(mocker, workflow=False, sync=False)._on_publish()
    text = box.askyesno.call_args[0][1]
    assert "gh release create" in text and "Zip dist/" in text


def test_the_wizard_refuses_notes_github_would_reject_before_changing_anything(mocker):
    box = mocker.patch.object(rw, "messagebox")
    thread = mocker.patch.object(rw.threading, "Thread")
    dialog = _publish_dialog(mocker, workflow=True, sync=True)
    dialog._notes_txt = type("T", (), {"get": lambda self, *a: "x" * (rw.RELEASE_NOTES_LIMIT + 1)})()
    dialog._on_publish()
    assert "too long" in box.showwarning.call_args[0][0] and "125,000" in box.showwarning.call_args[0][1]
    box.askyesno.assert_not_called()
    thread.assert_not_called()


# ── opening the real dialog ─────────────────────────────────────────────
#
# Every test above stands a bare instance in for the dialog, so none of them ever ran the
# constructor. It crashed on the first real use after fragments were wired in: the fragment
# helpers want a Path, the dialog passed a str, and because that line ran outside the try/except
# that guards the widgets the person got an EMPTY window and no message. These build the real thing.

import shutil
import subprocess
import types

_GIT = shutil.which("git")


def _repo_with_fragment(tmp_path, *, workflow=True):
    def git(*args):
        subprocess.run([_GIT, "-C", str(tmp_path), *args], check=True, capture_output=True,
                       env={**__import__("os").environ, "GIT_CONFIG_GLOBAL": "NUL" if __import__("os").name == "nt" else "/dev/null"})
    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.invalid")
    (tmp_path / "CHANGELOG.md").write_text("# C\n\n## [Unreleased]\n", encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "202601010000-thing-ab12.md").write_text(
        "### Added\n- (x) **A thing.** Why it matters.\n", encoding="utf-8")
    if workflow:
        flow = tmp_path / ".github" / "workflows"
        flow.mkdir(parents=True)
        (flow / "release.yml").write_text(_DRAFTING, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return str(tmp_path)


@pytest.mark.skipif(_GIT is None, reason="needs git")
def test_the_real_dialog_opens_on_a_repository_with_a_changelog_fragment(tk_root, tmp_path, mocker):
    mocker.patch.object(rw, "messagebox")
    path = _repo_with_fragment(tmp_path)
    dialog = ReleaseWizardDialog(tk_root, path, types.SimpleNamespace(git_exe=_GIT))
    try:
        assert dialog.winfo_exists()
        assert hasattr(dialog, "_body") and dialog._release_workflow is True
        assert len(dialog._frag.folded) == 1 and not dialog._frag.errors
        assert "A thing." in dialog._notes_txt.get("1.0", "end")
        rw.messagebox.showerror.assert_not_called()
    finally:
        dialog.destroy()


@pytest.mark.skipif(_GIT is None, reason="needs git")
def test_a_dialog_that_cannot_read_the_repository_says_so_and_closes_instead_of_staying_blank(tk_root, tmp_path, mocker):
    box = mocker.patch.object(rw, "messagebox")
    mocker.patch.object(rw.cf, "collect", side_effect=RuntimeError("boom"))
    path = _repo_with_fragment(tmp_path)
    created = []
    original = rw.ReleaseWizardDialog.__init__

    def spy(self, *a, **k):
        created.append(self)
        original(self, *a, **k)
    mocker.patch.object(rw.ReleaseWizardDialog, "__init__", spy)
    rw.ReleaseWizardDialog(tk_root, path, types.SimpleNamespace(git_exe=_GIT))
    assert "could not open" in box.showerror.call_args[0][0] and "boom" in box.showerror.call_args[0][1]
    assert not created[0].winfo_exists(), "the half-built window was left on screen"
