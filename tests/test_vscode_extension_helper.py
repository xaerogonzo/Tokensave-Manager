"""tests/test_vscode_extension_helper.py — the extension lifecycle model.

What this is really testing is that the panel can see the two bugs that
motivated it, which are NOT the same bug:

  * **TokenSave Manager's**: the source version moved to 2.6.0 and the built
    and installed artefacts stayed at 2.3.x. A version comparison finds this.
  * **PyScope's**: every version reads 0.1.0 and the artefact is simply older
    than the compile. No version comparison can ever find this; only a
    timestamp can.

A model that reported "three matching version numbers, all good" would be
green on the second one forever, which is precisely how it survived.

Timestamps here are always set explicitly with `os.utime` rather than by
writing a file and hoping. Filesystem timestamp granularity is coarse enough
that "modify, then compare" can produce equality, and a freshness test that
flakes is worse than none.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from helpers import vscode_extension as vx
from helpers import vscode_tasks as vt
from helpers.vscode_extension import Artefact, Status

PUBLISHER = "tokensave"
NAME = "tokensave-manager"


# ── fixtures ──────────────────────────────────────────────────────────────

def _write_vsix(path: Path, publisher=PUBLISHER, name=NAME, version="1.0.0",
                mtime=None, body=None):
    """A minimal .vsix carrying a real manifest at the path vsce uses."""
    manifest = body if body is not None else json.dumps(
        {"publisher": publisher, "name": name, "version": version})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(vx.PACKAGED_MANIFEST, manifest)
        archive.writestr("extension/out/extension.js", "//")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _extension_dir(tmp_path: Path, version="1.0.0") -> Path:
    ext = tmp_path / "vscode-extension"
    (ext / "src").mkdir(parents=True)
    (ext / "package.json").write_text(json.dumps(
        {"publisher": PUBLISHER, "name": NAME, "version": version}),
        encoding="utf-8")
    (ext / "src" / "extension.ts").write_text("//", encoding="utf-8")
    (ext / ".vscodeignore").write_text("src/**\n", encoding="utf-8")
    return ext


def _artefact(version="1.0.0", mtime=1000.0) -> Artefact:
    return Artefact(path=Path("x.vsix"), publisher=PUBLISHER, name=NAME,
                    version=version, mtime=mtime)


# ── precedence ────────────────────────────────────────────────────────────
#
# More than one condition can hold at once, so the order these are checked in
# is a decision rather than an implementation detail. Each test below pins one
# edge of that decision.

def test_nothing_built_is_its_own_state():
    assert vx.derive_status("1.0.0", None, None, 5000.0, True) == Status.NO_BUILD


def test_a_version_bump_reads_as_built_differs_not_as_stale():
    """THE ordering test, and the reason BUILT_DIFFERS is checked first.

    Bumping a version edits package.json, which is itself one of the inputs the
    freshness walk sees — so a version bump ALWAYS moves the newest input past
    the artefact as well. Check freshness first and every bump reports
    SOURCE_NEWER while BUILT_DIFFERS becomes unreachable, collapsing the two
    states the panel exists to keep apart.
    """
    status = vx.derive_status(
        source_version="1.0.1",
        built=_artefact(version="1.0.0", mtime=1000.0),
        installed_version="1.0.0",
        newest_input_mtime=2000.0,     # newer too, as it always would be
        editor_ok=True,
    )
    assert status == Status.BUILT_DIFFERS


def test_matching_versions_with_a_newer_source_is_stale():
    """PyScope's live condition: three equal version numbers, old artefact."""
    status = vx.derive_status(
        source_version="0.1.0",
        built=_artefact(version="0.1.0", mtime=1000.0),
        installed_version="0.1.0",
        newest_input_mtime=1060.0,     # one minute later, set not written
        editor_ok=True,
    )
    assert status == Status.SOURCE_NEWER


def test_an_unaskable_editor_is_unknown_never_not_installed():
    """Absence of evidence must not render as evidence of absence — that is
    the same class of mistake as the drift this module exists to surface."""
    status = vx.derive_status(
        source_version="1.0.0",
        built=_artefact(mtime=5000.0),
        installed_version=None,
        newest_input_mtime=1000.0,
        editor_ok=False,
    )
    assert status == Status.UNKNOWN


def test_an_askable_editor_with_nothing_installed_says_so():
    status = vx.derive_status("1.0.0", _artefact(mtime=5000.0), None,
                              1000.0, True)
    assert status == Status.NOT_INSTALLED


def test_a_current_artefact_and_an_older_install_is_its_own_state():
    """Independent of build freshness: the artefact here is perfectly good."""
    status = vx.derive_status(
        source_version="1.0.0",
        built=_artefact(version="1.0.0", mtime=5000.0),
        installed_version="0.9.0",
        newest_input_mtime=1000.0,
        editor_ok=True,
    )
    assert status == Status.INSTALLED_DIFFERS


def test_everything_aligned_is_up_to_date():
    status = vx.derive_status("1.0.0", _artefact(version="1.0.0", mtime=5000.0),
                              "1.0.0", 1000.0, True)
    assert status == Status.UP_TO_DATE


def test_a_stale_build_outranks_an_out_of_date_install():
    """Both are true; the rebuild is reported because it is the fix for both.
    Naming the install first would send the reader to install a stale thing."""
    status = vx.derive_status(
        source_version="1.0.0",
        built=_artefact(version="1.0.0", mtime=1000.0),
        installed_version="0.9.0",
        newest_input_mtime=2000.0,
        editor_ok=True,
    )
    assert status == Status.SOURCE_NEWER


# ── artefact discovery ────────────────────────────────────────────────────

def test_the_built_version_comes_from_the_manifest_not_the_filename(tmp_path):
    """A stale build renamed to look current is the case a filename check
    cannot see, and renaming is a thing people do."""
    ext = _extension_dir(tmp_path)
    _write_vsix(ext / f"{NAME}-win32-x64-9.9.9.vsix", version="0.1.0")

    found = vx.discover_artefact(ext, PUBLISHER, NAME)
    assert found.version == "0.1.0"


def test_a_vsix_for_another_extension_is_not_this_extensions_artefact(tmp_path):
    ext = _extension_dir(tmp_path)
    _write_vsix(ext / "something-else-2.0.0.vsix", publisher="someone",
                name="other", version="2.0.0")
    assert vx.discover_artefact(ext, PUBLISHER, NAME) is None


def test_a_foreign_vsix_does_not_outrank_the_real_one_by_being_newer(tmp_path):
    """`max(mtime)` over the glob would pick the stranger. Copying a .vsix into
    the directory gives it the copy's timestamp, so this is not contrived."""
    ext = _extension_dir(tmp_path)
    _write_vsix(ext / "ours-1.0.0.vsix", version="1.0.0", mtime=1000.0)
    _write_vsix(ext / "stranger.vsix", publisher="someone", name="other",
                version="5.0.0", mtime=9000.0)

    found = vx.discover_artefact(ext, PUBLISHER, NAME)
    assert (found.version, found.path.name) == ("1.0.0", "ours-1.0.0.vsix")


def test_the_newest_valid_artefact_wins_among_several(tmp_path):
    ext = _extension_dir(tmp_path)
    _write_vsix(ext / "old.vsix", version="1.0.0", mtime=1000.0)
    _write_vsix(ext / "new.vsix", version="1.0.1", mtime=9000.0)
    assert vx.discover_artefact(ext, PUBLISHER, NAME).version == "1.0.1"


@pytest.mark.parametrize("corrupt", ["not a zip at all", None])
def test_an_unreadable_vsix_disqualifies_itself_rather_than_the_panel(
        tmp_path, corrupt):
    """One bad file in a directory must not take the whole reading down."""
    ext = _extension_dir(tmp_path)
    bad = ext / "bad.vsix"
    if corrupt is None:
        _write_vsix(bad, body="{ not json", mtime=9000.0)
    else:
        bad.write_text(corrupt, encoding="utf-8")
        os.utime(bad, (9000.0, 9000.0))
    _write_vsix(ext / "good.vsix", version="1.0.0", mtime=1000.0)

    found = vx.discover_artefact(ext, PUBLISHER, NAME)
    assert found is not None and found.version == "1.0.0"


# ── freshness inputs ──────────────────────────────────────────────────────

def test_a_vscodeignore_edit_counts_as_a_build_input(tmp_path):
    """It changes what ships without touching a single .ts file, which is why
    the first draft of this — a glob over src/**/*.ts — was not enough."""
    ext = _extension_dir(tmp_path)
    for path in ext.rglob("*"):
        if path.is_file():
            os.utime(path, (1000.0, 1000.0))
    os.utime(ext / ".vscodeignore", (5000.0, 5000.0))

    newest, mtime = vx.newest_input(ext)
    assert newest.name == ".vscodeignore" and mtime == 5000.0


def test_generated_and_installed_trees_are_not_inputs(tmp_path):
    """out/ is produced by the build and node_modules by npm ci; if either
    counted, every build would immediately report itself stale."""
    ext = _extension_dir(tmp_path)
    for path in ext.rglob("*"):
        if path.is_file():
            os.utime(path, (1000.0, 1000.0))
    for directory in ("out", "node_modules", ".vscode-test"):
        (ext / directory).mkdir()
        generated = ext / directory / "thing.js"
        generated.write_text("//", encoding="utf-8")
        os.utime(generated, (9000.0, 9000.0))

    _, mtime = vx.newest_input(ext)
    assert mtime == 1000.0


def test_the_artefact_is_not_an_input_to_itself(tmp_path):
    """Otherwise a fresh build would be stale the moment it finished."""
    ext = _extension_dir(tmp_path)
    for path in ext.rglob("*"):
        if path.is_file():
            os.utime(path, (1000.0, 1000.0))
    _write_vsix(ext / "built.vsix", mtime=9000.0)

    _, mtime = vx.newest_input(ext)
    assert mtime == 1000.0


def test_a_freshly_built_extension_is_up_to_date(tmp_path, monkeypatch):
    """The end-to-end shape of the good case, through read_state."""
    ext = _extension_dir(tmp_path, version="1.0.0")
    for path in ext.rglob("*"):
        if path.is_file():
            os.utime(path, (1000.0, 1000.0))
    _write_vsix(ext / "built.vsix", version="1.0.0", mtime=2000.0)

    monkeypatch.setattr(vx, "_run_editor", lambda cmd, args, timeout=20: (
        f"{PUBLISHER}.{NAME}@1.0.0\n", 0))

    state = vx.read_state(ext, "code")
    assert state.status == Status.UP_TO_DATE
    assert state.qualified_name == f"{PUBLISHER}.{NAME}"


def test_read_state_sees_the_pyscope_bug(tmp_path, monkeypatch):
    """The canonical regression: every version agrees, the artefact is older
    than the source, and the state must still say rebuild."""
    ext = _extension_dir(tmp_path, version="1.0.0")
    # Every input onto one old baseline first. Without this the files just
    # written carry the real wall clock and outrank the timestamps under test,
    # so the assertion about WHICH input is newest passes or fails by accident.
    for path in ext.rglob("*"):
        if path.is_file():
            os.utime(path, (1000.0, 1000.0))
    _write_vsix(ext / "built.vsix", version="1.0.0", mtime=2000.0)
    os.utime(ext / "src" / "extension.ts", (2060.0, 2060.0))

    monkeypatch.setattr(vx, "_run_editor", lambda cmd, args, timeout=20: (
        f"{PUBLISHER}.{NAME}@1.0.0\n", 0))

    state = vx.read_state(ext, "code")
    assert state.source_version == state.built.version == state.installed_version
    assert state.status == Status.SOURCE_NEWER
    assert state.newest_input.name == "extension.ts"


# ── the installed lookup ──────────────────────────────────────────────────

def test_the_installed_id_is_matched_exactly_not_by_substring(monkeypatch):
    """`publisher.name-beta` starts with `publisher.name`. A substring test
    would report the beta's version as this extension's."""
    listing = (f"{PUBLISHER}.{NAME}-beta@9.9.9\n"
               f"{PUBLISHER}.{NAME}@1.2.3\n"
               "someone.else@0.0.1\n")
    monkeypatch.setattr(vx, "_run_editor",
                        lambda cmd, args, timeout=20: (listing, 0))
    assert vx.installed_version("code", PUBLISHER, NAME) == "1.2.3"


def test_only_the_beta_installed_does_not_answer_for_the_real_one(monkeypatch):
    monkeypatch.setattr(vx, "_run_editor", lambda cmd, args, timeout=20: (
        f"{PUBLISHER}.{NAME}-beta@9.9.9\n", 0))
    assert vx.installed_version("code", PUBLISHER, NAME) is None


def test_a_version_containing_an_at_sign_still_parses(monkeypatch):
    """Split on the FIRST @. The id cannot contain one; the version is the
    open-ended half, so it is the half that must absorb any extras."""
    monkeypatch.setattr(vx, "_run_editor", lambda cmd, args, timeout=20: (
        f"{PUBLISHER}.{NAME}@1.0.0-rc@2\n", 0))
    assert vx.installed_version("code", PUBLISHER, NAME) == "1.0.0-rc@2"


def test_an_editor_that_cannot_be_run_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(vx, "_run_editor",
                        lambda cmd, args, timeout=20: (None, None))
    assert vx.editor_supports_extensions("nonsense") is False
    assert vx.installed_version("nonsense", PUBLISHER, NAME) is None


def test_the_editor_probe_asks_rather_than_matching_a_name(monkeypatch):
    """`code`, `code.cmd`, `code-insiders` and a full path all answer this;
    a basename pattern would have to enumerate them and would still be wrong
    for the next one."""
    seen = {}

    def fake(cmd, args, timeout=20):
        seen["cmd"], seen["args"] = cmd, args
        return "", 0

    monkeypatch.setattr(vx, "_run_editor", fake)
    assert vx.editor_supports_extensions("/opt/weird/editor") is True
    assert seen == {"cmd": "/opt/weird/editor", "args": ["--list-extensions"]}


# ── resolving the editor executable ───────────────────────────────────────
#
# The lookup itself lives in helpers/vscode_tasks.py, which is the module that
# launches editors; `resolve_editor` here is the drop-the-flags view of it.
# These patch `vt.shutil` for that reason.
#
# Windows-specific and worth the tests: the VS Code CLI is `code.CMD`, and
# CreateProcess only ever appends `.exe` to a bare name. `["code", ...]`
# therefore raises FileNotFoundError from Python while the identical word works
# in PowerShell — and because _run_editor swallows OSError, the symptom was
# "the editor did not answer" for an editor that was sitting right there.

def test_a_bare_name_is_resolved_through_pathext(monkeypatch):
    seen = {}

    def fake_which(name):
        seen["name"] = name
        return r"C:\VS Code\bin\code.CMD"

    monkeypatch.setattr(vt.shutil, "which", fake_which)
    assert vx.resolve_editor("code") == r"C:\VS Code\bin\code.CMD"
    assert seen["name"] == "code"


def test_flags_in_the_configured_command_are_dropped(monkeypatch):
    """`editor_cmd` is documented as accepting flags, and they are all about
    opening a window. `-n` is meaningless when listing extensions and
    `--wait` would hang this call forever on an editor nobody can see."""
    monkeypatch.setattr(vt.shutil, "which", lambda name: f"/resolved/{name}")
    assert vx.resolve_editor("code -n --wait") == "/resolved/code"


def test_an_unresolvable_editor_is_none_not_a_guess(monkeypatch):
    monkeypatch.setattr(vt.shutil, "which", lambda name: None)
    assert vx.resolve_editor("nonsense") is None


@pytest.mark.parametrize("broken", ['code "unbalanced', "", "   "])
def test_an_unparseable_editor_command_is_none(broken, monkeypatch):
    monkeypatch.setattr(vt.shutil, "which", lambda name: "/resolved")
    assert vx.resolve_editor(broken) is None


def test_the_resolved_path_is_what_gets_run(monkeypatch):
    """Not the configured word. This is the whole point of the resolution."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        class R:
            stdout, returncode = "", 0
        return R()

    monkeypatch.setattr(vt.shutil, "which", lambda name: r"C:\VS Code\code.CMD")
    monkeypatch.setattr(vx.subprocess, "run", fake_run)
    vx._run_editor("code", ["--list-extensions"])
    assert seen["argv"][0] == r"C:\VS Code\code.CMD"


def test_an_editor_that_cannot_be_resolved_never_reaches_subprocess(monkeypatch):
    """Otherwise the FileNotFoundError path is exercised on every call for an
    editor that simply is not installed."""
    def explode(*a, **k):
        raise AssertionError("subprocess must not be reached")

    monkeypatch.setattr(vt.shutil, "which", lambda name: None)
    monkeypatch.setattr(vx.subprocess, "run", explode)
    assert vx._run_editor("nope", ["--list-extensions"]) == (None, None)
