"""Project discovery + pinned-active-project helpers.

`find_projects(roots)` walks the user's configured search roots and returns
a sorted list of project dicts. Each project is identified by the presence
of at least one of: `.tokensave/tokensave.db`, `.codegraph/codegraph.db`,
or `.git/` — tokensave and CodeGraph are equal citizens.

The pin file (`~/.tokensave/desktop-project.txt`) drives which project
Claude Desktop's MCP server serves. get_pinned / set_pinned / clear_pinned
are the only writers.

Per the Round 4 plan, signatures take the values they need rather than
reading module globals. Caller passes the live `SEARCH_ROOTS` / template
paths in.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from constants import DESKTOP_PROJECT_FILE, SKIP_DIRS, MAX_DEPTH
from helpers.detection import _root_path, _root_label


def find_projects(roots: list) -> list:
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
    for root in roots:
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
            # else codegraph db mtime (last full-build), else dirpath.
            #
            # tokensave uses SQLite in WAL mode. Incremental syncs write to
            # tokensave.db-wal; the main tokensave.db file's mtime only
            # advances when SQLite checkpoints (typically on close, or after
            # `sync --force`). So `getmtime(tokensave.db)` shows stale "6h
            # ago" even right after an incremental sync that's clearly
            # touched the index. Take the MAX mtime across the .db + WAL +
            # SHM sibling files so the "Last Synced" column tracks actual
            # sync activity, not just the last full checkpoint.
            if has_ts:
                db = os.path.join(dirpath, ".tokensave", "tokensave.db")
                ts_dir = os.path.join(dirpath, ".tokensave")
                mtime_candidates = []
                for fname in ("tokensave.db", "tokensave.db-wal",
                              "tokensave.db-shm"):
                    full = os.path.join(ts_dir, fname)
                    try:
                        mtime_candidates.append(os.path.getmtime(full))
                    except OSError:
                        pass
                if mtime_candidates:
                    mtime = max(mtime_candidates)
                else:
                    mtime = os.path.getmtime(dirpath)
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


# ── Pin file (Claude Desktop active-project) ─────────────────────────────────

def get_pinned():
    """Return the path the user pinned (or None if no valid pin)."""
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        p = open(DESKTOP_PROJECT_FILE, encoding="utf-8").read().strip()
        if p and os.path.isfile(os.path.join(p, ".tokensave", "tokensave.db")):
            return p
    return None


def set_pinned(path):
    """Write `path` to the pin file (creates parent dir if needed)."""
    os.makedirs(os.path.dirname(DESKTOP_PROJECT_FILE), exist_ok=True)
    with open(DESKTOP_PROJECT_FILE, "w", encoding="utf-8") as f:
        f.write(path)


def clear_pinned():
    """Remove the pin file if it exists (no-op otherwise)."""
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        os.remove(DESKTOP_PROJECT_FILE)


# ── Misc display helpers ─────────────────────────────────────────────────────

def fmt_age(mtime):
    """Human-readable age relative to now (e.g. '3h ago', 'Mar 14')."""
    diff = datetime.now().timestamp() - mtime
    if diff < 60:         return "just now"
    if diff < 3600:       return f"{int(diff / 60)}m ago"
    if diff < 86400:      return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 7:  return f"{int(diff / 86400)}d ago"
    return datetime.fromtimestamp(mtime).strftime("%b %d")


def load_basic_instructions_template(template_path: str, baseline_include_line: str) -> str:
    """Load the BASIC_INSTRUCTIONS.md template text, or return a minimal fallback.

    `template_path` is the absolute path to the user-customisable template
    file. `baseline_include_line` is the @<path>\\project-baseline.md line
    that any `@...project-baseline.md` reference in the template is rewritten
    to — keeps the written BASIC_INSTRUCTIONS.md pointing at the current
    baseline location regardless of where the template was authored.
    """
    if os.path.isfile(template_path):
        raw = open(template_path, encoding="utf-8").read()
        # Replace any @<path>/project-baseline.md line with the current computed path
        # so the written BASIC_INSTRUCTIONS.md always points to the right location.
        # Use a lambda so baseline_include_line is never parsed as a regex
        # replacement string — Windows paths contain backslashes that re.sub
        # would misinterpret as escape sequences (e.g. \p → bad escape error).
        raw = re.sub(r"^@[^\n]*project-baseline\.md",
                     lambda _: baseline_include_line, raw, flags=re.MULTILINE)
        return raw
    # Minimal inline fallback if template file is missing
    return (
        "# [PROJECT NAME] — Basic Instructions\n\n"
        "<!-- CLAUDE: Replace all [PLACEHOLDER] sections on first use. -->\n\n"
        f"{baseline_include_line}\n\n"
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
