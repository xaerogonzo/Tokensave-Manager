"""Verify a built .vsix before it becomes a release asset.

A release artefact is the one build nobody re-checks, so the checks that matter
run here rather than being trusted to `.vscodeignore` alone. Two independent
failure modes, both silent:

**A bundled CLI shipping by accident.** `.vscodeignore` excluded only
`bin/**/README.md` until Roadmap-13, so a locally built
`bin/windows-x64/tokensave-manager-cli.exe` — gitignored, but present after any
run of the Manager's build.ps1 — would have been packaged. That turns a ~14 KB
extension into a ~15 MB one and ships a snapshot CLI that goes stale the moment
the Manager changes. The ignore rule is fixed; this is the net under it, and it
still holds if that file is edited wrongly later.

**Version drift.** `constants.APP_VERSION` is the project's single canonical
version. The extension manifest carries its own copy, and the packaged artefact
carries a third. The Python suite checks the first two against each other; only
here can the third — what is actually inside the built package — be checked.

Run from the repository root. Exits non-zero with a specific reason.
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "vscode-extension"

#: Nothing matching these may appear in the package.
FORBIDDEN = (
    re.compile(r"(?i)\.exe$"),
    re.compile(r"(?i)(^|/)node_modules/"),
    re.compile(r"(?i)(^|/)extension/src/"),
    re.compile(r"(?i)(^|/)extension/test/"),
    re.compile(r"(?i)\.ts$"),
    re.compile(r"(?i)\.map$"),
)

#: At least one entry must match each of these, or the package is not usable.
REQUIRED = (
    re.compile(r"(?i)(^|/)extension/package\.json$"),
    re.compile(r"(?i)(^|/)extension/out/extension\.js$"),
    re.compile(r"(?i)(^|/)extension/out/cli\.js$"),
    re.compile(r"(?i)(^|/)extension/out/diagnostics\.js$"),
    re.compile(r"(?i)(^|/)extension/README\.md$"),
)

#: A package this big means a binary got in despite the checks above.
MAX_BYTES = 2 * 1024 * 1024


def app_version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from constants import APP_VERSION
    return APP_VERSION


def find_package() -> Path:
    candidates = sorted(EXTENSION.glob("*.vsix"))
    if not candidates:
        sys.exit("no .vsix found in vscode-extension/ — did packaging run?")
    if len(candidates) > 1:
        # Two artefacts means one is stale, and uploading the wrong one is
        # exactly the mistake this script exists to stop.
        names = ", ".join(p.name for p in candidates)
        sys.exit(f"more than one .vsix present ({names}); clean the directory "
                 "so there is no question which one ships")
    return candidates[0]


def check_package(package: Path, version: str) -> list:
    """Everything wrong with *package*, as a list of reasons. Empty is a pass.

    Separated from `main` so the rules can be tested against synthetic
    archives — a release check nobody has seen fail is a release check nobody
    knows works.
    """
    problems: list = []

    if f"-{version}.vsix" not in package.name:
        problems.append(
            f"{package.name} does not carry the canonical version {version}")

    size = package.stat().st_size
    if size > MAX_BYTES:
        problems.append(
            f"{package.name} is {size / 1024 / 1024:.1f} MB, over the "
            f"{MAX_BYTES / 1024 / 1024:.0f} MB ceiling — something large got in")

    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()

    for pattern in FORBIDDEN:
        hits = [n for n in names if pattern.search(n)]
        if hits:
            problems.append(
                f"forbidden entries matching {pattern.pattern}: "
                + ", ".join(sorted(hits)[:5]))

    for pattern in REQUIRED:
        if not any(pattern.search(n) for n in names):
            problems.append(f"nothing matches required {pattern.pattern}")

    return problems


def main() -> int:
    package = find_package()
    version = app_version()
    problems = check_package(package, version)

    if problems:
        print(f"{package.name}: NOT fit to release", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    size_kb = package.stat().st_size / 1024
    print(f"{package.name}: {size_kb:.0f} KB, version {version} "
          "— ok to release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
