"""release_zip -- package a built `dist\\` as a release zip that cannot be mistaken for a finished one.

The order is the point, and each gate stops the rest:

  1. write `Tokensave-Manager-v<version>-windows.zip.partial`, every entry under ONE
     versioned top folder, so unzipping a new version beside an old one cannot mix two
     versions' files (the old zip put everything at the archive root);
  2. validate the inventory: one prefix, no traversal, the three executables and the
     data they read present, no build or run residue, and a `manager-config.json` that is
     still the clean first-run config;
  3. extract to a fresh folder whose name contains a SPACE and run the smoke test on THAT
     copy, because what people get is the zip, not `dist\\`;
  4. only then hash the exact final bytes into `SHA256SUMS.txt` and promote both files.

A failure at any step leaves no file that looks final and never touches a previous
release; an existing release of the same version is not replaced without `force`.

The checksum detects damage in transit. It is not a signature and says nothing about who
published the zip.

Adapted from `templates/release/make_release_zip.py.tmpl` (itself from OpenChem Studio) for
this build's flat three-executable `dist\\`. Pure standard library; no UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

ARTIFACT_PREFIX = "Tokensave-Manager"
PLATFORM_TAG = "windows"

#: Must be in the archive (relative to the top folder). The data files are ones the app
#: reads at run time and whose absence is a feature that silently vanishes.
REQUIRED = (
    "tokensave-manager.exe", "tokensave-wrapper.exe", "tokensave-manager-cli.exe",
    "manager-config.json", "README.md", "CHANGELOG.md",
    "templates/project-baseline.md", "docs/USER_GUIDE.md",
)

#: Path PARTS that may not appear anywhere (build debris).
FORBIDDEN_PARTS = ("__pycache__",)
#: Directory prefixes (ending in "/") and exact paths that may not ship: what a run of the
#: app leaves behind. `logs/` shipped inside v2.1.0 because a smoke test wrote it into dist.
FORBIDDEN_PATHS = ("logs/",)

#: The keys of the clean config build.ps1 writes. A key outside this set means the file in
#: the archive was rewritten by a run of the app, which adds keys (`mcp_skip_warnings`).
CLEAN_CONFIG_KEYS = frozenset({
    "tokensave_exe", "template_dir", "python_exe", "editor_cmd", "git_exe", "codegraph_exe",
    "search_roots", "project_categories", "user_snippets", "auto_commit_after_sync",
})


class ReleaseError(RuntimeError):
    """The zip cannot be made or does not pass its own checks."""


def zip_name(version: str) -> str:
    return "%s-v%s-%s.zip" % (ARTIFACT_PREFIX, version, PLATFORM_TAG)


def top_folder(version: str) -> str:
    return "%s-v%s-%s" % (ARTIFACT_PREFIX, version, PLATFORM_TAG)


def check_version(version: str) -> None:
    if not version or version.lower() == "unknown" or any(c in version for c in '/\\ "'):
        raise ReleaseError("refusing to build a release with the version %r" % (version,))


def write_zip(source: Path, destination: Path, version: str) -> int:
    """Every file under `source`, deflated, under the versioned top folder. Returns the entry count."""
    top = top_folder(version)
    count = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(source).as_posix()
            if any(part in FORBIDDEN_PARTS for part in PurePosixPath(relative).parts):
                continue
            archive.write(path, "%s/%s" % (top, relative))
            count += 1
    return count


def _entry_problems(name: str, top: str) -> "tuple[list[str], str | None]":
    """(problems, path relative to the top folder or None) for one archive entry."""
    pure = PurePosixPath(name)
    if name.startswith("/") or "\\" in name or ".." in pure.parts or (len(name) > 1 and name[1] == ":"):
        return ["an entry escapes its folder: %r" % name], None
    if pure.parts[0] != top:
        return ["an entry is outside %s/: %r" % (top, name)], None
    relative = "/".join(pure.parts[1:])
    problems = []
    if any(part in FORBIDDEN_PARTS for part in pure.parts):
        problems.append("build debris in the archive: %r" % name)
    for forbidden in FORBIDDEN_PATHS:
        if relative == forbidden.rstrip("/") or (forbidden.endswith("/") and relative.startswith(forbidden)):
            problems.append("a run's residue is in the archive: %r" % name)
    return problems, relative


def validate_inventory(archive_path: Path, version: str) -> "list[str]":
    """Everything wrong with the archive's contents; empty means it may ship."""
    problems: "list[str]" = []
    top = top_folder(version)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            bad = archive.testzip()
            config = archive.read("%s/manager-config.json" % top) if "%s/manager-config.json" % top in names else None
    except (OSError, zipfile.BadZipFile) as exc:
        return ["the archive cannot be read: %s" % exc]
    if bad:
        problems.append("a corrupt entry: %s" % bad)
    if not names:
        return problems + ["the archive is empty"]
    inside = set()
    for name in names:
        found, relative = _entry_problems(name, top)
        problems += found
        if relative is not None:
            inside.add(relative)
    problems += ["missing from the archive: %s" % r for r in REQUIRED if r not in inside]
    problems += _config_problems(config)
    return problems


def _config_problems(raw: "bytes | None") -> "list[str]":
    if raw is None:
        return []  # already reported as missing
    try:
        keys = set(json.loads(raw.decode("utf-8-sig")))
    except (ValueError, TypeError, AttributeError):
        return ["manager-config.json in the archive is not a JSON object"]
    extra = sorted(keys - CLEAN_CONFIG_KEYS)
    return ["manager-config.json holds keys a clean config does not (%s): it was written by a run of the app"
            % ", ".join(extra)] if extra else []


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(sums_path: Path, archive_path: Path, name: str, keep_from: "Path | None" = None) -> None:
    """`<sha256> *<name>` for the exact bytes on disk, keeping other lines of `keep_from`.

    UTF-8, no BOM, LF: a BOM or CRLF makes `sha256sum -c` misread the first name.
    """
    line = "%s *%s" % (sha256_of(archive_path), name)
    own = " *%s" % name
    kept: "list[str]" = []
    if keep_from is not None and keep_from.exists():
        kept = [e for e in keep_from.read_text(encoding="utf-8").splitlines()
                if e.strip() and not e.rstrip().endswith(own)]
    sums_path.write_bytes(("\n".join([line] + kept) + "\n").encode("utf-8"))


def build_release(dist: Path, version: str, out_dir: Path, *, force: bool = False,
                  smoke: "Callable[[Path], bool] | None" = None) -> Path:
    """The whole pipeline. `smoke(extracted_dir) -> bool` judges the extracted copy."""
    check_version(version)
    if not (dist / "tokensave-manager.exe").is_file():
        raise ReleaseError("%s does not hold a built application (no tokensave-manager.exe)" % dist)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / zip_name(version)
    sums = out_dir / "SHA256SUMS.txt"
    partial = final.with_name(final.name + ".partial")
    partial_sums = sums.with_name(sums.name + ".partial")
    if (final.exists() or sums.exists()) and not force:
        raise ReleaseError("%s already exists; pass force to replace it" % final.name)
    for stale in (partial, partial_sums):
        stale.unlink(missing_ok=True)

    scratch: "Path | None" = None
    try:
        count = write_zip(dist, partial, version)
        print("release_zip: wrote %d entries to %s" % (count, partial.name))
        problems = validate_inventory(partial, version)
        if problems:
            raise ReleaseError("the archive failed its inventory check:\n  " + "\n  ".join(problems))
        # A folder name WITH A SPACE: the path a person actually extracts to.
        scratch = Path(tempfile.mkdtemp(prefix="tokensave manager release "))
        with zipfile.ZipFile(partial) as archive:
            archive.extractall(scratch)
        if smoke is not None and not smoke(scratch / top_folder(version)):
            raise ReleaseError("the smoke test failed on the extracted zip")
        # The sums name the FINAL file, which is what a downloader has.
        write_checksums(partial_sums, partial, final.name, keep_from=sums)
        os.replace(partial, final)
        os.replace(partial_sums, sums)
    except BaseException:
        partial.unlink(missing_ok=True)
        partial_sums.unlink(missing_ok=True)
        raise
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    print("release_zip: %s\nrelease_zip: %s" % (final, sums.read_text(encoding="utf-8").strip()))
    return final
