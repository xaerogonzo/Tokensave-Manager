"""VS Code extension lifecycle — what is built, what is installed, what is stale.

The Manager's extension went three minor versions out of date without anything
being able to say so, because the three copies of "which version" never met:

    source      vscode-extension/package.json
    built       the manifest INSIDE the .vsix
    installed   what the editor reports

This module puts them in one place. It is deliberately Tk-free and process-
light so the dialog above it does no thinking of its own — everything here is
testable against a temporary directory and a fake editor.

Two rules are load-bearing, both learned from the way the drift hid:

**The built version comes from the manifest inside the archive, never the
filename.** A filename is metadata anybody can rewrite; the manifest is what
the editor installs the thing as. A stale build renamed to look current passes
every filename check there is.

**Version parity is not freshness.** The PyScope sibling of this bug had all
three versions reading 0.1.0 with an artefact older than the compile that
produced it. A version number cannot see that; a timestamp can. So the two are
separate signals here and stay separate all the way to the screen.
"""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from constants import CREATE_NO_WINDOW
from helpers.vscode_tasks import resolve_editor_argv

#: Where vsce puts the manifest inside the archive.
PACKAGED_MANIFEST = "extension/package.json"

#: Directories that are outputs, dependencies or scratch rather than inputs.
#: `out/` is generated from `src/`, `node_modules/` from the lockfile, and the
#: `.vscode-test*` trees hold downloaded editors and user profiles (17 MB of
#: them at the time of writing) which would dominate a walk and mean nothing.
#:
#: `test/` is excluded on purpose and it is the one worth arguing about: a test
#: change really can change whether the build *passes*, but it cannot change
#: what the artefact *contains*, and freshness here answers the second
#: question. Editing a test does not make an installed extension wrong.
_PRUNED_DIRS = {"out", "node_modules", "test", ".git"}
_PRUNED_PREFIXES = (".vscode-test",)


class Status:
    """The states the lifecycle can be in.

    Deliberately more than a boolean. `stale = True/False` cannot express the
    difference between "the artefact is behind the source" and "the editor is
    behind the artefact", and those two want different actions from the reader.
    """

    UP_TO_DATE = "UP_TO_DATE"
    NO_BUILD = "NO_BUILD"
    SOURCE_NEWER = "SOURCE_NEWER"
    BUILT_DIFFERS = "BUILT_DIFFERS"
    NOT_INSTALLED = "NOT_INSTALLED"
    INSTALLED_DIFFERS = "INSTALLED_DIFFERS"
    UNKNOWN = "UNKNOWN"


#: Shown next to the status. Kept beside the constants so a new state cannot be
#: added without someone deciding what it says to a person.
STATUS_TEXT = {
    Status.UP_TO_DATE: "Up to date",
    Status.NO_BUILD: "Never built",
    Status.SOURCE_NEWER: "Source is newer than the artefact — rebuild",
    Status.BUILT_DIFFERS: "The artefact is a different version from the source",
    Status.NOT_INSTALLED: "Built, but not installed",
    Status.INSTALLED_DIFFERS: "The installed version is not the built one",
    Status.UNKNOWN: "Could not be determined",
}


@dataclass(frozen=True)
class Artefact:
    """A .vsix that has been opened and found to be this extension."""

    path: Path
    publisher: str
    name: str
    version: str
    mtime: float

    @property
    def identity(self) -> str:
        return f"{self.publisher}.{self.name}@{self.version}"


@dataclass(frozen=True)
class ExtensionState:
    """Everything the panel renders, computed in one pass."""

    publisher: str
    name: str
    source_version: str
    built: Artefact | None
    installed_version: str | None
    newest_input: Path | None
    newest_input_mtime: float
    editor_ok: bool
    status: str

    @property
    def qualified_name(self) -> str:
        """`publisher.name` — the id the editor installs and uninstalls by."""
        return f"{self.publisher}.{self.name}"

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)


# ── reading the three versions ────────────────────────────────────────────

def source_manifest(ext_dir: Path) -> dict:
    """The extension's own package.json. The canonical identity lives here."""
    return json.loads(
        (Path(ext_dir) / "package.json").read_text(encoding="utf-8"))


def packaged_identity(vsix: Path) -> tuple | None:
    """`(publisher, name, version)` from inside *vsix*, or None if unreadable.

    None rather than an exception because this runs over every .vsix in a
    directory, and one unreadable file should disqualify itself rather than
    take the whole panel down.
    """
    try:
        with zipfile.ZipFile(vsix) as archive:
            manifest = json.loads(archive.read(PACKAGED_MANIFEST))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    identity = (manifest.get("publisher"), manifest.get("name"),
                manifest.get("version"))
    return None if None in identity else identity


def discover_artefact(ext_dir: Path, publisher: str, name: str) -> Artefact | None:
    """The newest .vsix in *ext_dir* that actually IS this extension.

    Not `max(mtime)` over the glob. Two ways that gets the wrong answer, both
    of which look completely normal on disk: a .vsix copied in from elsewhere
    carries the copy's timestamp and can outrank a genuinely newer build, and
    a renamed file claims whatever version its name claims. So every candidate
    is opened, and only those whose packaged manifest matches this extension
    are eligible.
    """
    candidates = []
    for path in sorted(Path(ext_dir).glob("*.vsix")):
        identity = packaged_identity(path)
        if identity is None:
            continue
        got_publisher, got_name, version = identity
        if (got_publisher, got_name) != (publisher, name):
            continue
        candidates.append(Artefact(path=path, publisher=got_publisher,
                                   name=got_name, version=version,
                                   mtime=path.stat().st_mtime))
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.mtime)


def newest_input(ext_dir: Path) -> tuple:
    """`(path, mtime)` of the most recently touched thing the build reads.

    Everything under the extension directory counts except the pruned
    directories above. An allowlist of `src/**/*.ts` was the first draft and it
    is not enough: editing `.vscodeignore` changes what ships without touching
    a single TypeScript file, and so does adding an icon. Walking and excluding
    the known outputs means a newly packaged asset is covered on the day it is
    added rather than whenever somebody remembers to extend a list.
    """
    newest_path, newest_mtime = None, 0.0
    for root, dirs, files in os.walk(ext_dir):
        dirs[:] = [d for d in dirs
                   if d not in _PRUNED_DIRS
                   and not d.startswith(_PRUNED_PREFIXES)]
        for filename in files:
            if filename.endswith(".vsix"):
                continue          # the artefact is not an input to itself
            path = Path(root) / filename
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_path, newest_mtime = path, mtime
    return newest_path, newest_mtime


# ── the editor ────────────────────────────────────────────────────────────

def resolve_editor(editor_cmd: str) -> str | None:
    """Absolute path to the editor executable, or None if it cannot be found.

    **`subprocess` cannot launch `code` by name on Windows, and the shell can.**
    The VS Code CLI is `code.CMD`, and `CreateProcess` only ever appends `.exe`
    to a bare name — it does not consult `PATHEXT`. So `["code", ...]` raises
    FileNotFoundError from Python while the exact same word works in
    PowerShell, which is a confusing pair of facts to meet at a bug report.
    `shutil.which` does walk `PATHEXT`, so it returns the full `code.CMD` path
    that launches cleanly.

    Only the first token is resolved, and any further tokens in `editor_cmd`
    are dropped. `editor_cmd` is documented as supporting flags, and those
    flags are about opening windows: `-n` is meaningless when listing
    extensions and `--wait` would hang this call forever waiting for an editor
    the user never sees.
    """
    try:
        argv = resolve_editor_argv(editor_cmd)
    except ValueError:                    # unbalanced quotes in the config
        return None
    return argv[0] if argv else None


def _run_editor(editor_cmd: str, args: list, timeout: int = 20):
    """Run the configured editor, returning `(stdout, rc)` or `(None, None)`.

    Never raises. The editor is user-configured and may be absent, may be
    something else entirely, or may hang; none of those should be able to stop
    the panel from rendering what it does know.
    """
    exe = resolve_editor(editor_cmd)
    if exe is None:
        return None, None
    try:
        proc = subprocess.run(
            [exe] + args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    return proc.stdout, proc.returncode


def editor_supports_extensions(editor_cmd: str) -> bool:
    """Whether *editor_cmd* answers `--list-extensions`.

    A capability probe, not a name match. `editor_cmd` defaults to "code" but
    is user-configurable and documented as possibly not being VS Code at all;
    meanwhile `code`, `code.cmd`, `code-insiders` and a full path to code.exe
    are all legitimate and all answer this. Asking is both more permissive and
    more accurate than pattern-matching the basename.
    """
    stdout, rc = _run_editor(editor_cmd, ["--list-extensions"])
    return rc == 0 and stdout is not None


def installed_version(editor_cmd: str, publisher: str, name: str) -> str | None:
    """The installed version of `publisher.name`, or None if not installed.

    `--show-versions` prints `publisher.name@version`, one per line.

    Split on the FIRST "@", not the last. The two halves are not symmetric: an
    extension id is `[a-z0-9][a-z0-9-]*` twice over and cannot contain "@",
    while the version is the open-ended half. Anchoring to the half that cannot
    contain the delimiter is right whatever the version turns out to look like;
    `rpartition` silently returned the wrong id the moment one did.

    Then compare the id EXACTLY: a substring test would let
    `tokensave.tokensave-manager-beta` answer for `tokensave.tokensave-manager`.
    """
    stdout, rc = _run_editor(editor_cmd, ["--list-extensions", "--show-versions"])
    if rc != 0 or not stdout:
        return None
    wanted = f"{publisher}.{name}"
    for line in stdout.splitlines():
        line = line.strip()
        if "@" not in line:
            continue
        identifier, _, version = line.partition("@")
        if identifier == wanted:
            return version
    return None


# ── putting it together ───────────────────────────────────────────────────

def derive_status(source_version: str,
                  built: Artefact | None,
                  installed_version: str | None,
                  newest_input_mtime: float,
                  editor_ok: bool) -> str:
    """Reduce the four observations to the single state the panel leads with.

    More than one condition can hold at once — an artefact can be both a
    different version from the source AND older than the newest source file,
    while the editor holds a third version — so this is a precedence decision,
    not a lookup.
    """
    # The artefact is upstream of the install, so build problems are reported
    # first. Not for tidiness: if the artefact is stale, "the installed version
    # is behind" would send the reader to install a stale thing. Rebuilding is
    # the right action in both cases, and Build & Install performs it.
    if built is None:
        return Status.NO_BUILD

    # BEFORE the timestamp check, and this ordering is the whole subtlety.
    # Bumping a version edits package.json, which IS one of the inputs the
    # freshness walk sees -- so a version bump always moves the newest input
    # past the artefact too. Checking freshness first would therefore report
    # SOURCE_NEWER for every version bump and BUILT_DIFFERS essentially never,
    # collapsing the two states the panel exists to keep apart.
    if built.version != source_version:
        return Status.BUILT_DIFFERS

    # Same version, older artefact: the case no version number can see, and
    # the one PyScope is in right now.
    if newest_input_mtime > built.mtime:
        return Status.SOURCE_NEWER

    # The artefact is current. Only now does the editor's opinion matter.
    if not editor_ok:
        # We could not ask. Saying NOT_INSTALLED here would be the same kind of
        # lie this module exists to stop -- an absence of evidence rendered as
        # evidence of absence.
        return Status.UNKNOWN
    if installed_version is None:
        return Status.NOT_INSTALLED
    if installed_version != built.version:
        return Status.INSTALLED_DIFFERS

    return Status.UP_TO_DATE


def read_state(ext_dir: Path, editor_cmd: str = "code") -> ExtensionState:
    """One pass over disk and the editor, producing everything the panel shows."""
    ext_dir = Path(ext_dir)
    manifest = source_manifest(ext_dir)
    publisher = manifest["publisher"]
    name = manifest["name"]
    source_version = manifest["version"]

    built = discover_artefact(ext_dir, publisher, name)
    input_path, input_mtime = newest_input(ext_dir)

    editor_ok = editor_supports_extensions(editor_cmd)
    installed = (installed_version(editor_cmd, publisher, name)
                 if editor_ok else None)

    return ExtensionState(
        publisher=publisher,
        name=name,
        source_version=source_version,
        built=built,
        installed_version=installed,
        newest_input=input_path,
        newest_input_mtime=input_mtime,
        editor_ok=editor_ok,
        status=derive_status(source_version, built, installed,
                             input_mtime, editor_ok),
    )
