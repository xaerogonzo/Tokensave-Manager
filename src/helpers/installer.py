"""installer -- compile the Inno Setup installer from the VALIDATED release zip.

The installer is built from the zip's own contents, not from `dist\\`: the zip has already passed its
inventory check (no config, no logs, the three executables present) and a smoke test on its extracted
copy, so an installer compiled from that tree cannot hold a different set of files. It is then named
into the same `SHA256SUMS.txt`, hashed from its final bytes.

Pure standard library; no UI. Order, each step stopping the rest:

  1. the zip's inventory is validated again (a zip handed in by hand is not trusted);
  2. extract to a scratch folder (with a space in its name: the compiler must cope);
  3. compile `packaging/tokensave-manager.iss` with the version and the extracted folder;
  4. the output must exist and be non-empty;
  5. it is promoted next to the zip and added to the checksums, hashed from its final bytes.

Adapted from OpenChem Studio's `tools/make_installer.py` (xaerogonzo/OpenChem-Studio).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from helpers import release_zip as rz

SCRIPT = Path(__file__).resolve().parent.parent.parent / "packaging" / "tokensave-manager.iss"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class InstallerError(RuntimeError):
    """The installer cannot be built or does not pass its own checks."""


def check_version(version: str) -> None:
    """The zip's check, plus the shape Windows version resources and the driver's bump need."""
    rz.check_version(version)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise InstallerError("the installer needs a version like 2.7.0, got %r" % (version,))


def installer_name(version: str) -> str:
    check_version(version)
    return "%s-v%s-%s-setup.exe" % (rz.ARTIFACT_PREFIX, version, rz.PLATFORM_TAG)


def find_iscc(env: "dict[str, str] | None" = None, explicit: str = "") -> str:
    """The Inno Setup compiler: an explicit path, `ISCC`, a per-user install, a system install, PATH.

    Never a bare name: "ISCC" on a machine without it is a WinError 2 that names our command line
    rather than the missing tool.
    """
    env = os.environ if env is None else env
    candidates = [explicit, env.get("ISCC", "")]
    local = env.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe"))
    for key in ("ProgramFiles(x86)", "ProgramFiles"):
        if env.get(key):
            candidates.append(os.path.join(env[key], "Inno Setup 6", "ISCC.exe"))
    candidates.append(shutil.which("ISCC") or "")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise InstallerError("Inno Setup's ISCC.exe was not found (set ISCC, or install Inno Setup 6)")


def command_line(iscc: str, script: Path, defines: "dict[str, str]") -> str:
    """One command-line STRING. ISCC re-parses its arguments, so a path with a space must reach it as
    `/DName="a b"`; a list passed to `subprocess` would escape the quotes ISCC needs."""
    parts = ['"%s"' % iscc, "/Q"]
    for name, value in defines.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise InstallerError("not a define name: %r" % name)
        parts.append('/D%s' % name if value == "" else '/D%s="%s"' % (name, value))
    parts.append('"%s"' % script)
    return " ".join(parts)


def compile_installer(iscc: str, source_dir: Path, version: str, out_dir: Path, *, test: bool = False,
                      script: Path = SCRIPT) -> Path:
    """Compile and return the produced setup file. Raises with the compiler's output on failure."""
    check_version(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    defines = {"AppVersion": version, "SourceDir": str(source_dir), "OutputDir": str(out_dir)}
    if test:
        defines["TestBuild"] = ""
    done = subprocess.run(command_line(iscc, script, defines), capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW, timeout=900)
    if done.returncode != 0:
        raise InstallerError("ISCC failed (exit %d):\n%s" % (done.returncode, (done.stdout + done.stderr).strip()))
    made = sorted(out_dir.glob("*-setup.exe"), key=lambda p: p.stat().st_mtime)
    if not made or made[-1].stat().st_size == 0:
        raise InstallerError("ISCC reported success but produced no setup file in %s" % out_dir)
    return made[-1]


def extract_release(zip_path: Path, version: str, scratch: Path) -> Path:
    """Validate the zip's inventory, extract it, and return its versioned top folder."""
    problems = rz.validate_inventory(zip_path, version)
    if problems:
        raise InstallerError("the release zip failed its inventory check:\n  " + "\n  ".join(problems))
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(scratch)
    top = scratch / rz.top_folder(version)
    if not (top / "tokensave-manager.exe").is_file():
        raise InstallerError("%s does not hold tokensave-manager.exe" % top)
    return top


def build_installer(zip_path: Path, version: str, out_dir: Path, *, iscc: str = "", force: bool = False) -> Path:
    """The whole pipeline. The driven install/upgrade/uninstall test is `installer_drive`, run by
    `scripts/make_installer.py` BEFORE this, on a test build of the same script."""
    final = out_dir / installer_name(version)
    sums = out_dir / "SHA256SUMS.txt"
    if final.exists() and not force:
        raise InstallerError("%s already exists; pass force to replace it" % final.name)
    compiler = find_iscc(explicit=iscc)
    scratch = Path(tempfile.mkdtemp(prefix="tokensave manager installer "))
    try:
        top = extract_release(zip_path, version, scratch)
        built = compile_installer(compiler, top, version, scratch / "out")
        partial = final.with_name(final.name + ".partial")
        shutil.copyfile(built, partial)
        rz.write_checksums(sums.with_name(sums.name + ".partial"), partial, final.name, keep_from=sums)
        os.replace(partial, final)
        os.replace(sums.with_name(sums.name + ".partial"), sums)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        for stale in (final.with_name(final.name + ".partial"), sums.with_name(sums.name + ".partial")):
            stale.unlink(missing_ok=True)
    print("installer: %s\ninstaller: %s" % (final, sums.read_text(encoding="utf-8").strip()))
    return final
