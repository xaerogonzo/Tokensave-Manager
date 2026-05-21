"""
TokenSave Manager
A GUI for managing tokensave projects and controlling which project
Claude Desktop uses via the wrapper script.
"""

import os
import re
import json
import shlex
import shutil
import subprocess
import threading
import time
import logging
import logging.handlers
import ctypes
import sys
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont
import math
import pystray
from PIL import Image, ImageDraw

_ANSI = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])')

# ── Paths ─────────────────────────────────────────────────────────────────────
# Under Nuitka --onefile, NUITKA_ONEFILE_PARENT is the actual .exe path.
# In dev mode, the script lives in src/ so go up one level.

if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_CONFIG_PATH = os.path.join(_BASE_DIR, "manager-config.json")
LOG_DIR      = os.path.join(_BASE_DIR, "logs")
LOG_FILE     = os.path.join(LOG_DIR, "manager.log")

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM if present
        return json.load(f)

def _save_config(cfg: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

_cfg = _load_config()

TOKENSAVE    = _cfg.get("tokensave_exe", "")
TEMPLATE_DIR = _cfg.get("template_dir", "") or os.path.join(_BASE_DIR, "templates")
SEARCH_ROOTS = _cfg.get("search_roots", [])


def _detect_git() -> str:
    """Return the best available path to git.exe.

    Priority:
      1. Explicit path in manager-config.json  (caller checks that first)
      2. shutil.which("git")  — works if Git is on PATH
      3. Common Git-for-Windows install locations
      4. Bare "git" fallback (will fail with a clear error if not found)
    """
    found = shutil.which("git")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "git"


def _detect_gh() -> str:
    """Return the path to gh.exe (GitHub CLI) if installed, else empty string.

    Checks PATH first, then common winget/scoop install locations.
    Returns "" when not found so callers can easily test with `if _detect_gh()`.
    """
    found = shutil.which("gh")
    if found:
        return found
    for candidate in [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\gh.exe"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_npm() -> str:
    """Return the path to npm, else empty string.

    On Windows npm is a `.cmd` shim, not a `.exe` — `subprocess.run` with
    a bare `.cmd` raises FileNotFoundError unless the absolute path
    (including the .cmd extension) is supplied. So we probe `.cmd` first.
    """
    for name in ("npm.cmd", "npm"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\npm.cmd"),
        os.path.expandvars(r"%ProgramFiles%\nodejs\npm.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_codegraph() -> str:
    """Return the path to the codegraph CLI, else empty string.

    Same Windows-.cmd-first priority as _detect_npm because codegraph is
    installed by npm as a .cmd shim. Returns "" (not the bare command
    name) so callers can test `if CODEGRAPH_EXE:` cleanly without
    accidentally invoking a bare command via subprocess.
    """
    for name in ("codegraph.cmd", "codegraph"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\codegraph.cmd"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\codegraph.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _is_codegraph_project(path: str) -> bool:
    """True iff `path` has been initialised by CodeGraph (the .codegraph/
    SQLite database exists)."""
    return os.path.isfile(os.path.join(path, ".codegraph", "codegraph.db"))


# Resolved at startup; updated whenever Settings are saved.
# All git subprocess calls use this variable so the user only has to
# configure the path in one place.
GIT_EXE: str = _cfg.get("git_exe") or _detect_git()

# Resolved at startup; rebuilt in _on_settings_saved. Empty string when not
# installed (codegraph is optional, distributed via npm — not bundled).
CODEGRAPH_EXE: str = _cfg.get("codegraph_exe") or _detect_codegraph()


# ── Search-root helpers (support both legacy str and new {"path":…,"label":…} format) ──

def _root_path(r):
    """Return the directory path from a search-root entry (str or dict)."""
    return r if isinstance(r, str) else r["path"]

def _root_label(r):
    """Return the display label for a search-root entry."""
    p = _root_path(r)
    if isinstance(r, str):
        return os.path.basename(p.rstrip("/\\"))
    return r.get("label", os.path.basename(p.rstrip("/\\"))) or os.path.basename(p.rstrip("/\\"))

BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"

DESKTOP_PROJECT_FILE = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".tokensave", "desktop-project.txt",
)
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "target", "build", "dist", "out", ".gradle", "bin", "obj",
}
MAX_DEPTH = 4
CREATE_NO_WINDOW = 0x08000000
AUTO_REFRESH_MS = 60_000  # auto-refresh project list every 60 s

# Git network operations — prevents infinite hang when credentials aren't cached.
# GIT_TERMINAL_PROMPT=0 tells git to fail immediately instead of waiting for
# stdin. Compatible with Git Credential Manager (GCM authenticates via browser,
# not stdin, so this env var doesn't interfere with it).
_GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class _Tooltip:
    """Hover tooltip for tkinter widgets.

    Shows a small popup after `delay` ms; hides on leave or click.
    Text wraps at ~300 px so multi-line tips stay readable.
    """
    _DELAY = 650   # ms before appearing

    def __init__(self, widget, text: str):
        self._widget  = widget
        self._text    = text
        self._job     = None
        self._tip_win = None
        widget.bind("<Enter>",       self._schedule,  add="+")
        widget.bind("<Leave>",       self._cancel,    add="+")
        widget.bind("<ButtonPress>", self._cancel,    add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self._widget.after(self._DELAY, self._show)

    def _cancel(self, _event=None):
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None

    def _show(self):
        self._job = None
        try:
            x = self._widget.winfo_rootx() + 12
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        except Exception:
            return
        self._tip_win = win = tk.Toplevel(self._widget)
        win.wm_overrideredirect(True)   # no title bar / border
        win.wm_attributes("-topmost", True)
        win.wm_geometry(f"+{x}+{y}")
        win.configure(bg=C["surface1"])
        # Thin border effect via a 1-px frame
        outer = tk.Frame(win, bg=C["overlay0"], padx=1, pady=1)
        outer.pack()
        tk.Label(outer, text=self._text,
                 font=("Segoe UI", 9),
                 bg=C["surface1"], fg=C["text"],
                 padx=10, pady=6,
                 wraplength=300,
                 justify=tk.LEFT).pack()


def _is_auth_error(text: str) -> bool:
    """Return True if git output looks like an authentication failure."""
    t = text.lower()
    return any(s in t for s in (
        "authentication failed",
        "could not read username",
        "permission denied",
        "fatal: authentication",
        "remote: repository not found",
        "invalid username or password",
    ))

def _suggest_commit_message(status_text: str) -> str:
    """Generate a conventional-commit-style message from `git status --short` output.

    Tries to produce something meaningful based on which files changed and how.
    The user can always edit or replace the suggestion.
    """
    # BUG FIX (see GitCommitDialog parsing): never strip() the full
    # status_text before splitlines() — strip eats the leading space from
    # the first line, shifting columns and silently dropping the first
    # character of the filename when the first entry is a working-tree
    # modification (which starts with a single leading space).
    lines = [l for l in status_text.splitlines() if len(l) >= 4]
    if not lines:
        return ""

    files = []
    for line in lines:
        xy   = line[:2].strip()
        fname = line[3:]
        # Handle renames: "old -> new" format
        if " -> " in fname:
            fname = fname.split(" -> ")[-1]
        files.append((xy, fname))

    if not files:
        return ""

    basenames = [os.path.basename(f) for _, f in files]
    exts      = {os.path.splitext(b)[1].lower() for b in basenames}
    has_del   = any("D" in xy for xy, _ in files)
    has_add   = any("A" in xy or "?" in xy for xy, _ in files)

    # ── Single file ──
    if len(files) == 1:
        xy, fname = files[0]
        bname = os.path.basename(fname)
        if "D" in xy:
            return f"chore: remove {bname}"
        if "A" in xy or "?" in xy:
            ext = os.path.splitext(bname)[1].lower()
            return f"docs: add {bname}" if ext in (".md", ".txt", ".rst") else f"feat: add {bname}"
        if bname.lower().endswith((".md", ".txt", ".rst")):
            return f"docs: update {bname}"
        return f"chore: update {bname}"

    # ── Multiple files ──
    doc_exts = {".md", ".txt", ".rst", ".adoc"}
    code_exts = {".py", ".js", ".ts", ".cs", ".cpp", ".c", ".h", ".rs", ".go", ".java"}

    if exts <= doc_exts:
        return "docs: update documentation"

    if exts <= code_exts:
        if len(basenames) <= 3:
            return f"chore: update {', '.join(basenames)}"
        return f"chore: update {len(files)} source files"

    if has_del and not has_add:
        return f"chore: remove {len(files)} files"

    # Mixed — list up to two names then summarise the rest
    if len(basenames) <= 2:
        return f"chore: update {', '.join(basenames)}"
    return f"chore: update {basenames[0]}, {basenames[1]} + {len(basenames) - 2} more"


# ── Shadow-link helpers ────────────────────────────────────────────────────────

# Default extension map: ZScript → C++, ACS → C, DECORATE lump → C++.
# Keys starting with '.' are matched against the file extension
#   (e.g. ".zsc" matches "Blood.zsc" → shadow "Blood.zsc.cpp").
# Keys WITHOUT a leading dot are matched by exact filename, case-insensitive
#   (e.g. "DECORATE" matches the extensionless lump → shadow "DECORATE.cpp").
DEFAULT_SHADOW_EXT_MAP = {
    ".zs":  ".cpp",
    ".zsc": ".cpp",
    ".acs": ".c",
    "DECORATE": ".cpp",   # extensionless lump — matched by exact filename
}

_SHADOW_SKIP_DIRS = {".tokensave", ".git", "node_modules", "__pycache__",
                     ".venv", "venv", "target", "build", "dist", "out"}


def generate_shadow_links(path: str, ext_map: dict) -> tuple:
    """
    Walk *path* and create NTFS hardlinks so tokensave can index
    non-standard extensions via an existing tree-sitter grammar.

    Two matching modes, determined by key format:
    - Dot-prefixed keys (".zsc") match by file extension → Blood.zsc → Blood.zsc.cpp
    - Non-dot keys ("DECORATE") match by exact filename, case-insensitive →
      DECORATE → DECORATE.cpp  (handles extensionless Doom lumps)

    Existing shadow files are left untouched.
    Returns (created, skipped, failed) counts.
    """
    created = skipped = failed = 0
    ext_keys  = {k: v for k, v in ext_map.items() if k.startswith(".")}
    name_keys = {k.upper(): v for k, v in ext_map.items() if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext in ext_keys:
                shadow_suffix = ext_keys[ext]
            elif fname.upper() in name_keys:
                shadow_suffix = name_keys[fname.upper()]
            else:
                continue
            src = os.path.join(root, fname)
            dst = src + shadow_suffix
            if os.path.exists(dst):
                skipped += 1
            else:
                try:
                    os.link(src, dst)
                    created += 1
                except OSError:
                    failed += 1
    return created, skipped, failed


def remove_shadow_links(path: str, ext_map: dict) -> int:
    """Delete all shadow hardlink files created by generate_shadow_links."""
    removed = 0
    suffixes  = set(ext_map.values())
    src_exts  = {k for k in ext_map if k.startswith(".")}
    src_names = {k.upper() for k in ext_map if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            for suf in suffixes:
                if fname.endswith(suf):
                    base = fname[:-len(suf)]
                    if (any(base.endswith(e) for e in src_exts) or
                            base.upper() in src_names):
                        try:
                            os.remove(os.path.join(root, fname))
                            removed += 1
                        except OSError:
                            pass
    return removed


def update_gitignore_for_shadows(path: str, ext_map: dict):
    """
    Append shadow-file patterns to .gitignore (if not already present).
    Creates .gitignore if it doesn't exist.
    Extension-based entries use a glob (*.zsc.cpp); exact-name entries use
    a literal filename (DECORATE.cpp) — no leading wildcard.
    """
    gi_path = os.path.join(path, ".gitignore")
    patterns = []
    for key, val in ext_map.items():
        if key.startswith("."):
            patterns.append(f"*{key}{val}")   # glob:  *.zsc.cpp
        else:
            patterns.append(f"{key}{val}")    # exact: DECORATE.cpp
    try:
        existing = open(gi_path, encoding="utf-8", errors="ignore").read() \
                   if os.path.isfile(gi_path) else ""
        to_add = [p for p in patterns if p not in existing]
        if to_add:
            header = "\n# tokensave shadow extension hardlinks\n"
            with open(gi_path, "a", encoding="utf-8") as f:
                f.write(header + "\n".join(to_add) + "\n")
    except OSError:
        pass


def _is_git_repo(path: str) -> bool:
    """Return True if *path* is inside an initialised git repository.

    NOTE: this walks UPWARD via `git rev-parse --git-dir` — so a project
    folder inside a parent git repo will also return True. For the strict
    'this folder IS a repo root' check, use _is_local_git_repo instead.
    """
    try:
        proc = subprocess.run(
            [GIT_EXE,"-C", path, "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def _find_tracked_but_ignored(path: str) -> list:
    """Return a list of paths that are TRACKED by git in `path` but ALSO
    match a pattern in `.gitignore`.

    Uses `git ls-files -ci --exclude-standard`:
      -c  show cached (tracked) files
      -i  filter to those that are ignored
      --exclude-standard  use the project's actual .gitignore rules

    Returns paths relative to the repo root, one per line, empty string
    filtered out. Returns [] if the call fails (not a repo, git missing,
    etc.) — caller can treat empty as "nothing to do".

    This is the canonical way to find the "stale tracking" problem: a
    file that was committed before being added to .gitignore. Git will
    keep tracking it until `git rm --cached <file>` is run, even though
    .gitignore implies the user no longer wants it in the repo.
    """
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path,
             "ls-files", "-ci", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _is_local_git_repo(path: str) -> bool:
    """Return True only if *path* itself is a git repo root.

    Strict local check — does NOT walk upward. Use this whenever the
    intent is 'should we treat this folder as its own version-controlled
    project?' (e.g. commit-prompt flows, .gitignore writes).

    Uses os.path.exists rather than os.path.isdir because git worktrees
    store `.git` as a flat text file pointing to the main repo's .git/.
    os.path.isdir would miss those; os.path.exists handles both.
    """
    return os.path.exists(os.path.join(path, ".git"))


def _parse_git_status_v2(text: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` output.

    Returns a dict with keys:
      dirty      — True if any working-tree or index changes exist
      ahead      — int, commits ahead of upstream (0 if no upstream)
      behind     — int, commits behind upstream
      has_remote — True if `# branch.upstream <name>` line is present

    Pure function — never raises; bad input returns the empty default.
    """
    result = {"dirty": False, "ahead": 0, "behind": 0, "has_remote": False}
    for line in text.splitlines():
        if line.startswith("# branch.upstream "):
            result["has_remote"] = True
        elif line.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M"
            try:
                parts = line.split()
                # parts: ['#', 'branch.ab', '+N', '-M']
                result["ahead"]  = int(parts[2].lstrip("+"))
                result["behind"] = int(parts[3].lstrip("-"))
            except (ValueError, IndexError):
                pass
        elif line and line[0] in ("1", "2", "u", "?"):
            # Tracked-modified (1), renamed/copied (2), unmerged (u), untracked (?)
            result["dirty"] = True
    return result


def _format_git_status_cell(status: dict | None, has_git: bool) -> tuple:
    """Return (display_text, tag_name) for the Git column on the Projects tab.

    status: dict from _parse_git_status_v2, or None if not yet computed
    has_git: True if the project has a .git/ directory at all

    Tags map to colours in _build_projects_tab via tree.tag_configure.
    """
    if not has_git:
        return ("—", "git_none")
    if status is None:
        return ("…", "git_pending")
    if not status["has_remote"]:
        # Repo exists but no remote — can't be ahead/behind
        if status["dirty"]:
            return ("●", "git_dirty")
        return ("✓", "git_clean")
    dirty  = status["dirty"]
    ahead  = status["ahead"]
    behind = status["behind"]
    if not dirty and ahead == 0 and behind == 0:
        return ("✓", "git_clean")
    parts = []
    if dirty:
        parts.append("●")
    if ahead:
        parts.append(f"↑{ahead}")
    if behind:
        parts.append(f"↓{behind}")
    text = "".join(parts)
    # Tag priority: mixed (dirty + remote drift) > behind > ahead > dirty
    if dirty and (ahead or behind):
        tag = "git_mixed"
    elif behind:
        tag = "git_behind"
    elif ahead:
        tag = "git_ahead"
    else:
        tag = "git_dirty"
    return (text, tag)


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

# ---------------------------------------------------------------------------

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


# ─── Gitignore management helpers (used by GitignoreDialog) ─────────────────

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


# Stop hook injected into .claude/settings.json by _scaffold_git_hook.
# Commits whatever Claude changed at session end — skips if working tree is clean.
_STOP_HOOK_CMD = (
    'git add -A && git diff --cached --quiet || '
    'git commit -m "auto: Claude session"'
)


def _scaffold_git_hook(path: str) -> list:
    """Write/merge a Claude Code Stop hook into .claude/settings.json.

    Creates .claude/ if it doesn't exist. Merges non-destructively — reads
    existing JSON and only appends when our hook isn't already present.
    Returns a list of human-readable action strings (for retrofit summaries).
    """
    settings_dir  = os.path.join(path, ".claude")
    settings_path = os.path.join(settings_dir, "settings.json")
    try:
        os.makedirs(settings_dir, exist_ok=True)
    except OSError:
        return ["Could not create .claude/ directory"]

    existing = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8-sig") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # treat as empty rather than failing

    hooks = existing.setdefault("hooks", {})
    stop  = hooks.setdefault("Stop", [])

    # Idempotent: skip if any Stop hook command already starts with "git add -A"
    already = any(
        e.get("type") == "command" and e.get("command", "").startswith("git add -A")
        for entry in stop for e in entry.get("hooks", [])
    )
    if already:
        return ["Auto-commit Stop hook already present — skipped"]

    stop.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": _STOP_HOOK_CMD}],
    })
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return ["Created/updated .claude/settings.json — Stop hook added"]
    except OSError as exc:
        return [f"Could not write .claude/settings.json: {exc}"]


def _setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=500_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger = logging.getLogger("tsm")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger

log = _setup_logger()

# ── Prompt snippets ────────────────────────────────────────────────────────────

PROMPT_SNIPPETS = [
    (
        "Codebase overview",
        "Give me a high-level overview of this project using tokensave_context. "
        "What are the main components, how do they relate, and what is the entry point?"
    ),
    (
        "Find a symbol",
        "Use tokensave_search to find [symbol name]. Then use tokensave_context "
        "to explain what it does, what it calls, and what calls it."
    ),
    (
        "What calls this function?",
        "Use tokensave_callers to find everything that calls [function name]. "
        "Show me the full call chain."
    ),
    (
        "Impact of changing X",
        "Use tokensave_impact to analyze what would be affected if I modify "
        "[function or class name]. Show me the full impact chain."
    ),
    (
        "Code health check",
        "Run tokensave_health, tokensave_complexity, and tokensave_god_class. "
        "Give me a health report — flag god classes, high complexity, and circular dependencies."
    ),
    (
        "Find dead code",
        "Use tokensave_dead_code and tokensave_unused_imports to find any "
        "unused code or imports in this project. List them with file locations."
    ),
    (
        "List all TODOs",
        "Use tokensave_todos to list all TODO and FIXME comments in this project. "
        "Group them by file."
    ),
    (
        "Generate changelog",
        "Use tokensave_changelog to generate a changelog based on recent commits. "
        "Format it as a proper CHANGELOG.md entry."
    ),
    (
        "Module public API",
        "Use tokensave_module_api to show me the public API of [module or file name]. "
        "What does it export and how is it meant to be used?"
    ),
    (
        "Circular dependencies",
        "Use tokensave_circular to find any circular dependencies in this project. "
        "Explain how each one could be resolved."
    ),
    (
        "Largest / most complex files",
        "Use tokensave_largest and tokensave_complexity to find the biggest and most "
        "complex files. Which ones are the best candidates for refactoring?"
    ),
    (
        "Refactor rename preview",
        "Use tokensave_rename_preview to show what would change if I rename "
        "[old name] to [new name]. List every affected file and line."
    ),
]

# ── Colours (Catppuccin Mocha) ─────────────────────────────────────────────────

C = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "overlay0": "#6c7086",
    "text":     "#cdd6f4",
    "subtext":  "#bac2de",
    "blue":     "#89b4fa",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "red":      "#f38ba8",
    "lavender": "#b4befe",
    "sky":      "#89dceb",
    "peach":    "#fab387",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_projects():
    """Discover projects under every configured search root.

    A folder qualifies if it contains any of:
      - a tokensave index  (.tokensave/tokensave.db)  — full tokensave features
      - a CodeGraph index  (.codegraph/codegraph.db)  — full CodeGraph features
      - a git repository   (.git/)                    — git features only

    All three types are shown in the Projects tab. Each tool's commands show a
    friendly 'not initialised yet' prompt when called on a project that doesn't
    have its index — keeping tokensave and CodeGraph as equal citizens.
    """
    projects = []
    seen: set = set()
    for root in SEARCH_ROOTS:
        rpath  = _root_path(root)
        rlabel = _root_label(root)
        if not os.path.isdir(rpath):
            continue
        for dirpath, dirnames, _ in os.walk(rpath):
            rel = os.path.relpath(dirpath, rpath)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= MAX_DEPTH:
                dirnames.clear()
                continue

            # Check for markers BEFORE filtering dirnames
            has_ts  = ".tokensave" in dirnames
            has_git = ".git"       in dirnames
            has_cg  = ".codegraph" in dirnames

            # Strip hidden dirs and known noise from further traversal
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not d.startswith(".")]

            if not (has_ts or has_git or has_cg):
                continue
            if dirpath in seen:
                continue
            seen.add(dirpath)

            # mtime: tokensave db age if available, else last git commit,
            # else codegraph db mtime (last full-build), else dirpath
            if has_ts:
                db    = os.path.join(dirpath, ".tokensave", "tokensave.db")
                mtime = os.path.getmtime(db) if os.path.isfile(db) else os.path.getmtime(dirpath)
            elif has_git:
                db    = None
                git_dir    = os.path.join(dirpath, ".git")
                commit_msg = os.path.join(git_dir, "COMMIT_EDITMSG")
                mtime = (os.path.getmtime(commit_msg)
                         if os.path.isfile(commit_msg)
                         else os.path.getmtime(git_dir))
            else:   # codegraph-only
                db    = None
                cg_db = os.path.join(dirpath, ".codegraph", "codegraph.db")
                mtime = os.path.getmtime(cg_db) if os.path.isfile(cg_db) else os.path.getmtime(dirpath)

            projects.append({
                "path":          dirpath,
                "name":          os.path.basename(dirpath),
                "db":            db,
                "mtime":         mtime,
                "root_label":    rlabel,
                "has_tokensave": has_ts,
                "has_git":       has_git,
                "has_codegraph": has_cg,
            })
    return sorted(projects, key=lambda p: p["mtime"], reverse=True)


def get_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        p = open(DESKTOP_PROJECT_FILE, encoding="utf-8").read().strip()
        if p and os.path.isfile(os.path.join(p, ".tokensave", "tokensave.db")):
            return p
    return None


def set_pinned(path):
    os.makedirs(os.path.dirname(DESKTOP_PROJECT_FILE), exist_ok=True)
    with open(DESKTOP_PROJECT_FILE, "w", encoding="utf-8") as f:
        f.write(path)


def clear_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        os.remove(DESKTOP_PROJECT_FILE)


def fmt_age(mtime):
    diff = datetime.now().timestamp() - mtime
    if diff < 60:         return "just now"
    if diff < 3600:       return f"{int(diff / 60)}m ago"
    if diff < 86400:      return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 7:  return f"{int(diff / 86400)}d ago"
    return datetime.fromtimestamp(mtime).strftime("%b %d")


def load_basic_instructions_template():
    """Load the BASIC_INSTRUCTIONS.md template text, or return a minimal fallback."""
    if os.path.isfile(BASIC_INSTRUCTIONS_TEMPLATE):
        raw = open(BASIC_INSTRUCTIONS_TEMPLATE, encoding="utf-8").read()
        # Replace any @<path>/project-baseline.md line with the current computed path
        # so the written BASIC_INSTRUCTIONS.md always points to the right location.
        # Use a lambda so BASELINE_INCLUDE_LINE is never parsed as a regex
        # replacement string — Windows paths contain backslashes that re.sub
        # would misinterpret as escape sequences (e.g. \p → bad escape error).
        raw = re.sub(r"^@[^\n]*project-baseline\.md",
                     lambda _: BASELINE_INCLUDE_LINE, raw, flags=re.MULTILINE)
        return raw
    # Minimal inline fallback if template file is missing
    return (
        "# [PROJECT NAME] — Basic Instructions\n\n"
        "<!-- CLAUDE: Replace all [PLACEHOLDER] sections on first use. -->\n\n"
        f"{BASELINE_INCLUDE_LINE}\n\n"
        "---\n\n"
        "## Project Overview\n\n"
        "**Name:** [PROJECT NAME]\n"
        "**Stack:** [Languages and frameworks]\n"
        "**Entry point:** [Main file or command]\n"
        "**Purpose:** [One sentence]\n\n"
        "---\n\n"
        "## Architecture\n\n[Replace with high-level structure.]\n\n"
        "---\n\n"
        "## Key Files\n\n[Replace with important files and their roles.]\n\n"
        "---\n\n"
        "## Project-Specific Rules\n\n[Replace or delete this section.]\n"
    )

# ── Single-instance ───────────────────────────────────────────────────────────

_MUTEX_NAME = "TokenSaveManager_SingleInstance"
_mutex_handle = None

def _acquire_instance_lock():
    """Return True if this is the first instance, False if another is running."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS

def _bring_existing_to_front():
    """Find the existing window by title and restore it."""
    hwnd = ctypes.windll.user32.FindWindowW(None, "TokenSave Manager")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)

# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_tray_icon():
    """Generate a 64×64 tray icon: dark circle with a white star."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (30, 30, 46, 255)   # Catppuccin Mocha base
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)
    # Simple 5-point star approximation using a polygon
    cx, cy, r_out, r_in = size / 2, size / 2, 26, 11
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=(137, 180, 250, 255))  # Catppuccin blue
    return img

# ── App ────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TokenSave Manager")
        self.geometry("760x600")
        self.minsize(600, 520)
        self.configure(bg=C["base"])
        self._current_proc = None
        self._stop_requested = False
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {TOKENSAVE}")
        log.info(f"  templates: {TEMPLATE_DIR}")
        log.info(f"  log file : {LOG_FILE}")
        self._style()
        self._build()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray = None
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.bind("<Unmap>", self._on_unmap)
        self.after(300, self._check_config)

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.withdraw()
        log.debug("Window hidden to tray")

    def _on_unmap(self, event):
        if event.widget is self:
            self.after(100, self._maybe_hide)

    def _maybe_hide(self):
        if self.state() == "iconic":
            self.withdraw()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None):
        log.info("Quit requested from tray")
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)

    # ── Styles ─────────────────────────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C["base"], foreground=C["text"],
            font=("Segoe UI", 10), borderwidth=0)

        s.configure("Treeview",
            background=C["mantle"], foreground=C["text"],
            fieldbackground=C["mantle"], rowheight=30,
            font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["surface0"], foreground=C["subtext"],
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", C["surface1"])],
            foreground=[("selected", C["text"])])

        s.configure("TButton",
            background=C["surface0"], foreground=C["text"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("TButton",
            background=[("active", C["surface1"]), ("pressed", C["surface1"])])

        s.configure("Primary.TButton",
            background=C["blue"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Primary.TButton",
            background=[("active", C["lavender"]), ("pressed", C["lavender"])])

        s.configure("Action.TButton",
            background=C["peach"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Action.TButton",
            background=[("active", C["yellow"]), ("pressed", C["yellow"])])

        s.configure("Danger.TButton",
            background=C["surface0"], foreground=C["red"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("Danger.TButton",
            background=[("active", C["surface1"])])

        s.configure("TScrollbar",
            background=C["surface0"], troughcolor=C["mantle"],
            bordercolor=C["base"], arrowcolor=C["overlay0"],
            relief="flat")

        s.configure("TSeparator", background=C["surface0"])

        s.configure("TNotebook",
            background=C["base"], borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=C["surface0"], foreground=C["subtext"],
            padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
            background=[("selected", C["base"])],
            foreground=[("selected", C["blue"])])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=C["mantle"], pady=12, padx=16)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="TokenSave Manager",
                 font=("Segoe UI", 15, "bold"),
                 bg=C["mantle"], fg=C["blue"]).pack(side=tk.LEFT)

        self.active_badge = tk.Label(hdr, text="",
            font=("Segoe UI", 9), bg=C["surface0"],
            fg=C["green"], padx=8, pady=3)
        self.active_badge.pack(side=tk.RIGHT)

        # ── Credit bar ──
        tk.Label(self, text="TokenSave Manager  ·  Alexander L Corthell",
                 font=("Segoe UI", 7), bg=C["crust"], fg=C["overlay0"],
                 pady=2).pack(fill=tk.X, side=tk.BOTTOM)

        # ── Separator + Log — packed BEFORE notebook so expand=True doesn't eat it ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, side=tk.BOTTOM)

        log_frame = tk.Frame(self, bg=C["base"], padx=14, pady=8)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Notebook ──
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._build_projects_tab()
        self._build_git_tab()
        self._build_reference_tab()
        self._build_help_tab()

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        self._stop_btn = ttk.Button(log_header, text="■  Stop",
                                    style="Danger.TButton",
                                    command=self._stop_current,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._running_label = tk.Label(log_header, text="",
                                       font=("Segoe UI", 8),
                                       bg=C["base"], fg=C["yellow"])
        self._running_label.pack(side=tk.RIGHT, padx=(0, 8))

        log_inner = tk.Frame(log_frame, bg=C["mantle"])
        log_inner.pack(fill=tk.X)

        self.log = tk.Text(log_inner, height=4,
            font=("Consolas", 9), bg=C["mantle"], fg=C["green"],
            insertbackground=C["green"], relief=tk.FLAT,
            padx=10, pady=6, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(log_inner, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_projects_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Projects  ")

        # ── Toolbar + hint packed first with side=BOTTOM so they are always
        #    visible — the treeview (expand=True) fills whatever space remains.
        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self.refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self.cmd_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

        # ── Project list — fills remaining space ──
        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "cg", "git", "scaffold"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0",       text="Project")
        self.tree.heading("active",   text="")
        self.tree.heading("path",     text="Path")
        self.tree.heading("synced",   text="Last Synced")
        self.tree.heading("cg",       text="CG")
        self.tree.heading("git",      text="Git")
        self.tree.heading("scaffold", text="Scaffold")

        self.tree.column("#0",       width=170, stretch=False)
        self.tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self.tree.column("path",     width=220)
        self.tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self.tree.column("cg",       width=36,  stretch=False, anchor=tk.CENTER)
        self.tree.column("git",      width=60,  stretch=False, anchor=tk.CENTER)
        self.tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("active",      foreground=C["green"])
        self.tree.tag_configure("normal",      foreground=C["text"])
        self.tree.tag_configure("scaffold",    foreground=C["peach"])
        self.tree.tag_configure("git_only",    foreground=C["overlay0"])
        self.tree.tag_configure("pending",     foreground=C["yellow"])
        # Git status override tags — applied AFTER the baseline tag so they
        # take precedence (tkinter Treeview resolves tag properties from the
        # last matching tag in the tuple). When a row has e.g. tags=("normal",
        # "git_dirty"), the foreground comes from git_dirty.
        self.tree.tag_configure("git_clean",   foreground=C["green"])
        self.tree.tag_configure("git_dirty",   foreground=C["yellow"])
        self.tree.tag_configure("git_ahead",   foreground=C["sky"])
        self.tree.tag_configure("git_behind",  foreground=C["red"])
        self.tree.tag_configure("git_mixed",   foreground=C["peach"])
        self.tree.tag_configure("git_pending", foreground=C["overlay0"])
        self.tree.tag_configure("git_none",    foreground=C["overlay0"])
        self.tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("subcategory", foreground=C["lavender"])

        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_project_select)
        self._build_context_menu()

    def _build_git_tab(self):
        """Build the Git tab — shows live git state for the selected project."""
        self._git_path           = None
        self._git_status_files   = []   # [(xy, filepath), …]
        self._git_all_btns       = []
        self._git_push_pull_btns = []
        self._git_op_in_flight   = False   # True while a push/pull/commit is running

        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Git  ")

        # ── Header: project info + branch + remote ──────────────────────────
        hdr = tk.Frame(tab, bg=C["mantle"], padx=14, pady=8)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=C["mantle"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._git_project_lbl = tk.Label(left,
            text="Select a project in the Projects tab",
            font=("Segoe UI", 10, "bold"), bg=C["mantle"], fg=C["blue"])
        self._git_project_lbl.pack(anchor=tk.W)

        info_row = tk.Frame(left, bg=C["mantle"])
        info_row.pack(anchor=tk.W, pady=(2, 0))

        self._git_branch_lbl = tk.Label(info_row,
            text="Branch: —", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"])
        self._git_branch_lbl.pack(side=tk.LEFT, padx=(0, 20))

        self._git_remote_lbl = tk.Label(info_row,
            text="Remote: No remote set", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["overlay0"])
        self._git_remote_lbl.pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=C["mantle"])
        right.pack(side=tk.RIGHT, anchor=tk.N)

        btn_set_remote = ttk.Button(right, text="Set Remote",
                                    command=self.cmd_git_set_remote)
        btn_set_remote.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_set_remote,
            "Connect this project to a GitHub repository.\n"
            "Paste the HTTPS URL from github.com/new.\n"
            "Required before you can Push or Pull.")

        btn_github = ttk.Button(right, text="🐙  GitHub…",
                                command=self.cmd_github_setup)
        btn_github.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_github,
            "Open the GitHub Setup wizard — walks you through creating\n"
            "a GitHub account, connecting this project, pushing your code,\n"
            "and publishing a Release with your built .exe file.")

        btn_refresh = ttk.Button(right, text="⟳  Refresh",
                                 command=self._git_refresh)
        btn_refresh.pack(side=tk.LEFT)
        _Tooltip(btn_refresh, "Re-check the project's current git state and update this tab.")

        # ── Middle: status (left) + log (right) ─────────────────────────────
        mid = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        mid.pack(fill=tk.X)
        mid.columnconfigure(0, weight=1, minsize=200)
        mid.columnconfigure(1, weight=1, minsize=200)

        tk.Label(mid, text="WORKING TREE",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=0, sticky=tk.W, pady=(0, 4))

        tk.Label(mid, text="RECENT COMMITS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))

        status_wrap = tk.Frame(mid, bg=C["mantle"])
        status_wrap.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 4))

        status_vsb = ttk.Scrollbar(status_wrap, orient="vertical")
        self._git_status_lb = tk.Listbox(
            status_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            selectbackground=C["surface1"],
            activestyle="none",
            relief=tk.FLAT, bd=0,
            yscrollcommand=status_vsb.set)
        status_vsb.configure(command=self._git_status_lb.yview)
        self._git_status_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                  padx=6, pady=4)
        status_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._git_status_lb.bind("<<ListboxSelect>>", self._on_git_status_select)

        log_wrap = tk.Frame(mid, bg=C["mantle"])
        log_wrap.grid(row=1, column=1, sticky=tk.NSEW, padx=(4, 0))

        log_vsb = ttk.Scrollbar(log_wrap, orient="vertical")
        self._git_log_txt = tk.Text(
            log_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=log_vsb.set)
        log_vsb.configure(command=self._git_log_txt.yview)
        self._git_log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Action buttons (packed before diff so they're always visible) ───
        acts = tk.Frame(tab, bg=C["base"])
        acts.pack(fill=tk.X, padx=14, pady=(6, 4))

        row1 = tk.Frame(acts, bg=C["base"])
        row1.pack(anchor=tk.W, pady=(0, 4))
        row2 = tk.Frame(acts, bg=C["base"])
        row2.pack(anchor=tk.W)

        btn_push   = ttk.Button(row1, text="⬆  Push",
                                command=self.cmd_git_push)
        btn_pull   = ttk.Button(row1, text="⬇  Pull",
                                command=self.cmd_git_pull)
        btn_commit = ttk.Button(row1, text="📝  Commit…",
                                command=self.cmd_git_commit)
        btn_undo   = ttk.Button(row1, text="↩  Undo Last Commit",
                                command=self.cmd_git_undo_commit)
        btn_new    = ttk.Button(row2, text="🌿  New Branch",
                                command=self.cmd_git_new_branch)
        btn_switch = ttk.Button(row2, text="🔀  Switch Branch…",
                                command=self.cmd_git_switch_branch)
        btn_del    = ttk.Button(row2, text="🗑  Delete Branch…",
                                command=self.cmd_git_delete_branch)
        btn_openpr = ttk.Button(row2, text="🔗  Open PR",
                                command=self.cmd_git_open_pr)

        for btn in (btn_push, btn_pull, btn_commit, btn_undo,
                    btn_new, btn_switch, btn_del, btn_openpr):
            btn.pack(side=tk.LEFT, padx=(0, 6))

        _Tooltip(btn_push,
            "Send your saved commits to GitHub.\n"
            "Like uploading a backup — your work is now safe online\n"
            "and others can see it.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_pull,
            "Download any new commits from GitHub to this machine.\n"
            "Use this if you made changes on another computer,\n"
            "or if a collaborator pushed new work.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_commit,
            "Save a snapshot of your current changes.\n"
            "Like a save point in a game — you can always come back here.\n\n"
            "You'll write a short message describing what you changed.\n"
            "A suggestion is generated automatically from the file list.")
        _Tooltip(btn_undo,
            "Remove the most recent save point, but keep all your changes.\n"
            "Nothing is deleted — your edits stay exactly as they were.\n\n"
            "Useful if you committed too early or with the wrong message.")
        _Tooltip(btn_new,
            "Create a separate copy of the project to try out an idea.\n"
            "Changes on this branch won't touch your main code\n"
            "until you're ready to merge them in.")
        _Tooltip(btn_switch,
            "Jump to a different branch (version) of the project.\n"
            "For example: switch from an experiment back to 'master'.\n\n"
            "Tip: commit your changes first — switching with\n"
            "unsaved edits will fail.")
        _Tooltip(btn_del,
            "Delete a branch you no longer need.\n"
            "Safe by default — warns you if the branch has changes\n"
            "that haven't been saved back to the main branch yet.\n"
            "You can force-delete if you're sure you don't need them.")
        _Tooltip(btn_openpr,
            "Open a Pull Request on GitHub for the current branch.\n\n"
            "A Pull Request is a way to say: 'I made some changes on a\n"
            "separate branch — please review them and merge into main.'\n\n"
            "On master/main: shows you how to create a branch first.\n"
            "On any other branch: opens GitHub's compare page directly.\n\n"
            "Requires a GitHub remote and the branch to be pushed first.")

        self._git_all_btns       = [btn_set_remote, btn_push, btn_pull,
                                     btn_commit, btn_undo, btn_new,
                                     btn_switch, btn_del, btn_openpr]
        self._git_push_pull_btns = [btn_push, btn_pull, btn_openpr]

        # Start all buttons disabled — enabled by _git_update_ui once state is known
        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

        # ── Diff viewer (below buttons — expands to fill remaining space) ────
        tk.Label(tab, text="DIFF  (click a file above to preview)",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(
                     anchor=tk.W, padx=14, pady=(4, 4))

        diff_wrap = tk.Frame(tab, bg=C["mantle"])
        diff_wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        diff_vsb = ttk.Scrollbar(diff_wrap, orient="vertical")
        diff_hsb = ttk.Scrollbar(diff_wrap, orient="horizontal")
        self._git_diff_txt = tk.Text(
            diff_wrap,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=diff_vsb.set,
            xscrollcommand=diff_hsb.set)
        diff_vsb.configure(command=self._git_diff_txt.yview)
        diff_hsb.configure(command=self._git_diff_txt.xview)
        self._git_diff_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        diff_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        diff_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self._git_diff_txt.tag_configure("plus",   foreground=C["green"])
        self._git_diff_txt.tag_configure("minus",  foreground=C["red"])
        self._git_diff_txt.tag_configure("header", foreground=C["blue"])
        self._git_diff_txt.tag_configure("meta",   foreground=C["overlay0"])

    # ── Git tab data methods ────────────────────────────────────────────────

    def _git_tab_is_visible(self) -> bool:
        try:
            return self.nb.tab(self.nb.select(), "text").strip() == "Git"
        except (tk.TclError, AttributeError):
            return False

    def _on_project_select(self, event=None):
        """Fires when the user clicks a row in the Projects Treeview."""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("proj:"):
            return
        path = iid[5:]
        if path != self._git_path:
            self._git_path = path
            if self._git_tab_is_visible():
                self._git_refresh()

    def _on_tab_changed(self, event=None):
        """Fires when the user switches notebook tabs."""
        if not self._git_tab_is_visible():
            return
        # Sync to currently selected project (or active project)
        sel = self.tree.selection()
        if sel and sel[0].startswith("proj:"):
            self._git_path = sel[0][5:]
        elif not self._git_path and self.active_path:
            self._git_path = self.active_path
        if self._git_path:
            self._git_refresh()

    def _git_refresh(self):
        """Kick off a background thread that re-reads all git state."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            branch_out, brc = self._shell_capture(
                [GIT_EXE,"-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
            is_repo = brc == 0
            branch  = branch_out.strip() if is_repo else "—"

            remote_out, rrc = self._shell_capture(
                [GIT_EXE,"-C", path, "remote", "get-url", "origin"], path)
            remote = remote_out.strip() if rrc == 0 else ""

            status_out, _ = self._shell_capture(
                [GIT_EXE,"-C", path, "status", "--short"], path)

            log_out, lrc = self._shell_capture(
                [GIT_EXE,"-C", path, "log", "--oneline", "-15"], path)
            log_text = log_out.strip() if lrc == 0 else ""

            self.after(0, lambda: self._git_update_ui(
                path, name, is_repo, branch, remote, status_out, log_text))

        threading.Thread(target=worker, daemon=True).start()

    def _git_begin_op(self):
        """Mark a git operation as in flight and disable all Git tab buttons.

        Call this synchronously on the main thread BEFORE spawning the worker.
        Pair with `self.after(0, self._git_end_op)` in the worker's `finally`.
        """
        self._git_op_in_flight = True
        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _git_end_op(self):
        """Clear the in-flight flag and refresh the Git tab to re-enable buttons
        based on current repo/remote state."""
        self._git_op_in_flight = False
        self._git_refresh()

    def _git_update_ui(self, path, name, is_repo, branch, remote,
                       status_raw, log_text):
        """Main-thread update of all Git tab widgets."""
        self._git_project_lbl.config(text=name)

        if is_repo:
            self._git_branch_lbl.config(text=f"Branch:  {branch}",
                                         fg=C["text"])
        else:
            self._git_branch_lbl.config(
                text="Not a git repository — right-click project → 🔧 Git Init",
                fg=C["peach"])

        if remote:
            disp = remote.replace("https://", "").replace("http://", "")
            disp = disp.rstrip("/")
            if disp.endswith(".git"):
                disp = disp[:-4]
            self._git_remote_lbl.config(text=f"Remote:  {disp}",
                                         fg=C["overlay0"])
        else:
            self._git_remote_lbl.config(text="Remote:  No remote set",
                                         fg=C["overlay0"])

        # Enable/disable buttons. If a git operation is in flight, ALL buttons
        # stay disabled regardless of repo/remote state — prevents double-click
        # races (e.g. two concurrent pushes where the second one fails with
        # non-fast-forward).
        if getattr(self, "_git_op_in_flight", False):
            for btn in self._git_all_btns:
                btn.configure(state=tk.DISABLED)
        else:
            repo_state = tk.NORMAL if is_repo else tk.DISABLED
            for btn in self._git_all_btns:
                btn.configure(state=repo_state)
            push_pull_state = tk.NORMAL if (is_repo and remote) else tk.DISABLED
            for btn in self._git_push_pull_btns:
                btn.configure(state=push_pull_state)

        # Populate status listbox
        self._git_status_lb.configure(state=tk.NORMAL)
        self._git_status_lb.delete(0, tk.END)
        self._git_status_files = []
        if is_repo:
            # BUG FIX: do NOT call status_raw.strip() before splitlines() —
            # status lines for working-tree changes start with a leading space
            # (" M file.py"), and strip() eats that leading space from the
            # FIRST line only, shifting column 0..1 (status) and 3+ (path) so
            # the path silently loses its first character. Use splitlines()
            # directly and skip blanks individually.
            has_lines = False
            for line in status_raw.splitlines():
                if len(line) < 4:
                    continue
                has_lines = True
                xy   = line[:2]
                fname = line[3:]
                self._git_status_lb.insert(tk.END, f"  {xy}  {fname}")
                self._git_status_files.append((xy.strip(), fname))
            if not has_lines:
                self._git_status_lb.insert(tk.END, "  (working tree clean)")

        # Populate log
        self._git_log_txt.configure(state=tk.NORMAL)
        self._git_log_txt.delete("1.0", tk.END)
        if is_repo:
            self._git_log_txt.insert(tk.END,
                log_text if log_text else "(no commits yet)")
        self._git_log_txt.configure(state=tk.DISABLED)

        # Clear diff
        self._git_diff_txt.configure(state=tk.NORMAL)
        self._git_diff_txt.delete("1.0", tk.END)
        if not is_repo:
            self._git_diff_txt.insert(tk.END,
                "This project has no git history.\n\n"
                "Right-click the project in the Projects tab\n"
                "→ 🔧 Git Init to set one up.")
        self._git_diff_txt.configure(state=tk.DISABLED)

    def _on_git_status_select(self, event=None):
        """Click a file in the status listbox → show its diff below."""
        sel = self._git_status_lb.curselection()
        if not sel or not self._git_status_files:
            return
        idx = sel[0]
        if idx >= len(self._git_status_files):
            return
        _, fname = self._git_status_files[idx]
        path = self._git_path
        if not path:
            return

        def worker():
            # Unstaged changes
            d1, _ = self._shell_capture(
                [GIT_EXE,"-C", path, "diff", "--", fname], path)
            # Staged (cached) changes
            d2, _ = self._shell_capture(
                [GIT_EXE,"-C", path, "diff", "--cached", "--", fname], path)
            combined = "\n".join(filter(None, [d1.strip(), d2.strip()]))
            if not combined:
                combined = f"(no diff available — {fname} may be untracked or binary)"
            self.after(0, lambda d=combined: self._git_show_diff(d))

        threading.Thread(target=worker, daemon=True).start()

    def _git_show_diff(self, diff_text):
        """Render a diff string into the diff Text widget with colour tags."""
        txt = self._git_diff_txt
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        lines = diff_text.splitlines(keepends=True)
        CAP = 2000
        if len(lines) > CAP:
            lines = lines[:CAP]
            lines.append(f"\n… [diff capped at {CAP} lines — file is very large] …\n")

        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                txt.insert(tk.END, line, "plus")
            elif line.startswith("-") and not line.startswith("---"):
                txt.insert(tk.END, line, "minus")
            elif line.startswith("@@"):
                txt.insert(tk.END, line, "header")
            elif line.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file")):
                txt.insert(tk.END, line, "meta")
            else:
                txt.insert(tk.END, line)

        txt.configure(state=tk.DISABLED)

    # ── Git action commands ──────────────────────────────────────────────────

    def cmd_git_push(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return   # belt-and-suspenders — button is also disabled
        name = os.path.basename(path)
        self._log(f"Pushing {name}…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._shell_capture(
                    [GIT_EXE,"-C", path, "push", "-u", "origin", "HEAD"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log(f"  {line}", col)
                if rc != 0 and _is_auth_error(out):
                    self.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git push\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self))
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_pull(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._log(f"Pulling {name}…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._shell_capture(
                    [GIT_EXE,"-C", path, "pull"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log(f"  {line}", col)
                if rc != 0:
                    if _is_auth_error(out):
                        self.after(0, lambda: messagebox.showinfo(
                            "GitHub Authentication Required",
                            "GitHub needs to verify your identity.\n\n"
                            "Open a terminal in this project folder and run:\n"
                            "    git pull\n\n"
                            "A browser window will open asking you to log in to GitHub.\n"
                            "After that, this button will work normally.",
                            parent=self))
                    elif "conflict" in out.lower():
                        self.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            "Pull completed but there are merge conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<<).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.",
                            parent=self))
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_open_pr(self):
        """Open a pull-request comparison page on GitHub for the current branch.

        If on master/main, explains the branch workflow to the user first.
        Requires a GitHub remote to be set (button is disabled otherwise).
        """
        path = self._git_path
        if not path:
            return

        # Read branch + remote synchronously (both are instant)
        branch_out, brc = self._shell_capture(
            [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        remote_out, rrc = self._shell_capture(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)

        branch = branch_out.strip() if brc == 0 else ""
        remote = remote_out.strip() if rrc == 0 else ""

        if not remote:
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self)
            return

        # Normalise remote to a plain https://github.com/owner/repo URL
        base = remote.rstrip("/").removesuffix(".git")
        if base.startswith("git@github.com:"):
            base = "https://github.com/" + base[len("git@github.com:"):]

        is_main = branch in ("master", "main", "")

        if is_main:
            # Guide the user — they need a feature branch for a proper PR
            go = messagebox.askyesno(
                "You're on the main branch",
                f"You're on '{branch}' — the main/default branch.\n\n"
                "Pull Requests work like this:\n\n"
                "  1. 🌿 New Branch  →  give it a name (e.g. 'my-feature')\n"
                "  2. Make your changes, then 📝 Commit\n"
                "  3. ⬆ Push  →  sends the branch to GitHub\n"
                "  4. 🔗 Open PR  →  GitHub shows a 'Compare & pull request' button\n"
                "  5. Fill in the description and click 'Create pull request'\n\n"
                "Open the repository page on GitHub now?",
                parent=self)
            if go:
                os.startfile(base)
        else:
            # Feature branch — open the compare URL directly
            pr_url = f"{base}/compare/{branch}"
            self._log(f"  Opening PR page for branch '{branch}'…", C["peach"])
            os.startfile(pr_url)

    def cmd_git_undo_commit(self):
        """Undo the last commit, keeping all changes staged (git reset --soft HEAD~1)."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        if not messagebox.askyesno(
                "Undo Last Commit",
                "Undo the last commit?\n\n"
                "Your changes will be kept and moved back to 'staged'.\n"
                "Nothing is deleted — you can re-commit at any time.",
                parent=self):
            return
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._shell_capture(
                    [GIT_EXE,"-C", path, "reset", "--soft", "HEAD~1"], path)
                col = C["green"] if rc == 0 else C["red"]
                msg = "Last commit undone — changes are now staged." if rc == 0 else out.strip()
                self._log(f"  {msg}", col)
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_set_remote(self):
        """Open the Set Remote dialog to connect this project to GitHub."""
        path = self._git_path
        if not path:
            return
        # Check whether a remote already exists so the dialog can show current URL
        out, rc = self._shell_capture(
            [GIT_EXE,"-C", path, "remote", "get-url", "origin"], path)
        current_url = out.strip() if rc == 0 else ""
        SetRemoteDialog(self, path, current_url, self._do_git_set_remote)

    def _do_git_set_remote(self, path: str, url: str):
        """Callback from SetRemoteDialog — add or update the origin remote."""
        self._git_begin_op()
        def worker():
            try:
                # Check if remote already exists
                _, rc_check = self._shell_capture(
                    [GIT_EXE,"-C", path, "remote", "get-url", "origin"], path)
                if rc_check == 0:
                    cmd = [GIT_EXE,"-C", path, "remote", "set-url", "origin", url]
                else:
                    cmd = [GIT_EXE,"-C", path, "remote", "add", "origin", url]
                out, rc = self._shell_capture(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = "updated" if rc_check == 0 else "added"
                msg = f"Remote {action}: {url}" if rc == 0 else out.strip()
                self._log(f"  {msg}", col)
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_github_setup(self):
        """Open the GitHub Setup wizard for the selected/current project."""
        path = self._git_path
        if not path:
            # Fall back to the active project
            path = self.active_path
        if not path:
            messagebox.showwarning("No project selected",
                "Select a project first.", parent=self)
            return
        GitHubSetupDialog(self, path)

    def cmd_git_new_branch(self):
        """Open New Branch dialog."""
        path = self._git_path
        if not path:
            return
        NewBranchDialog(self, path, self._do_git_new_branch)

    def _do_git_new_branch(self, path: str, name: str, switch: bool):
        self._git_begin_op()
        def worker():
            try:
                if switch:
                    cmd = [GIT_EXE,"-C", path, "checkout", "-b", name]
                else:
                    cmd = [GIT_EXE,"-C", path, "branch", name]
                out, rc = self._shell_capture(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = f"Created and switched to '{name}'" if (switch and rc == 0) \
                         else (f"Created '{name}'" if rc == 0 else out.strip())
                self._log(f"  {action}", col)
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_switch_branch(self):
        """Open Switch Branch dialog."""
        path = self._git_path
        if not path:
            return
        # Get branch list on main thread (fast)
        out, rc = self._shell_capture(
            [GIT_EXE,"-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self)
            return
        branches = []
        current  = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                branches.append(line.strip())
        SwitchBranchDialog(self, path, branches, current, self._do_git_switch_branch)

    def _do_git_switch_branch(self, path: str, name: str):
        self._git_begin_op()
        def worker():
            try:
                out, rc = self._shell_capture(
                    [GIT_EXE,"-C", path, "checkout", name], path)
                if rc != 0:
                    self.after(0, lambda: messagebox.showerror(
                        "Switch Failed",
                        "Could not switch branches.\n\n"
                        "You may have uncommitted changes that conflict with the target branch.\n\n"
                        "Please commit or undo your changes before switching.",
                        parent=self))
                else:
                    self._log(f"  Switched to branch '{name}'", C["green"])
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_delete_branch(self):
        """Delete a non-current branch with safe/force-delete distinction."""
        path = self._git_path
        if not path:
            return
        out, rc = self._shell_capture(
            [GIT_EXE,"-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self)
            return
        # Collect non-current branches
        non_current = []
        for line in out.strip().splitlines():
            if not line.startswith("* "):
                non_current.append(line.strip())
        if not non_current:
            messagebox.showinfo("No Branches",
                "There are no other branches to delete.", parent=self)
            return
        # Let user pick
        branch = SwitchBranchDialog.pick(self, "Delete Branch", non_current,
                                          parent=self)
        if not branch:
            return
        if not messagebox.askyesno(
                "Delete Branch",
                f"Delete branch '{branch}'?\n\n"
                "If this branch has been merged, it will be removed safely.",
                parent=self):
            return

        self._git_begin_op()

        def worker():
            try:
                out, rc = self._shell_capture(
                    [GIT_EXE,"-C", path, "branch", "-d", branch], path)
                if rc == 0:
                    self._log(f"  Deleted branch '{branch}'", C["green"])
                    self.after(0, self._git_end_op)
                    return
                out_l = out.lower()
                if "not fully merged" in out_l or "unmerged" in out_l:
                    # Ask for force-delete on main thread; that path will
                    # release the in-flight lock when it finishes (or the
                    # user cancels).
                    self.after(0, ask_force)
                else:
                    self.after(0, lambda: messagebox.showerror(
                        "Delete Failed",
                        f"Could not delete branch '{branch}':\n\n{out.strip()}",
                        parent=self))
                    self.after(0, self._git_end_op)
            except Exception:
                self.after(0, self._git_end_op)
                raise

        def ask_force():
            if not messagebox.askyesno(
                    "Force Delete?",
                    f"Branch '{branch}' has unmerged changes.\n\n"
                    "Force-delete anyway?\n"
                    "This permanently discards those commits.",
                    parent=self):
                # User cancelled — release the lock
                self._git_end_op()
                return
            def force_worker():
                try:
                    o2, r2 = self._shell_capture(
                        [GIT_EXE,"-C", path, "branch", "-D", branch], path)
                    col = C["green"] if r2 == 0 else C["red"]
                    msg = f"Force-deleted '{branch}'" if r2 == 0 else o2.strip()
                    self._log(f"  {msg}", col)
                finally:
                    self.after(0, self._git_end_op)
            threading.Thread(target=force_worker, daemon=True).start()

        threading.Thread(target=worker, daemon=True).start()

    def _build_reference_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Reference  ")

        # ── Top: CLI cheatsheet ───────────────────────────────────────────────
        tk.Label(tab, text="CLI COMMANDS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=14, pady=(10, 4))

        cli_wrap = tk.Frame(tab, bg=C["mantle"])
        cli_wrap.pack(fill=tk.X, padx=14)

        cli_sb = ttk.Scrollbar(cli_wrap, orient="vertical")
        cli_txt = tk.Text(cli_wrap, height=9, font=("Consolas", 9),
                          bg=C["mantle"], fg=C["text"], relief=tk.FLAT,
                          padx=12, pady=8, wrap=tk.NONE,
                          cursor="arrow", state=tk.NORMAL,
                          yscrollcommand=cli_sb.set)
        cli_sb.configure(command=cli_txt.yview)
        cli_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cli_sb.pack(side=tk.RIGHT, fill=tk.Y)

        cli_txt.tag_configure("hd",  font=("Segoe UI", 9, "bold"), foreground=C["blue"],   spacing1=8, spacing3=2)
        cli_txt.tag_configure("cmd", font=("Consolas", 9),          foreground=C["peach"],  spacing3=1)
        cli_txt.tag_configure("dim", font=("Consolas", 9),          foreground=C["overlay0"])

        def cli_row(cmd, desc):
            cli_txt.insert(tk.END, f"  {cmd:<38}", "cmd")
            cli_txt.insert(tk.END, f"{desc}\n", "dim")

        def cli_h(t):
            cli_txt.insert(tk.END, f"\n  {t}\n", "hd")

        cli_h("Daily use")
        cli_row("tokensave sync",               "Incremental re-index (fast)")
        cli_row("tokensave sync --force",        "Full re-index from scratch")
        cli_row("tokensave sync --doctor",       "Sync and list what changed")
        cli_row("tokensave status",              "Stats + estimated token savings")
        cli_row("tokensave status --details",    "Stats with node-kind breakdown")
        cli_row("tokensave files",               "List all indexed files")
        cli_row("tokensave monitor",             "Live TUI of MCP tool calls")
        cli_h("Setup")
        cli_row("tokensave init",                "First-time index of a project")
        cli_row("tokensave install --agent claude", "Wire up Claude Code integration")
        cli_row("tokensave daemon",              "Auto-sync daemon (foreground)")
        cli_row("tokensave daemon --enable-autostart", "Install daemon as a service")
        cli_h("Troubleshooting")
        cli_row("tokensave doctor",              "Health check — diagnose issues")
        cli_row("tokensave upgrade",             "Self-update to latest version")
        cli_h("Cost & token tracking")
        cli_row("tokensave cost",                "7-day cost summary")
        cli_row("tokensave cost today",          "Today's spend only")
        cli_row("tokensave cost --by-model",     "Breakdown by Claude model")
        cli_h("Branches")
        cli_row("tokensave branch add",          "Track current git branch")
        cli_row("tokensave branch list",         "View tracked branches + DB sizes")
        cli_row("tokensave branch gc",           "Clean up deleted branches")

        cli_txt.configure(state=tk.DISABLED)

        # ── Bottom: Claude prompt snippets ────────────────────────────────────
        snippets_header = tk.Frame(tab, bg=C["base"])
        snippets_header.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(snippets_header, text="CLAUDE PROMPT SNIPPETS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(snippets_header, text="📖  Open Full Guide",
                   command=self._open_guide).pack(side=tk.RIGHT)

        snippets_frame = tk.Frame(tab, bg=C["base"])
        snippets_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # Left: listbox of snippet titles
        list_wrap = tk.Frame(snippets_frame, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self.snippet_lb = tk.Listbox(
            list_wrap, width=26, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        list_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                                command=self.snippet_lb.yview)
        self.snippet_lb.configure(yscrollcommand=list_sb.set)
        self.snippet_lb.pack(side=tk.LEFT, fill=tk.Y)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Right: preview + copy + add/edit/delete buttons
        right = tk.Frame(snippets_frame, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        # Populate listbox and build the parallel metadata map
        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    def _refresh_snippet_list(self, reselect_index=None):
        """Rebuild the snippet listbox and _active_snippets_map from scratch."""
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        # Built-in snippets
        for title, text in PROMPT_SNIPPETS:
            self.snippet_lb.insert(tk.END, f"  {title}")
            self._active_snippets_map.append({"type": "builtin", "data": {"title": title, "text": text}})

        # Separator
        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        # User snippets
        for idx, u in enumerate(_cfg.get("user_snippets", [])):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        # Restore selection
        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            # Clear preview and reset buttons if nothing to reselect
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            # Deselect separator — don't show anything
            self.snippet_lb.selection_clear(0, tk.END)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            return

        text = meta["data"]["text"]
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        is_user = (meta["type"] == "user")
        self._edit_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return
        self.clipboard_clear()
        self.clipboard_append(meta["data"]["text"])
        self._copy_status.configure(text="✔ Copied!")
        self.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        SnippetEditDialog(self, meta, self._on_snippet_saved)

    def _delete_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Delete snippet",
            f"Delete '{title}'?\n\nThis cannot be undone.",
            parent=self,
        ):
            return
        user_snippets = _cfg.get("user_snippets", [])
        idx = meta["index"]
        del user_snippets[idx]
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog — save and refresh."""
        user_snippets = _cfg.get("user_snippets", [])
        if edit_meta is None:
            # Add new snippet
            user_snippets.append({"title": title, "text": text})
            new_idx = len(PROMPT_SNIPPETS) + 1 + len(user_snippets) - 1  # separator + 0-based
        else:
            # Update existing
            idx = edit_meta["index"]
            user_snippets[idx] = {"title": title, "text": text}
            new_idx = len(PROMPT_SNIPPETS) + 1 + idx
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list(reselect_index=new_idx)

    def _open_guide(self):
        guide = os.path.join(_BASE_DIR, "TOKENSAVE_GUIDE.md")
        if os.path.isfile(guide):
            os.startfile(guide)
        else:
            messagebox.showerror("Not found",
                f"Guide not found at:\n{guide}", parent=self)

    def _build_help_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  Project Categories",  self._help_categories),
            ("  Git: What & Why",     self._help_git_concepts),
            ("  Git: Daily Workflow", self._help_git_workflow),
            ("  Git Tab Buttons",     self._help_git_tab),
            ("  GitHub Setup",        self._help_github_setup),
            ("  CodeGraph",           self._help_codegraph),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    def _help_show(self, fn):
        """Clear the help text widget, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ──────────────────────────────────────────────────────────

    def _help_switching(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_categories(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Project Categories")
            p("Projects are automatically grouped under the label of the search root "
              "folder they belong to. You can override any project's category — and add "
              "an optional sub-category — without moving any files.")
            br()
            h2("How root labels work")
            p("Each entry in Settings → Search Roots has a Label. That label becomes "
              "the category header for all projects found inside that folder. Edit the "
              "label in Settings to rename the whole group at once.")
            br()
            h2("Overriding a single project")
            ins("  1. Right-click the project row\n", "body")
            ins("  2. Choose  📁 Assign Category…\n", "body")
            ins("  3. Pick or type a Category (and optional Sub-category)\n", "body")
            ins("  4. Click OK — the project moves to the new group immediately\n", "body")
            br()
            p("To remove an override and return the project to its root's group, "
              "open Assign Category… and click Clear Override.")
            br()
            h2("Sub-categories")
            p("Sub-categories appear indented under their parent category (shown as "
              "↳ Sub-category). They work like folders-within-folders. Right-click "
              "any project at any time to move it between groups.")
            br()
            warn("⚠  Category headers and sub-category rows are not selectable — "
                 "right-click and action buttons only work on project rows.")
        self._help_show(_fill)

    def _help_git_concepts(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: What & Why")
            p("Git is a tool that remembers the history of every change you make to "
              "your project. Think of it like infinite undo — but smarter. You decide "
              "when to save a checkpoint, and you can always go back.")
            br()
            h2("Commit — a save point")
            p("A commit is a snapshot of your project at a moment in time. Each one "
              "has a short message you write, like 'fix: typo in README' or "
              "'feat: add dark mode'. Over time, these build up into a history "
              "you can scroll through.")
            br()
            h2("Repository (repo) — the project folder + its history")
            p("When you run Git Init on a project, git creates a hidden .git folder "
              "inside it. That folder stores every commit ever made. The whole thing "
              "— your files plus that history — is called a repository.")
            br()
            h2("Branch — a parallel version")
            p("Imagine photocopying your project so you can experiment on the copy "
              "without touching the original. That's a branch. When you're happy "
              "with the experiment, you can merge it back. The default branch is "
              "usually called 'master' or 'main'.")
            br()
            h2("Remote — a copy on GitHub")
            p("A remote is a second home for your repository, stored on GitHub's "
              "servers. It acts as a backup and lets others see your work. The "
              "remote is usually called 'origin'.")
            br()
            h2("Push — upload to GitHub")
            p("After making commits on your machine, Push sends them to GitHub. "
              "Nothing leaves your computer until you Push — commits are purely local "
              "until then.")
            br()
            h2("Pull — download from GitHub")
            p("Pull fetches any commits from GitHub that you don't have yet and "
              "adds them to your local history. Useful if you work on multiple "
              "machines, or if a collaborator pushed something new.")
            br()
            h2("Working tree — uncommitted changes")
            p("The working tree is the current state of your files right now, before "
              "you've committed them. The Git tab shows a list of files that have "
              "changed since your last commit. An 'M' means modified, '?' means "
              "a new file git hasn't seen before, 'D' means deleted.")
            br()
            h2("Staging — choosing what to commit")
            p("Git lets you pick exactly which changes to include in a commit. "
              "The 'Stage all changes' checkbox in the Commit dialog does this "
              "automatically — it stages everything in the working tree, which is "
              "almost always what you want.")
            br()
            ok("Bottom line: commit often, push when you're done for the day.")
        self._help_show(_fill)

    def _help_git_workflow(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: Daily Workflow")
            p("Here's how a typical coding session looks when using the Git tab.")
            br()
            h2("Starting a session")
            ins("  1. Switch to the Git tab\n", "body")
            ins("  2. Click ⟳ Refresh to see the current state\n", "body")
            ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
                "     changes from GitHub before you start editing\n", "body")
            br()
            h2("While you're working")
            p("Edit your files normally. The Working Tree list updates whenever "
              "you Refresh. Click any file in the list to see exactly what changed "
              "(green = added, red = removed).")
            br()
            h2("Saving your work (committing)")
            ins("  1. Click  📝 Commit…\n", "body")
            ins("  2. The dialog shows what files changed and suggests a message\n", "body")
            ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
            ins("  4. Click Commit\n", "body")
            br()
            p("There's no rule for how often to commit. A good rule of thumb: "
              "commit whenever you finish one thing. Small commits are better than "
              "one huge commit at the end of the day.")
            br()
            h2("Uploading to GitHub (pushing)")
            ins("  1. Click  ⬆ Push\n", "body")
            ins("  2. The output log shows whether it succeeded\n", "body")
            ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
            br()
            h2("Trying out an idea safely (branching)")
            ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
            ins("  2. Check 'Switch to this branch immediately'\n", "body")
            ins("  3. Make your changes and commit as normal\n", "body")
            ins("  4. If you like it: use 🔀 Switch Branch to go back to master,\n"
                "     then merge (currently via terminal: git merge try-new-ui)\n", "body")
            ins("  5. If you don't like it: just switch back to master — the\n"
                "     experiment branch stays there but your main code is untouched\n", "body")
            br()
            h2("Undoing mistakes")
            p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
              "as uncommitted edits — you can fix them and recommit, or just discard.")
            br()
            warn("⚠  Undo Last Commit only removes the last commit. To undo older "
                 "commits, use the terminal.")
            br()
            h2("Typical day in one line")
            dim("  Pull → Edit → Commit → Edit → Commit → Push")
        self._help_show(_fill)

    def _help_git_tab(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git Tab")
            p("The Git tab shows live status for whichever project is selected in the "
              "Projects tab. It updates automatically when you switch projects or switch "
              "to this tab.")
            br()
            h2("Working Tree & Diff")
            p("The Working Tree panel lists every modified, added, or deleted file. "
              "Click any file to see its diff below — added lines are green, removed "
              "lines are red.")
            br()
            h2("Committing changes")
            ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
            ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
            ins("  3. Edit the message if you like, then click Commit\n", "body")
            br()
            p("The suggested message is generated from the list of changed files "
              "(e.g. 'docs: update ARCHITECTURE.md' or 'chore: update 3 source files'). "
              "Click 💡 Suggest at any time to regenerate it.")
            br()
            h2("Undo Last Commit")
            p("Removes the most recent commit but keeps all your changes staged — "
              "nothing is deleted. Safe to use if you committed too early or with "
              "the wrong message.")
            br()
            h2("Branches")
            ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
            ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
            ins("  🗑 Delete Branch — safe-delete (warns if branch has unmerged changes)\n", "body")
            br()
            warn("⚠  Switching branches with uncommitted changes will fail. "
                 "Commit or undo first.")
            br()
            h2("Push & Pull")
            p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
              "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
        self._help_show(_fill)

    def _help_github_setup(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("GitHub Setup")
            p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
              "wizard for getting your project onto GitHub — even if you've never used "
              "GitHub before.")
            br()
            h2("Step 1 — Git identity")
            p("Every commit is stamped with your name and email. The wizard shows your "
              "current global settings and lets you update them. These are stored in "
              "your global git config and apply to every project on this machine.")
            br()
            h2("Step 2 — Create a GitHub account")
            p("Free at github.com. The wizard has a button to open the sign-up page.")
            br()
            h2("Step 3 — Create a repository")
            p("Go to github.com/new. Give it a name, leave it Public. "
              "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
              "Copy the HTTPS URL shown after creation (e.g. "
              "https://github.com/you/my-project.git).")
            br()
            h2("Step 4 — Paste the URL")
            p("Paste the URL into the wizard and click Set. This tells git where to "
              "send your code. The Git tab's Remote label will update immediately.")
            br()
            h2("Step 5 — Push")
            p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
              "you to log in to GitHub — this is Git Credential Manager doing its job. "
              "Log in once and future pushes happen silently.")
            br()
            warn("⚠  If Push fails with an authentication error, open a terminal in "
                 "the project folder and run:  git push\n"
                 "This triggers the browser login. After that, the Push button works normally.")
            br()
            h2("📦 GitHub Releases")
            p("A Release lets anyone download your .exe without needing Python "
              "installed. To create one:")
            ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
            ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
            ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
            ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
            br()
            p("Releases require the GitHub CLI (gh). If it's not installed, "
              "the wizard shows a link to cli.github.com.")
        self._help_show(_fill)

    def _help_codegraph(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("CodeGraph (alternative code-graph tool)")
            p("CodeGraph is a separate MCP server that does what tokensave does — "
              "builds a per-project code-graph index and exposes it to Claude Code. "
              "The two don't conflict; a project can have both at once.")
            br()
            h2("When to use which")
            ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
            ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
            ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
            br()
            warn("⚠  About CodeGraph's auto-sync: the file watcher only runs while "
                 "CodeGraph's MCP server is active inside an open Claude Code session. "
                 "If you edit code with Claude Code closed, those edits won't be "
                 "picked up automatically until the next session — at which point "
                 "the watcher catches up. You can also right-click → 🧠 CodeGraph Sync "
                 "to force an incremental update manually.")
            br()
            h2("Install")
            ins("  Settings → CodeGraph → Install via npm  ", "body")
            ins("(requires Node.js 18+)\n", "dim")
            br()
            h2("Use")
            ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
            ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
            br()
            h2("Why the manager doesn't run `codegraph install`")
            p("CodeGraph registers itself with Claude Code (and Cursor / Codex / "
              "opencode if you use them) via its own one-time installer: "
              "`npx @colbymchenry/codegraph`. The TokenSave Manager intentionally "
              "stays out of that flow — we handle per-project lifecycle only "
              "(init / sync / status / remove). This means tokensave and CodeGraph "
              "can both write their own sections into your global ~/.claude.json "
              "without fighting each other.")
        self._help_show(_fill)

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(TEMPLATE_DIR, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)

    # ── Data ───────────────────────────────────────────────────────────────────

    def _has_scaffold(self, path):
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def refresh(self):
        self.projects = find_projects()
        pinned = get_pinned()
        self.active_path = pinned or (self.projects[0]["path"] if self.projects else None)

        for item in self.tree.get_children():
            self.tree.delete(item)

        proj_cats = _cfg.get("project_categories", {})

        # Group projects by (category, subcategory)
        groups: dict = {}
        for p in self.projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            # Insert category header row if not yet present
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self.tree.insert("", tk.END, iid=ciid, text=cat,
                                 open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]

            # Insert sub-category header row if specified
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self.tree.exists(siid):
                    self.tree.insert(parent, tk.END, iid=siid, text=f"  ↳ {subcat}",
                                     open=True, tags=("subcategory",))
                parent = siid

            # Insert project rows
            for p in projs:
                is_active    = (p["path"] == self.active_path)
                has_scaffold = self._has_scaffold(p["path"])
                has_ts       = p.get("has_tokensave", True)
                has_git      = p.get("has_git", False)
                has_cg       = p.get("has_codegraph", False)
                if is_active:
                    base_tag = "active"
                elif not has_ts:
                    base_tag = "git_only"
                elif not has_scaffold:
                    base_tag = "scaffold"
                else:
                    base_tag = "normal"
                # synced column: show tokensave index age, or "—" for git-only projects
                synced_str = fmt_age(p["mtime"]) if has_ts else "—"
                # CG column: simple ✓/— marker (codegraph auto-syncs; no age shown)
                cg_text = "✓" if has_cg else "—"
                # Git column: placeholder "…" while the async refresh runs.
                # Non-git projects get "—" immediately and need no async update.
                git_text, git_tag = _format_git_status_cell(
                    p.get("git_status"), has_git)
                # Combine baseline + git tag (later tag wins for foreground)
                tags = (base_tag, git_tag) if has_git else (base_tag, "git_none")
                piid = f"proj:{p['path']}"
                self.tree.insert(parent, tk.END, iid=piid,
                                 text=p["name"],
                                 values=("★" if is_active else "",
                                         p["path"],
                                         synced_str,
                                         cg_text,
                                         git_text,
                                         "✔" if has_scaffold else "—"),
                                 tags=tags)

        if self.active_path:
            name = os.path.basename(self.active_path)
            tag  = "pinned" if pinned else "auto"
            self.active_badge.config(text=f"  ★ {name}  ({tag})  ")
        else:
            self.active_badge.config(text="  No project  ")

        # Keep Git tab in sync when it's visible and a project is tracked
        if self._git_tab_is_visible() and self._git_path:
            self._git_refresh()

        # Kick off background refresh of the Git status column so the
        # "…" placeholders get replaced with real ✓ / ● / ↑N / ↓N indicators.
        self._kick_off_git_status_refresh()

    def _kick_off_git_status_refresh(self):
        """Background-walk every git project and update its Git column cell.

        Cheap mtime-of-.git/index check first: if the index hasn't changed
        since the last computed status, reuse the cached result.
        """
        # Cancel any in-flight refresh; only one at a time
        if getattr(self, "_git_status_refresh_running", False):
            self._git_status_refresh_cancel = True
        self._git_status_refresh_cancel  = False
        self._git_status_refresh_running = True

        projects_snapshot = list(self.projects)

        def worker():
            try:
                for p in projects_snapshot:
                    if self._git_status_refresh_cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    # mtime cache: skip if .git/index hasn't changed since last
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue  # nothing new
                    try:
                        out, _rc = self._shell_capture(
                            [GIT_EXE, "-C", path,
                             "status", "--porcelain=v2", "--branch"],
                            path)
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]    = status
                    p["_git_idx_mtime"] = idx_mtime
                    piid = f"proj:{path}"
                    self.after(0, self._update_git_status_cell, piid, status)
                    # Yield to UI thread between checks
                    time.sleep(0.05)
            finally:
                self._git_status_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    # Set of tag names used as Git-status overrides on project rows.
    # _update_git_status_cell strips these (but never the baseline "git_only"
    # tag which uses a different meaning — git project with no tokensave).
    _GIT_STATUS_TAGS = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }

    def _update_git_status_cell(self, piid: str, status: dict):
        """Main-thread: update a single row's Git column value + override tag."""
        if not self.tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            self.tree.set(piid, "git", text)
        except tk.TclError:
            return
        # Preserve baseline tag (e.g. "active", "git_only", "scaffold", "normal"),
        # swap the prior status-override tag for the new one.
        existing = list(self.tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in self._GIT_STATUS_TAGS]
        existing.append(tag)
        self.tree.item(piid, tags=tuple(existing))

    def _build_context_menu(self):
        m = tk.Menu(self, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_separator()
        m.add_command(label="🧠  CodeGraph Init",          command=self.cmd_codegraph_init)
        m.add_command(label="🧠  CodeGraph Sync",          command=self.cmd_codegraph_sync)
        m.add_command(label="🧠  CodeGraph Status",        command=self.cmd_codegraph_status)
        m.add_command(label="🧠  Remove CodeGraph Index",  command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        if not row.startswith("proj:"):
            return   # no context menu on category/subcategory header rows
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _insert_pending_row(self, path, name):
        """Add a placeholder row while tokensave init is running.

        Six columns: active, path, synced, cg, git, scaffold — must match
        the Treeview definition in _build_projects_tab.
        """
        self.tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))

    def _check_config(self):
        problems = []
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            problems.append("tokensave.exe path is missing or invalid")
        if not TEMPLATE_DIR or not os.path.isdir(TEMPLATE_DIR):
            problems.append("Template directory is missing or invalid")
        if not problems:
            return
        note = "Please set the correct paths before using the manager."
        self._log("Config problem: " + " | ".join(problems), C["red"])
        SettingsDialog(self, _cfg, _save_config, self._on_settings_saved,
                       startup_note=note + "\n\n" + "\n".join(f"• {p}" for p in problems))

    def _auto_refresh(self):
        if self._current_proc is None:
            self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _log(self, msg, colour=None):
        def _do():
            self.log.configure(state=tk.NORMAL)
            tag = f"col_{colour}"
            self.log.tag_configure(tag, foreground=colour or C["green"])
            self.log.insert(tk.END, msg + "\n", tag)
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.", parent=self)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).", parent=self)
            return None
        return iid[5:]   # strip "proj:" prefix

    # ── Scaffold ────────────────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path) -> list:
        """Copy Nuitka build templates into path, auto-filling [PROJECT_NAME].

        Returns a list of action strings describing what was created (empty if
        both files already existed or if the templates couldn't be found).
        """
        name     = os.path.basename(path)
        src_ps1  = os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template")
        src_bat  = os.path.join(TEMPLATE_DIR, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._log("  [WARN] Nuitka templates not found in template directory — skipped",
                      C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {TEMPLATE_DIR}")
            return actions

        # build.ps1 — fill [PROJECT_NAME]; leave [ENTRY_SCRIPT] / [OUTPUT_NAME] for the user
        if os.path.isfile(dst_ps1):
            self._log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        # build.bat — copy as-is (ASCII, no customisation needed)
        if os.path.isfile(dst_bat):
            self._log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])

        return actions

    def _scaffold_project(self, path, create_bi=True, run_init=True,
                          scaffold_nuitka=False, add_git_hook=False):
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        # Write BASIC_INSTRUCTIONS.md synchronously (it's instant)
        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template()
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        # Copy Nuitka build templates synchronously (instant file copy)
        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        # Write/merge auto-commit Stop hook
        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._log(f"  {action}", C["green"])

        if run_init:
            # Show a pending row immediately so the user sees the project appearing
            self._insert_pending_row(path, name)
            self._log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self.after(0, self._set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self._current_proc = None
                    self.after(0, self._set_running, False)
                    self.after(0, self.refresh)
                    # Offer to commit the new BASIC_INSTRUCTIONS.md /
                    # Nuitka templates / hook files (if this is a git repo)
                    self.after(0, lambda: self._offer_commit_after_change(
                        path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self.refresh()
            # Even without tokensave init, file writes still happened
            self._offer_commit_after_change(path, "scaffold files")

    # ── Commands ───────────────────────────────────────────────────────────────

    def _require_tokensave(self, path: str) -> bool:
        """Return True if the project has a tokensave index.

        If not, shows a friendly info dialog explaining how to add one and
        returns False so the caller can bail out gracefully.
        """
        if os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db")):
            return True
        name = os.path.basename(path)
        messagebox.showinfo(
            "Not indexed with tokensave",
            f"'{name}' is a git project but doesn't have a tokensave index yet.\n\n"
            "Tokensave builds a code-graph that lets Claude navigate your project "
            "efficiently without reading every file.\n\n"
            "To add it:  right-click → ⚙ Retrofit…  and tick "
            "'Add tokensave @include + init'.",
            parent=self)
        return False

    def _require_codegraph_installed(self) -> bool:
        """Return True if the CodeGraph CLI is installed and resolvable.

        If not, shows a friendly install nudge dialog with an 'Open Settings'
        button that scrolls Settings to the CodeGraph section. Returns False
        so callers can bail out gracefully.
        """
        if CODEGRAPH_EXE and os.path.isfile(CODEGRAPH_EXE):
            return True
        # Custom yes/no dialog so we can label the buttons "Open Settings" / "Cancel"
        result = messagebox.askyesno(
            "CodeGraph is not installed",
            "CodeGraph is not installed on this machine.\n\n"
            "CodeGraph is an alternative code-graph tool that builds a "
            "per-project SQLite index for Claude Code to query. It complements "
            "tokensave — both can be enabled on the same project.\n\n"
            "Open Settings now to install it?",
            parent=self)
        if result:
            dlg = SettingsDialog(self, _cfg, _save_config, self._on_settings_saved)
            # Defer the scroll so the dialog has been mapped and sized first
            if hasattr(dlg, "_scroll_to_codegraph"):
                dlg.after(50, dlg._scroll_to_codegraph)
        return False

    def cmd_set_active(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        set_pinned(path)
        self._log(f"Pinned → {path}", C["green"])
        self._log("Restart Claude Desktop for the change to take effect.", C["yellow"])
        self.refresh()

    def cmd_auto(self):
        clear_pinned()
        self._log("Auto-detect enabled — wrapper picks the most-recently-synced project.", C["sky"])
        self._log("Restart Claude Desktop for the change to take effect.", C["yellow"])
        self.refresh()

    def _set_running(self, running, label=""):
        if running:
            self._stop_btn.configure(state=tk.NORMAL)
            self._running_label.configure(text=f"⏳ running: {label}")
        else:
            self._stop_btn.configure(state=tk.DISABLED)
            self._running_label.configure(text="")

    def _stop_current(self):
        self._stop_requested = True
        proc = self._current_proc
        if proc and proc.poll() is None:
            proc.kill()
            self._log("  ■ Stopped by user.", C["red"])

    def _open_log(self):
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("No log yet",
                "No log file exists yet — run an operation first.", parent=self)

    def _run(self, args, cwd, label):
        def worker():
            cmd_str = "tokensave " + " ".join(args)
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            log.debug(f"     cwd={cwd}")
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE] + args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                log.debug(f"     pid={proc.pid}")
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    self._log(stripped)
                    log.debug(f"  OUT {stripped}")
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                    # Auto-commit after sync if the toggle is on and this is a git repo
                    if (args and args[0] == "sync"
                            and _cfg.get("auto_commit_after_sync")
                            and _is_git_repo(cwd)):
                        self._log("  Auto-committing sync changes…", C["peach"])
                        self._shell_capture([GIT_EXE,"-C", cwd, "add", "-A"], cwd)
                        _, staged_rc = self._shell_capture(
                            [GIT_EXE,"-C", cwd, "diff", "--cached", "--quiet"], cwd)
                        if staged_rc != 0:   # non-zero = staged changes exist
                            # Amend the previous commit if it was also a sync commit
                            # so repeated syncs don't pile up in history
                            last_out, _ = self._shell_capture(
                                [GIT_EXE,"-C", cwd, "log", "-1", "--format=%s"], cwd)
                            if last_out.strip() == "chore: tokensave sync":
                                commit_cmd = [GIT_EXE,"-C", cwd, "commit",
                                              "--amend", "--no-edit"]
                                self._log("  Amending previous sync commit…", C["peach"])
                            else:
                                commit_cmd = [GIT_EXE,"-C", cwd, "commit",
                                              "-m", "chore: tokensave sync"]
                            cout, crc = self._shell_capture(commit_cmd, cwd)
                            col = C["green"] if crc == 0 else C["red"]
                            for line in cout.strip().splitlines()[-3:]:
                                self._log(f"  {line}", col)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)
        threading.Thread(target=worker, daemon=True).start()

    def _run_capture(self, args, cwd, label) -> tuple:
        """Run a tokensave command and return (raw_output, returncode, elapsed_s).

        Synchronous — must be called from a background thread.
        Handles _current_proc tracking and _set_running for the duration.
        The caller is responsible for logging and scheduling UI updates.
        """
        cmd_str = "tokensave " + " ".join(args)
        self._log(f"$ {cmd_str}  [{label}]", C["blue"])
        self.after(0, self._set_running, True, label)
        log.info(f"RUN  {cmd_str}")
        log.debug(f"     cwd={cwd}")
        t0 = time.monotonic()
        try:
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            proc = subprocess.Popen(
                [TOKENSAVE] + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self._current_proc = proc
            log.debug(f"     pid={proc.pid}")
            raw = proc.stdout.read()
            proc.wait()
            elapsed = time.monotonic() - t0
            log.info(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
            log.debug(f"  OUT {raw[:500]}")
            return raw, proc.returncode, elapsed
        finally:
            self._current_proc = None
            self.after(0, self._set_running, False)

    def _shell_capture(self, cmd: list, cwd: str, env=None) -> tuple:
        """Run any shell command and return (stdout+stderr, returncode).

        Generic helper — cmd[0] is the executable (not tokensave-specific).
        Synchronous — must be called from a background thread.
        Returns ("Error: '<exe>' not found on system PATH.", 1) if the
        executable is missing so callers always get a displayable string.

        Pass env= to override the process environment (e.g. set
        GIT_TERMINAL_PROMPT=0 for network git operations so they fail
        immediately instead of hanging waiting for stdin auth prompts).
        """
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
            out = proc.stdout.read()
            proc.wait()
            return out, proc.returncode
        except FileNotFoundError:
            return (f"Error: '{cmd[0]}' not found on system PATH.", 1)

    def cmd_sync(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._run(["sync"], cwd=path, label=os.path.basename(path))

    def cmd_sync_all(self):
        if not self.projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self)
            return
        ts_projects = [p for p in self.projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self)
            return
        count = len(ts_projects)
        skipped = len(self.projects) - count
        skip_note = f"\n({skipped} git-only project{'s' if skipped != 1 else ''} will be skipped)" if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self,
        ):
            return

        projects_snapshot = list(ts_projects)

        def worker():
            self._stop_requested = False
            self._log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self.after(0, self._set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._stop_requested:
                    self._log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self._current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self.after(0, self._set_running, False)
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._log(f"  Status OK — {data.get('node_count')} nodes, "
                              f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._log(line)
                self._log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name, msg):
        win = tk.Toplevel(self)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_git_log(self):
        """Navigate to the Git tab and show live status/log for the selected project.

        Replaces the old popup approach — all git information is now displayed
        inline in the Git tab, which avoids dialog clutter.
        """
        path = self._selected_path()
        if not path:
            return

        # Point the Git tab at this project and switch to it
        self._git_path = path
        try:
            for idx in range(self.nb.index("end")):
                if self.nb.tab(idx, "text").strip() == "Git":
                    self.nb.select(idx)
                    break
        except tk.TclError:
            pass

        self._git_refresh()

    # ── Git Init ───────────────────────────────────────────────────────────────

    def cmd_git_init(self):
        """Right-click: initialise a git repository in the selected project folder.

        git init is near-instantaneous (~5ms) so we run it synchronously on the
        main thread — no need for a background thread, and it keeps the
        messagebox flow simple and race-condition-free.
        """
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)

        # Guard: already a repo?
        if _is_git_repo(path):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self,
            )
            return

        # Run git init on the main thread (instantaneous)
        self._log(f"Running git init in {name}…", C["peach"])
        out, rc = self._shell_capture([GIT_EXE,"-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._log(f"  {line}", col)

        if rc != 0:
            self.refresh()
            return

        # Write a baseline .gitignore if none exists (protects git add -A)
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._log(f"  Warning: could not write .gitignore: {e}", C["yellow"])

        # Ask about initial commit — natural main-thread messagebox, no scheduling
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self,
        ):
            def do_commit():
                self._shell_capture([GIT_EXE,"-C", path, "add", "-A"], path)
                cout, crc = self._shell_capture(
                    [GIT_EXE,"-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", ccol))
                self.after(0, self.refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self.refresh()

    # ── Ensure .gitignore ──────────────────────────────────────────────────────

    def cmd_manage_gitignore(self):
        """Right-click: open the .gitignore management dialog for the selected project.

        Replaces the old cmd_ensure_gitignore command. The baseline-only
        quick-add is still available as the first button inside the dialog
        ('+ Baseline'). The module-level `_ensure_gitignore` function is
        unchanged and still called by `cmd_git_init` for first-time setup.
        """
        path = self._selected_path()
        if not path:
            return
        GitignoreDialog(self, path)

    def cmd_untrack_ignored(self):
        """Right-click: open the Untrack Ignored Files dialog.

        Finds every file that's tracked by git AND matches a pattern in
        .gitignore (the 'stale tracking' problem) and offers a checklist
        for selective untracking via `git rm --cached`.
        """
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo("Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self)
            return
        files = _find_tracked_but_ignored(path)
        if not files:
            messagebox.showinfo("Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self)
            return
        UntrackIgnoredDialog(self, path, files)

    def _do_untrack_ignored(self, path: str, files: list):
        """Worker: run `git rm --cached -- <files>` in a background thread.

        Files are removed from the index (`git rm --cached`) but the
        working-tree copies are preserved — this is the safe "stop
        tracking but keep the file" operation. After completion, offers
        a commit prompt so the user can record the untracking in history.
        """
        if not files:
            return
        name = os.path.basename(path)
        self._log(f"Untracking {len(files)} file"
                  f"{'s' if len(files) != 1 else ''} in {name}…",
                  C["peach"])

        def worker():
            try:
                # `git rm --cached` accepts multiple paths after the `--` arg
                # separator. We always pass --cached so the working tree is
                # never touched (only the index).
                out, rc = self._shell_capture(
                    [GIT_EXE, "-C", path, "rm", "-r", "--cached", "--"] + files,
                    path)
                col = C["green"] if rc == 0 else C["red"]
                # Log a few output lines for visibility
                for line in out.strip().splitlines()[-6:]:
                    self._log(f"  {line}", col)
                if rc != 0:
                    return
                self._log(
                    f"  ✓ Untracked {len(files)} file"
                    f"{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self.after(0, self.refresh)
                # Offer to commit the untracking in the same flow.
                # Builds on the existing post-action commit prompt.
                self.after(0, lambda: self._offer_commit_after_change(
                    path,
                    f"untrack {len(files)} ignored file"
                    f"{'s' if len(files) != 1 else ''}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Git Commit ─────────────────────────────────────────────────────────────

    def cmd_git_commit(self):
        """Right-click: open the Git Commit dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        self._open_commit_dialog(path)

    def _open_commit_dialog(self, path: str):
        """Open GitCommitDialog for a given project path. Reused by
        `cmd_git_commit` (Projects-tab right-click) AND by the
        offer-commit-after-change flow that runs after Ensure .gitignore,
        Shadow Links, Scaffold, and Retrofit."""
        # Fetch status synchronously (fast) so the dialog can show it immediately.
        status_out, _ = self._shell_capture(
            [GIT_EXE,"-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit)

    def _offer_commit_after_change(self, path: str, summary_label: str):
        """After a destructive manager action (Ensure .gitignore, Shadow Links,
        Scaffold, Retrofit, CodeGraph Init), check whether the working tree is
        now dirty and offer the user a commit dialog if so.

        Uses _is_local_git_repo (strict — does NOT walk upward) so a project
        nested inside an unrelated parent git repo never triggers a ghost
        commit-prompt against the wrong repository.

        Silent no-ops:
          - Path is not a git repo root locally (nothing to commit here)
          - Working tree is still clean (the operation didn't change anything)
        """
        if not _is_local_git_repo(path):
            return
        # Check whether the working tree is actually dirty
        status_out, _ = self._shell_capture(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self):
            self._open_commit_dialog(path)
        else:
            self._log(f"  Working tree left dirty — commit when you're ready.",
                      C["yellow"])

    def _do_git_commit(self, path: str, message: str, selected_files: list):
        """Stage only `selected_files` (clearing any pre-existing staging) and
        commit them with `message`. Files not in the list stay as un-committed
        changes in the working tree."""
        name = os.path.basename(path)
        # Lock Git tab buttons during the commit (prevents double-click races).
        self._git_begin_op()

        def worker():
            try:
                # 1. Clear the index so nothing already-staged sneaks into the
                # commit. `git reset` without args = `git reset HEAD` — safe,
                # only touches the index, never the working tree.
                self._shell_capture([GIT_EXE,"-C", path, "reset"], path)

                # 2. Stage exactly the picked files. `--` separates flags from
                # paths and protects against filenames that start with "-".
                if selected_files:
                    out, rc = self._shell_capture(
                        [GIT_EXE,"-C", path, "add", "--"] + selected_files, path)
                    if rc != 0:
                        self.after(0, lambda: self._log(
                            f"git add failed: {out.strip()}", C["red"]))
                        return

                # 3. Commit. Only the staged files will be in the commit.
                self._log(f"Committing {name} ({len(selected_files)} file"
                          f"{'s' if len(selected_files) != 1 else ''})…",
                          C["peach"])
                cout, crc = self._shell_capture(
                    [GIT_EXE,"-C", path, "commit", "-m", message], path)
                col = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", col))
                self.after(0, self.refresh)
            finally:
                self.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_force_sync(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self,
        ):
            self._run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def cmd_doctor(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._run(["doctor"], cwd=path, label=os.path.basename(path))

    # ── CodeGraph commands ─────────────────────────────────────────────────

    def cmd_codegraph_init(self):
        """Right-click: initialise CodeGraph in the selected project.

        Uses `--index` so init also builds the initial graph in one step
        (matches the experience of tokensave init).
        """
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if _is_codegraph_project(path):
            messagebox.showinfo("Already initialised",
                f"{os.path.basename(path)} already has CodeGraph initialised.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph init in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "init", "--index", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._log(f"  {line}", col)
            self.after(0, self.refresh)
            # Offer to commit the new .codegraph/config.json if this is a
            # local git repo — uses _is_local_git_repo so a parent-directory
            # repo doesn't ghost-trigger the prompt.
            self.after(0, lambda: self._offer_commit_after_change(
                path, "CodeGraph init files"))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_sync(self):
        """Right-click: incrementally sync CodeGraph's index.

        Note: CodeGraph auto-syncs via its native file watcher while its
        MCP server is running inside a Claude Code session. This manual
        sync is useful for catching up after editing files with Claude
        Code closed.
        """
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph sync in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "sync", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._log(f"  {line}", col)
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_status(self):
        """Right-click: show CodeGraph stats for the selected project."""
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph status in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "status", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines():
                self._log(f"  {line}", col)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_remove(self):
        """Right-click: delete the .codegraph/ directory after confirmation."""
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        cg_dir = os.path.join(path, ".codegraph")
        if not os.path.isdir(cg_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no CodeGraph index.", parent=self)
            return
        if not messagebox.askyesno(
                "Remove CodeGraph index?",
                f"Delete the CodeGraph index for {name}?\n\n"
                f"This removes .codegraph/ from the project folder. Your "
                "source files are not touched. You can re-create the index "
                "later via 🧠 CodeGraph Init.",
                parent=self):
            return
        try:
            shutil.rmtree(cg_dir)
            self._log(f"  Removed .codegraph/ from {name}", C["green"])
        except OSError as e:
            self._log(f"  Could not remove .codegraph/: {e}", C["red"])
        self.refresh()

    def cmd_open_folder(self):
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self):
        path = self._selected_path()
        if not path:
            return
        editor_str = _cfg.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self,
            )

    def cmd_copy_path(self):
        path = self._selected_path()
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self._log(f"Copied: {path}", C["sky"])

    def cmd_remove(self):
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self.refresh()
        except Exception as e:
            self._log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self)

    def cmd_settings(self):
        SettingsDialog(self, _cfg, _save_config, self._on_settings_saved)

    def _on_settings_saved(self):
        global TOKENSAVE, TEMPLATE_DIR, SEARCH_ROOTS, GIT_EXE, CODEGRAPH_EXE
        global BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE
        TOKENSAVE     = _cfg.get("tokensave_exe", "")
        TEMPLATE_DIR  = _cfg.get("template_dir", "")
        SEARCH_ROOTS  = _cfg.get("search_roots", [])
        GIT_EXE       = _cfg.get("git_exe") or _detect_git()
        CODEGRAPH_EXE = _cfg.get("codegraph_exe") or _detect_codegraph()
        BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
        BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"
        self.refresh()
        self._log("Settings saved and applied.", C["green"])

    # ── Shadow Links ───────────────────────────────────────────────────────────

    def cmd_shadow_links(self):
        """Right-click: open the Shadow Links dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict, run_sync: bool = True):
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            try:
                self._log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and TOKENSAVE and created > 0:
                    self._log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._log(f"    {line}", col)

                self.after(0, self.refresh)
                self.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}." if run_sync and created > 0 else ""),
                    parent=self))
                # Shadow Links wrote hardlinks + .gitignore entries — offer commit
                self.after(0, lambda: self._offer_commit_after_change(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()

    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_assign_category(self):
        """Right-click: open the Assign Category dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Build sorted list of all categories and subcategories currently in use
        all_cats: list = []
        all_subs: dict = {}  # cat -> set of subcategories
        for r in SEARCH_ROOTS:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in _cfg.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = _cfg.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path, cat, subcat):
        """Callback from AssignCategoryDialog — update config and refresh."""
        proj_cats = _cfg.setdefault("project_categories", {})
        if cat is None:
            # Clear override
            proj_cats.pop(path, None)
            self._log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        _save_config(_cfg)
        self.refresh()

    # ── Scaffold / Retrofit ────────────────────────────────────────────────────

    def cmd_scaffold(self):
        folder = filedialog.askdirectory(title="Select folder to scaffold", parent=self)
        if not folder:
            return
        ScaffoldDialog(self, folder, self._scaffold_project)

    def cmd_retrofit(self):
        """Toolbar button — pick any folder then open the Retrofit dialog."""
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self)
        if not folder:
            return
        RetrofitDialog(self, folder, self._do_retrofit)

    def cmd_retrofit_selected(self):
        """Right-click menu — open the Retrofit dialog for the selected project directly."""
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self, path, self._do_retrofit)

    def _do_retrofit(self, path, add_tokensave, add_basic_instructions,
                     add_nuitka=False, add_shadow_links=False, shadow_ext_map=None,
                     add_git_hook=False):
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} nuitka={add_nuitka}")
                self._log(f"Retrofitting {name}…", C["peach"])
                actions_taken = []

                # ── Tokensave integration ──────────────────────────────────────
                if add_tokensave:
                    claude_md = os.path.join(path, "CLAUDE.md")
                    include_line = BASELINE_INCLUDE_LINE

                    if os.path.isfile(claude_md):
                        content = open(claude_md, encoding="utf-8", errors="ignore").read()
                        if "project-baseline.md" in content:
                            log.info("  CLAUDE.md already has @include — skipped")
                            self._log("  Tokensave already integrated in CLAUDE.md — skipped",
                                      C["overlay0"])
                        else:
                            with open(claude_md, "r+", encoding="utf-8") as f:
                                existing = f.read()
                                f.seek(0)
                                f.write(include_line + "\n\n" + existing)
                            log.info("  prepended @include to CLAUDE.md")
                            self._log("  Added tokensave @include to CLAUDE.md", C["green"])
                            actions_taken.append("Added tokensave rules to CLAUDE.md")
                    else:
                        with open(claude_md, "w", encoding="utf-8") as f:
                            f.write(
                                f"# {name} — Claude Instructions\n\n"
                                f"{include_line}\n"
                            )
                        log.info("  created CLAUDE.md with @include")
                        self._log("  Created CLAUDE.md with tokensave @include", C["green"])
                        actions_taken.append("Created CLAUDE.md with tokensave rules")

                # ── BASIC_INSTRUCTIONS.md ─────────────────────────────────────
                if add_basic_instructions:
                    basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
                    if os.path.isfile(basic_md):
                        log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
                        self._log("  BASIC_INSTRUCTIONS.md already exists — skipped",
                                  C["overlay0"])
                    else:
                        template = load_basic_instructions_template()
                        with open(basic_md, "w", encoding="utf-8") as f:
                            f.write(template)
                        log.info("  created BASIC_INSTRUCTIONS.md")
                        self._log("  Created BASIC_INSTRUCTIONS.md", C["green"])
                        actions_taken.append("Created BASIC_INSTRUCTIONS.md")

                # ── Nuitka build files ─────────────────────────────────────────
                if add_nuitka:
                    nuitka_actions = self._scaffold_nuitka_build(path)
                    actions_taken.extend(nuitka_actions)

                # ── Shadow extension links ─────────────────────────────────────
                if add_shadow_links:
                    ext_map = shadow_ext_map or DEFAULT_SHADOW_EXT_MAP
                    self._log("  Generating shadow extension links…", C["peach"])
                    created, skipped, failed = generate_shadow_links(path, ext_map)
                    update_gitignore_for_shadows(path, ext_map)
                    sl_msg = f"Shadow links: created {created}"
                    if skipped:
                        sl_msg += f", {skipped} already existed"
                    if failed:
                        sl_msg += f", {failed} failed"
                    self._log(f"  {sl_msg}", C["green"])
                    log.info(f"  {sl_msg}")
                    if created > 0:
                        actions_taken.append(sl_msg)

                # ── Auto-commit Stop hook ──────────────────────────────────────
                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._log(f"  {action}", C["green"])
                    actions_taken.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions_taken or 'nothing changed'}")
                self._log(f"Retrofit complete: {path}", C["green"])
                self.after(0, self.refresh)

                if actions_taken:
                    summary = "\n".join(f"  ✔ {a}" for a in actions_taken)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions_taken):
                        msg += "\n\nNext step: open build.ps1 and replace [ENTRY_SCRIPT] and [OUTPUT_NAME] before building."
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self.after(0, lambda: messagebox.showinfo("Retrofit complete", msg, parent=self))
                # If anything actually changed, offer to commit
                if actions_taken:
                    self.after(0, lambda: self._offer_commit_after_change(
                        path, "retrofit additions"))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror("Retrofit failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()


# ── Retrofit dialog ────────────────────────────────────────────────────────────

class RetrofitDialog(tk.Toplevel):
    """Small dialog with two checkboxes for the retrofit options."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Retrofit Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.callback = callback
        self.path = path

        pad = {"padx": 20, "pady": 8}

        tk.Label(self, text="Retrofit options",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 4))

        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(0, 10))

        # Checkbox: tokensave integration
        self.var_ts = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Add tokensave rules to CLAUDE.md",
            variable=self.var_ts,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Prepends an @include line so Claude always loads the\n"
                 "  tokensave lookup table. Non-destructive — existing content kept.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self.var_bi = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Also create BASIC_INSTRUCTIONS.md",
            variable=self.var_bi,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Drops a full project template (overview, architecture,\n"
                 "  key files, rules) for Claude to fill in on first use.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self.var_nuitka = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add Nuitka build files",
            variable=self.var_nuitka,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text=f"  Copies build templates from the templates folder.{chr(10)}"
                 "  Edit [ENTRY_SCRIPT] and [OUTPUT_NAME] in build.ps1 before building.\n"
                 f"  {nuitka_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Shadow extension links
        self.var_shadow = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Generate shadow extension links",
            variable=self.var_shadow,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # Count existing shadow files
        existing_shadows = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        shadow_note = (f"  {existing_shadows} shadow file(s) already exist."
                       if existing_shadows else
                       "  None exist yet — click Apply to create them.")
        tk.Label(self,
            text="  Creates NTFS hardlinks (.zs→.cpp, .zsc→.cpp, .acs→.c, DECORATE→.cpp)\n"
                 "  so tokensave can parse ZScript/ACS/DECORATE as C++/C. Zero disk cost.\n"
                 "  Adds gitignore patterns. Use 🔗 Shadow Links… for custom mappings.\n"
                 f"  {shadow_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Auto-commit Stop hook
        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_note = "  (already present)" if os.path.isfile(hook_settings) else "  (.claude/settings.json)"
        self.var_hook = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add auto-commit Stop hook",
            variable=self.var_hook,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Auto-commits when Claude finishes a session in this project.\n"
                 "  Only commits when the working tree has changes. Safe on clean repos.\n"
                 f"  {hook_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 12))

        # Buttons
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 16))

        ttk.Button(btn_frame, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        # Centre over parent
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        ts     = self.var_ts.get()
        bi     = self.var_bi.get()
        nuitka = self.var_nuitka.get()
        shadow = self.var_shadow.get()
        hook   = self.var_hook.get()
        self.destroy()
        if ts or bi or nuitka or shadow or hook:
            self.callback(self.path, ts, bi, nuitka, shadow, add_git_hook=hook)


# ── Scaffold dialog ────────────────────────────────────────────────────────────

class ScaffoldDialog(tk.Toplevel):
    """Options dialog shown before scaffolding a new project."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Scaffold Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self.path = path
        self.callback = callback

        name = os.path.basename(path)
        has_bi = os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))
        has_db = os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db"))

        pad = dict(padx=20, pady=6)

        # Folder display
        tk.Label(self, text="Folder", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=path, bg=C["surface0"], fg=C["text"],
                 font=("Consolas", 9), padx=10, pady=6,
                 wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=20, pady=(2, 10))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self._bi_var = tk.BooleanVar(value=not has_bi)
        bi_text = "Create BASIC_INSTRUCTIONS.md"
        bi_note = "  (already exists — will overwrite)" if has_bi else "  (Claude instruction template)"
        bi_frame = tk.Frame(self, bg=C["base"])
        bi_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(bi_frame, text=bi_text, variable=self._bi_var).pack(side=tk.LEFT)
        tk.Label(bi_frame, text=bi_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Checkbox: tokensave init
        self._init_var = tk.BooleanVar(value=not has_db)
        init_text = "Run tokensave init"
        init_note = "  (already indexed)" if has_db else "  (builds the code graph — ~10–30s)"
        init_frame = tk.Frame(self, bg=C["base"])
        init_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(init_frame, text=init_text, variable=self._init_var).pack(side=tk.LEFT)
        tk.Label(init_frame, text=init_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Info note
        tk.Label(self,
                 text="Project appears in the list immediately while indexing runs in the background.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420).pack(padx=20, pady=(4, 8))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self._nuitka_var = tk.BooleanVar(value=False)
        nuitka_frame = tk.Frame(self, bg=C["base"])
        nuitka_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(nuitka_frame, text="Add Nuitka build files",
                        variable=self._nuitka_var).pack(side=tk.LEFT)
        tk.Label(nuitka_frame, text=nuitka_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        tk.Label(self,
                 text="Copies build.ps1 + build.bat from templates. Edit [ENTRY_SCRIPT] and\n"
                      "[OUTPUT_NAME] in build.ps1 before running your first build.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 8))

        # Checkbox: auto-commit Stop hook
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_exists = os.path.isfile(hook_settings)
        hook_note = "  (already present)" if hook_exists else "  (.claude/settings.json)"
        self._hook_var = tk.BooleanVar(value=False)
        hook_frame = tk.Frame(self, bg=C["base"])
        hook_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(hook_frame, text="Add auto-commit Stop hook",
                        variable=self._hook_var).pack(side=tk.LEFT)
        tk.Label(hook_frame, text=hook_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Label(self,
                 text="  Auto-commits when Claude finishes a session in this project.\n"
                      "  Safe: only commits if the working tree has changes.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 12))

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _apply(self):
        create_bi       = self._bi_var.get()
        run_init        = self._init_var.get()
        scaffold_nuitka = self._nuitka_var.get()
        add_git_hook    = self._hook_var.get()
        self.destroy()
        self.callback(self.path, create_bi=create_bi, run_init=run_init,
                      scaffold_nuitka=scaffold_nuitka, add_git_hook=add_git_hook)


# ── Settings dialog ────────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    """Edit manager-config.json through the GUI."""

    def __init__(self, parent, cfg: dict, save_fn, callback, startup_note=""):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._cfg = cfg
        self._save_fn = save_fn
        self._callback = callback

        pad = dict(padx=20, pady=4)

        if startup_note:
            tk.Label(self, text=startup_note,
                     bg=C["red"], fg=C["mantle"],
                     font=("Segoe UI", 9, "bold"),
                     justify=tk.LEFT, padx=14, pady=8,
                     wraplength=440).pack(fill=tk.X, pady=(0, 4))

        def field_row(label, key, is_file=False, is_dir=False, note=""):
            tk.Label(self, text=label, bg=C["base"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(10, 0))
            row = tk.Frame(self, bg=C["base"])
            row.pack(fill=tk.X, padx=20, pady=(2, 0))
            var = tk.StringVar(value=cfg.get(key, ""))
            entry = ttk.Entry(row, textvariable=var, width=52)
            entry.pack(side=tk.LEFT, padx=(0, 6))
            def browse(v=var, f=is_file, d=is_dir):
                if f:
                    p = filedialog.askopenfilename(
                        title=f"Select {label}", filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                        initialfile=v.get(), parent=self)
                elif d:
                    p = filedialog.askdirectory(title=f"Select {label}", parent=self)
                else:
                    return
                if p:
                    v.set(p)
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT)
            if note:
                tk.Label(row, text=note, bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 0))
            return var

        self._exe_var  = field_row("tokensave.exe  —  path to the tokensave binary",
                                   "tokensave_exe", is_file=True)
        self._tmpl_var = field_row("Template directory  —  folder containing claude-md-template.md and project-baseline.md",
                                   "template_dir", is_dir=True,
                                   note="(leave blank to auto-detect)")
        self._editor_var = field_row(
            "Editor command  —  launched by 'Open in Editor' (e.g. code, code --new-window, notepad)",
            "editor_cmd", note="(flags supported)")

        # ── Git executable ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(self, text="Git executable  —  path to git.exe",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(0, 0))
        git_row = tk.Frame(self, bg=C["base"])
        git_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._git_exe_var = tk.StringVar(value=cfg.get("git_exe", ""))
        git_entry = ttk.Entry(git_row, textvariable=self._git_exe_var, width=44)
        git_entry.pack(side=tk.LEFT, padx=(0, 6))
        def _browse_git():
            p = filedialog.askopenfilename(
                title="Select git.exe",
                filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                initialdir=r"C:\Program Files\Git\cmd",
                parent=self)
            if p:
                self._git_exe_var.set(p)
                self._verify_git(p)
        def _autodetect_git():
            found = _detect_git()
            self._git_exe_var.set(found)
            self._verify_git(found)
        ttk.Button(git_row, text="Browse…", command=_browse_git).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(git_row, text="Auto-detect", command=_autodetect_git).pack(side=tk.LEFT, padx=(0, 6))
        self._git_status_lbl = tk.Label(git_row, text="", bg=C["base"],
                                        font=("Segoe UI", 8), fg=C["overlay0"])
        self._git_status_lbl.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(self,
                 text="  Leave blank to auto-detect from PATH or common install locations.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        # Show current detected version on open
        self.after(100, lambda: self._verify_git(cfg.get("git_exe") or GIT_EXE))

        # ── GitHub CLI (gh) ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(self, text="GitHub CLI (gh)  —  enables 'Open PR on GitHub' and release creation",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        gh_row = tk.Frame(self, bg=C["base"])
        gh_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._gh_status_lbl = tk.Label(gh_row, text="Checking…",
                                       bg=C["base"], fg=C["overlay0"],
                                       font=("Segoe UI", 8))
        self._gh_status_lbl.pack(side=tk.LEFT, padx=(0, 12))

        def _check_gh_status():
            found = _detect_gh()
            if found:
                self._gh_status_lbl.config(text=f"✓  {found}", fg=C["green"])
                self._gh_install_btn.configure(state=tk.DISABLED)
            else:
                self._gh_status_lbl.config(text="✗  not installed", fg=C["red"])
                self._gh_install_btn.configure(state=tk.NORMAL)

        def _install_gh():
            self._gh_status_lbl.config(
                text="Installing…  (this may take a minute, a UAC prompt may appear)",
                fg=C["peach"])
            self._gh_install_btn.configure(state=tk.DISABLED)
            def worker():
                try:
                    result = subprocess.run(
                        ["winget", "install", "--id", "GitHub.cli",
                         "--silent", "--accept-package-agreements",
                         "--accept-source-agreements"],
                        capture_output=True, text=True, timeout=180,
                        creationflags=CREATE_NO_WINDOW)
                    if result.returncode == 0:
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text="✓  Installed!  Restart TokenSave Manager to use gh features.",
                            fg=C["green"]))
                    else:
                        err = (result.stdout + result.stderr).strip()[-120:]
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text=f"✗  Install failed (code {result.returncode}): {err}",
                            fg=C["red"]))
                        self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
                except Exception as ex:
                    self.after(0, lambda: self._gh_status_lbl.config(
                        text=f"✗  Error: {ex}", fg=C["red"]))
                    self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
            threading.Thread(target=worker, daemon=True).start()

        self._gh_install_btn = ttk.Button(gh_row, text="Install via winget",
                                          command=_install_gh)
        self._gh_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(gh_row, text="Check again",
                   command=_check_gh_status).pack(side=tk.LEFT)
        tk.Label(self,
                 text="  Once installed, use the Git tab's '🔗 Open PR' button to create pull requests on GitHub.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        self.after(150, _check_gh_status)

        # ── CodeGraph (alternative code-graph tool) ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._cg_section = tk.Frame(self, bg=C["base"])
        self._cg_section.pack(fill=tk.X)
        tk.Label(self._cg_section,
                 text="CodeGraph (codegraph)  —  optional alternative code-graph tool",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)

        cg_path_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_path_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._cg_exe_var = tk.StringVar(value=cfg.get("codegraph_exe", ""))
        self._cg_exe_entry = ttk.Entry(cg_path_row, textvariable=self._cg_exe_var,
                                        width=44)
        self._cg_exe_entry.pack(side=tk.LEFT, padx=(0, 6))

        def _browse_cg():
            p = filedialog.askopenfilename(
                title="Select codegraph executable",
                filetypes=[("Executable", "*.cmd;*.exe;*.bat"), ("All", "*.*")],
                initialdir=os.path.expandvars(r"%APPDATA%\npm"),
                parent=self)
            if p:
                self._cg_exe_var.set(p)
                self._verify_codegraph(p)

        def _autodetect_cg():
            found = _detect_codegraph()
            if found:
                self._cg_exe_var.set(found)
                self._verify_codegraph(found)
            else:
                self._cg_status_lbl.config(text="✗  not installed",
                                            fg=C["red"])

        ttk.Button(cg_path_row, text="Browse…",
                   command=_browse_cg).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_path_row, text="Auto-detect",
                   command=_autodetect_cg).pack(side=tk.LEFT, padx=(0, 6))

        cg_install_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_install_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        self._cg_status_lbl = tk.Label(cg_install_row, text="Checking…",
                                        bg=C["base"], fg=C["overlay0"],
                                        font=("Segoe UI", 8), justify=tk.LEFT,
                                        wraplength=420, anchor=tk.W)
        self._cg_status_lbl.pack(side=tk.LEFT, padx=(0, 12), fill=tk.X, expand=True)

        cg_btn_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_btn_row.pack(fill=tk.X, padx=20, pady=(4, 0))

        npm_path = _detect_npm()

        def _check_cg_status():
            found = _detect_codegraph()
            if found:
                self._cg_status_lbl.config(text=f"✓  {found}", fg=C["green"])
                self._cg_install_btn.configure(state=tk.DISABLED)
                if not self._cg_exe_var.get():
                    self._cg_exe_var.set(found)
            else:
                self._cg_status_lbl.config(text="✗  not installed",
                                            fg=C["red"])
                # Only re-enable Install if npm is actually available
                if _detect_npm():
                    self._cg_install_btn.configure(state=tk.NORMAL)
                else:
                    self._cg_install_btn.configure(state=tk.DISABLED)

        def _cg_finish_install(ok: bool, msg: str):
            """Main-thread callback after the install worker finishes."""
            if ok:
                # Re-detect so the resolved path shows up immediately
                path = _detect_codegraph()
                if path:
                    self._cg_exe_var.set(path)
                    self._cg_status_lbl.config(
                        text=f"✓  Installed — {path}", fg=C["green"])
                else:
                    self._cg_status_lbl.config(
                        text="✓  Installed.  Click 'Check again' to confirm.",
                        fg=C["green"])
                self._cg_install_btn.configure(state=tk.NORMAL)
            else:
                self._cg_status_lbl.config(text=msg, fg=C["red"])
                self._cg_install_btn.configure(state=tk.NORMAL)
                # Multi-line failures also pop up in a messagebox so the
                # error isn't lost when the user clicks elsewhere
                if "\n" in msg:
                    messagebox.showerror("CodeGraph install failed",
                                          msg, parent=self)

        def _install_cg():
            npm = _detect_npm()
            if not npm:
                self._cg_status_lbl.config(
                    text="✗  npm not found — install Node.js 18+ first "
                         "(https://nodejs.org)",
                    fg=C["red"])
                return
            self._cg_install_btn.configure(state=tk.DISABLED)
            self._cg_status_lbl.config(
                text="Installing…  (this may take a couple of minutes)",
                fg=C["yellow"])

            def worker():
                try:
                    result = subprocess.run(
                        [npm, "install", "-g", "@colbymchenry/codegraph"],
                        capture_output=True, text=True, timeout=300,
                        creationflags=CREATE_NO_WINDOW,
                        encoding="utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    self.after(0, lambda: _cg_finish_install(
                        ok=False, msg="Install timed out after 5 minutes."))
                    return
                except FileNotFoundError as e:
                    self.after(0, lambda: _cg_finish_install(
                        ok=False, msg=f"npm not found: {e}"))
                    return

                if result.returncode == 0:
                    self.after(0, lambda: _cg_finish_install(
                        ok=True, msg="✓ Installed successfully."))
                else:
                    err_text = (result.stderr or result.stdout or "").strip()
                    # EPERM / EACCES is the common Windows failure when Node
                    # was installed system-wide and the manager isn't elevated.
                    hint = ""
                    if "EPERM" in err_text or "EACCES" in err_text:
                        hint = ("\n\nThis usually happens when Node.js was "
                                "installed system-wide. Either run TokenSave "
                                "Manager as administrator OR reinstall "
                                "Node.js as a per-user install (the Node "
                                "installer offers this option).")
                    tail = "\n".join(err_text.splitlines()[-8:]) or "(no output)"
                    self.after(0, lambda: _cg_finish_install(
                        ok=False,
                        msg=f"✗  Install failed (exit {result.returncode}):\n\n{tail}{hint}"))

            threading.Thread(target=worker, daemon=True).start()

        self._cg_install_btn = ttk.Button(cg_btn_row, text="Install via npm",
                                           command=_install_cg)
        self._cg_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_btn_row, text="Check again",
                   command=_check_cg_status).pack(side=tk.LEFT, padx=(0, 6))

        if not npm_path:
            self._cg_install_btn.configure(state=tk.DISABLED)

        tk.Label(self._cg_section,
                 text="  npm install -g @colbymchenry/codegraph  —  requires Node.js 18+ on PATH.\n"
                      "  Per-project actions live in the right-click menu (🧠 CodeGraph …).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(4, 0))

        self.after(200, _check_cg_status)

        # ── Search roots — two-column Treeview (label + path) ──
        tk.Label(self,
                 text="Search roots  —  each root's label becomes a category in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))

        roots_frame = tk.Frame(self, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))

        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self._roots_tv = ttk.Treeview(
            tv_wrap,
            columns=("label", "path"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self._roots_tv.heading("label", text="Label")
        self._roots_tv.heading("path",  text="Path")
        self._roots_tv.column("label", width=130, stretch=False)
        self._roots_tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                   command=self._roots_tv.yview)
        self._roots_tv.configure(yscrollcommand=roots_vsb.set)
        self._roots_tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate from config (support legacy bare strings)
        for r in cfg.get("search_roots", []):
            lbl  = _root_label(r)
            path_val = _root_path(r)
            self._roots_tv.insert("", tk.END, values=(lbl, path_val))

        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",
                   command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label",
                   command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",
                   command=self._remove_root).pack(fill=tk.X)

        # ── Auto-commit toggle ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))

        self._var_autocommit = tk.BooleanVar(value=bool(cfg.get("auto_commit_after_sync", False)))
        tk.Checkbutton(self,
            text="Auto-commit after sync  (git add -A + git commit)",
            variable=self._var_autocommit,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(self,
            text="  Only fires when the project is a git repo and the working tree has changes.\n"
                 "  Commit message: \"chore: tokensave sync\"",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(8, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            f"Label for this category:\n(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self,
        )
        if lbl is None:
            return   # user cancelled
        self._roots_tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))

    def _edit_root_label(self):
        sel = self._roots_tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl  = self._roots_tv.set(iid, "label")
        new_lbl  = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self)
        if new_lbl is not None:
            self._roots_tv.set(iid, "label", new_lbl.strip() or cur_lbl)

    def _remove_root(self):
        sel = self._roots_tv.selection()
        if sel:
            self._roots_tv.delete(sel[0])

    def _verify_git(self, exe_path: str):
        """Run 'git --version' with the given path and update the status label."""
        if not exe_path:
            self._git_status_lbl.config(text="(will auto-detect on save)", fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW)
            version = result.stdout.strip() or result.stderr.strip()
            if result.returncode == 0:
                self._git_status_lbl.config(text=f"✓  {version}", fg=C["green"])
            else:
                self._git_status_lbl.config(text="✗  not found", fg=C["red"])
        except Exception:
            self._git_status_lbl.config(text="✗  not found", fg=C["red"])

    def _verify_codegraph(self, exe_path: str):
        """Run 'codegraph --version' with the given path; update status label."""
        if not exe_path:
            self._cg_status_lbl.config(text="(will auto-detect on save)",
                                        fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW)
            version = (result.stdout or result.stderr).strip()
            if result.returncode == 0:
                self._cg_status_lbl.config(text=f"✓  {version or 'OK'}",
                                            fg=C["green"])
                self._cg_install_btn.configure(state=tk.DISABLED)
            else:
                self._cg_status_lbl.config(text="✗  not found at that path",
                                            fg=C["red"])
        except Exception:
            self._cg_status_lbl.config(text="✗  not found at that path",
                                        fg=C["red"])

    def _scroll_to_codegraph(self):
        """Pull the CodeGraph section into view + focus its path entry.

        SettingsDialog is non-scrollable (resizable=False, no canvas wrapper),
        so all sections are always rendered. focus_set on the path entry is
        enough — no yview math needed. Wrapped in try/except so any future
        layout change that introduces scrolling fails gracefully rather than
        crashing the install-nudge flow.
        """
        try:
            self._cg_exe_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _save(self):
        exe = self._exe_var.get().strip()
        if exe and not os.path.isfile(exe):
            messagebox.showwarning("Not found",
                f"tokensave.exe not found at:\n{exe}", parent=self)
            return
        self._cfg["tokensave_exe"] = exe
        self._cfg["template_dir"]  = self._tmpl_var.get().strip()
        self._cfg["editor_cmd"]    = self._editor_var.get().strip() or "code"
        # python_exe is intentionally not exposed in the UI (used by the .bat
        # launcher only); preserve whatever value is already in the config.
        self._cfg["search_roots"] = [
            {"path": self._roots_tv.set(iid, "path"),
             "label": self._roots_tv.set(iid, "label")}
            for iid in self._roots_tv.get_children()
        ]
        self._cfg["auto_commit_after_sync"] = self._var_autocommit.get()
        self._cfg["git_exe"]       = self._git_exe_var.get().strip()
        self._cfg["codegraph_exe"] = self._cg_exe_var.get().strip()
        self._save_fn(self._cfg)
        self.destroy()
        self._callback()


# ── Snippet edit dialog ────────────────────────────────────────────────────────

class SnippetEditDialog(tk.Toplevel):
    """Add or edit a user-defined prompt snippet."""

    def __init__(self, parent, edit_meta, callback):
        """
        edit_meta: None for new snippet, or the _active_snippets_map entry for editing.
        callback(title, text, edit_meta): called on save; edit_meta is passed back.
        """
        super().__init__(parent)
        self.title("Add Snippet" if edit_meta is None else "Edit Snippet")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._callback = callback
        self._edit_meta = edit_meta

        pad = dict(padx=20, pady=4)

        tk.Label(self,
                 text="Add Snippet" if edit_meta is None else "Edit Snippet",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 8))

        # Title field
        tk.Label(self, text="Title", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._title_var = tk.StringVar(
            value=edit_meta["data"]["title"] if edit_meta else "")
        ttk.Entry(self, textvariable=self._title_var, width=52).pack(
            anchor=tk.W, padx=20, pady=(2, 6))

        # Body field
        tk.Label(self, text="Prompt text", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)

        body_wrap = tk.Frame(self, bg=C["mantle"])
        body_wrap.pack(fill=tk.X, padx=20, pady=(2, 12))
        vsb = ttk.Scrollbar(body_wrap, orient="vertical")
        self._body_txt = tk.Text(
            body_wrap, height=8, width=52,
            font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD,
            yscrollcommand=vsb.set,
        )
        vsb.configure(command=self._body_txt.yview)
        self._body_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        if edit_meta:
            self._body_txt.insert(tk.END, edit_meta["data"]["text"])

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        title = self._title_var.get().strip().replace("\n", " ")
        text  = self._body_txt.get("1.0", tk.END).strip()

        if not title:
            messagebox.showwarning("Empty title",
                "Please enter a title for this snippet.", parent=self)
            return
        if not text:
            messagebox.showwarning("Empty text",
                "Please enter the prompt text.", parent=self)
            return

        self.destroy()
        self._callback(title, text, self._edit_meta)


# ── Shadow Links dialog ────────────────────────────────────────────────────────

class ShadowLinksDialog(tk.Toplevel):
    """
    Configure and run shadow extension link generation for a project.
    Lets the user review/edit the extension map before applying.
    """

    def __init__(self, parent, path, callback):
        """
        callback(path, ext_map, run_sync): called on Apply.
        ext_map: dict mapping source extension → shadow suffix (e.g. {'.zsc': '.cpp'})
        """
        super().__init__(parent)
        self.title("Shadow Extension Links")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path = path
        self._callback = callback

        pad = dict(padx=20, pady=6)

        tk.Label(self,
                 text="🔗  Shadow Extension Links",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))

        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        tk.Label(self,
            text="Creates NTFS hardlinks with an appended extension so tokensave's\n"
                 "tree-sitter parsers can index non-standard file types. Hardlinks\n"
                 "cost zero extra disk space and update instantly with the source.",
            font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── Extension map editor ──
        tk.Label(self,
                 text="Mapping  (one per line:  .ext = .suffix  or  FILENAME = .suffix)",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        map_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        map_frame.pack(fill=tk.X, padx=20, pady=(0, 4))
        self._map_text = tk.Text(map_frame, height=6, width=36,
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, font=("Consolas", 10),
                                  padx=8, pady=6)
        self._map_text.pack(fill=tk.X)

        # Populate with DEFAULT_SHADOW_EXT_MAP
        for src_ext, tgt_suf in DEFAULT_SHADOW_EXT_MAP.items():
            self._map_text.insert(tk.END, f"{src_ext} = {tgt_suf}\n")

        tk.Label(self,
            text="  .ext = .suffix  →  extension match  (e.g. .txt = .cpp for HyperV files)\n"
                 "  NAME = .suffix  →  exact filename, case-insensitive  (e.g. DECORATE = .cpp)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        # ── Status summary ──
        existing = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        status_col = C["green"] if existing else C["overlay0"]
        status_txt = (f"✔  {existing} shadow file(s) already exist in this project."
                      if existing else "No shadow files found — none created yet.")
        tk.Label(self, text=status_txt,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=status_col).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))

        # ── Options ──
        self._var_sync = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Run tokensave sync after generating links",
                       variable=self._var_sync,
                       bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
                       activebackground=C["base"], activeforeground=C["text"],
                       font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(8, 16))

        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _parse_ext_map(self) -> dict:
        """Parse the text widget content into an ext_map dict.

        Two valid line formats:
          .ext = .suffix   → extension-based match (dot-prefixed key)
          NAME = .suffix   → exact filename match, case-insensitive (e.g. DECORATE)
        Lines starting with '#' and blank lines are ignored.
        """
        ext_map = {}
        for line in self._map_text.get("1.0", tk.END).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            src, _, tgt = line.partition("=")
            src = src.strip()
            tgt = tgt.strip()
            # tgt must be a dot-suffix; src can be a dot-extension OR a bare filename
            if tgt.startswith(".") and src:
                ext_map[src] = tgt
        return ext_map

    def _apply(self):
        ext_map = self._parse_ext_map()
        if not ext_map:
            messagebox.showwarning("No mappings",
                "Please define at least one extension mapping.", parent=self)
            return
        run_sync = self._var_sync.get()
        self.destroy()
        self._callback(self._path, ext_map, run_sync)


# ── Git Commit dialog ──────────────────────────────────────────────────────────

# ── Git helper dialogs ────────────────────────────────────────────────────────

class SetRemoteDialog(tk.Toplevel):
    """Connect a project to a GitHub repository by entering its HTTPS URL.

    Guides beginners through the three-step process: create repo on GitHub,
    copy the URL, paste here.
    Callback: callback(path, url)
    """

    def __init__(self, parent, path: str, current_url: str, callback):
        super().__init__(parent)
        self.title("Set Remote")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🔗  Connect to GitHub",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # Instructions
        instr = (
            "Steps:\n"
            "  1.  Go to  github.com/new  and create a new repository\n"
            "  2.  Copy the HTTPS URL from the repository page\n"
            "        (looks like: https://github.com/you/repo-name.git)\n"
            "  3.  Paste it in the box below and click Save"
        )
        tk.Label(self, text=instr,
                 font=("Segoe UI", 9), bg=C["base"], fg=C["text"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Remote URL:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._url_var = tk.StringVar(value=current_url)
        ttk.Entry(self, textvariable=self._url_var,
                  width=52).pack(anchor=tk.W, padx=20, pady=(4, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _save(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("URL required",
                "Please enter the GitHub repository URL.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@\n\n"
                "Example:  https://github.com/username/repo.git",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, url)


class NewBranchDialog(tk.Toplevel):
    """Create a new git branch, with an option to switch to it immediately.

    Callback: callback(path, branch_name, switch_immediately)
    """

    def __init__(self, parent, path: str, callback):
        super().__init__(parent)
        self.title("New Branch")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🌿  New Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["green"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Branch name:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._name_var = tk.StringVar()
        name_entry = ttk.Entry(self, textvariable=self._name_var, width=38)
        name_entry.pack(anchor=tk.W, padx=20, pady=(4, 10))
        name_entry.focus_set()

        self._switch_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Switch to this branch immediately",
            variable=self._switch_var,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Create", style="Primary.TButton",
                   command=self._create).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.bind("<Return>", lambda _: self._create())

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _create(self):
        name = self._name_var.get().strip()
        if not name:
            messagebox.showwarning("Name required",
                "Enter a branch name.", parent=self)
            return
        if " " in name:
            messagebox.showwarning("Invalid name",
                "Branch names cannot contain spaces.\n"
                "Try using a hyphen instead, e.g. my-feature",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, name, self._switch_var.get())


class SwitchBranchDialog(tk.Toplevel):
    """Select a branch to switch to from a list of local branches.

    Also used by cmd_git_delete_branch via the static pick() helper.
    Callback: callback(path, branch_name)
    """

    def __init__(self, parent, path: str, branches: list, current: str,
                 callback):
        super().__init__(parent)
        self.title("Switch Branch")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback
        self._result   = None

        tk.Label(self, text="🔀  Switch Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["lavender"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        if current:
            tk.Label(self, text=f"Current: {current}",
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        lb_wrap = tk.Frame(self, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        self._lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                               bg=C["mantle"], fg=C["text"],
                               selectbackground=C["surface1"],
                               activestyle="none",
                               relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            self._lb.insert(tk.END, f"  {b}")
        self._lb.pack(padx=6, pady=6)
        self._lb.bind("<Double-Button-1>", lambda _: self._switch())

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Switch", style="Primary.TButton",
                   command=self._switch).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _switch(self):
        sel = self._lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected",
                "Select a branch first.", parent=self)
            return
        name = self._lb.get(sel[0]).strip()
        self.destroy()
        if self._callback:
            self._callback(self._path, name)

    @staticmethod
    def pick(parent, title: str, branches: list, parent_widget=None) -> str:
        """Synchronous branch picker — returns chosen branch name or ''."""
        result = [""]
        pw = parent_widget or parent

        def cb(path, name):
            result[0] = name

        dlg = SwitchBranchDialog.__new__(SwitchBranchDialog)
        tk.Toplevel.__init__(dlg, parent)
        dlg.title(title)
        dlg.configure(bg=C["base"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(parent)
        dlg._path     = ""
        dlg._callback = None
        dlg._result   = result

        tk.Label(dlg, text=f"Select a branch:",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(16, 8))
        lb_wrap = tk.Frame(dlg, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                        bg=C["mantle"], fg=C["text"],
                        selectbackground=C["surface1"],
                        activestyle="none",
                        relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            lb.insert(tk.END, f"  {b}")
        lb.pack(padx=6, pady=6)

        def confirm():
            sel = lb.curselection()
            if sel:
                result[0] = lb.get(sel[0]).strip()
            dlg.destroy()

        lb.bind("<Double-Button-1>", lambda _: confirm())
        btn_row = tk.Frame(dlg, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text=title.split()[0], style="Primary.TButton",
                   command=confirm).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px, py = pw.winfo_x(), pw.winfo_y()
        pw2, ph = pw.winfo_width(), pw.winfo_height()
        dlg.geometry(f"{w}x{h}+{px + (pw2 - w) // 2}+{py + (ph - h) // 2}")

        parent.wait_window(dlg)
        return result[0]


# ── Assign Category dialog ────────────────────────────────────────────────────

class AssignCategoryDialog(tk.Toplevel):
    """Assign or override the category (and optional sub-category) for a project.

    Categories are sourced from search-root labels and existing overrides.
    Both comboboxes are editable so the user can type a new category/sub-category
    without any prior setup.

    Callback signature: callback(path, cat_or_None, subcat_str)
    Passing cat=None means "clear override" (restore root default).
    """

    def __init__(self, parent, path: str,
                 all_cats: list, subs_by_cat: dict,
                 current: dict, callback):
        super().__init__(parent)
        self.title("Assign Category")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._subs_map = subs_by_cat
        self._callback = callback

        pad = dict(padx=20, pady=4)

        # ── Title ──
        tk.Label(self, text="📁  Assign Category",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Category ──
        tk.Label(self, text="Category:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._cat_var = tk.StringVar(value=current.get("category", ""))
        self._cat_cb  = ttk.Combobox(self, textvariable=self._cat_var,
                                     values=all_cats, width=36)
        self._cat_cb.pack(anchor=tk.W, padx=20, pady=(0, 8))
        self._cat_cb.bind("<<ComboboxSelected>>", self._on_cat_changed)
        self._cat_var.trace_add("write", lambda *_: self._on_cat_changed())

        # ── Sub-category ──
        tk.Label(self, text="Sub-category:  (optional)",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._sub_var = tk.StringVar(value=current.get("subcategory", ""))
        self._sub_cb  = ttk.Combobox(self, textvariable=self._sub_var,
                                     values=subs_by_cat.get(self._cat_var.get(), []),
                                     width=36)
        self._sub_cb.pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Clear Override",
                   command=self._clear).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="OK", style="Primary.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _on_cat_changed(self, *_):
        cat  = self._cat_var.get()
        subs = self._subs_map.get(cat, [])
        self._sub_cb.configure(values=subs)

    def _ok(self):
        cat    = self._cat_var.get().strip()
        subcat = self._sub_var.get().strip()
        if not cat:
            messagebox.showwarning("Category required",
                "Enter a category name, or click 'Clear Override' to restore the default.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, cat, subcat)

    def _clear(self):
        self.destroy()
        self._callback(self._path, None, "")


class GitHubSetupDialog(tk.Toplevel):
    """Step-by-step GitHub setup wizard.

    Walks first-time users through: git identity → GitHub account →
    create repo → set remote URL → first push → optional GitHub Release.
    Each step shows a live status indicator (✅ / ⬜ / ℹ️) based on the
    current git state of the project.
    """

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self._app  = parent   # App instance — gives access to _shell_capture, _log, etc.
        self._path = path
        self.title("GitHub Setup")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(480, 500)
        self.grab_set()
        self.transient(parent)

        self._name_var      = tk.StringVar()
        self._email_var     = tk.StringVar()
        self._remote_var    = tk.StringVar()
        self._tag_var       = tk.StringVar(value="v1.0.0")
        self._rel_title_var = tk.StringVar(value="Release")

        # Scrollable area: canvas + scrollbar wrap the body Frame.
        # body is a child of self (not canvas) — keeps Windows rendering happy.
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        _vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = tk.Frame(self, bg=C["base"])
        self._body_id = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._body_id, width=e.width))
        self._body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        try:
            self._build()
            self._refresh()
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            messagebox.showerror(
                "GitHub Setup — build error",
                f"The wizard failed to render:\n\n{ex}\n\n{tb[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent window.
        content_h = self._body.winfo_reqheight() + 20
        max_h = max(400, parent.winfo_height() - 60)
        w, h = 520, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── shell helper (fast config/remote queries — main-thread OK) ───────────

    def _sh(self, cmd) -> tuple:
        return self._app._shell_capture(cmd, self._path)

    # ── build ────────────────────────────────────────────────────────────────

    def _build(self):
        body = self._body   # all widgets pack into the scrollable canvas child frame
        P    = dict(padx=20)

        # ── Header ───────────────────────────────────────────────────────────
        tk.Label(body, text="🐙  GitHub Setup",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 2), **P)
        tk.Label(body, text=os.path.basename(self._path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6), **P)
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Step 1: Git identity ─────────────────────────────────────────────
        self._s1_icon = self._step_header(body, "1",
            "Your name & email  (shown on every commit)")

        id_frame = tk.Frame(body, bg=C["surface0"], padx=10, pady=8)
        id_frame.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))

        for lbl_text, var in (("Name:", self._name_var), ("Email:", self._email_var)):
            row = tk.Frame(id_frame, bg=C["surface0"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=lbl_text, width=7, anchor=tk.W,
                     bg=C["surface0"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=30,
                      font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(id_frame, text="Save Identity",
                   command=self._save_identity).pack(anchor=tk.W, pady=(6, 0))

        # ── Step 2: Sign in / create GitHub account ──────────────────────────
        self._s2_icon = self._step_header(body, "2",
            "Sign in to GitHub  (or create a free account)")
        s2 = tk.Frame(body, bg=C["base"])
        s2.pack(anchor=tk.W, padx=(44, 20), pady=(2, 4))
        ttk.Button(s2, text="Sign in to GitHub →",
                   command=lambda: os.startfile("https://github.com/login")).pack(
                   side=tk.LEFT, padx=(0, 8))
        ttk.Button(s2, text="Create free account →",
                   command=lambda: os.startfile("https://github.com/signup")).pack(side=tk.LEFT)
        tk.Label(body,
                 text="If you already have an account, just sign in — no need to create one.",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=(44, 20), pady=(0, 10))

        # ── Step 3: Create repository ────────────────────────────────────────
        self._s3_icon = self._step_header(body, "3",
            "Create a new repository on GitHub")
        s3 = tk.Frame(body, bg=C["base"])
        s3.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s3,
                 text="Go to github.com/new, fill in the repo name, leave it Public.\n"
                      "Do NOT check 'Add README' or 'Add .gitignore' — you already\n"
                      "have those. Then copy the HTTPS URL it shows you.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        ttk.Button(s3, text="Open github.com/new →",
                   command=lambda: os.startfile("https://github.com/new")).pack(anchor=tk.W)

        # ── Step 4: Set remote URL ───────────────────────────────────────────
        self._s4_icon = self._step_header(body, "4",
            "Paste your repository URL here")
        s4 = tk.Frame(body, bg=C["base"])
        s4.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        url_row = tk.Frame(s4, bg=C["base"])
        url_row.pack(fill=tk.X)
        ttk.Entry(url_row, textvariable=self._remote_var, width=34,
                  font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Set", command=self._set_remote).pack(side=tk.LEFT)
        tk.Label(s4,
                 text="e.g. https://github.com/you/my-project.git",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(4, 0))

        # ── Step 5: Push ─────────────────────────────────────────────────────
        self._s5_icon = self._step_header(body, "5",
            "Upload your code to GitHub")
        s5 = tk.Frame(body, bg=C["base"])
        s5.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s5,
                 text="This sends all your commits to GitHub. The first time, a\n"
                      "browser window will open asking you to log in — that's normal.\n"
                      "After that, pushes happen silently.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        self._push_btn = ttk.Button(s5, text="⬆  Push to GitHub",
                                    command=self._do_push)
        self._push_btn.pack(anchor=tk.W)

        # ── Releases section ─────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
        tk.Label(body, text="📦  GitHub Releases  (share your built .exe)",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["peach"]).pack(anchor=tk.W, padx=20, pady=(0, 4))
        tk.Label(body,
                 text="A Release lets anyone download your .exe without needing Python.\n"
                      "Build dist\\ first (run build.bat), then tag a release here.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        gh_on_path = bool(shutil.which("gh"))
        if gh_on_path:
            rel_grid = tk.Frame(body, bg=C["base"])
            rel_grid.pack(anchor=tk.W, padx=20, pady=(0, 6))
            for col, (lbl, var, w) in enumerate([
                    ("Tag:", self._tag_var, 9),
                    ("Title:", self._rel_title_var, 22)]):
                tk.Label(rel_grid, text=lbl, bg=C["base"], fg=C["text"],
                         font=("Segoe UI", 9)).grid(
                         row=0, column=col*2, sticky=tk.W,
                         padx=(0 if col == 0 else 12, 4))
                ttk.Entry(rel_grid, textvariable=var, width=w,
                          font=("Segoe UI", 9)).grid(row=0, column=col*2+1, sticky=tk.W)
            ttk.Button(body, text="📦  Create Release",
                       command=self._create_release).pack(anchor=tk.W, padx=20, pady=(0, 4))
        else:
            tk.Label(body,
                     text="Install GitHub CLI to enable one-click releases from here:",
                     font=("Segoe UI", 9), bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20)
            ttk.Button(body, text="Get GitHub CLI  (cli.github.com) →",
                       command=lambda: os.startfile("https://cli.github.com")).pack(
                       anchor=tk.W, padx=20, pady=(4, 4))
            tk.Label(body,
                     text="After installing, re-open this dialog to enable releases.",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
        ttk.Button(body, text="Close", command=self.destroy).pack(
            anchor=tk.E, padx=20, pady=(0, 16))

    def _step_header(self, parent, num: str, text: str) -> tk.Label:
        """Numbered step row — returns the icon label so caller can update it."""
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(0, 2))
        icon = tk.Label(row, text="⬜", bg=C["base"], font=("Segoe UI", 10))
        icon.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=f"Step {num} — {text}",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        return icon

    # ── Refresh: query git and update step icons ─────────────────────────────

    def _refresh(self):
        # Step 1 — git identity
        name_out,  _ = self._sh([GIT_EXE,"config", "--global", "user.name"])
        email_out, _ = self._sh([GIT_EXE,"config", "--global", "user.email"])
        name  = name_out.strip()
        email = email_out.strip()
        if not self._name_var.get():
            self._name_var.set(name)
        if not self._email_var.get():
            self._email_var.set(email)
        self._s1_icon.config(text="✅" if (name and email) else "⚠️")

        # Steps 2 & 3 — can't detect automatically; show info marker
        self._s2_icon.config(text="ℹ️")
        self._s3_icon.config(text="ℹ️")

        # Step 4 — remote
        remote_out, rrc = self._sh(
            [GIT_EXE,"-C", self._path, "remote", "get-url", "origin"])
        remote = remote_out.strip() if rrc == 0 else ""
        self._remote_var.set(remote)
        self._s4_icon.config(text="✅" if remote else "⬜")

        # Step 5 — push (only enabled when remote exists)
        self._s5_icon.config(text="⬜")
        self._push_btn.config(state=tk.NORMAL if remote else tk.DISABLED)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _save_identity(self):
        name  = self._name_var.get().strip()
        email = self._email_var.get().strip()
        if not name or not email:
            messagebox.showwarning("Incomplete",
                "Please enter both a name and an email address.", parent=self)
            return
        self._sh([GIT_EXE,"config", "--global", "user.name",  name])
        self._sh([GIT_EXE,"config", "--global", "user.email", email])
        self._refresh()
        messagebox.showinfo("Identity saved",
            f"Git will now sign commits as:\n{name} <{email}>", parent=self)

    def _set_remote(self):
        url = self._remote_var.get().strip()
        if not url:
            messagebox.showwarning("No URL",
                "Paste the HTTPS URL from your new GitHub repository.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@", parent=self)
            return
        _, rrc = self._sh([GIT_EXE,"-C", self._path, "remote", "get-url", "origin"])
        if rrc == 0:
            self._sh([GIT_EXE,"-C", self._path, "remote", "set-url", "origin", url])
            self._app._log(f"  Remote updated: {url}", C["green"])
        else:
            self._sh([GIT_EXE,"-C", self._path, "remote", "add", "origin", url])
            self._app._log(f"  Remote added: {url}", C["green"])
        self._refresh()
        self._app.after(0, self._app._git_refresh)

    def _do_push(self):
        self.destroy()
        self._app._git_path = self._path
        self._app.cmd_git_push()

    def _create_release(self):
        tag   = self._tag_var.get().strip()
        title = self._rel_title_var.get().strip() or tag
        if not tag:
            messagebox.showwarning("No tag",
                "Enter a version tag, e.g. v1.0.0", parent=self)
            return
        # Collect .exe files from dist\
        dist_dir  = os.path.join(self._path, "dist")
        exe_files = []
        if os.path.isdir(dist_dir):
            exe_files = [os.path.join(dist_dir, f)
                         for f in os.listdir(dist_dir) if f.endswith(".exe")]
        if not exe_files:
            if not messagebox.askyesno(
                    "No .exe files found",
                    "No .exe files found in dist\\\n\n"
                    "Run build.bat first to compile them.\n\n"
                    "Create a release without uploading any files anyway?",
                    parent=self):
                return
        cmd = ["gh", "release", "create", tag,
               "--title", title, "--generate-notes"] + exe_files
        self.destroy()
        self._app._log(f"Creating GitHub release {tag}…", C["peach"])
        def worker():
            out, rc = self._app._shell_capture(cmd, self._path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-6:]:
                self._app._log(f"  {line}", col)
            if rc == 0:
                self._app._log(f"  ✓ Release {tag} created — check GitHub!", C["green"])
        threading.Thread(target=worker, daemon=True).start()


class UntrackIgnoredDialog(tk.Toplevel):
    """Checklist dialog for untracking files that are tracked-but-ignored.

    Shows every file returned by `git ls-files -ci --exclude-standard`
    (i.e. files in git's index whose path matches the project's .gitignore)
    with a checkbox. Untrack Selected runs `git rm --cached -- <files>`
    which removes them from the index without touching the working tree,
    so the local copies stay where they are.

    Triggered:
      - manually via right-click → 🧹 Untrack Ignored Files…
      - automatically via GitignoreDialog._on_save when new ignore rules
        match files that were already tracked

    On success, calls _offer_commit_after_change on the parent App so the
    user can immediately commit the untracking as one atomic change.
    """

    def __init__(self, parent, path: str, files: list, *,
                 reason: str = "tracked but listed in .gitignore"):
        super().__init__(parent)
        self._app   = parent
        self._path  = path
        self._files = files
        name = os.path.basename(path)
        self.title(f"Untrack Ignored Files — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 380)
        self.grab_set()
        self.transient(parent)

        # ── Header ──
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🧹  Untrack Ignored Files", bg=C["base"],
                 fg=C["blue"], font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=name, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

        # ── Explanation ──
        expl = tk.Frame(self, bg=C["base"])
        expl.pack(fill=tk.X, padx=18, pady=(0, 6))
        tk.Label(expl,
                 text=(
                   f"The following files are {reason}.\n\n"
                   "Untracking removes them from git's index (i.e. git stops "
                   "treating them as part of the project) but leaves the "
                   "files on your disk. Future modifications won't appear "
                   "in git status — which is what your .gitignore intends.\n\n"
                   "This is the standard fix for the 'I added a path to "
                   ".gitignore but git keeps showing it as modified' problem."
                 ),
                 bg=C["base"], fg=C["text"], font=("Segoe UI", 9),
                 justify=tk.LEFT, anchor=tk.W,
                 wraplength=500).pack(anchor=tk.W)

        # ── File checklist (scrollable) ──
        tk.Label(self, text="FILES TO UNTRACK",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=18, pady=(8, 2))
        list_outer = tk.Frame(self, bg=C["mantle"])
        list_outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=180)
        _vsb = ttk.Scrollbar(list_outer, orient="vertical",
                              command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        self._file_vars: list = []
        if not files:
            tk.Label(self._list_body,
                     text="  (no tracked-but-ignored files found)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 9, "italic"),
                     padx=10, pady=10).pack(anchor=tk.W)
        else:
            for fname in files:
                row = tk.Frame(self._list_body, bg=C["mantle"])
                row.pack(fill=tk.X, padx=4, pady=1)
                var = tk.BooleanVar(value=True)
                self._file_vars.append((var, fname))
                cb = tk.Checkbutton(row, variable=var, bg=C["mantle"],
                                     activebackground=C["mantle"],
                                     selectcolor=C["surface0"])
                cb.pack(side=tk.LEFT)
                tk.Label(row, text=fname, anchor=tk.W,
                         font=("Consolas", 9),
                         bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(4, 6))

        # ── Quick-select buttons ──
        if files:
            sel_row = tk.Frame(self, bg=C["base"])
            sel_row.pack(fill=tk.X, padx=18, pady=(0, 8))
            ttk.Button(sel_row, text="Select All",
                       command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Select None",
                       command=lambda: self._set_all(False)).pack(side=tk.LEFT)

        # ── Action buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._action_btn = ttk.Button(btn_row, text="Untrack Selected",
                                       command=self._apply,
                                       state=tk.NORMAL if files else tk.DISABLED)
        self._action_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Centre
        self.update_idletasks()
        w, h = 580, 520
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    def _set_all(self, value: bool):
        for var, _f in self._file_vars:
            var.set(value)

    def _apply(self):
        selected = [fname for var, fname in self._file_vars if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to untrack.", parent=self)
            return
        path = self._path
        self.destroy()
        self._app._do_untrack_ignored(path, selected)


class GitignoreDialog(tk.Toplevel):
    """View and edit a project's .gitignore through a structured dialog.

    Layout:
      1. Header: project name + file path + entry count
      2. Scrollable "Current entries" frame — one widget row per non-blank line
         of .gitignore; each pattern row has a × button to mark for removal
         (rendered with strikethrough font); comment rows are visible but
         require a confirm dialog before removal; blank lines are tracked
         by index for layout preservation but not displayed
      3. "Inject template patterns" row — push buttons (not checkboxes; see
         the Gemini critique note in the plan file). One click adds that
         category's missing patterns to the pending additions
      4. Custom entry field + Add button
      5. Pending changes panel (Text widget, read-only) showing + / − diff
      6. Save / Cancel buttons. Save calls _write_gitignore_lines (atomic) and
         then triggers _offer_commit_after_change on the parent App.
    """

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self._app  = parent
        self._path = path
        name = os.path.basename(path)
        self.title(f"Manage .gitignore — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 520)
        self.grab_set()
        self.transient(parent)

        # ── State ──────────────────────────────────────────────────────────
        self._original_lines: list  = _read_gitignore_lines(path)
        self._removed_indices: set  = set()
        self._additions:      list  = []
        self._row_widgets:    dict  = {}   # idx -> {label, btn, frame}

        self._normal_font = tkfont.Font(family="Consolas", size=9)
        self._strike_font = tkfont.Font(family="Consolas", size=9, overstrike=1)

        # ── Header ─────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📋  .gitignore", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        gi_path = os.path.join(path, ".gitignore")
        sub = tk.Frame(hdr, bg=C["base"])
        sub.pack(fill=tk.X, pady=(4, 0))
        tk.Label(sub, text=gi_path, bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        functional_count = sum(
            1 for ln in self._original_lines
            if ln.strip() and not ln.strip().startswith("#"))
        self._count_lbl = tk.Label(sub,
            text=f"  ({functional_count} pattern"
                 f"{'s' if functional_count != 1 else ''})",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8))
        self._count_lbl.pack(side=tk.LEFT)

        # ── Current entries: scrollable canvas + body Frame ────────────────
        cur_label = tk.Label(self, text="CURRENT ENTRIES",
                             bg=C["base"], fg=C["overlay0"],
                             font=("Segoe UI", 8, "bold"))
        cur_label.pack(anchor=tk.W, padx=18, pady=(0, 4))
        cur_wrap = tk.Frame(self, bg=C["mantle"])
        cur_wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))

        self._cur_canvas = tk.Canvas(cur_wrap, bg=C["mantle"],
                                     highlightthickness=0, height=180)
        cur_vsb = ttk.Scrollbar(cur_wrap, orient="vertical",
                                command=self._cur_canvas.yview)
        self._cur_canvas.configure(yscrollcommand=cur_vsb.set)
        cur_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cur_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._cur_body = tk.Frame(self._cur_canvas, bg=C["mantle"])
        self._cur_body_id = self._cur_canvas.create_window(
            (0, 0), window=self._cur_body, anchor="nw")
        self._cur_canvas.bind("<Configure>",
            lambda e: self._cur_canvas.itemconfigure(
                self._cur_body_id, width=e.width))
        self._cur_body.bind("<Configure>",
            lambda e: self._cur_canvas.configure(
                scrollregion=self._cur_canvas.bbox("all")))

        self._populate_current_entries()

        # ── Template Inject buttons (action, not state — see plan) ────────
        tmpl_label = tk.Label(self,
            text="INJECT TEMPLATE PATTERNS  (one-click — hover to see what each adds)",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8, "bold"))
        tmpl_label.pack(anchor=tk.W, padx=18, pady=(4, 4))
        tmpl_wrap = tk.Frame(self, bg=C["base"])
        tmpl_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))

        # Two-row grid of buttons; up to 6 per row
        per_row = 6
        for i, cat_name in enumerate(_GITIGNORE_TEMPLATES.keys()):
            row, col = divmod(i, per_row)
            btn = ttk.Button(tmpl_wrap, text=f"+ {cat_name}",
                             command=lambda n=cat_name: self._inject_template(n))
            btn.grid(row=row, column=col, padx=(0, 4), pady=(0, 4),
                     sticky=tk.W)
            _Tooltip(btn, self._template_tooltip_text(cat_name))

        # ── Custom entry ──────────────────────────────────────────────────
        custom_wrap = tk.Frame(self, bg=C["base"])
        custom_wrap.pack(fill=tk.X, padx=18, pady=(4, 0))
        tk.Label(custom_wrap, text="Custom entry:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._custom_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_wrap, textvariable=self._custom_var,
                                  font=("Consolas", 9), width=40)
        custom_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                          padx=(0, 6))
        custom_entry.bind("<Return>", lambda e: self._add_custom())
        ttk.Button(custom_wrap, text="+ Add",
                   command=self._add_custom).pack(side=tk.LEFT)

        self._custom_hint = tk.Label(self, text="", bg=C["base"],
                                     fg=C["overlay0"], font=("Segoe UI", 8))
        self._custom_hint.pack(anchor=tk.W, padx=18)

        # ── Pending changes panel ────────────────────────────────────────
        pend_label = tk.Label(self, text="PENDING CHANGES",
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8, "bold"))
        pend_label.pack(anchor=tk.W, padx=18, pady=(10, 2))
        pend_wrap = tk.Frame(self, bg=C["mantle"])
        pend_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))
        self._pend_txt = tk.Text(pend_wrap, height=4,
                                  font=("Consolas", 9),
                                  bg=C["mantle"], fg=C["text"],
                                  relief=tk.FLAT, padx=6, pady=4,
                                  wrap=tk.NONE, state=tk.DISABLED)
        self._pend_txt.pack(fill=tk.X, expand=True)
        self._pend_txt.tag_configure("add",  foreground=C["green"])
        self._pend_txt.tag_configure("rem",  foreground=C["red"])
        self._pend_txt.tag_configure("dim",  foreground=C["overlay0"])

        # ── Save / Cancel ────────────────────────────────────────────────
        btns = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        btns.pack(fill=tk.X)
        self._save_btn = ttk.Button(btns, text="Save changes",
                                     command=self._on_save)
        self._save_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

        self._update_pending_panel()

        # Centre on parent
        self.update_idletasks()
        w, h = 640, 620
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Row population ─────────────────────────────────────────────────────

    def _populate_current_entries(self):
        """Build a row widget for every non-blank line in _original_lines.

        Blank lines are tracked by index (in _original_lines) but not
        displayed — they get preserved on save by iterating original_lines
        on write and skipping anything in _removed_indices.
        """
        if not self._original_lines:
            empty_lbl = tk.Label(self._cur_body,
                text="(no .gitignore yet — inject a template or add a custom entry below)",
                bg=C["mantle"], fg=C["overlay0"],
                font=("Segoe UI", 9, "italic"), padx=8, pady=12)
            empty_lbl.pack(anchor=tk.W)
            return

        for idx, raw in enumerate(self._original_lines):
            stripped = raw.strip()
            if not stripped:
                continue   # blank, preserved by index but invisible
            row = tk.Frame(self._cur_body, bg=C["mantle"])
            row.pack(fill=tk.X, padx=4, pady=1)

            is_comment = stripped.startswith("#")
            pattern_text = raw  # show raw (keeps any leading indentation)

            lbl = tk.Label(row, text=pattern_text, bg=C["mantle"],
                           fg=(C["peach"] if is_comment else C["text"]),
                           font=self._normal_font, anchor=tk.W)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))

            if is_comment:
                marker = tk.Label(row, text="(comment)",
                                   bg=C["mantle"], fg=C["overlay0"],
                                   font=("Segoe UI", 8, "italic"))
                marker.pack(side=tk.LEFT, padx=(0, 4))

            btn = tk.Label(row, text="×", bg=C["mantle"],
                            fg=C["red"], font=("Segoe UI", 11, "bold"),
                            cursor="hand2", padx=8)
            btn.pack(side=tk.RIGHT)
            btn.bind("<Button-1>",
                lambda _e, i=idx: self._toggle_removal(i))

            self._row_widgets[idx] = {
                "frame":      row,
                "label":      lbl,
                "btn":        btn,
                "is_comment": is_comment,
            }

    # ── Removal toggle ─────────────────────────────────────────────────────

    def _toggle_removal(self, idx: int):
        """Mark or un-mark a row for removal. Confirms before removing comments."""
        widgets = self._row_widgets.get(idx)
        if not widgets:
            return
        if idx in self._removed_indices:
            # Undo removal
            self._removed_indices.discard(idx)
            widgets["label"].configure(font=self._normal_font,
                fg=(C["peach"] if widgets["is_comment"] else C["text"]))
            widgets["btn"].configure(text="×", fg=C["red"])
        else:
            # Adding removal — confirm if it's a comment
            if widgets["is_comment"]:
                ok = messagebox.askyesno(
                    "Remove comment line?",
                    f"Remove this comment line?\n\n  {self._original_lines[idx]}",
                    parent=self)
                if not ok:
                    return
            self._removed_indices.add(idx)
            widgets["label"].configure(font=self._strike_font,
                fg=C["overlay0"])
            widgets["btn"].configure(text="↺", fg=C["green"])
        self._update_pending_panel()

    # ── Template injection (smart-clear conflict aware) ───────────────────

    def _current_pattern_state(self) -> set:
        """Return the set of patterns currently in 'final state' = original
        minus pending-removed plus pending-added. Used to decide what a
        template injection actually needs to add."""
        in_state = set()
        for idx, raw in enumerate(self._original_lines):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if idx in self._removed_indices:
                continue
            in_state.add(s)
        for a in self._additions:
            in_state.add(a.strip())
        return in_state

    def _inject_template(self, cat_name: str):
        """Apply a category's patterns. Smart-resolves conflicts with
        pending removals: if the category contains a pattern currently
        marked for removal, un-remove it instead of appending a duplicate."""
        patterns = _GITIGNORE_TEMPLATES.get(cat_name, [])
        if not patterns:
            return
        already = self._current_pattern_state()
        added_any = False
        # First pass: un-remove any pattern that's currently in _removed_indices
        # AND the category wants it (revert the removal instead of duplicating)
        for idx, raw in enumerate(self._original_lines):
            if idx not in self._removed_indices:
                continue
            s = raw.strip()
            if s in patterns:
                # Revert this row's removal
                self._toggle_removal(idx)   # already updates pending panel
                already.add(s)
                added_any = True
        # Second pass: append patterns that genuinely aren't present
        for p in patterns:
            if p not in already:
                self._additions.append(p)
                already.add(p)
                added_any = True
        if added_any:
            self._update_pending_panel()
        # Don't bother flashing on no-op clicks

    def _template_tooltip_text(self, cat_name: str) -> str:
        """Tooltip text listing the patterns this category contributes."""
        pats = _GITIGNORE_TEMPLATES.get(cat_name, [])
        return f"Click to add to .gitignore:\n" + "\n".join(f"  {p}" for p in pats)

    # ── Custom entry ─────────────────────────────────────────────────────

    def _add_custom(self):
        text = self._custom_var.get().strip()
        if not text:
            return
        # Dedup
        if text in self._current_pattern_state():
            self._custom_hint.configure(text="(already present)", fg=C["overlay0"])
            self.after(2000,
                lambda: self._custom_hint.configure(text=""))
            return
        # Suspicious-looking pattern? Confirm before adding.
        if " " in text or text.startswith("/"):
            # Note: leading slash is actually valid gitignore (anchors to root),
            # but mixed with whitespace it's almost always a typo
            if " " in text:
                if not messagebox.askyesno(
                    "Suspicious pattern",
                    f"This doesn't look like a typical gitignore pattern "
                    f"(contains whitespace):\n\n  {text}\n\nAdd anyway?",
                    parent=self):
                    return
        self._additions.append(text)
        self._custom_var.set("")
        self._custom_hint.configure(text="")
        self._update_pending_panel()

    # ── Pending changes panel ────────────────────────────────────────────

    def _update_pending_panel(self):
        """Refresh the diff display and the Save button's enabled state."""
        self._pend_txt.configure(state=tk.NORMAL)
        self._pend_txt.delete("1.0", tk.END)
        any_changes = False
        for idx in sorted(self._removed_indices):
            line = self._original_lines[idx].strip()
            self._pend_txt.insert(tk.END, f"− {line}\n", "rem")
            any_changes = True
        for line in self._additions:
            self._pend_txt.insert(tk.END, f"+ {line}\n", "add")
            any_changes = True
        if not any_changes:
            self._pend_txt.insert(tk.END,
                "No changes — inject a template, add a custom entry, "
                "or click × on a row to begin.", "dim")
            self._save_btn.configure(state=tk.DISABLED)
        else:
            self._save_btn.configure(state=tk.NORMAL)
        self._pend_txt.configure(state=tk.DISABLED)

    # ── Save ──────────────────────────────────────────────────────────────

    def _on_save(self):
        # Build the final line list: original minus removed, plus additions
        kept_lines = [ln for i, ln in enumerate(self._original_lines)
                      if i not in self._removed_indices]
        final_lines = list(kept_lines)
        if self._additions:
            # Add a blank separator + header before new entries (only if the
            # last existing line isn't already blank — avoids double-blank)
            if final_lines and final_lines[-1].strip() != "":
                final_lines.append("")
            final_lines.append("# Added by TokenSave Manager")
            final_lines.extend(self._additions)
        try:
            _write_gitignore_lines(self._path, final_lines)
        except OSError as e:
            messagebox.showerror("Save failed",
                f"Could not write .gitignore:\n\n{e}", parent=self)
            return
        # Log via the App's log panel
        added_n   = len(self._additions)
        removed_n = len(self._removed_indices)
        bits = []
        if added_n:   bits.append(f"+{added_n}")
        if removed_n: bits.append(f"-{removed_n}")
        change_str = "  ".join(bits) if bits else "(no diff)"
        self._app._log(
            f"  Saved .gitignore  ({change_str})", C["green"])
        path = self._path
        self.destroy()
        # After the .gitignore write, check whether the new rules now match
        # files that were ALREADY tracked. If so, offer to untrack them in
        # the same flow — otherwise the user hits the confusing 'git keeps
        # showing this as modified after I added it to gitignore' problem.
        # Only relevant when at least one addition was made AND this is a
        # local git repo; pure removals or non-git projects skip this.
        if added_n > 0 and _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path)
            if stale:
                ask = messagebox.askyesno(
                    "Untrack files that match your new rules?",
                    f"Your .gitignore now matches "
                    f"{len(stale)} file{'s' if len(stale) != 1 else ''} "
                    "that {} already tracked by git:\n\n".format(
                        "are" if len(stale) != 1 else "is")
                    + "\n".join(f"  • {f}" for f in stale[:10])
                    + ("\n  ..." if len(stale) > 10 else "")
                    + "\n\nUntracking removes them from git's index but "
                    "keeps the local files. This is the standard fix for "
                    "'I added it to .gitignore but git keeps showing it.'\n\n"
                    "Open the Untrack Ignored Files dialog now?",
                    parent=self._app)
                if ask:
                    UntrackIgnoredDialog(self._app, path, stale,
                        reason="now matched by your updated .gitignore")
                    return  # untrack flow handles commit prompt itself
        # Otherwise: trigger the existing commit-after-change prompt
        self._app._offer_commit_after_change(path, ".gitignore")


class GitCommitDialog(tk.Toplevel):
    """
    Stage and commit changes in a project's git repository.
    Shows the working-tree files as a checklist so the user can pick which
    files to include in this commit. On commit, only checked files are staged
    and committed; un-checked files stay as un-committed changes.
    """

    # status-char meanings shown next to filenames
    _STATUS_DESC = {
        "M":  "modified",
        "A":  "added",
        "D":  "deleted",
        "R":  "renamed",
        "C":  "copied",
        "U":  "conflict",
        "?":  "untracked",
        "!":  "ignored",
    }

    def __init__(self, parent, path, status_text: str, is_repo: bool, callback):
        """
        callback(path, message, selected_files): called on Commit.
        selected_files is a list of paths (relative to repo root) to stage+commit.
        status_text: output of `git status --short` (may be empty).
        is_repo: False disables the Commit button and shows a warning.
        """
        super().__init__(parent)
        self.title(f"Git Commit — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 480)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback
        self._is_repo  = is_repo
        self._status_raw = status_text

        # Parse status into [(xy, fname), ...] — same logic as Git tab.
        # CRITICAL: do NOT call .strip() on status_text BEFORE splitlines().
        # `git status --short` lines have a 2-char status (XY) + 1 space +
        # path. For working-tree-modified files the first char is a space
        # (" M file.py"). Calling strip() on the whole multi-line string
        # eats that leading space from the FIRST line only, shifting its
        # columns by 1, which caused `line[3:]` to drop the first character
        # of the path — e.g. ".claude/settings.json" became
        # "claude/settings.json" and git add failed with pathspec error.
        self._files = []
        if is_repo:
            for line in status_text.splitlines():
                if len(line) >= 4:
                    self._files.append((line[:2], line[3:]))

        # ── Header ──
        tk.Label(self,
                 text="📝  Git Commit",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))
        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── File checklist ──
        hdr_row = tk.Frame(self, bg=C["base"])
        hdr_row.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(hdr_row,
                 text="Pick files to include in this commit:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        if self._files:
            tk.Label(hdr_row,
                     text=f"  ({len(self._files)} changed)",
                     font=("Segoe UI", 9),
                     bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        # Scrollable checklist via canvas + frame (body parented to self so
        # children render reliably on Windows — same pattern as the wizard).
        list_outer = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        list_outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=160)
        _vsb = ttk.Scrollbar(list_outer, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        # Populate the checklist
        self._file_vars: list = []   # parallel to self._files — list of (BooleanVar, fname)
        if not is_repo:
            tk.Label(self._list_body,
                     text="⚠  Not a git repository — run 🔧 Git Init first.",
                     bg=C["mantle"], fg=C["red"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
        elif not self._files:
            tk.Label(self._list_body,
                     text="(nothing to commit — working tree clean)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
        else:
            for xy, fname in self._files:
                var = tk.BooleanVar(value=True)
                self._file_vars.append((var, fname, xy))
                row = tk.Frame(self._list_body, bg=C["mantle"])
                row.pack(fill=tk.X, padx=4, pady=1)
                # Status badge — colored char in a fixed-width slot
                xy_clean = xy.strip() or "?"
                status_char = xy_clean[0]
                color = {
                    "M": C["yellow"], "A": C["green"], "D": C["red"],
                    "R": C["sky"], "C": C["sky"], "U": C["red"],
                    "?": C["blue"], "!": C["overlay0"],
                }.get(status_char, C["text"])
                desc = self._STATUS_DESC.get(status_char, status_char)
                cb = tk.Checkbutton(row, variable=var,
                                    bg=C["mantle"], activebackground=C["mantle"],
                                    selectcolor=C["surface0"])
                cb.pack(side=tk.LEFT)
                tk.Label(row, text=xy_clean, width=3, anchor=tk.W,
                         font=("Consolas", 9, "bold"),
                         bg=C["mantle"], fg=color).pack(side=tk.LEFT)
                tk.Label(row, text=fname, anchor=tk.W,
                         font=("Consolas", 9),
                         bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(2, 6))
                tk.Label(row, text=f"({desc})", anchor=tk.W,
                         font=("Segoe UI", 8, "italic"),
                         bg=C["mantle"], fg=C["overlay0"]).pack(side=tk.LEFT)

        # ── Quick-select buttons ──
        if is_repo and self._files:
            sel_row = tk.Frame(self, bg=C["base"])
            sel_row.pack(fill=tk.X, padx=20, pady=(0, 8))
            ttk.Button(sel_row, text="Select All",
                       command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Select None",
                       command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Modified Only  (skip untracked)",
                       command=self._select_modified).pack(side=tk.LEFT)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(2, 8))

        # ── Commit message ──
        msg_hdr = tk.Frame(self, bg=C["base"])
        msg_hdr.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(msg_hdr,
                 text="Commit message:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        ttk.Button(msg_hdr, text="💡 Suggest",
                   command=self._fill_suggestion).pack(side=tk.RIGHT)

        msg_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        msg_frame.pack(fill=tk.X, padx=20, pady=(0, 12))
        self._msg_txt = tk.Text(msg_frame, height=3,
                                bg=C["mantle"], fg=C["text"],
                                insertbackground=C["text"],
                                relief=tk.FLAT, font=("Segoe UI", 10),
                                padx=8, pady=6, wrap=tk.WORD)
        self._msg_txt.pack(fill=tk.X)

        # Auto-populate with a suggestion based on currently selected files
        suggestion = _suggest_commit_message(status_text) if is_repo else ""
        if suggestion:
            self._msg_txt.insert(tk.END, suggestion)
            self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)  # pre-select so typing replaces it
        self._msg_txt.focus_set()

        # ── Action buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 16))
        self._commit_btn = ttk.Button(btn_row, text="Commit Selected",
                                      style="Primary.TButton",
                                      command=self._apply,
                                      state=tk.NORMAL if is_repo and self._files else tk.DISABLED)
        self._commit_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Ctrl+Enter = commit
        self._msg_txt.bind("<Control-Return>", lambda e: self._apply())

        self.update_idletasks()
        content_h = self.winfo_reqheight() + 20
        max_h = max(420, parent.winfo_height() - 60)
        w = 580
        h = min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── Selection helpers ────────────────────────────────────────────────────

    def _set_all(self, value: bool):
        for var, _f, _xy in self._file_vars:
            var.set(value)

    def _select_modified(self):
        """Select all tracked changes; uncheck untracked (??) files."""
        for var, _f, xy in self._file_vars:
            var.set(xy.strip() != "??")

    def _fill_suggestion(self):
        """Replace the commit message field with a fresh suggestion based on
        currently *selected* files only."""
        selected_lines = []
        for var, fname, xy in self._file_vars:
            if var.get():
                selected_lines.append(f"{xy} {fname}")
        sub_status = "\n".join(selected_lines) if selected_lines else self._status_raw
        suggestion = _suggest_commit_message(sub_status)
        if not suggestion:
            suggestion = "chore: update files"
        self._msg_txt.delete("1.0", tk.END)
        self._msg_txt.insert(tk.END, suggestion)
        self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)
        self._msg_txt.focus_set()

    def _apply(self):
        message = self._msg_txt.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Empty message",
                "Please enter a commit message.", parent=self)
            return
        selected_files = [fname for var, fname, _xy in self._file_vars if var.get()]
        if not selected_files:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to include in this commit.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, message, selected_files)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()
