"""Launcher for ``src/helpers/changelog_fragments.py`` (see its docstring for the commands)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helpers.changelog_fragments import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
