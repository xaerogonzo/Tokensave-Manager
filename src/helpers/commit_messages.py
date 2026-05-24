"""Smart commit-message generation — multi-strategy orchestrator + sanitizer.

Public entry point: ``_suggest_commit_message(repo_path, status_text, cfg, git_exe)``.

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

import os
import re
import subprocess
import textwrap
from collections import Counter

from constants import CREATE_NO_WINDOW
from helpers.llm import _build_llm_prompt, _call_llm


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
        canonical = _IMPERATIVE_REWRITES.get(first.lower())
        if canonical:
            subject = f"{prefix}{canonical}{rest}"

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


def _pending_diff(repo_path: str, *paths: str, git_exe: str,
                  lines_of_context: int = 0) -> str:
    """Return the diff between HEAD and the working tree (staged + unstaged).

    Used for commit-message suggestion BEFORE the GitCommitDialog actually
    stages files. ``git diff HEAD`` captures everything that would land in
    the commit if the user stages and commits all working-tree changes.

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
    return proc.stdout if proc.returncode == 0 else ""


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
        # Skip diff metadata
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("@@"):
            # Hunk boundary — section context from a different hunk is no
            # longer reliable. Reset so we don't misattribute the next bullet.
            section = None
            current_bullet = None
            continue

        # Classify the line: + (added), - (removed), space (context)
        if raw_line.startswith("+"):
            line = raw_line[1:]
            is_added = True
        elif raw_line.startswith(" "):
            line = raw_line[1:]
            is_added = False
        elif raw_line.startswith("-"):
            continue   # ignore removed lines for section/bullet tracking
        else:
            # Should not happen for unified diff output
            continue

        # Section header: ### Added, ### Changed, etc.
        # Track from BOTH context and added lines so a bullet added under an
        # existing section header still gets the right section attribution.
        m_section = re.match(r"^#{2,4}\s+(\w+)\s*$", line)
        if m_section:
            section = m_section.group(1)
            current_bullet = None
            continue

        # Only PROCESS added lines for bullets — context lines just inform section.
        if not is_added:
            continue

        # New bullet starting with "- "
        m_bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m_bullet and section:
            text = m_bullet.group(1)
            # Bolded lead-in pattern: handles
            #   **lead** — desc       (en-dash, em-dash, hyphen, colon)
            #   **lead.** desc        (period INSIDE the bold; no separator after)
            #   **lead** desc         (no separator at all)
            m_bold = re.match(
                r"^\*\*(?P<lead>[^*]+?)\*\*\s*(?:[—\-–:]\s*)?(?P<desc>.+)$",
                text,
            )
            if m_bold:
                lead = m_bold.group("lead").strip().rstrip(".").strip()
                desc = m_bold.group("desc").strip()
            else:
                # Fall back to first 60 chars or first sentence as lead
                first_sent = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
                lead = (first_sent[:60].rstrip() if len(first_sent) > 60
                        else first_sent).rstrip(".")
                desc = text[len(first_sent):].strip() or text
            current_bullet = {"section": section, "lead_in": lead,
                              "description": desc}
            bullets.append(current_bullet)
            continue

        # Continuation of a previous bullet (no leading dash)
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
        return "docs: update documentation", ""

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
        return "docs: update documentation"
    if exts <= code_exts:
        if len(basenames) <= 3:
            return f"chore: update {', '.join(basenames)}"
        return f"chore: update {len(files)} source files"
    if has_del and not has_add:
        return f"chore: remove {len(files)} files"
    if len(basenames) <= 2:
        return f"chore: update {', '.join(basenames)}"
    return f"chore: update {basenames[0]}, {basenames[1]} + {len(basenames) - 2} more"


# ── LLM strategy + orchestrator entry point ──────────────────────────────────

def _call_llm_for_commit_message(cfg: dict, repo_path: str, git_exe: str) -> str | None:
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
    diff_lines = diff.count("\n")
    if diff_lines < int(cfg.get("min_diff_lines", 30)):
        return None

    max_chars = int(cfg.get("max_diff_chars", 24000))
    recent    = _recent_commit_subjects(repo_path, git_exe, n=5)
    system, user = _build_llm_prompt(diff, recent, max_chars)
    return _call_llm(cfg, system, user, max_tokens=1500)


def _strat_llm(repo_path: str, has_source: bool,
               cfg: dict, git_exe: str) -> "tuple[str, str] | None":
    """Strategy 0: LLM (opt-in). Returns (subject, body) or None.

    `cfg` is the full manager config dict (so we can read
    `cfg["commit_message_llm"]` ourselves — keeps callers simple).
    """
    if not repo_path:
        return None
    llm_cfg = (cfg.get("commit_message_llm") or {}) if isinstance(cfg, dict) else {}
    if not llm_cfg.get("enabled"):
        return None
    raw = _call_llm_for_commit_message(llm_cfg, repo_path, git_exe)
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


def _suggest_commit_message(repo_path: str = "", status_text: str = "",
                            cfg: dict | None = None,
                            git_exe: str = "") -> str:
    """Generate a conventional-commit-style message for the staged changes.

    Multi-strategy orchestrator — tries the highest-quality strategy first
    and falls through to weaker ones on empty results. Returns "" only if
    every strategy yields nothing AND there are no staged files at all.

    `repo_path` is optional for backwards compatibility — if empty, only
    the file-name strategy runs (it doesn't need shell access). `cfg` is
    the full manager config dict (used to read AI settings). `git_exe` is
    the git executable path; if empty, git-touching strategies skip.
    """
    files, has_source = _parse_commit_status(status_text)
    cfg = cfg or {}

    strategies = [
        lambda: _strat_llm(repo_path, has_source, cfg, git_exe) if git_exe else None,
        lambda: _strat_changelog(repo_path, files, git_exe) if git_exe else None,
        lambda: _strat_diff(repo_path, files, git_exe) if git_exe else None,
        lambda: _strat_filenames(status_text),
    ]
    for strategy in strategies:
        try:
            result = strategy()
        except Exception:
            continue
        if result is None:
            continue
        subj, body = _sanitize_commit_message(
            result[0], result[1], has_source_changes=has_source)
        if subj:
            return subj + (("\n\n" + body) if body else "")

    # Generic backstop — reached only when every strategy was empty or
    # produced a filename-listing that sanitization rejected.
    if files:
        if has_source:
            scope = _dominant_directory(files)
            return f"refactor({scope}): update sources" if scope else "refactor: update sources"
        return "chore: update files"
    return ""
