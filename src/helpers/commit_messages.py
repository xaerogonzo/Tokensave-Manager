"""Smart commit-message generation — multi-strategy orchestrator + sanitizer.

Public entry point: ``_suggest_commit_message(repo_path, status_text, cfg, git_exe) -> CommitSuggestion``.

The orchestrator tries strategies in order — LLM (opt-in), CHANGELOG bullet
parsing, diff-content analysis, file-name patterns — and returns the first
non-empty result after running it through ``_sanitize_commit_message`` for
conventional-commit format enforcement.

Per the Round 4 plan, every function that shells out to git takes an explicit
``git_exe`` parameter (no GIT_EXE module global), and functions that read AI
settings take a ``cfg`` parameter (no ``_cfg`` global). ``_strat_llm`` /
``_suggest_commit_message`` thread both down to the call sites that need
them.

Shares ``_CONVENTIONAL_RE`` semantics with ``helpers/release.py`` — but no
function here actually needs the regex (the release-side classifier owns
that), so we don't import it. If a future commit-message strategy wants
to parse a conventional-commit subject, import it from helpers.release.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
import subprocess
import textwrap
from collections import Counter

from constants import CREATE_NO_WINDOW
from helpers.llm import _build_llm_prompt, _call_llm
from helpers.claude_cli import call_claude_cli_print


# ── v4.2 grounding helpers ───────────────────────────────────────────────────

# Diff header marker for the post-image filename: ``+++ b/<path>``.
# `/dev/null` indicates a deletion target and is filtered.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def _files_from_diff(diff_text: str) -> list[str]:
    """Extract changed-file paths from a unified diff (``+++ b/<path>``).

    Used by commit-message draft, PR draft, and AI code review to feed
    ``build_codegraph_block(changed_files=...)`` which enables the
    ``codegraph affected --stdin`` test-impact path.
    """
    return [m.group(1) for m in _DIFF_FILE_RE.finditer(diff_text or "")
            if not m.group(1).startswith("/dev/null")]


def _build_commit_grounding(repo_path: str, tokensave_exe: str,
                            codegraph_exe: str, diff_text: str) -> str:
    """Build the combined tokensave+codegraph grounding block for commit messages.

    Recipe: ``commit_range_context``. Returns ``""`` on any failure (both
    sub-builders fail silent per v4.1 contract).
    """
    # Lazy import — keeps the module's import graph stable when grounding
    # is disabled (helpers.doc_grounding pulls in subprocess/json paths).
    try:
        from helpers.doc_grounding import (
            build_grounding_block,
            build_codegraph_block,
            build_combined_grounding,
        )
    except ImportError:
        return ""
    files = _files_from_diff(diff_text)
    try:
        ts_block = build_grounding_block(
            repo_path, "commit_range_context",
            tokensave_exe=tokensave_exe or "",
        )
    except Exception:
        ts_block = ""
    try:
        # v4.3: ensure index is fresh before building grounding block.
        if codegraph_exe:
            try:
                from helpers.codegraph_freshness import ensure_fresh
                ensure_fresh(repo_path, codegraph_exe)
            except Exception:
                pass
        cg_block = build_codegraph_block(
            repo_path, "commit_range_context",
            changed_files=files,
            codegraph_exe=codegraph_exe or "",
        )
    except Exception:
        cg_block = ""
    return build_combined_grounding(ts_block, cg_block)


# ── Conventional-commit reverse-lookup + scope vocabulary ────────────────────

# Inverse of `_TYPE_TO_SECTION`, used when reading a CHANGELOG bullet
# under (e.g.) `### Added` and producing a `feat:` prefix in the commit.
# Picks the SINGLE canonical type per section — `Changed` maps back to
# `refactor` by default; `_message_from_changelog` escalates to `feat` when
# the bullet text hints at user-visible UI changes.
_SECTION_TO_TYPE = {
    "Added":      "feat",
    "Fixed":      "fix",
    "Changed":    "refactor",
    "Removed":    "refactor",
    "Deprecated": "chore",
    "Security":   "fix",
    "Docs":       "docs",
    "Breaking":   "feat",   # surfaced with ! marker in `_message_from_changelog`
    "Other":      "chore",
}

# Vocabulary for inferring conventional-commit scope from CHANGELOG bullet
# lead-ins or commit-subject text. Patterns are case-insensitive regex; the
# first match wins. Order matters — put more-specific patterns first.
# Maintain in lockstep with the project's actual subsystem names.
_SCOPE_PATTERNS = [
    (r"release\s*wizard",           "release-wizard"),
    (r"git\s*commit\s*dialog",      "commit-dialog"),
    (r"git\s*(status|column)",      "git-status"),
    (r"auto[-\s]*commit",           "auto-commit"),
    (r"settings?\s*dialog",         "settings"),
    (r"project\s*tree",             "tree"),
    (r"scaffold(?:ing)?",           "scaffold"),
    (r"shadow[-\s]*link",           "shadow-link"),
    (r"sync\b",                     "sync"),
    (r"tokensave\b",                "tokensave"),
    (r"changelog\b",                "changelog"),
]

# Words in a bullet description that signal "user-visible feature" rather
# than internal refactor. Used to escalate `Changed` section bullets from
# `refactor:` to `feat:` in commit subjects.
_USER_VISIBLE_HINTS = re.compile(
    r"\b(button|dialog|wizard|menu|toolbar|hotkey|shortcut|"
    r"command|option|toggle|checkbox|tab|panel|label|tooltip|"
    r"radio|dropdown|preview|banner|popup)\b",
    re.IGNORECASE,
)

# Imperative-mood rewriter — applied to the FIRST word of the subject only.
# Catches the most common past-tense / -ing slip-ups.
_IMPERATIVE_REWRITES = {
    "added":   "add",
    "adds":    "add",
    "adding":  "add",
    "fixed":   "fix",
    "fixes":   "fix",
    "fixing":  "fix",
    "updated": "update",
    "updates": "update",
    "updating":"update",
    "removed": "remove",
    "removes": "remove",
    "removing":"remove",
    "changed": "change",
    "changes": "change",
    "changing":"change",
    "refactored": "refactor",
    "improved":   "improve",
    "improves":   "improve",
}

# Common locations for project changelog files. First hit wins.
_CHANGELOG_CANDIDATES = [
    "CHANGELOG.md", "CHANGELOG", "Changelog.md",
    "docs/CHANGELOG.md", "HISTORY.md", "RELEASES.md",
]

# Anti-pattern: filename listings in the subject. Catches three variants
# the LLM commonly produces despite the system prompt telling it not to:
#   "update X.md, Y.md"           — comma-separated (the original case)
#   "update X.md and Y.md"        — natural-language "and" connector
#   "update X.md, Y.md, and Z.md" — Oxford-comma variant
#   "update X.md; Y.md"           — semicolon-separated
# All match the same anti-pattern: a verb followed by a filename followed by
# any separator and another filename-shaped token.
_FILENAME_LISTING_RE = re.compile(
    r"\b(update|change|modify|edit|refactor|fix)\s+"
    r"[\w.-]+\.\w+\s+"                       # first filename + any whitespace
    r"(?:,\s*(?:and\s+)?|and\s+|;\s*|or\s+)" # connector: ',', ', and', 'and', ';', 'or'
    r"[\w.-]+\.\w+",                         # second filename
    re.IGNORECASE,
)


# Matches a conventional-commit prefix that contains a scope, e.g.
# "feat(src/controllers/update_poller.py): " — used to detect and clean
# path-based scopes that local LLMs copy verbatim from diff headers.
_SCOPE_CLEAN_RE = re.compile(r"^(\w+)\(([^)]+)\)(!?:\s*)$")


def _clean_prefix_scope(prefix: str) -> str:
    """If the scope looks like a file path (contains / or .), reduce it to the
    last path component without extension, capped at 20 chars.

    Examples:
      feat(src/controllers/update_poller.py):  →  feat(update_poller):
      fix(helpers/llm.py):                     →  fix(llm):
      chore(scripts):                          →  chore(scripts):   (unchanged)
      ""                                       →  ""                (unchanged)
    """
    if not prefix:
        return prefix
    sm = _SCOPE_CLEAN_RE.match(prefix)
    if not sm:
        return prefix
    verb, scope, tail = sm.group(1), sm.group(2), sm.group(3)
    if "/" not in scope and "." not in scope:
        return prefix           # already a short clean scope — leave it
    last = scope.rsplit("/", 1)[-1]    # get the filename component
    last = last.rsplit(".", 1)[0]      # strip extension
    last = last.replace(".", "_")      # any remaining dots → underscore
    last = last[:20]                   # cap at 20 chars
    return f"{verb}({last}){tail}"


# ── Pure sanitizer functions ─────────────────────────────────────────────────

def _strip_md(s: str) -> str:
    """Strip bold, italic, inline code, and markdown links from a string."""
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)        # bold
    s = re.sub(r"\*([^*]+)\*",      r"\1", s)        # italic
    s = re.sub(r"`([^`]+)`",        r"\1", s)        # code
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # links
    return s


def _escalate_commit_type(subject: str, has_source_changes: bool) -> str:
    """Escalate generic `chore:` and `docs:` prefixes to `refactor:` when
    source files are among the changed files.

    Scoped variants like `chore(deps):` or `docs(api):` are left alone —
    they carry enough signal to be treated as intentional.
    """
    if not has_source_changes:
        return subject
    for prefix in ("chore", "docs"):
        if subject.lower().startswith(f"{prefix}:"):
            if not re.match(rf"^{prefix}\([^)]+\):", subject, re.IGNORECASE):
                subject = "refactor:" + subject[len(f"{prefix}:"):]
            break
    return subject


def _normalize_commit_body(body: str) -> str:
    """Wrap body text at 72 chars per paragraph.

    Also splits jammed bullets (`. - Foo` on one line) that small local
    LLMs (e.g. Qwen 2.5 14B Q4) produce when they don't respect newlines.
    """
    if not body:
        return body
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    normalised = []
    for p in paragraphs:
        # Split `. - text` → `.\n- text` (also `: - text`)
        p = re.sub(r"([.:])\s+-\s+", r"\1\n- ", p)
        if "\n- " in p:
            wrapped_lines = []
            for line in p.split("\n"):
                if line.startswith("- "):
                    wrapped_lines.append(textwrap.fill(
                        line, width=72,
                        initial_indent="", subsequent_indent="  ",
                        break_long_words=False, break_on_hyphens=False,
                    ))
                else:
                    wrapped_lines.append(textwrap.fill(
                        line, width=72,
                        break_long_words=False, break_on_hyphens=False,
                    ))
            normalised.append("\n".join(wrapped_lines))
        else:
            normalised.append(textwrap.fill(
                p, width=72,
                break_long_words=False, break_on_hyphens=False,
            ))
    return "\n\n".join(normalised)


def _sanitize_commit_message(subject: str, body: str = "",
                              has_source_changes: bool = False) -> tuple[str, str]:
    """Enforce subject/body invariants on any candidate message.

    Returns (subject, body). Empty subject means "give up; caller should
    fall through to a lower-priority strategy".

    Rules:
      * Strip markdown noise: ``**x**`` → ``x``, backticks → none,
        ``[text](url)`` → ``text``
      * Strip surrounding quotes / leading "Here's a commit message:" preambles
      * Imperative mood — rewrite first word if it matches a known past tense
      * Subject ≤ 72 chars (truncate at last word boundary that fits)
      * Escalate ``chore:`` / ``docs:`` → ``refactor:`` when source files changed
      * Reject filename-listing anti-pattern → empty subject (forces fallback)
      * Body wrapped at 72 chars per line
    """
    subject = _strip_md((subject or "").strip())
    body    = _strip_md((body or "").strip())

    for pre in ("Here's a commit message:", "Commit message:",
                "Suggested commit message:"):
        if subject.lower().startswith(pre.lower()):
            subject = subject[len(pre):].lstrip(":").strip()
    subject = subject.strip("`'\"")

    if "\n" in subject:
        head, _, tail = subject.partition("\n")
        subject = head.strip()
        if tail.strip():
            body = (tail.strip() + ("\n\n" + body if body else "")).strip()

    m = re.match(r"^(?:(\w+(?:\([^)]+\))?!?:\s+))?(\w+)(.*)$", subject)
    if m:
        prefix, first, rest = m.group(1) or "", m.group(2), m.group(3)
        prefix = _clean_prefix_scope(prefix)     # reduce path-based scopes
        canonical = _IMPERATIVE_REWRITES.get(first.lower())
        if canonical:
            first = canonical
        cleaned = f"{prefix}{first}{rest}"
        if cleaned != subject:
            subject = cleaned

    if _FILENAME_LISTING_RE.search(subject):
        return "", ""

    subject = _escalate_commit_type(subject, has_source_changes)

    if len(subject) > 72:
        truncated = subject[:72]
        sp = truncated.rfind(" ")
        if sp > 40:
            truncated = truncated[:sp]
        subject = truncated.rstrip(",;:-")

    body = _normalize_commit_body(body)
    return subject, body


# ── Status / scope helpers ───────────────────────────────────────────────────

def _parse_commit_status(status_text: str) -> "tuple[list[tuple[str,str]], bool]":
    """Parse `git status --short` output into (files, has_source) for the commit strategies."""
    _SOURCE_EXTS = {".py", ".js", ".ts", ".cs", ".cpp", ".c", ".h", ".rs",
                    ".go", ".java", ".rb", ".php", ".swift", ".kt", ".scala"}
    files = []
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        xy    = line[:2].strip()
        fname = line[3:]
        if " -> " in fname:
            fname = fname.split(" -> ")[-1]
        files.append((xy, fname))
    has_source = any(
        os.path.splitext(os.path.basename(f))[1].lower() in _SOURCE_EXTS
        for _xy, f in files
    )
    return files, has_source


def _extract_scope(text: str) -> str | None:
    """Match `text` against the scope vocabulary; return scope or None."""
    for pat, scope in _SCOPE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return scope
    return None


def _dominant_directory(files: list) -> str | None:
    """Find the most-common meaningful directory among staged files.

    `files` is a list of (xy, fname) tuples (the format `_suggest_from_filenames`
    uses). Returns the last component of the deepest common directory, or
    None if all files are at repo root.
    """
    if not files:
        return None
    dirs = []
    for _xy, fname in files:
        parent = os.path.dirname(fname.replace("\\", "/"))
        if parent:
            dirs.append(parent)
    if not dirs:
        return None
    # Most common directory
    most_common, _ = Counter(dirs).most_common(1)[0]
    # Return the LAST meaningful component (e.g. src/components/wizard → wizard)
    parts = [p for p in most_common.split("/") if p and p not in
             ("src", "lib", "app", "source")]
    return parts[-1] if parts else None


# ── tokensave commit_context enrichment ──────────────────────────────────────

_ctx_cache: dict = {}


def _fetch_commit_context(tokensave_exe: str, repo_path: str, git_exe: str,
                          diff_text: str) -> str:
    """Run tokensave commit_context and format as compact markdown.

    Returns "" on any failure. Result is cached by (head_sha, staged_diff_hash)
    so repeated Suggest clicks don't re-invoke the subprocess.
    Catches json.JSONDecodeError explicitly to guard against malformed output
    after tokensave upgrades.
    """
    if not tokensave_exe or not repo_path or not git_exe:
        return ""
    try:
        sha_proc = subprocess.run(
            [git_exe, "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        head_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else ""
    except Exception:
        head_sha = ""
    staged_diff_hash = hashlib.md5(diff_text.encode()).hexdigest()
    cache_key = (head_sha, staged_diff_hash)
    if cache_key in _ctx_cache:
        return _ctx_cache[cache_key]
    try:
        proc = subprocess.run(
            # `--staged-only` is a valued flag, not a bare switch: passing it
            # alone makes tokensave exit with "flag requires a value", which
            # this function then swallows as an empty result. It did exactly
            # that on every call from the day it shipped, so the commit
            # message never actually saw this context.
            #
            # `--project` scopes the query to the repo being committed.
            # Without it the query resolves against the MANAGER's working
            # directory, which is wherever the app was launched from and has
            # nothing to do with the project in the Git tab.
            [tokensave_exe, "tool", "commit_context",
             "--staged-only", "true",
             "--project", repo_path],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            _ctx_cache[cache_key] = ""
            return ""
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _ctx_cache[cache_key] = ""
        return ""
    except Exception:
        _ctx_cache[cache_key] = ""
        return ""
    lines: list[str] = []
    symbols = data.get("changed_symbols") or []
    if symbols:
        lines.append("## Changed symbols")
        for s in symbols:
            name = s.get("name", "")
            file_ = s.get("file", "")
            lines.append(f"- {name}" + (f" ({file_})" if file_ else ""))
    roles = data.get("file_roles") or {}
    if roles:
        lines.append("## File roles")
        for f, role in roles.items():
            lines.append(f"- {f}: {role}")
    style = data.get("recent_commit_style") or []
    if style:
        lines.append("## Recent commit style")
        lines.append(", ".join(style) if isinstance(style, list) else str(style))
    result = "\n".join(lines)
    _ctx_cache[cache_key] = result
    return result


# ── Git-touching helpers (take git_exe per Rule 1) ───────────────────────────

def _recent_commit_subjects(repo_path: str, git_exe: str, n: int = 5) -> list:
    """Return the last `n` commit subject lines (most-recent first)."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo_path, "log", f"-n{n}", "--format=%s"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _find_changelog_file(repo_path: str) -> str | None:
    """Locate the first present CHANGELOG-style file in the repo."""
    for candidate in _CHANGELOG_CANDIDATES:
        full = os.path.join(repo_path, candidate)
        if os.path.isfile(full):
            return candidate
    return None


def _new_files_diff(repo_path: str, git_exe: str, max_chars: int = 8000) -> str:
    """Build a unified-diff-style block for untracked new files.

    Called when ``git diff HEAD`` returns empty (all pending changes are new,
    untracked files not yet known to git).  Reads each file directly and
    formats it as a ``+``-prefixed pseudo-diff so LLM strategies have content
    to work with.  Capped at ``max_chars`` to avoid oversized prompts.
    """
    try:
        ls = subprocess.run(
            [git_exe, "-C", repo_path, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if ls.returncode != 0 or not ls.stdout.strip():
        return ""

    parts: list[str] = []
    total = 0
    for rel_path in ls.stdout.splitlines():
        rel_path = rel_path.strip()
        if not rel_path:
            continue
        abs_path = os.path.join(repo_path, rel_path)
        try:
            content = open(abs_path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        lines = content.splitlines()
        header = (
            f"diff --git a/{rel_path} b/{rel_path}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{rel_path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
        )
        body = "".join(f"+{line}\n" for line in lines)
        chunk = header + body
        remaining = max_chars - total
        if len(chunk) >= remaining:
            if remaining > 200:
                parts.append(chunk[:remaining] + "\n... (truncated)\n")
            break
        parts.append(chunk)
        total += len(chunk)

    return "".join(parts)


def _pending_diff(repo_path: str, *paths: str, git_exe: str,
                  lines_of_context: int = 0) -> str:
    """Return the diff between HEAD and the working tree (staged + unstaged).

    Used for commit-message suggestion BEFORE the GitCommitDialog actually
    stages files. ``git diff HEAD`` captures everything that would land in
    the commit if the user stages and commits all working-tree changes.

    When HEAD and the working tree agree on all tracked files (e.g. all
    pending changes are brand-new untracked files), falls back to
    ``_new_files_diff`` so strategies still have content to analyse.

    Keyword-only ``git_exe`` keeps the optional ``*paths`` varargs working
    naturally — callers pass paths positionally and git_exe by name.
    """
    cmd = [git_exe, "-C", repo_path, "diff", "HEAD", "--no-color",
           f"-U{lines_of_context}"]
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    result = proc.stdout if proc.returncode == 0 else ""
    if not result and not paths:
        result = _new_files_diff(repo_path, git_exe)
    return result


def _parse_diff_bullet(line: str, section: str | None) -> "dict | None":
    """Parse a `+`-prefixed diff line as a CHANGELOG bullet.

    Returns a bullet dict ``{section, lead_in, description}`` when `line`
    starts a new bullet under `section`, or None otherwise. Handles the
    bolded lead-in pattern (**lead** — desc) with a plain-text fallback.
    """
    if not section:
        return None
    m_bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
    if not m_bullet:
        return None
    text = m_bullet.group(1)
    m_bold = re.match(
        r"^\*\*(?P<lead>[^*]+?)\*\*\s*(?:[—\-–:]\s*)?(?P<desc>.+)$",
        text,
    )
    if m_bold:
        lead = m_bold.group("lead").strip().rstrip(".").strip()
        desc = m_bold.group("desc").strip()
    else:
        first_sent = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        lead = (first_sent[:60].rstrip() if len(first_sent) > 60
                else first_sent).rstrip(".")
        desc = text[len(first_sent):].strip() or text
    return {"section": section, "lead_in": lead, "description": desc}


def _extract_changelog_additions(repo_path: str, git_exe: str) -> list:
    """Parse the staged CHANGELOG diff into structured bullet records.

    Returns a list of dicts: ``{"section": "Added"|"Fixed"|..., "lead_in": str,
    "description": str}``. Returns ``[]`` if no changelog or no additions.

    Handles Keep-a-Changelog format with bolded lead-ins:
        ### Added
        - **Hotfix bump option in the Release Wizard** — fourth radio option…
    Falls back gracefully to plain bullets without bold lead-ins.
    """
    cl_file = _find_changelog_file(repo_path)
    if not cl_file:
        return []
    # NOTE: use generous context (-U20) so the section header (### Added /
    # ### Changed) appears as a CONTEXT line above any newly-added bullets.
    # Without this, bullets added to EXISTING sections become invisible to
    # the parser because the section header isn't itself a `+` line.
    diff = _pending_diff(repo_path, cl_file, git_exe=git_exe, lines_of_context=20)
    if not diff:
        return []

    section = None
    bullets = []
    current_bullet = None

    for raw_line in diff.splitlines():
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("@@"):
            # Hunk boundary — reset section so we don't misattribute next bullet.
            section = None
            current_bullet = None
            continue

        if raw_line.startswith("+"):
            line, is_added = raw_line[1:], True
        elif raw_line.startswith(" "):
            line, is_added = raw_line[1:], False
        elif raw_line.startswith("-"):
            continue
        else:
            continue

        # Track section headers from both context and added lines.
        m_section = re.match(r"^#{2,4}\s+(\w+)\s*$", line)
        if m_section:
            section = m_section.group(1)
            current_bullet = None
            continue

        if not is_added:
            continue

        bullet = _parse_diff_bullet(line, section)
        if bullet is not None:
            current_bullet = bullet
            bullets.append(current_bullet)
            continue

        if current_bullet and line.strip():
            current_bullet["description"] = (
                current_bullet["description"] + " " + line.strip()
            ).strip()

    return bullets


def _diff_added_python_symbols(repo_path: str, git_exe: str) -> dict:
    """Parse `git diff --cached` for newly-added top-level def/class names."""
    diff = _pending_diff(repo_path, git_exe=git_exe, lines_of_context=0)
    if not diff:
        return {"functions": [], "classes": []}

    functions, classes = [], []
    for line in diff.splitlines():
        # Only consider added lines that start at column 0 (top-level definitions)
        # The leading + is followed immediately by the keyword.
        m_fn = re.match(r"^\+(?:async\s+)?def\s+(\w+)", line)
        m_cl = re.match(r"^\+class\s+(\w+)", line)
        if m_fn:
            functions.append(m_fn.group(1))
        elif m_cl:
            classes.append(m_cl.group(1))
    # Dedup, preserve order
    return {
        "functions": list(dict.fromkeys(functions)),
        "classes":   list(dict.fromkeys(classes)),
    }


# ── Changelog-bullet → message builder (pure) ────────────────────────────────

def _message_from_changelog(bullets: list, files: list) -> tuple:
    """Build (subject, body) from CHANGELOG bullet additions. Returns ("","")
    if `bullets` is empty.

    Strategy:
      * Pick the highest-impact section (Breaking > Added > Fixed > Changed > …)
      * Map section → conventional-commit type (`feat` / `fix` / `refactor` / …)
      * Escalate Changed → feat when bullet description hints at user-visible UI
      * Infer scope from bullet lead-ins (vocabulary), then file paths
      * Subject = `type(scope): lead-in` (or "+ N more" if multiple bullets)
      * Body = stripped descriptions, one paragraph per bullet, wrapped at 72
    """
    if not bullets:
        return "", ""

    # Group bullets by section
    by_section = {}
    for b in bullets:
        by_section.setdefault(b["section"], []).append(b)

    # Impact order: prefer Added > Fixed > Changed > Removed > Deprecated > Docs > Security
    impact_order = ["Breaking", "Added", "Fixed", "Changed", "Security",
                    "Removed", "Deprecated", "Docs", "Other"]
    dominant_section = next((s for s in impact_order if s in by_section), None)
    if not dominant_section:
        return "", ""

    dom_bullets = by_section[dominant_section]
    ctype = _SECTION_TO_TYPE.get(dominant_section, "chore")

    # Escalate Changed → feat when bullet text describes user-visible UI changes
    if dominant_section == "Changed":
        for b in dom_bullets:
            combined = b["lead_in"] + " " + b["description"]
            if _USER_VISIBLE_HINTS.search(combined):
                ctype = "feat"
                break

    # Breaking marker
    bang = "!" if dominant_section == "Breaking" else ""

    # Scope: try vocabulary on every lead-in, then fall back to directory
    scope = None
    for b in dom_bullets:
        s = _extract_scope(b["lead_in"])
        if s:
            scope = s
            break
    if not scope:
        scope = _dominant_directory(files)

    # Subject
    primary_lead = dom_bullets[0]["lead_in"]
    # Lowercase first character so it reads naturally after "feat(x): "
    if primary_lead:
        primary_lead = primary_lead[0].lower() + primary_lead[1:]
    # Count ALL other bullets across ALL sections (not just same-section)
    total_bullets = sum(len(v) for v in by_section.values())
    extras = total_bullets - 1
    if extras > 0:
        primary_lead += f" + {extras} more"

    scope_str = f"({scope})" if scope else ""
    subject = f"{ctype}{scope_str}{bang}: {primary_lead}"

    # Body: one paragraph per bullet across ALL sections, lead-in + description
    body_paragraphs = []
    for section_name in impact_order:
        section_bullets = by_section.get(section_name) or []
        for b in section_bullets:
            lead = b["lead_in"].strip()
            desc = b["description"].strip()
            # Take first ~2 sentences of description, capped at 240 chars
            sentences = re.split(r"(?<=[.!?])\s+", desc, maxsplit=2)
            short_desc = " ".join(sentences[:2])[:240].strip()
            if short_desc and short_desc != lead:
                body_paragraphs.append(f"{lead}: {short_desc}")
            else:
                body_paragraphs.append(lead)
    body = "\n\n".join(body_paragraphs)

    return subject, body


# ── Diff-content + filename strategies ───────────────────────────────────────

def _suggest_from_diff_content(repo_path: str, files: list, git_exe: str) -> tuple:
    """Strategy 2 — infer message from file kinds + added Python symbols.

    `files` is a list of (xy, fname) tuples. Returns ("","") if no signal.
    """
    if not files:
        return "", ""

    basenames = [os.path.basename(f) for _xy, f in files]
    exts = {os.path.splitext(b)[1].lower() for b in basenames}
    paths = [f.replace("\\", "/") for _xy, f in files]

    doc_exts    = {".md", ".rst", ".txt", ".adoc"}
    test_paths  = [p for p in paths if re.search(r"(?:^|/)tests?/", p)
                   or os.path.basename(p).startswith("test_")
                   or os.path.basename(p).endswith("_test.py")]
    config_files = {"requirements.txt", "pyproject.toml", "package.json",
                    "package-lock.json", "Pipfile", "Pipfile.lock",
                    "poetry.lock", "setup.py", "setup.cfg"}
    config_paths = [p for p in paths if os.path.basename(p) in config_files]
    ci_paths     = [p for p in paths if "/.github/workflows/" in "/" + p]
    scope        = _dominant_directory(files)

    # Docs only
    if exts and exts <= doc_exts:
        if len(files) == 1:
            return f"docs: update {basenames[0]}", ""
        # Name up to 3 files instead of the generic "documentation"
        stems = [os.path.splitext(b)[0] for b in sorted(set(basenames))[:3]]
        return f"docs({', '.join(stems)}): update", ""

    # CI workflows
    if ci_paths and len(ci_paths) == len(files):
        return f"ci: update {os.path.basename(ci_paths[0])}", ""

    # Config / deps
    if config_paths and len(config_paths) == len(files):
        return "chore(deps): update dependencies", ""

    # Tests only
    if test_paths and len(test_paths) == len(files):
        if scope:
            return f"test({scope}): add tests", ""
        return f"test: add {basenames[0]}", ""

    # Source-code changes: look at added Python symbols
    has_py = any(p.endswith(".py") for p in paths)
    if has_py:
        syms = _diff_added_python_symbols(repo_path, git_exe)
        scope_str = f"({scope})" if scope else ""
        if syms["classes"]:
            return f"feat{scope_str}: add {syms['classes'][0]}", ""
        if syms["functions"]:
            fn = syms["functions"][0]
            return f"feat{scope_str}: add {fn}", ""
        # No new top-level defs AND no inferable scope — there's nothing useful
        # left to say at this level of analysis. Return empty so the
        # orchestrator falls through to the generic backstop strategy. (Using
        # `basenames[0]` here produces misleading messages like
        # "refactor: update BASIC_INSTRUCTIONS.md" for multi-file commits that
        # touched many things — the first alphabetical filename is not a scope.)
        if scope:
            return f"refactor({scope}): update {scope}", ""
        return "", ""

    return "", ""


def _suggest_from_filenames(status_text: str) -> str:
    """Legacy file-pattern strategy. Last-resort fallback.

    Generates a conventional-commit-style message from `git status --short`
    output ONLY (no diff content, no CHANGELOG). The strategies above this
    in the chain are preferred when they have signal.
    """
    # NOTE: never strip() the full status_text before splitlines() — strip
    # eats the leading space from the first line, shifting columns and
    # silently dropping the first character of the filename when the first
    # entry is a working-tree modification (single leading space).
    lines = [l for l in status_text.splitlines() if len(l) >= 4]
    if not lines:
        return ""

    files = []
    for line in lines:
        xy   = line[:2].strip()
        fname = line[3:]
        if " -> " in fname:
            fname = fname.split(" -> ")[-1]
        files.append((xy, fname))

    if not files:
        return ""

    basenames = [os.path.basename(f) for _, f in files]
    exts      = {os.path.splitext(b)[1].lower() for b in basenames}
    has_del   = any("D" in xy for xy, _ in files)
    has_add   = any("A" in xy or "?" in xy for xy, _ in files)

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

    doc_exts  = {".md", ".txt", ".rst", ".adoc"}
    code_exts = {".py", ".js", ".ts", ".cs", ".cpp", ".c", ".h", ".rs", ".go", ".java"}

    if exts <= doc_exts:
        dir_scope = _dominant_directory(files)
        if dir_scope:
            return f"docs({dir_scope}): update"
        stems = [os.path.splitext(b)[0] for b in sorted(set(basenames))[:3]]
        return f"docs({', '.join(stems)}): update"
    if exts <= code_exts:
        if len(basenames) <= 3:
            return f"chore: update {', '.join(basenames)}"
        return f"chore: update {len(files)} source files"
    if has_del and not has_add:
        return f"chore: remove {len(files)} files"
    if len(basenames) <= 2:
        return f"chore: update {', '.join(basenames)}"
    return f"chore: update {basenames[0]}, {basenames[1]} + {len(basenames) - 2} more"


# ── Claude CLI strategy ───────────────────────────────────────────────────────


def _strat_claude_cli(repo_path: str, git_exe: str,
                      claude_cli_exe: str,
                      claude_cli_model: str = "",
                      grounding: str = "",
                      tokensave_exe: str = "") -> "tuple[str, str] | None":
    """Strategy: Claude CLI (claude --print). Returns (subject, body) or None.

    Reuses _build_llm_prompt from helpers.llm so Claude CLI produces the same
    rich subject+body output Ollama does. Quality differences between backends
    now reflect the model itself, not asymmetric prompting.

    system_prompt= is intentionally omitted: passing it triggers
    --append-system-prompt, which bolts our commit instructions onto the END
    of Claude Code's full built-in "coding assistant" system prompt. The model
    receives two competing personas and commit quality degrades significantly.
    Embedding everything in the user message gives the model a single, focused
    task with no competing framing.
    """
    if not claude_cli_exe or not repo_path or not git_exe:
        return None
    diff = _pending_diff(repo_path, git_exe=git_exe)
    if not diff:
        return None
    recent = _recent_commit_subjects(repo_path, git_exe, n=5)
    ts_ctx = _fetch_commit_context(tokensave_exe, repo_path, git_exe, diff)
    if ts_ctx:
        diff = f"[Tokensave semantic context]\n{ts_ctx}\n\n[Git diff]\n{diff}"
    system, user = _build_llm_prompt(diff, recent, max_diff_chars=24000,
                                     grounding=grounding)
    # cwd=home: Claude Code loads <cwd>/CLAUDE.md as project context, which
    # puts smaller models like Haiku into "assistant mode" — they respond
    # conversationally to the diff instead of generating a commit message.
    # Pointing cwd at $HOME isolates this call from any project's CLAUDE.md.
    result = call_claude_cli_print(
        claude_cli_exe,
        "Do NOT run bash, git, or any tools. "
        "The complete staged diff is provided below — generate the commit message from it directly.\n\n"
        + system + "\n\n" + user,
        timeout=45,
        model=claude_cli_model,
        cwd=os.path.expanduser("~"),
    )
    if not result:
        return None
    head, _, tail = result.partition("\n\n")
    return head.strip(), tail.strip()


# ── LLM strategy + orchestrator entry point ──────────────────────────────────

def _call_llm_for_commit_message(cfg: dict, repo_path: str, git_exe: str,
                                 grounding: str = "",
                                 tokensave_exe: str = "") -> str | None:
    """Commit-message-specific LLM wrapper. Composes the prompt and calls _call_llm.

    Skipped when the diff is shorter than `min_diff_lines` — trivial commits
    don't justify an LLM call when the heuristic chain handles them fine.

    `cfg` here is the LLM-config sub-dict from manager-config.json
    (i.e. `_cfg["commit_message_llm"]`), NOT the full ManagerConfig.
    """
    if not cfg.get("enabled"):
        return None

    diff = _pending_diff(repo_path, git_exe=git_exe)
    if not diff:
        return None
    # Skip the min_diff_lines gate for local providers (Ollama, localhost
    # openai_compatible) — there is no per-token cost, so gating on diff
    # size only prevents useful suggestions. The gate still applies for
    # cloud APIs (anthropic, openai) to control spend.
    provider = cfg.get("provider", "")
    is_local = (
        provider == "ollama"
        or (provider == "openai_compatible"
            and "localhost" in cfg.get("base_url", ""))
    )
    if not is_local:
        diff_lines = diff.count("\n")
        if diff_lines < int(cfg.get("min_diff_lines", 10)):
            return None

    max_chars = int(cfg.get("max_diff_chars", 24000))
    if is_local:
        # Local models have small default context windows (2k–4k tokens).
        # Cap diff to ~1500 tokens so system + header + diff fit in num_ctx=8192
        # with headroom for generation. User can override max_diff_chars lower.
        max_chars = min(max_chars, 6000)
        # Inject num_ctx=8192 if the user hasn't explicitly configured it.
        # _call_llm passes this to Ollama/openai_compatible via payload["num_ctx"].
        if not cfg.get("num_ctx"):
            cfg = {**cfg, "num_ctx": 8192}   # shadow; caller's dict is unchanged

    recent = _recent_commit_subjects(repo_path, git_exe, n=5)
    ts_ctx = _fetch_commit_context(tokensave_exe, repo_path, git_exe, diff)
    if ts_ctx:
        diff = f"[Tokensave semantic context]\n{ts_ctx}\n\n[Git diff]\n{diff}"
    system, user = _build_llm_prompt(diff, recent, max_chars, grounding=grounding)
    return _call_llm(cfg, system, user, max_tokens=1500)


def _strat_llm(repo_path: str, has_source: bool,
               cfg: dict, git_exe: str,
               grounding: str = "",
               tokensave_exe: str = "") -> "tuple[str, str] | None":
    """Strategy 0: LLM (opt-in). Returns (subject, body) or None.

    `cfg` is the full manager config dict (so we can read
    `cfg["commit_message_llm"]` ourselves — keeps callers simple).
    """
    if not repo_path:
        return None
    llm_cfg = (cfg.get("commit_message_llm") or {}) if isinstance(cfg, dict) else {}
    if not llm_cfg.get("enabled"):
        return None
    raw = _call_llm_for_commit_message(llm_cfg, repo_path, git_exe,
                                       grounding=grounding,
                                       tokensave_exe=tokensave_exe)
    if not raw:
        return None
    head, _, tail = raw.partition("\n\n")
    return head, tail


def _strat_changelog(repo_path: str, files: list,
                     git_exe: str) -> "tuple[str, str] | None":
    """Strategy 1: CHANGELOG bullets. Returns (subject, body) or None."""
    if not repo_path:
        return None
    try:
        additions = _extract_changelog_additions(repo_path, git_exe)
    except Exception:
        return None
    if not additions:
        return None
    return _message_from_changelog(additions, files)


def _strat_diff(repo_path: str, files: list,
                git_exe: str) -> "tuple[str, str] | None":
    """Strategy 2: Diff content (added defs/classes, file kinds). Returns (subject, body) or None."""
    if not repo_path or not files:
        return None
    subj, body = _suggest_from_diff_content(repo_path, files, git_exe)
    return (subj, body) if subj else None


def _strat_filenames(status_text: str) -> "tuple[str, str] | None":
    """Strategy 3: File-name patterns (legacy fallback). Returns (subject, body) or None."""
    result = _suggest_from_filenames(status_text)
    return (result, "") if result else None


@dataclass
class CommitSuggestion:
    message: str
    strategy: str  # "llm" | "changelog" | "diff" | "filenames" | "backstop" | "none"


def _suggest_commit_message(repo_path: str = "", status_text: str = "",
                            cfg: dict | None = None,
                            git_exe: str = "",
                            mc=None) -> CommitSuggestion:
    """Generate a conventional-commit-style message for the staged changes.

    Multi-strategy orchestrator — tries the highest-quality strategy first
    and falls through to weaker ones on empty results. Returns a
    CommitSuggestion with message="" only if every strategy yields nothing
    AND there are no staged files at all.

    `repo_path` is optional for backwards compatibility — if empty, only
    the file-name strategy runs (it doesn't need shell access). `cfg` is
    the full manager config dict (used to read AI settings). `git_exe` is
    the git executable path; if empty, git-touching strategies skip.
    """
    files, has_source = _parse_commit_status(status_text)
    cfg = cfg or {}

    backend          = (cfg.get("commit_message_backend") or "auto").lower()
    claude_cli_exe   = cfg.get("claude_cli_exe", "")
    # raw.get returns None if the JSON key is `null`; coerce defensively.
    claude_cli_model = cfg.get("claude_cli_model") or ""
    ts_exe           = (getattr(mc, "tokensave_exe", "") or "") if mc is not None else ""

    # v4.2: build the tokensave+codegraph grounding block ONCE if the
    # master switch is on. Both LLM-shaped strategies (`_strat_llm`,
    # `_strat_claude_cli`) consume the same block; heuristics-only
    # strategies ignore it. Builds in the orchestrator (which has the
    # full ManagerConfig in scope) keeps the strategies cfg-dict-only.
    #
    # v4.6: gated by `enable_commit_grounding` (per-feature, default OFF)
    # rather than the master `enable_llm_grounding`. Live testing showed
    # that grounding hurts commit-message quality on big multi-file diffs:
    # small models like qwen2.5-coder copy recent commit subjects verbatim
    # when the prompt gets large, and Haiku CLI truncates its output.
    # Users who want grounded commit messages opt in via Settings.
    grounding = ""
    if mc is not None and getattr(mc, "enable_commit_grounding", False) and repo_path and git_exe:
        try:
            diff_for_grounding = _pending_diff(repo_path, git_exe=git_exe)
        except Exception:
            diff_for_grounding = ""
        if diff_for_grounding:
            grounding = _build_commit_grounding(
                repo_path,
                getattr(mc, "tokensave_exe", "") or "",
                getattr(mc, "codegraph_exe", "") or "",
                diff_for_grounding,
            )

    # Build strategy chain from backend setting — no if/elif cascade.
    _cli   = ("claude_cli", lambda: _strat_claude_cli(repo_path, git_exe, claude_cli_exe, claude_cli_model, grounding=grounding, tokensave_exe=ts_exe) if git_exe else None)
    _llm   = ("llm",        lambda: _strat_llm(repo_path, has_source, cfg, git_exe, grounding=grounding, tokensave_exe=ts_exe) if git_exe else None)
    _cl    = ("changelog",  lambda: _strat_changelog(repo_path, files, git_exe) if git_exe else None)
    _diff  = ("diff",       lambda: _strat_diff(repo_path, files, git_exe) if git_exe else None)
    _fnames= ("filenames",  lambda: _strat_filenames(status_text))

    _BACKEND_CHAINS: dict[str, list] = {
        "auto":       [_cli, _llm, _cl, _diff, _fnames],
        "llm_first":  [_llm, _cli, _cl, _diff, _fnames],
        "claude_cli": [_cli, _cl, _diff, _fnames],
        "llm":        [_llm, _cl, _diff, _fnames],
    }
    named_strategies = _BACKEND_CHAINS.get(backend, _BACKEND_CHAINS["auto"])
    for name, strategy in named_strategies:
        try:
            result = strategy()
        except Exception:
            continue
        if result is None:
            continue
        subj, body = _sanitize_commit_message(
            result[0], result[1], has_source_changes=has_source)
        if subj:
            return CommitSuggestion(
                message=subj + (("\n\n" + body) if body else ""),
                strategy=name,
            )

    # Generic backstop — reached only when every strategy was empty or
    # produced a filename-listing that sanitization rejected.
    if files:
        if has_source:
            scope = _dominant_directory(files)
            msg = f"refactor({scope}): update sources" if scope else "refactor: update sources"
        else:
            msg = "chore: update files"
        return CommitSuggestion(message=msg, strategy="backstop")
    return CommitSuggestion(message="", strategy="none")
