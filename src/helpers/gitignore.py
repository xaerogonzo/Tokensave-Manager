"""Read / write / merge .gitignore files for the manager.

Pure-function module. The four helpers below operate on a project path
string; the two templates (_BASELINE_GITIGNORE and _GITIGNORE_TEMPLATES)
live here so there's a single source of truth between cmd_git_init's
auto-write and the GitignoreDialog category buttons.

No module globals from tokensave-manager.py are read.
"""

from __future__ import annotations

import os


# Baseline .gitignore written by cmd_git_init when none exists yet.
_BASELINE_GITIGNORE = """\
# Machine-specific config (if your project uses one)
*.local.json

# Claude Code local session settings
.claude/

# tokensave index (machine-specific binary database)
.tokensave/

# CodeGraph SQLite index — machine-specific binary database.
# IMPORTANT: do NOT blanket-ignore .codegraph/ — CodeGraph deliberately
# expects .codegraph/config.json to be TRACKED (per-project indexing
# configuration intended to be shared across machines). CodeGraph itself
# writes a .codegraph/.gitignore that handles binary-DB exclusion.
.codegraph/codegraph.db
.codegraph/codegraph.db-*

# Python cache
__pycache__/
*.pyc
*.pyo

# Nuitka build output
*.onefile-build/
*.build/
dist/
build/

# Virtual environments
.venv/
venv/

# Logs
logs/

# OS noise
Thumbs.db
.DS_Store
"""


def _ensure_gitignore(path: str) -> list:
    """Merge _BASELINE_GITIGNORE entries into the project's .gitignore.

    Non-destructive: reads existing content and only appends lines that are
    not already present (exact-match, ignoring blank lines and comments).
    Returns a list of human-readable result strings for logging.
    """
    gi_path = os.path.join(path, ".gitignore")

    existing_raw = ""
    if os.path.isfile(gi_path):
        try:
            with open(gi_path, encoding="utf-8", errors="replace") as f:
                existing_raw = f.read()
        except OSError as e:
            return [f"Could not read .gitignore: {e}"]

    # Build a set of non-blank, non-comment lines that are already present
    existing_lines = set()
    for ln in existing_raw.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            existing_lines.add(s)

    # Find baseline entries that are missing
    missing = []
    for ln in _BASELINE_GITIGNORE.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and s not in existing_lines:
            missing.append(ln)

    if not missing:
        return ["✔ .gitignore already contains all baseline entries — nothing to add"]

    # Append missing entries with a header comment
    addition = "\n\n# Added by TokenSave Manager (baseline entries)\n" + "\n".join(missing) + "\n"
    try:
        with open(gi_path, "a", encoding="utf-8") as f:
            f.write(addition)
    except OSError as e:
        return [f"Could not update .gitignore: {e}"]

    return [
        f"✔ .gitignore {'created' if not existing_raw else 'updated'} — "
        f"added {len(missing)} missing {'entry' if len(missing) == 1 else 'entries'}:",
        *[f"  + {ln}" for ln in missing if ln.strip()],
    ]


def _baseline_patterns() -> list:
    """Return _BASELINE_GITIGNORE as a flat list of pattern lines.

    Strips comments and blank lines so the result can be fed directly into
    _GITIGNORE_TEMPLATES as the canonical Baseline category. Keeps a single
    source of truth between cmd_git_init's auto-write and the dialog.
    """
    return [ln.strip() for ln in _BASELINE_GITIGNORE.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _read_gitignore_lines(path: str) -> list:
    """Read `<path>/.gitignore` and return its lines (no terminal newlines).

    Returns an empty list when the file doesn't exist. Uses utf-8-sig so any
    UTF-8 BOM (sometimes written by PowerShell) is silently stripped.
    """
    gi_path = os.path.join(path, ".gitignore")
    if not os.path.isfile(gi_path):
        return []
    try:
        with open(gi_path, encoding="utf-8-sig", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _write_gitignore_lines(path: str, lines: list) -> None:
    """Atomically write `lines` to `<path>/.gitignore`.

    Writes to `.gitignore.tmp` first then renames — protects against
    half-written files if the process is killed. Ensures a trailing newline.
    Raises OSError on failure; caller logs/displays.
    """
    gi_path  = os.path.join(path, ".gitignore")
    tmp_path = gi_path + ".tmp"
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, gi_path)


# Categories of gitignore patterns offered as one-click "inject" buttons in
# GitignoreDialog. The Baseline entry is derived from _BASELINE_GITIGNORE at
# module load so we never have to keep two lists in sync.
_GITIGNORE_TEMPLATES = {
    "Baseline (TokenSave standard)": _baseline_patterns(),
    "Python": [
        "__pycache__/", "*.pyc", "*.pyo", "*.egg-info/",
        ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
        ".tox/", ".coverage", "htmlcov/",
    ],
    "Node.js": [
        "node_modules/", "npm-debug.log*", "yarn-debug.log*",
        "yarn-error.log*", ".pnpm-debug.log*", ".next/", "out/",
    ],
    "Rust": [
        "target/", "**/*.rs.bk",
    ],
    "Java / JVM": [
        "*.class", "*.jar", ".gradle/", "build/", "target/",
    ],
    ".NET / Visual Studio": [
        "bin/", "obj/", "*.user", "*.suo", "*.userprefs", ".vs/",
    ],
    "VS Code": [
        ".vscode/",
    ],
    "JetBrains IDEs": [
        ".idea/", "*.iml",
    ],
    "macOS": [
        ".DS_Store", "._*", ".Spotlight-V100", ".Trashes",
    ],
    "Windows": [
        "Thumbs.db", "ehthumbs.db", "Desktop.ini",
    ],
    "Nuitka": [
        "*.build/", "*.dist/", "*.onefile-build/", "dist/",
        "nuitka-crash-report.xml",
    ],
}
