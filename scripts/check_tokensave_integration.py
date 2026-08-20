"""Tokensave integration gap checker.

Usage:
    python scripts/check_tokensave_integration.py [--available VERSION] [--fix]

    --available VERSION  Show an upgrade nudge when VERSION is newer than installed.
    --fix                Apply pending lifecycle actions (archive resolved issues,
                         create stubs for new open issues). Without this flag the
                         script is read-only and only reports what would change.

Reads local files and optionally queries GitHub via `gh` CLI. Exit code always
0 — this is an advisory report, never a blocking check.

Run this AFTER:
  1. tokensave upgrade  (or binary swap)
  2. git pull  (so CHANGELOG.md and docs/upstream-issues/ are current)

Running before those steps will produce a false-clean report because the
script checks the locally installed version against locally present files.

──────────────────────────────────────────────────────────────────────────────
TEMPLATE: Integration Check Script
──────────────────────────────────────────────────────────────────────────────
To adapt for a new upstream dependency:

1. Add an entry to docs/tracked-issues.json:
       {"repo": "owner/repo", "issues": [NNN, ...]}

2. Copy this script to scripts/check_<tool>_integration.py.

3. Update the stale-snippet detection logic to match the new tool's
   removal-detection pattern and update the footer "Integration workflow" line.

4. Add a "🔄 Integration audit (after upgrade)" prompt to src/prompts.py
   following the same 6-step structure (call changelog, check snippet coverage,
   check stale snippets, upstream issues, manager code, produce action list).

5. Wire it to UpdatePollerController.cmd_integration_check() if the tool
   belongs to this manager. Otherwise invoke it standalone:
       python scripts/check_<tool>_integration.py [--available VERSION] [--fix]
──────────────────────────────────────────────────────────────────────────────
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

def _force_utf8_stdout() -> None:
    """Force UTF-8 output so ✓/⚠ render on Windows cmd / PowerShell.

    Called from the __main__ guard, NOT at import time. Rebinding sys.stdout
    on import makes the module unimportable under pytest: it wraps pytest's
    capture object, which is then closed underneath the wrapper, and every
    test in the file errors with "I/O operation on closed file". Every real
    caller runs this as a script (directly, or as a subprocess from
    UpdatePollerController.cmd_integration_check), so they all still get it.
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")

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

    The file is committed project config (not gitignored). Returns [] on
    file-not-found (normal on a fresh clone before the file is created).
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

def _fetch_gh_json(
    gh_exe: str, endpoint: str, body: str | None = None
) -> tuple:
    """Call `gh api <endpoint>` and return ``(data, None)`` or ``(None, error_str)``.

    When ``body`` is provided, it is passed via stdin (``--input -``) to avoid
    OS command-line length limits (critical for GraphQL queries).

    Returning a tuple preserves the error message for callers to print as a
    warning rather than silently discarding transport vs. semantic failures.
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
    except subprocess.TimeoutExpired:
        return None, "GitHub API call timed out"
    except Exception as exc:
        return None, str(exc)
    if r.returncode != 0:
        stderr = (r.stderr or "").strip()[:200]
        return None, f"gh exited {r.returncode}: {stderr or '(no stderr)'}"
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    # Detect GitHub API semantic error responses (auth failure, rate limit, etc.)
    if isinstance(data, dict) and "message" in data:
        return None, f"GitHub API error: {data['message']}"
    if isinstance(data, dict) and "errors" in data:
        return None, f"GraphQL errors: {data['errors']}"
    return data, None


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
        data, err = _fetch_gh_json(gh_exe, "graphql", body=query)
        if err:
            # Chunk failed — warn but continue so other chunks still run
            print(
                f"  ⚠  GraphQL chunk "
                f"{chunk_start}–{chunk_start + len(chunk) - 1} failed: {err}"
            )
            for n in chunk:
                results.append((n, f"(issue #{n})", "UNKNOWN", ""))
            continue

        gql_data = (data or {}).get("data") or {}
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
    data, _err = _fetch_gh_json(gh_exe, f"repos/{repo}/releases?per_page=10")
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
    """Parse CLI arguments. Returns dict with keys: available, fix."""
    result: dict = {"available": None, "fix": False}
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--available" and i + 1 < len(args):
            result["available"] = args[i + 1]
            i += 2
        elif args[i] == "--fix":
            result["fix"] = True
            i += 1
        else:
            i += 1
    return result


# ── Upstream-issue lifecycle helpers ───────────────────────────────────────────

# Matches the ways a doc can name the issue it tracks:
#   "ISSUE: #NNN"                     — frontmatter marker (auto-generated stubs)
#   "issue #NNN" / "issue NNN"        — inline prose; the "#" is routinely dropped
#   ".../issues/NNN"                  — a bare GitHub URL, e.g. in a STATUS line
# The URL form matters because a hand-authored doc often cites its issue ONLY as
# a link. Missing it had two live consequences: a resolved issue's doc was never
# archived (it stayed ⚠ forever), and --fix would write a duplicate stub on top
# of an existing hand-written doc.
_ISSUE_DOC_RE = re.compile(
    r"(?:ISSUE:\s*#|issue\s+#?|/issues/)(\d+)", re.IGNORECASE)


def _find_issue_doc(number: int) -> "Path | None":
    """Return the Path of the active .md doc tracking *number*, or None.

    Scans docs/upstream-issues/*.md (not archived/) for any of:
      • A dedicated "ISSUE: #NNN" frontmatter line       (auto-generated stubs)
      • An inline "issue #NNN" or "issue NNN" substring  (hand-written prose)
      • A ".../issues/NNN" GitHub URL                    (STATUS lines, links)
    """
    active_dir = _ISSUES
    if not active_dir.is_dir():
        return None
    for md in active_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _ISSUE_DOC_RE.finditer(text):
            if int(m.group(1)) == number:
                return md
    return None


def _auto_stub_if_missing(number: int, title: str, url: str) -> bool:
    """Create a .md stub for *number* if no active doc exists. Returns True if created.

    The stub includes AUTO_GENERATED and ⚠ markers so LLM audits don't treat
    placeholder TODO content as meaningful authored analysis.
    Only called when --fix is passed.
    """
    if _find_issue_doc(number):
        return False  # doc already exists
    from datetime import date as _date
    stub_name = f"issue-{number}.md"
    path = _ISSUES / stub_name
    path.write_text(
        f"AUTO_GENERATED: true\n"
        f"ISSUE: #{number}\n"
        f"# ⚠ AUTO-GENERATED STUB — NO HUMAN ANALYSIS YET\n"
        f"# Upstream issue #{number} — {title}\n\n"
        f"> **STATUS: OPEN**\n\n"
        f"**URL:** {url}\n"
        f"**Filed:** {_date.today()}\n\n"
        f"## Impact on Token Save Manager\n"
        f"TODO: replace this stub with real analysis before using in LLM audits.\n\n"
        f"## Resolution\n"
        f"TODO: fill when resolved\n",
        encoding="utf-8",
    )
    return True


def _auto_archive_resolved(number: int, title: str) -> "str | None":
    """Rewrite STATUS line + move .md to archived/ for a confirmed-CLOSED issue.

    Returns a human-readable summary line or None if no doc was found.
    Only called when --fix is passed.
    """
    import shutil as _shutil
    from datetime import date as _date

    md = _find_issue_doc(number)
    if md is None:
        return None

    archived_dir = _ISSUES / "archived"
    archived_dir.mkdir(exist_ok=True)

    # Rewrite STATUS line — MULTILINE-anchored to avoid matching STATUS mentions
    # inside code blocks or prose paragraphs. Preserves leading blockquote prefix.
    text = md.read_text(encoding="utf-8", errors="replace")
    today = _date.today()
    new_status = f"STATUS: CLOSED — verified via GitHub API {today}"
    text = re.sub(
        r"^([ \t]*>?\s*\*{0,2})(STATUS:\s*[^\n]+)",
        lambda m: m.group(1) + new_status,
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    md.write_text(text, encoding="utf-8")

    dest = archived_dir / md.name
    _shutil.move(str(md), str(dest))
    return f"  📦  Archived #{number} ({md.name}) → archived/"


# ── Main report ────────────────────────────────────────────────────────────────

def main() -> None:
    cli = _parse_args()
    fix_mode: bool = cli["fix"]
    cfg = _read_config()
    tokensave_exe = cfg.get("tokensave_exe", "")

    # gh is always detected from PATH — it's not stored in manager-config.json
    gh_exe = shutil.which("gh") or ""

    fix_banner = "  [--fix mode: lifecycle mutations will be applied]\n" if fix_mode else ""
    print(f"\n## Tokensave integration check — {date.today()}\n{fix_banner}")

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
    # Accumulate all GitHub results for lifecycle processing below
    all_gh_results: list[tuple[int, str, str, str]] = []

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
            all_gh_results.extend(issues)
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

    # ── Upstream issue lifecycle ──────────────────────────────────────────────
    # Identify pending lifecycle actions from GitHub results:
    #   • OPEN issues with no local .md → offer to create a stub
    #   • CLOSED issues with an active local .md → offer to archive it
    # Without --fix: only report what would change.
    # With --fix: apply the changes and report what was done.

    stubs_needed: list[tuple[int, str, str]] = []
    archive_candidates: list[tuple[int, str, str]] = []
    for n, title, state, url in all_gh_results:
        if state == "OPEN" and not _find_issue_doc(n):
            stubs_needed.append((n, title, url))
        elif state == "CLOSED" and _find_issue_doc(n):
            archive_candidates.append((n, title, url))

    if stubs_needed or archive_candidates:
        if fix_mode:
            print("\n### Upstream issue lifecycle (--fix applied)")
        else:
            print(
                "\n### Pending lifecycle actions  "
                "(re-run with --fix to apply)"
            )
        for n, t, u in stubs_needed:
            if fix_mode:
                if _auto_stub_if_missing(n, t, u):
                    print(f"  📄  Created docs/upstream-issues/issue-{n}.md stub")
            else:
                print(f"  📄  Would create stub for open issue #{n}: {t[:60]}")
        for n, t, u in archive_candidates:
            if fix_mode:
                line = _auto_archive_resolved(n, t)
                if line:
                    print(line)
            else:
                md = _find_issue_doc(n)
                fname = md.name if md else "?"
                print(f"  📦  Would archive #{n} ({fname}) — CLOSED on GitHub")
        if fix_mode:
            print("  (commit the lifecycle changes to keep the repo clean)")

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
    _force_utf8_stdout()
    main()
