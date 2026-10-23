"""Adopt the opt-in release starters into ONE project.

    python scripts/adopt_release.py list
    python scripts/adopt_release.py tokens
    python scripts/adopt_release.py adopt --project D:/path/to/project --dry-run \\
        --set PRODUCT_NAME="My App" --set ARTIFACT_PREFIX=MyApp --set REPO=me/my-app [NAME ...]

With no NAME every starter is adopted. Nothing is overwritten: an existing file is reported
and left alone. Review the written files and commit them yourself; they are the project's
now, and the Manager keeps no copy to sync. See `docs/gotchas/release-hardening.md`.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from helpers import release_starters as rs  # noqa: E402


def _values(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set expects NAME=VALUE, got {pair!r}")
        out[key.strip()] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--templates", default=str(ROOT / "templates"), help="the Manager's templates folder")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the starters and where each lands")
    sub.add_parser("tokens", help="every token, whether it is required, and what it means")
    adopt = sub.add_parser("adopt", help="render starters into a project")
    adopt.add_argument("names", nargs="*")
    adopt.add_argument("--project", required=True)
    adopt.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    adopt.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        for s in rs.STARTERS:
            print(f"{s.name:18} -> {s.dest_rel:34} {s.summary}")
        return 0
    if args.command == "tokens":
        for name, spec in rs.TOKENS.items():
            kind = "generated" if spec.generated else ("required" if spec.default is None else "default ")
            print(f"{name:20} {kind}  {spec.help}")
        return 0

    if not Path(args.project).is_dir():
        print(f"adopt_release: {args.project} is not a folder", file=sys.stderr)
        return 2
    names = args.names or [s.name for s in rs.STARTERS]
    results = rs.adopt(args.project, args.templates, names, _values(args.set), dry_run=args.dry_run)
    bad = 0
    for r in results:
        print(f"{r.outcome:16} {r.name:18} {r.dest_rel}")
        for reason in r.reasons:
            print(f"                   - {reason}")
        bad += r.outcome in (rs.OUT_INVALID, rs.OUT_FAILED)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
