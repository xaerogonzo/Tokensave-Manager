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
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock())
    ctx = _ReleaseCtx(tag="v1.0.0", title="T", notes="n")

    assert dialog._pub_patch_changelog(ctx) is True
    assert ctx.staged_files == ["CHANGELOG.md"]


def test_the_version_written_to_the_changelog_has_no_v_prefix(mocker):
    """`## [1.0.0]` — Keep-a-Changelog headings carry the bare number."""
    patcher = mocker.patch("dialogs.release_wizard.insert_changelog_release",
                           return_value=(True, "inserted"))
    mocker.patch.object(ReleaseWizardDialog, "_set_status")
    dialog = _bare(_sync_cl_var=_Var(True), _has_changelog=True,
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock())

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
                   _changelog_path="CHANGELOG.md", _app=mocker.MagicMock())
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
