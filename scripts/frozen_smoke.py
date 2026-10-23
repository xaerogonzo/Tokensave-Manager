"""Run the BUILT executables and judge them, layer by layer.

    python scripts/frozen_smoke.py --dist dist
    python scripts/frozen_smoke.py --dist "release/extracted/Tokensave-Manager-v2.6.0-windows"

Layers (each is separate evidence): the version resource of all three executables, the CLI
from a foreign working directory and the GUI on `scripts/drive/frozen-smoke.json` (a first-run
config, so every tokensave and git field is empty), and then the same executables CONFIGURED: the
real tokensave and git, the build's own templates, and a scratch repository that `tokensave init`
has really indexed, which the program must be seen to use. No tokensave on this machine is a
failure; `--no-tokensave` skips those two layers and says SKIPPED. Everything
runs in a scratch folder with a scratch config, so `dist/` is not written to. Exit status 1 if
any layer fails, and a failed run prints the tail of what the program said.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from helpers import frozen_smoke as fs  # noqa: E402
from constants import APP_VERSION  # noqa: E402

DEFAULT_SCRIPT = ROOT / "scripts" / "drive" / "frozen-smoke.json"
CONFIGURED_SCRIPT = ROOT / "scripts" / "drive" / "frozen-smoke-configured.json"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="folder holding the three executables")
    parser.add_argument("--version", default=APP_VERSION, help="expected version (default: constants.APP_VERSION)")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--product", default="", help="expected product name in the version resource")
    parser.add_argument("--configured-script", type=Path, default=CONFIGURED_SCRIPT)
    parser.add_argument("--tokensave", default="", help="path to tokensave.exe (default: TOKENSAVE_EXE, then PATH)")
    parser.add_argument("--no-tokensave", action="store_true", help="skip the configured layers (reported as SKIPPED)")
    args = parser.parse_args(argv)
    if not args.dist.is_dir():
        print("frozen_smoke: %s is not a folder" % args.dist, file=sys.stderr)
        return 2
    results = fs.smoke_dist(str(args.dist), args.version, str(args.script), expected_product=args.product,
                            configured_script=str(args.configured_script), tokensave_exe=args.tokensave,
                            skip_tokensave=args.no_tokensave)
    for result in results:
        print(result.line())
        if not fs.layer_ok(result) and result.work_dir:
            tail = fs.evidence_tail(result.work_dir)
            if tail:
                print(tail)
    failed = [r for r in results if not fs.layer_ok(r)]
    skipped = [r for r in results if r.skipped]
    print("frozen_smoke: %d layer(s), %d failed, %d skipped" % (len(results), len(failed), len(skipped)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
