"""release_profile -- recognise how a project is built and how its release is made.

The Release Wizard used to assume one world (Nuitka build, zip made on this machine). A
project can now differ on two independent axes, and they must not be conflated:

  * **Build system** (Nuitka, PyInstaller, plus an installer such as Inno Setup): what
    produces the binaries. Informational and a source of defaults -- it does NOT change
    the release steps.
  * **Release profile**: WHO makes the release files. `ci_draft` when the project's own
    `.github/workflows/release.yml` builds from a pushed tag and drafts the release;
    `local_build` when the Wizard builds and zips on this machine.

A build system is a ROW of marker files / strings, never an `if nuitka` branch: adding
PyInstaller was one row, adding the next is another. Every fact keeps its evidence (which
file, which marker) so the header can say why it believes what it says, and a person can
disagree with it.

Pure standard library, no UI. Reads at most `_READ_CAP` bytes per marker file.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

PROFILE_CI_DRAFT = "ci_draft"
PROFILE_LOCAL_BUILD = "local_build"

#: The workflow a project may carry to build and DRAFT its releases from a pushed tag.
RELEASE_WORKFLOW = os.path.join(".github", "workflows", "release.yml")

_READ_CAP = 300_000


@dataclass(frozen=True)
class Marker:
    #: Project-relative glob, POSIX-style (`packaging/*.spec`).
    pattern: str
    #: None = the file merely existing is the evidence; otherwise a regex searched in it.
    regex: "str | None" = None


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    markers: "tuple[Marker, ...]"


BUILD_SYSTEMS: "tuple[Row, ...]" = (
    Row("nuitka", "Nuitka", (
        Marker("build.ps1", r"(?i)nuitka"),
        Marker("build.bat", r"(?i)nuitka"),
        Marker("pyproject.toml", r"(?i)\bnuitka\b"),
    )),
    Row("pyinstaller", "PyInstaller", (
        Marker("*.spec"),
        Marker("packaging/*.spec"),
        Marker("build.ps1", r"(?i)pyinstaller"),
        Marker("pyproject.toml", r"(?i)\bpyinstaller\b"),
    )),
)

INSTALLERS: "tuple[Row, ...]" = (
    Row("inno", "Inno Setup", (
        Marker("*.iss"),
        Marker("packaging/*.iss"),
        Marker("installer/*.iss"),
    )),
)


@dataclass(frozen=True)
class Evidence:
    fact: str
    source: str


@dataclass(frozen=True)
class Profile:
    build_systems: "tuple[str, ...]" = ()
    installers: "tuple[str, ...]" = ()
    release: str = PROFILE_LOCAL_BUILD
    evidence: "tuple[Evidence, ...]" = field(default_factory=tuple)

    @property
    def ci_draft(self) -> bool:
        return self.release == PROFILE_CI_DRAFT

    def label(self) -> str:
        """One line for a header: what was found and who makes the release."""
        names = {r.key: r.label for r in BUILD_SYSTEMS + INSTALLERS}
        parts = [names[k] for k in self.build_systems + self.installers]
        built = " + ".join(parts) if parts else "build system not recognised"
        who = ("release built by the project's workflow (draft)" if self.ci_draft
               else "release built on this machine")
        return f"{built}; {who}"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(_READ_CAP)
    except OSError:
        return ""


def _match(root: str, row: Row) -> "str | None":
    """The first marker of `row` that holds in `root`, as 'file' or 'file ~ regex', else None."""
    for marker in row.markers:
        for hit in sorted(glob.glob(os.path.join(root, *marker.pattern.split("/")))):
            if not os.path.isfile(hit):
                continue
            rel = os.path.relpath(hit, root).replace(os.sep, "/")
            if marker.regex is None:
                return rel
            if re.search(marker.regex, _read(hit)):
                return f"{rel} mentions {marker.regex.replace('(?i)', '')}"
    return None


def workflow_drafts(path: str) -> bool:
    """Does `path` build and DRAFT its releases from a pushed tag?

    A deliberately small text check: the file exists, reacts to `tags:` and creates its
    release with `--draft`. A workflow that does not draft is not one the Wizard may hand
    the release to (it might publish by itself, or create nothing).
    """
    text = _read(os.path.join(path, RELEASE_WORKFLOW))
    return (bool(re.search(r"^\s*tags:", text, re.MULTILINE))
            and "gh release create" in text and "--draft" in text)


def detect(path: str) -> Profile:
    """What `path` is built with and who makes its release. Never raises."""
    evidence: "list[Evidence]" = []
    systems: "list[str]" = []
    for row in BUILD_SYSTEMS:
        hit = _match(path, row)
        if hit:
            systems.append(row.key)
            evidence.append(Evidence(row.label, hit))
    installers: "list[str]" = []
    for row in INSTALLERS:
        hit = _match(path, row)
        if hit:
            installers.append(row.key)
            evidence.append(Evidence(row.label, hit))
    if workflow_drafts(path):
        release = PROFILE_CI_DRAFT
        evidence.append(Evidence("release by workflow",
                                 RELEASE_WORKFLOW.replace(os.sep, "/") + " triggers on tags and uses --draft"))
    else:
        release = PROFILE_LOCAL_BUILD
    return Profile(tuple(systems), tuple(installers), release, tuple(evidence))
