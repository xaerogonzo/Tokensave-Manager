"""Tokensave integration gap checker.

Usage:
    python scripts/check_tokensave_integration.py [--available VERSION]

Reads local files and optionally queries GitHub via `gh` CLI. Exit code always
0 — this is an advisory report, never a blocking check.

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
import shutil
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
_TRACKED   = _ROOT / "docs" / "tracked-issues.json"

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

# Extracts (major, minor, patch) — handles v-prefix and pre-release suffixes
_VER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


# ── Version helpers ────────────────────────────────────────────────────────────

def _parse_version(v_str: str) -> tuple[int, int, int]:
    """Parse a semver string → (major, minor, patch) tuple for comparison.

    Handles v-prefix and pre-release suffixes (e.g. v6.1.1-rc.1) safely.
    Returns (0, 0, 0) on parse failure.
    """
    m = _VER_RE.match(v_str or "")
    return tuple(map(int, m.groups())) if m else (0, 0, 0)


# ── Config / file helpers ──────────────────────────────────────────────────────

def _read_config() -> dict:
    """Load manager-config.json; return {} on any error."""
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _load_tracked_issues() -> list[dict]:
    """Load docs/tracked-issues.json → list of {repo, issues} dicts.

    Returns [] on file-not-found (normal — file is optional and gitignored).
    Prints a warning and returns [] on JSON syntax error.
    """
    try:
        return json.loads(_TRACKED.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        print("  ⚠  tracked-issues.json has a JSON syntax error — skipping")
        return []


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


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _fetch_gh_json(gh_exe: str, endpoint: str, body: str | None = None):
    """Call `gh api <endpoint>` and return the parsed JSON, or None on error.

    When ``body`` is provided, it is passed via stdin (``--input -``) to avoid
    OS command-line length limits (critical for GraphQL queries).

    Detects GitHub API error responses ({"message": ..., "errors": [...]}) and
    returns None so callers don't have to check for error-shaped dicts.
    """
    args = [gh_exe, "api", endpoint]
    kwargs: dict = dict(
        capture_output=True, text=True, timeout=15,
        encoding="utf-8", errors="replace",
    )
    if body is not None:
        args += ["--input", "-"]
        kwargs["input"] = body          # stdin, NOT a CLI positional arg
    try:
        r = subprocess.run(args, **kwargs)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except Exception:
        return None
    # Detect GitHub API error response (auth failure, rate limit, etc.)
    if isinstance(data, dict) and ("message" in data or "errors" in data):
        return None
    return data


def _check_github_issues_graphql(
    repo: str, issue_numbers: list[int], gh_exe: str
) -> list[tuple[int, str, str, str]]:
    """Fetch all tracked issue statuses in one GraphQL call (chunked at 30).

    Returns [(number, title, state, url), ...] where state ∈ {"OPEN", "CLOSED",
    "NOT_FOUND"}.
    """
    if not issue_numbers:
        return []

    owner, name = repo.split("/", 1)
    results: list[tuple[int, str, str, str]] = []

    # Chunk at 30 to stay within GraphQL complexity limits
    for chunk_start in range(0, len(issue_numbers), 30):
        chunk = issue_numbers[chunk_start:chunk_start + 30]
        # Build one alias per issue number
        aliases = " ".join(
            f'i{n}: repository(owner:"{owner}", name:"{name}") '
            f'{{ issue(number:{n}) {{ number title state url }} }}'
            for n in chunk
        )
        query = json.dumps({"query": f"{{ {aliases} }}"})
        data = _fetch_gh_json(gh_exe, "graphql", body=query)
        if data is None:
            # Whole chunk failed; mark all as unknown
            for n in chunk:
                results.append((n, f"(issue #{n})", "UNKNOWN", ""))
            continue

        gql_data = data.get("data") or {}
        for n in chunk:
            alias_val = gql_data.get(f"i{n}")
            if alias_val is None:
                results.append((n, f"(issue #{n})", "UNKNOWN", ""))
                continue
            issue = alias_val.get("issue")
            if issue is None:
                # Issue number does not exist in the repo
                results.append((n, f"(issue #{n})", "NOT_FOUND", ""))
                continue
            results.append((
                n,
                issue.get("title") or f"(issue #{n})",
                issue.get("state") or "UNKNOWN",
                issue.get("url") or "",
            ))

    return results


def _fetch_tokensave_releases(
    repo: str, gh_exe: str, installed_version: str
) -> str:
    """Fetch GitHub releases newer than installed_version and format as text.

    Uses a single API call (per_page=10, no --paginate).
    Returns an empty string when no newer releases are found or gh fails.
    """
    data = _fetch_gh_json(gh_exe, f"repos/{repo}/releases?per_page=10")
    if not isinstance(data, list):
        return ""

    installed_tuple = _parse_version(installed_version)
    newer = [
        r for r in data
        if _parse_version(r.get("tag_name", "")) > installed_tuple
    ]
    if not newer:
        return ""

    lines = [f"### GitHub releases — {repo} (since v{installed_version})\n"]
    for rel in newer:
        tag = rel.get("tag_name", "?")
        published = (rel.get("published_at") or "")[:10]  # YYYY-MM-DD
        body = (rel.get("body") or "").strip()
        if len(body) > 1500:
            body = body[:1500] + "\n  … (truncated)"
        lines.append(f"\n#### {tag} — {published}")
        if body:
            lines.append(body)
    return "\n".join(lines)


# ── Snippet / CHANGELOG helpers ────────────────────────────────────────────────

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
    IN_NONE       = 0  # haven't found our target section yet
    IN_UNRELEASED = 1  # inside [Unreleased] block
    IN_FALLBACK   = 2  # fallback: inside first versioned-release block

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


# ── CLI arg parsing ────────────────────────────────────────────────────────────

def _parse_args() -> dict:
    """Parse CLI arguments. Returns dict with keys: available."""
    result: dict = {"available": None}
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--available" and i + 1 < len(args):
            result["available"] = args[i + 1]
            i += 2
        else:
            i += 1
    return result


# ── Main report ────────────────────────────────────────────────────────────────

def main() -> None:
    cli = _parse_args()
    cfg = _read_config()
    tokensave_exe = cfg.get("tokensave_exe", "")

    # gh is always detected from PATH — it's not stored in manager-config.json
    gh_exe = shutil.which("gh") or ""

    print(f"\n## Tokensave integration check — {date.today()}\n")

    # ── Version ──────────────────────────────────────────────────────────────

    installed = _get_installed_version(tokensave_exe)
    if installed:
        print(f"Installed:  v{installed}")
    else:
        print("Installed:  ⚠ could not determine (tokensave_exe not set or not found)")

    if cli["available"]:
        avail = cli["available"].lstrip("v")
        if installed and _parse_version(avail) > _parse_version(installed):
            print(f"Available:  v{avail}  ← upgrade recommended")
        elif installed:
            print(f"Available:  v{avail}  ✓ already installed")
        else:
            print(f"Available:  v{avail}")

    # Read CHANGELOG lines once
    try:
        cl_lines = _CHANGELOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        cl_lines = []
        print(f"CHANGELOG:  ⚠ could not read {_CHANGELOG}")

    # ── GitHub-tracked issues ─────────────────────────────────────────────────

    print("\n### GitHub-tracked issues (docs/tracked-issues.json)")
    tracked_entries = _load_tracked_issues()
    if not tracked_entries:
        print("  (no tracked-issues.json found — add one to track GitHub issues live)")
    elif not gh_exe:
        print("  ⚠  GitHub CLI (gh) not found on PATH — cannot query issue status")
        print("     Install gh (winget install GitHub.cli) and re-run.")
    else:
        for entry in tracked_entries:
            repo = entry.get("repo", "")
            issue_numbers = entry.get("issues", [])
            if not repo or not issue_numbers:
                continue
            print(f"  Repo: {repo}")
            issues = _check_github_issues_graphql(repo, issue_numbers, gh_exe)
            _CLOSED = {"CLOSED"}
            for n, title, state, url in issues:
                icon = "✓" if state in _CLOSED else "⚠"
                url_part = f"  {url}" if url else ""
                print(f"  {icon}  #{n:<4}  {title[:55]:<55}  {state}{url_part}")

    # ── GitHub releases since installed ───────────────────────────────────────

    print("\n### GitHub releases (since installed version)")
    if not gh_exe:
        print("  ⚠  GitHub CLI (gh) not found on PATH — cannot fetch release notes")
    elif not installed:
        print("  ⚠  Installed version unknown — cannot filter releases")
    else:
        releases_text = ""
        for entry in (tracked_entries or [{"repo": "aovestdipaperino/tokensave"}]):
            repo = entry.get("repo", "")
            if not repo:
                continue
            releases_text = _fetch_tokensave_releases(repo, gh_exe, installed)
            break  # only the first repo for now
        if releases_text:
            print(releases_text)
        else:
            print("  ✓  No newer releases found (already on latest)")

    # ── Upstream-issue docs ───────────────────────────────────────────────────
    # Keep this section for backwards compatibility — existing .md files are
    # still valid documentation even if their STATUS is no longer the primary
    # source of truth.

    print("\n### Upstream-issue docs (docs/upstream-issues/)")
    issues_list = _check_upstream_issues()
    if not issues_list:
        print("  (no upstream-issues/*.md files found)")
    else:
        _RESOLVED = {"fixed", "shipped", "moot"}
        for fname, status in issues_list:
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

    try:
        prompts_text = _PROMPTS.read_text(encoding="utf-8")
        snippet_map  = _collect_snippet_tool_refs(prompts_text)
    except Exception as exc:
        print(f"\n⚠ Could not read {_PROMPTS}: {exc}")
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
        "  For LLM-driven new-tool discovery:\n"
        "    • Use '🔍 Run Audit ▼' in the integration check dialog (Claude CLI / Local LLM)\n"
        "    • Or copy '🔄 Integration audit (after upgrade)' from the Reference tab\n"
        "      and paste it into a Claude Code CLI session in this project\n"
    )


if __name__ == "__main__":
    main()
