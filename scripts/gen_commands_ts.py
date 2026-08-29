"""Regenerate `vscode-extension/src/commands.ts` from the Python table.

The extension cannot import Python, so the command vocabulary is generated into
TypeScript rather than restated by hand — restating it is exactly how a fifth
hand-maintained list of the same eleven commands would appear, and how a label
corrected in one place and not the others produces two names for one operation.

Run after editing `src/helpers/commands.py`::

    python scripts/gen_commands_ts.py

`tests/test_commands_table.py` compares the checked-in file against fresh
output and fails on any difference, so forgetting to run this is caught rather
than merged. The output is deterministic — the table's own order, no
timestamps — so a regeneration that changes nothing rewrites nothing.
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from helpers.commands import TYPESCRIPT_PATH, as_typescript  # noqa: E402


def main() -> int:
    target = _ROOT / TYPESCRIPT_PATH
    fresh = as_typescript()
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        current = ""

    if current == fresh:
        print(f"{TYPESCRIPT_PATH} is already up to date")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" so a Windows checkout does not produce a file that differs
    # from CI's only by line endings — which would fail the drift test for a
    # reason that has nothing to do with the table.
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(fresh)
    print(f"wrote {TYPESCRIPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
