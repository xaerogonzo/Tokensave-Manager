"""Tokensave integration gap checker.

Usage:
    python scripts/check_tokensave_integration.py

Reads local files only (no network, no LLM). Exit code always 0 — this is an
advisory report, never a blocking check.

Run this AFTER:
  1. tokensave upgrade  (or binary swap)
  2. git pull  (so CHANGELOG.md and docs/upstream-issues/ are current)

Running before those steps will produce a false-clean report because the
script checks the locally installed version against locally present files.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# Force UTF-8 output so ✓/⚠ render correctly on Windows cmd / PowerShell
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Repo root ──────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent          # Token Save Manager Source/

_CONFIG    = _ROOT / "manager-config.json"
_CHANGELOG = _ROOT / "CHANGELOG.md"
_ISSUES    = _ROOT / "docs" / "upstream-issues"
_PROMPTS   = _ROOT / "src" / "prompts.py"

# ── Regexes ────────────────────────────────────────────────────────────────────

# Matches any versioned release header in CHANGELOG (not [Unreleased]):
#   ## [v6.2.0]  ## [6.2.0]  ## v6.2.0 - 2026-05-25  ## v6.2.0
_TAG_RE = re.compile(
    r"^##\s+"
    r"(?:\[v?[\d.]+\]"      # [v6.2.0] or [6.2.0]
    r"|v?[\d.]+(?:\s|$))",  # bare v6.2.0 or 6.2.0 (not [Unreleased])
    re.IGNORECASE,
)

# Matches ### sub-section headers: ### Added, ### Changed, ### Fixed, ### Removed
_SUBSECT_RE = re.compile(
    r"^###\s+(Added|Changed|Fixed|Removed)\b",
    re.IGNORECASE,
)

# Matches [Unreleased] header
_UNRELEASED_RE = re.compile(r"^##\s+\[Unreleased\]", re.IGNORECASE)

# Extracts tokensave tool names with word boundaries (exact match, no substring)
_TOOL_RE = re.compile(r"\b(tokensave_[a-z_]+)\b")

# Extracts STATUS value from upstream-issue doc headers (case-insensitive)
# Handles:  > **STATUS: FIXED**  |  STATUS: MOOT  |  Status: open
_STATUS_RE = re.compile(r"(?i)status\s*:\s*(\w[\w /.-]*)")

# Extracts semver from version string output
_SEMVER_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_config() -> dict:
    """Load manager-config.json; return {} on any error."""
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _get_installed_version(exe: str) -> str | None:
    """Run `tokensave --version` and return the semver string, or None."""
    if not exe or not os.path.isfile(exe):
        return None
    try:
        r = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        m = _SEMVER_RE.search(r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _get_changelog_latest_release(lines: list[str]) -> str | None:
    """Return the version string from the first versioned header in CHANGELOG."""
    for line in lines:
        if _UNRELEASED_RE.match(line):
            continue
        if _TAG_RE.match(line):
            m = _SEMVER_RE.search(line)
            return m.group(1) if m else None
    return None


def _collect_unreleased_tools(lines: list[str]) -> tuple[set[str], set[str]]:
    """Scan CHANGELOG for tool names in the [Unreleased] section.

    Returns:
        added   — set of tool names found in ### Added sub-blocks
        removed — set of tool names found in ### Removed sub-blocks

    Fallback: if [Unreleased] is absent or empty, scans lines under the
    first versioned release header instead (handles hotfix-direct-to-tag pattern).
    """
    added: set[str] = set()
    removed: set[str] = set()

    # State machine
    IN_NONE      = 0  # haven't found our target section yet
    IN_UNRELEASED = 1  # inside [Unreleased] block
    IN_FALLBACK  = 2  # fallback: inside first versioned-release block

    state = IN_NONE
    current_subsect: str | None = None
    unreleased_had_content = False

    for line in lines:
        # Detect section boundaries
        if _UNRELEASED_RE.match(line):
            state = IN_UNRELEASED
            current_subsect = None
            continue

        if _TAG_RE.match(line):
            if state == IN_UNRELEASED:
                if unreleased_had_content:
                    break  # done — collected everything in [Unreleased]
                else:
                    # [Unreleased] was empty; fall through to the first release
                    state = IN_FALLBACK
                    current_subsect = None
                    continue
            elif state == IN_FALLBACK:
                break  # done with first versioned block
            elif state == IN_NONE:
                # [Unreleased] was never found; use this first versioned block
                state = IN_FALLBACK
                current_subsect = None
                continue

        if state not in (IN_UNRELEASED, IN_FALLBACK):
            continue

        # Track sub-section changes (### Added, ### Removed, etc.)
        sub_m = _SUBSECT_RE.match(line)
        if sub_m:
            current_subsect = sub_m.group(1).lower()
            continue

        # Collect tool names only from Added / Removed sub-blocks
        if current_subsect in ("added", "removed"):
            for tool in _TOOL_RE.findall(line):
                unreleased_had_content = True
                if current_subsect == "added":
                    added.add(tool)
                else:
                    removed.add(tool)
        elif line.strip() and not line.startswith("#"):
            # Any non-blank, non-header content counts as "had content"
            unreleased_had_content = True

    return added, removed


def _collect_snippet_tool_refs(prompts_text: str) -> dict[str, set[str]]:
    """Return {snippet_title: {tool_names...}} from src/prompts.py body text.

    Only scans the string bodies (between triple-quotes/parens), not titles.
    """
    result: dict[str, set[str]] = {}
    # Parse tuples: ("title", "body...") — works for the actual prompts.py format
    # where each snippet is a 2-tuple literal.
    tuple_re = re.compile(
        r'\(\s*"([^"]+)"[^,]*,\s*(.*?)\)',
        re.DOTALL,
    )
    for m in tuple_re.finditer(prompts_text):
        title = m.group(1)
        body  = m.group(2)
        tools = set(_TOOL_RE.findall(body))
        result[title] = tools
    return result


def _all_snippet_bodies(prompts_text: str) -> set[str]:
    """Return a flat set of every tool name mentioned in any snippet body."""
    result: set[str] = set()
    for tools in _collect_snippet_tool_refs(prompts_text).values():
        result |= tools
    return result


def _check_upstream_issues() -> list[tuple[str, str | None]]:
    """Return [(filename, status_value|None)] for each upstream-issue doc."""
    results = []
    if not _ISSUES.is_dir():
        return results
    for md in sorted(_ISSUES.glob("*.md")):
        status: str | None = None
        try:
            for i, line in enumerate(md.read_text(encoding="utf-8").splitlines()):
                m = _STATUS_RE.search(line)
                if m:
                    # Strip trailing bold marker (**) and whitespace
                    status = m.group(1).rstrip("*").strip()
                    break
                if i >= 7:
                    break  # only scan first 8 lines
        except Exception:
            pass
        results.append((md.name, status))
    return results


# ── Main report ────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = _read_config()
    tokensave_exe = cfg.get("tokensave_exe", "")

    print(f"\n## Tokensave integration check — {date.today()}\n")

    # ── Version ──────────────────────────────────────────────────────────────

    installed = _get_installed_version(tokensave_exe)
    if installed:
        print(f"Installed:  v{installed}")
    else:
        print("Installed:  ⚠ could not determine (tokensave_exe not set or not found)")

    # Read CHANGELOG lines once
    try:
        cl_lines = _CHANGELOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        cl_lines = []
        print(f"CHANGELOG:  ⚠ could not read {_CHANGELOG}")

    # ── Version note ─────────────────────────────────────────────────────────
    # NOTE: This CHANGELOG belongs to TokenSave Manager, not to tokensave.
    # The version headers (e.g. ## [1.0.4]) are the manager's release tags.
    # We do NOT compare tokensave's installed version against the manager's
    # CHANGELOG version — they're different projects and different semver tracks.
    # To find the latest tokensave release, run `tokensave_changelog` via
    # the '🔄 Integration audit' snippet in the Reference tab.

    # ── Upstream issues ───────────────────────────────────────────────────────

    print("\n### Upstream issues (docs/upstream-issues/)")
    issues = _check_upstream_issues()
    if not issues:
        print("  (no upstream-issues/*.md files found)")
    else:
        _RESOLVED = {"fixed", "shipped", "moot"}
        for fname, status in issues:
            if status is None:
                print(f"  ⚠  {fname:<45}  no STATUS line found")
            elif any(r in status.lower() for r in _RESOLVED):
                print(f"  ✓  {fname:<45}  STATUS: {status}")
            else:
                print(f"  ⚠  {fname:<45}  STATUS: {status}  (not FIXED/SHIPPED/MOOT)")

    # ── Stale snippet check ───────────────────────────────────────────────────
    # Scans CHANGELOG [Unreleased] ### Removed sub-blocks for tokensave_* tool
    # names, then checks if any prompt snippet body still calls those removed
    # tools. This catches the "daemon removed in v6.0.0" class of breakage.
    #
    # NOTE: "New tools without snippets" is intentionally NOT done here.
    # The manager's CHANGELOG mentions tokensave tools throughout in descriptive
    # prose (not just as newly-added tool definitions), so programmatic detection
    # produces false positives. Use the '🔄 Integration audit' LLM prompt to
    # discover new tool → snippet gaps — it calls tokensave_changelog directly
    # and cross-references with word-boundary exactness.

    try:
        prompts_text = _PROMPTS.read_text(encoding="utf-8")
        snippet_map  = _collect_snippet_tool_refs(prompts_text)
    except Exception as e:
        print(f"\n⚠ Could not read {_PROMPTS}: {e}")
        snippet_map = {}

    _added_tools, removed_tools = _collect_unreleased_tools(cl_lines)

    print("\n### Stale snippet references (tool removed upstream)")
    stale: dict[str, list[str]] = {}  # tool → [snippet titles]
    for title, tools in snippet_map.items():
        for t in tools & removed_tools:
            stale.setdefault(t, []).append(title)
    if stale:
        for tool, titles in sorted(stale.items()):
            for t in sorted(titles):
                print(f"  ⚠  snippet '{t}' still calls {tool} (removed upstream)")
    else:
        print("  ✓  (no snippets reference tools listed in CHANGELOG ### Removed)")

    # ── Footer ────────────────────────────────────────────────────────────────

    print(
        "\n### Next steps\n"
        "  Free checks above are complete.\n"
        "  For new-tool discovery and full LLM analysis:\n"
        "    1. Open the Reference tab → copy '🔄 Integration audit (after upgrade)'\n"
        "    2. Run it in a Claude Code CLI session in this project\n"
        "    3. It will call tokensave_changelog and cross-reference src/prompts.py\n"
    )


if __name__ == "__main__":
    main()
