"""Package a built `dist/` as a release zip that cannot be mistaken for a finished one.

    python scripts/make_release_zip.py --dist dist --version 2.6.0

Writes `release/Tokensave-Manager-v<version>-windows.zip` and `release/SHA256SUMS.txt` after the
archive passes its inventory check and the extracted copy (in a folder with a space in its name)
passes `scripts/frozen_smoke.py`'s layers. See `helpers/release_zip.py` for the order and why.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from helpers import frozen_smoke as fs  # noqa: E402
from helpers import release_zip as rz  # noqa: E402

DEFAULT_SCRIPT = ROOT / "scripts" / "drive" / "frozen-smoke.json"
CONFIGURED_SCRIPT = ROOT / "scripts" / "drive" / "frozen-smoke-configured.json"


def _smoke(version: str, script: Path):
    def run(extracted: Path) -> bool:
        results = fs.smoke_dist(str(extracted), version, str(script), configured_script=str(CONFIGURED_SCRIPT))
        for r in results:
            print("  " + r.line())
        return all(fs.layer_ok(r) for r in results)
    return run


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-smoke", action="store_true", help="skip the smoke test (tests only)")
    args = parser.parse_args(argv)
    try:
        rz.build_release(args.dist, args.version, args.out_dir, force=args.force,
                         smoke=None if args.no_smoke else _smoke(args.version, args.script))
    except rz.ReleaseError as exc:
        print("make_release_zip: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
