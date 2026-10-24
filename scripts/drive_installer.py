"""Drive the installer for real: install, upgrade, uninstall, and say what happened.

    python scripts/drive_installer.py --zip release/Tokensave-Manager-v2.7.0-windows.zip --version 2.7.0
    python scripts/drive_installer.py --source <folder holding the release tree> --version 2.7.0

Compiles a TEST build of `packaging/tokensave-manager.iss` (its own AppId and data folder) and drives it
per-user and silent. With --zip the installed application is also smoke-tested by
`scripts/frozen_smoke.py`'s layers. See `helpers/installer_drive.py` for the scenarios and the refusals.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from helpers import frozen_smoke as fs  # noqa: E402
from helpers import installer as inst  # noqa: E402
from helpers import installer_drive as drive  # noqa: E402

SCRIPTS = ROOT / "scripts" / "drive"


def smoke_installed(version: str):
    def run(installed: Path) -> bool:
        results = fs.smoke_dist(str(installed), version, str(SCRIPTS / "frozen-smoke.json"),
                                configured_script=str(SCRIPTS / "frozen-smoke-configured.json"),
                                migration_script=str(SCRIPTS / "frozen-smoke-migration.json"))
        for r in results:
            print("  installed: " + r.line())
        return all(fs.layer_ok(r) for r in results)
    return run


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--zip", type=Path)
    source.add_argument("--source", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--iscc", default="")
    args = parser.parse_args(argv)
    scratch = None
    try:
        if args.zip:
            scratch = Path(tempfile.mkdtemp(prefix="tokensave manager drive "))
            tree = inst.extract_release(args.zip, args.version, scratch)
            smoke = smoke_installed(args.version)
        else:
            tree, smoke = args.source, None
        checks = drive.drive(tree, args.version, iscc=args.iscc, smoke=smoke)
    except inst.InstallerError as exc:
        print("drive_installer: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    for check in checks:
        print(check.line())
    failed = [c for c in checks if not c.ok]
    print("drive_installer: %d check(s), %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
