"""Build the Windows installer from the release zip, after proving it works.

    python scripts/make_installer.py --version 2.7.0

Needs `release/Tokensave-Manager-v<version>-windows.zip` (made by `build.ps1 -Release`) and Inno Setup 6.
In order: a TEST build of `packaging/tokensave-manager.iss` is really installed, upgraded and
uninstalled (`helpers/installer_drive.py`), with the installed application smoke-tested; only then is
the shipped installer compiled from the zip's own validated contents and named into `SHA256SUMS.txt`.
The installer is not signed: Windows SmartScreen will say "unknown publisher".
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from helpers import installer as inst  # noqa: E402
from helpers import installer_drive as drive  # noqa: E402
from helpers import release_zip as rz  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from drive_installer import smoke_installed  # noqa: E402


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--iscc", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    zip_path = args.out_dir / rz.zip_name(args.version)
    if not zip_path.is_file():
        print("make_installer: %s is missing; run build.ps1 -Release first" % zip_path, file=sys.stderr)
        return 1
    scratch = Path(tempfile.mkdtemp(prefix="tokensave manager installer source "))
    try:
        tree = inst.extract_release(zip_path, args.version, scratch)
        checks = drive.drive(tree, args.version, iscc=args.iscc, smoke=smoke_installed(args.version))
        for check in checks:
            print(check.line())
        if not checks or any(not c.ok for c in checks):
            print("make_installer: the driven install test failed; no installer was made", file=sys.stderr)
            return 1
        inst.build_installer(zip_path, args.version, args.out_dir, iscc=args.iscc, force=args.force)
    except inst.InstallerError as exc:
        print("make_installer: %s" % exc, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
