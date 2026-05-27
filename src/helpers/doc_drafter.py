"""Doc-update drafter logic — pure functions called by the doc-drafter dialog.

Responsibilities:
  - **Commit-range resolution**: turn a high-level mode ("since last doc
    commit", "since last release", "custom") into a concrete git range +
    list of commits.  Handles the mixed-commit edge case (a commit that
    touched BOTH code and docs in the same commit).
  - **Prompt building**: per-target (CHANGELOG / README) system + user
    prompts that include the commit list, the existing target-section
    content, optional CLAUDE.md blueprint context, and sparse-commit
    hints when the user's commit messages are too short to be useful.
  - **LLM dispatch**: routes to ``call_claude_cli_print`` or
    ``_call_llm`` based on the provider in ``llm_cfg``.

Pure-function module — no Tkinter.  Safe to call from any thread.
"""

from __future__ import annotations

import os
import re
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.release import (
    _commits_since, _last_release_tag,
)


# Doc files whose modifications mark a commit as "documented".  Used by
# "since last doc commit" mode.  Deliberately narrow: only CHANGELOG.md and
# README.md count as "doc anchors" so that adding new docs/ files (e.g.
# AGENT_BACKENDS.md, ARCHITECTURE.md) doesn't accidentally mask unrecorded
# code commits by moving the anchor forward to HEAD.
_DOC_PATHSPECS = ["CHANGELOG.md", "README.md"]

# Average commit-subject character length below which we trigger the
# sparse-commit safety net (extra prompt hint + changed-file path list).
_SPARSE_AVG_THRESHOLD = 15


# ── Prompt-output STOP marker + shared rules ─────────────────────────────────
#
# Every system prompt asks the model to emit `<<<END_OF_DRAFT>>>` as its final
# line. The post-processor truncates the draft at that marker so trailing
# prose ("These bullets capture...", "In summary..."), echo-loops, and
# rambling explanations are stripped before the filter runs. The marker is
# intentionally a triple-bracketed multi-word token so that legitimate
# documentation referencing `[end]` in prose or code samples cannot
# false-positive trigger truncation.

# Case-insensitive + multiline. Tolerates surrounding markdown decoration
# (heading hash, bold, blockquote prefix). Whole-line match — the marker must
# be alone on its line (possibly with decoration) to count.
_END_MARKER_RE = re.compile(
    r"(?im)^[\s>#*_`]*<<<\s*end[_\s]of[_\s]draft\s*>>>[\s.>#*_`]*$"
)


def _strip_end_marker(text: str) -> str:
    """Truncate ``text`` at the first ``<<<END_OF_DRAFT>>>`` marker.

    Also drops a dangling unclosed code fence if the truncation lands inside
    an open ``` block — appending a closing fence at root indentation would
    risk corrupting nested-block layouts (a blockquote-internal fence
    appended without ``> `` prefix would render as a separate sibling block).
    Dropping the half-open fence is the conservative choice.
    """
    if not text:
        return text
    m = _END_MARKER_RE.search(text)
    if not m:
        return text
    truncated = text[:m.start()].rstrip()
    if truncated.count("```") % 2 == 1:
        # Odd fence count → there's an open fence we can't close cleanly.
        # Cut everything from the unclosed fence onward.
        last_fence = truncated.rfind("```")
        if last_fence >= 0:
            line_start = truncated.rfind("\n", 0, last_fence)
            truncated = truncated[:line_start].rstrip() if line_start >= 0 else ""
    return truncated


# Shared rule blocks appended to every _*_SYSTEM constant. Centralising them
# here keeps the six prompts in lockstep — a single edit to either rule
# propagates to all drafters.
#
# Tone: 2026 prompt research shows that aggressive caps ("CRITICAL!",
# "(MANDATORY)", "YOU MUST", "NEVER EVER") overtriggers newer Claude models
# and produces worse output. Calm direct prose works better. We also frame
# positively where possible — "Don't X" forces the model to process X
# first (the "Pink Elephant" problem). These blocks therefore read as
# descriptions of the execution environment, not warnings.
_STOP_MARKER_RULE = (
    "\n\nTermination:\n"
    "When your output is complete, write `<<<END_OF_DRAFT>>>` on its own "
    "line. The post-processor strips this marker and anything past it, so "
    "leave nothing of value after it."
)

_ANTI_FABRICATION_RULE = (
    "\n\nGrounding:\n"
    "Cite only details that appear in the commit subjects, classified "
    "changes, or changed-file paths in the user prompt. Generic-but-"
    "accurate is better than detailed-but-fabricated. Symbol names in "
    "backticks must be derivable from the changed files — if you can't "
    "trace a name back to a path, drop the citation rather than guess."
)

_STATELESS_FILTER_RULE = (
    "\n\nExecution context:\n"
    "Your output is consumed by a markdown patcher script. The user prompt "
    "is the complete input — produce the requested output in one pass. "
    "Start writing the markdown content immediately; begin with the leading "
    "characters the output-format section above specifies (e.g. `## `)."
)


# ── Commit-range resolution ──────────────────────────────────────────────────

def _last_doc_commit_sha(project_path, git_exe, pathspecs=None):
    """Return the SHA of the most recent commit that touched the given paths.

    ``pathspecs`` overrides the module-level ``_DOC_PATHSPECS`` so each
    DocType can use its own anchor file(s) instead of the shared fallback.
    """
    specs = pathspecs if pathspecs else _DOC_PATHSPECS
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "log", "-n", "1",
             "--pretty=format:%H",
             "--diff-filter=AM", "--", *specs],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _commit_touches_code(project_path, sha, git_exe):
    """Return True if a commit touched any file OUTSIDE the doc set.

    A commit that only touched CHANGELOG / README / docs/ is pure-doc.
    A commit that ALSO touched code files is "mixed" — we want to INCLUDE
    it in the range so its code changes are visible to the drafter, even
    though its doc changes are already documented.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "show", "--name-only",
             "--pretty=format:", sha],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    files = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    for f in files:
        if f == "CHANGELOG.md" or f == "README.md":
            continue
        if f.startswith("docs/"):
            continue
        return True
    return False


def resolve_commit_range(project_path, mode, custom_ref, git_exe, pathspecs=None):
    """Resolve a commit range from a high-level mode to a concrete spec + commits.

    Args:
        project_path: Absolute path to the project repo.
        mode:         One of ``"since_last_doc"``, ``"since_last_commit"``,
                      ``"since_last_tag"``, ``"custom"``.
        custom_ref:   Used only when ``mode == "custom"``.  May be a single
                      ref (treated as ``<ref>..HEAD``) or a full ``A..B`` range.
        git_exe:      Path to git executable.

    Returns dict::

        {
            "range_label":     "Since last doc commit (abc123)",  # for UI / prompt
            "range_spec":      "abc123..HEAD",                    # git rev range
            "commits":         [{"hash": ..., "subject": ..., "body": ...}],
            "boundary_mixed":  True/False,
            "boundary_note":   str | None,
        }

    On any failure (no commits, malformed mode, git failure) returns dict
    with empty ``commits`` and a ``range_label`` describing the issue.
    """
    if mode == "since_last_doc":
        return _resolve_since_last_doc(project_path, git_exe, pathspecs)
    if mode == "since_last_commit":
        return {
            "range_label":    "Since last commit (HEAD~1..HEAD)",
            "range_spec":     "HEAD~1..HEAD",
            "commits":        _commits_since(project_path, "HEAD~1", git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    if mode == "since_last_tag":
        return _resolve_since_last_tag(project_path, git_exe)
    if mode == "custom":
        return _resolve_custom(project_path, custom_ref, git_exe)
    return {
        "range_label":    f"Unknown mode: {mode!r}",
        "range_spec":     "",
        "commits":        [],
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _resolve_since_last_doc(project_path, git_exe, pathspecs=None) -> dict:
    """Range resolver for mode='since_last_doc'."""
    sha = _last_doc_commit_sha(project_path, git_exe, pathspecs)
    if not sha:
        return {
            "range_label":    "All commits (no prior doc commit found)",
            "range_spec":     "HEAD",
            "commits":        _commits_since(project_path, None, git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    # Mixed-commit handling: if the boundary commit touched code as well
    # as docs, INCLUDE it in the range using ``<sha>^..HEAD`` so its
    # code changes don't get lost.  Pure-doc boundary just uses ``..``.
    mixed = _commit_touches_code(project_path, sha, git_exe)
    if mixed:
        range_spec = f"{sha}^..HEAD"
        note = (
            f"Note: commit {sha[:8]} is a boundary commit with mixed "
            "content — it already includes its own doc updates.  "
            "Reference its CODE changes only when drafting; do NOT "
            "re-describe what its doc diff already said."
        )
    else:
        range_spec = f"{sha}..HEAD"
        note = None
    return {
        "range_label":    f"Since last doc commit ({sha[:8]})",
        "range_spec":     range_spec,
        "commits":        _commits_since(project_path,
                                         sha if not mixed else f"{sha}^",
                                         git_exe),
        "boundary_mixed": mixed,
        "boundary_note":  note,
    }


def _resolve_since_last_tag(project_path, git_exe) -> dict:
    """Range resolver for mode='since_last_tag'."""
    tag = _last_release_tag(project_path, git_exe)
    if not tag:
        return {
            "range_label":    "All commits (no release tag found)",
            "range_spec":     "HEAD",
            "commits":        _commits_since(project_path, None, git_exe),
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    return {
        "range_label":    f"Since last release tag ({tag})",
        "range_spec":     f"{tag}..HEAD",
        "commits":        _commits_since(project_path, tag, git_exe),
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _resolve_custom(project_path, custom_ref, git_exe) -> dict:
    """Range resolver for mode='custom'."""
    ref = (custom_ref or "").strip()
    if not ref:
        return {
            "range_label":    "Custom range (empty)",
            "range_spec":     "",
            "commits":        [],
            "boundary_mixed": False,
            "boundary_note":  None,
        }
    # Single ref → treat as <ref>..HEAD; already A..B → pass through.
    if ".." not in ref:
        range_spec = f"{ref}..HEAD"
        commits = _commits_since(project_path, ref, git_exe)
    else:
        range_spec = ref
        commits = _commits_in_range(project_path, range_spec, git_exe)
    return {
        "range_label":    f"Custom range ({range_spec})",
        "range_spec":     range_spec,
        "commits":        commits,
        "boundary_mixed": False,
        "boundary_note":  None,
    }


def _commits_in_range(project_path, range_spec, git_exe):
    """Variant of `_commits_since` that takes an arbitrary `A..B` range.

    Needed for the "custom range" mode where the user may pass a
    non-HEAD endpoint (e.g. ``main..feature/foo``).
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "log", range_spec,
             "--pretty=format:%H%x09%s%x09%b%x1f"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits = []
    for record in proc.stdout.split("\x1f"):
        if not record.strip():
            continue
        record = record.lstrip("\n")
        parts = record.split("\x09", 2)
        if len(parts) < 2:
            continue
        commits.append({
            "hash":    parts[0],
            "subject": parts[1] if len(parts) >= 2 else "",
            "body":    parts[2] if len(parts) >= 3 else "",
        })
    return commits


# ── Context gathering ───────────────────────────────────────────────────────

def changed_file_paths(project_path, range_spec, git_exe):
    """Return the deduped list of file paths changed in ``range_spec``.

    Used when sparse-commit mode triggers, so the LLM gets explicit
    structural signal beyond just commit subjects.  Capped at 60 paths.
    """
    if not range_spec:
        return []
    try:
        proc = subprocess.run(
            [git_exe, "-C", project_path, "diff", "--name-only",
             range_spec],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    seen = []
    for ln in proc.stdout.splitlines():
        p = ln.strip()
        if p and p not in seen:
            seen.append(p)
        if len(seen) >= 60:
            break
    return seen


def read_blueprint_context(project_path, max_chars=1500):
    """Return the first ``max_chars`` of CLAUDE.md or BASIC_INSTRUCTIONS.md.

    Tries ``CLAUDE.md`` first (Claude Code convention), then
    ``BASIC_INSTRUCTIONS.md`` (this manager's convention).  Returns the
    empty string if neither exists or both are unreadable.

    Capped at ``max_chars`` so the prompt doesn't bloat — the goal is a
    structural map for the model, not the full document.
    """
    for name in ("CLAUDE.md", "BASIC_INSTRUCTIONS.md"):
        p = os.path.join(project_path, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read(max_chars + 1)
        except OSError:
            continue
        if text:
            return text[:max_chars]
    return ""


def is_sparse(commits, threshold=_SPARSE_AVG_THRESHOLD):
    """Return True if the average commit-subject length is under ``threshold``.

    Used by the prompt builders to inject the sparse-commit safety hint
    + explicit changed-file list.
    """
    if not commits:
        return False
    subjects = [c.get("subject", "").strip() for c in commits]
    subjects = [s for s in subjects if s]
    if not subjects:
        return False
    avg = sum(len(s) for s in subjects) / len(subjects)
    return avg < threshold


# ── Prompt builders ─────────────────────────────────────────────────────────

_CHANGELOG_SYSTEM = (
    "You are a CHANGELOG.md maintainer.  Draft NEW Keep-a-Changelog "
    "bullets for the provided commit range.  The patcher will APPEND "
    "these bullets to the existing [Unreleased] sub-sections — your "
    "output ADDS to what's already there; it does NOT replace it.\n\n"
    "Output format (exactly this — nothing else):\n\n"
    "### Added\n"
    "- bullet 1\n"
    "- bullet 2\n\n"
    "### Fixed\n"
    "- bullet 3\n\n"
    "### Changed\n"
    "- bullet 4\n\n"
    "Rules:\n"
    "- Output ONLY ### Added / ### Changed / ### Fixed / ### Removed "
    "headers followed by bullets.  Omit any section with no new bullets.\n"
    "- If a section has NO new bullets to add, OMIT THE ENTIRE `### Section` "
    "HEADER.  Do NOT write `- None`, `- N/A`, `- TBD`, `- nothing to add`, "
    "or any other placeholder under an empty header.  An ABSENT section is "
    "the correct signal for 'nothing changed here' — a placeholder bullet "
    "is NOT.\n"
    "- No '## [Unreleased]' line, no prose preamble, no markdown code "
    "fences, no trailing explanation.\n"
    "- Output ONLY bullets describing the provided commit range.  Do NOT "
    "echo, summarise, restate, or re-describe bullets already in the "
    "'Current [Unreleased] content' below — the patcher keeps all of "
    "those VERBATIM.  Repeating them creates duplicates.\n"
    "- If the provided commits don't introduce anything not already in "
    "the current [Unreleased] content, output nothing.\n"
    "- Each bullet uses conventional-commit scope prefix in parens, e.g. "
    "'(gitignore-dialog) 🤖 AI Suggest button — one-click pattern recs'.\n"
    "- Cite file paths and helper names in backticks.  No marketing fluff.\n"
    "- AIM for ONE bullet per distinct code change.  A commit that touches "
    "three subsystems likely warrants three bullets, not one summary.  If "
    "the commit range spans N commits, target 3-8 bullets per ### section "
    "unless the changes are genuinely trivial.  HIDING multiple shipped "
    "changes behind one generic bullet ('add filtering and sanitisation') "
    "is a documentation failure.\n"
    "- CITE specific file paths and helper/class names in backticks. "
    "'add bullet-quality filters for truncation' is too generic. "
    "'`_looks_truncated` stop-word heuristic in `dialogs/doc_drafter.py`' "
    "is the correct level of detail.\n"
    "- When the commit range spans MULTIPLE distinct features (e.g. a "
    "truncation fix AND a redundancy filter AND a CI cleanup), produce "
    "separate bullets for each feature — do NOT collapse them into one."
) + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE

_README_SYSTEM = (
    "You are a README.md maintainer.  Draft ONE bold sub-section for the "
    "'Recent highlights (Unreleased)' block, describing only what the "
    "provided commit range actually changes.\n\n"
    "Output format (exactly this — nothing else):\n"
    "  **<sub-section header>**\n"
    "  - bullet 1\n"
    "  - bullet 2\n"
    "  - …\n\n"
    "The patcher will SPLICE this sub-section into the highlights block.  "
    "If a sub-section with the same header already exists, its bullets "
    "are REPLACED.  Otherwise the new sub-section is inserted at the top "
    "of the highlights block.\n\n"
    "Rules:\n"
    "- ⚠ LOAD-BEARING RULE — preserve all existing sub-section bullets "
    "VERBATIM.  The patcher REPLACES the entire sub-section, so omitting "
    "a bullet DELETES it from the file.  If a matching sub-section "
    "already exists in the current body, ALL of its existing bullets "
    "MUST appear in your output verbatim, BEFORE the new bullets you "
    "add.  A code-side safety check (mirror-contract enforcement) "
    "hard-rejects any draft that drops more than 25% of existing "
    "bullets — your draft will NOT be applied if you break this rule.\n"
    "- Output exactly ONE sub-section.  No surrounding anchor line, no "
    "`---` separator, no other sub-sections, no prose preamble, no "
    "explanations, no markdown code fences.\n"
    "- The first line MUST be the bold header.  Inspect the 'Candidate "
    "sub-section headers' list in the user prompt below.  If any "
    "candidate thematically matches your new bullets (e.g. continuing "
    "an ongoing roadmap effort), output that candidate VERBATIM — "
    "copy it character-for-character including emoji and dash style.  "
    "When no candidate fits, create a NEW header using a CONCRETE "
    "roadmap number from context (e.g. `**Roadmap-6 — short topic**` "
    "or `**Roadmap-7 — short topic**`), or `**Feature area — short "
    "topic**` for non-roadmap work.\n"
    "- Bullets are verb-first, technical detail in parens (file paths, "
    "helper names, threshold values).  No marketing fluff.  Match the "
    "voice of bullets in the 'Current highlights body' below.\n"
    "- Use the project's existing emoji prefix when obvious (🤖 for AI, "
    "🔍 for review, 🔌 for MCP, 🦙 for Ollama, ⚙ for settings, etc.).\n"
    "- For the NEW bullets you ADD beyond the existing sub-section "
    "content, AIM for ONE bullet per distinct shipped feature.  A commit "
    "range that spans three features likely warrants three new bullets, "
    "not one summary mega-bullet ('updates — feature A, feature B, "
    "feature C').  If the commit range spans N distinct features, target "
    "N new bullets.\n"
    "- CITE specific file paths and helper/class names in backticks. "
    "'doc-drafter updates — backend dropdown, noop filtering, dedup' is "
    "too generic.  'backend override dropdown (`_backend_override_var` "
    "in `dialogs/doc_drafter.py`)' is the correct level of detail.\n"
    "- When the commit range spans MULTIPLE distinct features (a UI "
    "dropdown AND a new filter AND a CI cleanup), produce SEPARATE new "
    "bullets for each — do NOT collapse them into one comma-separated "
    "mega-bullet.  Existing bullets stay verbatim; new bullets get the "
    "per-feature treatment."
) + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE


# Phase 2.0 — sub-section header detection for the prompt's candidate-list
# block.  Whitespace tolerance before the optional colon catches manually-
# edited `**Header** :` forms.  Indented `  **Header**  ` also matches.
# Mid-line bold inside a paragraph does NOT match (entire line must be
# the bold span — anchored with `^\s*` and `\s*:?\s*$`).
#
# Used ONLY for prompt enumeration.  The apply-time matching stays with
# `_normalise_subheader` in `helpers/readme_patch.py` — that's the single
# source of truth for "does this header match an existing sub-section".
_SUBSECTION_HEADER_RE = re.compile(
    r"^\s*(\*\*[^\n*][^\n]*\*\*)\s*:?\s*$"
)


def _extract_subsection_headers(highlights_body):
    """Return list of bold-header lines from a highlights body, verbatim.

    Used by `build_readme_prompt` to give the model an explicit menu of
    headers it can reuse character-for-character — small models follow
    enumerated candidate lists far better than they follow "look at the
    body and infer" instructions.

    Returns the headers with their ``**`` markers intact.  ATX
    ``### Header`` style is NOT supported (project convention is bold
    sub-headers); if a future project mixes formats, that's a Roadmap-7
    cross-format support item.
    """
    if not highlights_body:
        return []
    headers = []
    for line in highlights_body.splitlines():
        m = _SUBSECTION_HEADER_RE.match(line)
        if m:
            headers.append(m.group(1))
    return headers


def _render_commit_list(classified):
    """Render the classified-commits dict as a markdown list for the prompt."""
    lines = []
    for section, items in classified.items():
        if not items:
            continue
        lines.append(f"### {section}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()


# Scope-prefix pattern for CHANGELOG-style bullets, e.g.
# "- (doc-drafter) ..." → captures "doc-drafter". {1,30} bounds the scope to
# a reasonable identifier length; the longest scope in this project is
# `gitignore-dialog` (16 chars).
_SCOPE_PREFIX_RE = re.compile(r"^\s*[-*]\s+\(([^)]{1,30})\)")


def _extract_scope_prefixes(bullets, limit=12):
    """Pull unique `(scope-name)` prefixes from a list of bullets.

    Returning prefixes (not full bullets) lets the model see what scope
    vocabulary the project uses without anchoring on the specific symbols
    or wording inside the bullets themselves. Local 14B-class models tend
    to copy semantic content from any sample bullets shown — leaking the
    bullets themselves causes context contamination that an explicit
    "do not echo" instruction cannot reliably prevent.
    """
    seen = []
    for b in bullets:
        m = _SCOPE_PREFIX_RE.match(b)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
            if len(seen) >= limit:
                break
    return seen


def _summarise_existing_headings(existing_text, max_levels=3):
    """Return an indented list of headings present in `existing_text`, up to
    `max_levels` deep. Used by build_architecture_prompt /
    build_roadmap_prompt / build_generic_doc_prompt to give the model
    section-structure context without dumping the full file body.

    Level-1 (`# `) is skipped (document title); level-(max_levels+1) and
    deeper are skipped (over-detailed for the navigation purpose).
    """
    lines = []
    for ln in (existing_text or "").splitlines():
        stripped = ln.strip()
        if not stripped.startswith("#"):
            continue
        n = len(stripped) - len(stripped.lstrip("#"))
        if 2 <= n <= max_levels:
            indent = "  " * (n - 2)
            lines.append(f"  {indent}- {stripped}")
    if not lines:
        return "  (no existing sections)"
    return "\n".join(lines)


# ── Theme C: three-signal candidate section selector ────────────────────────
#
# Picks the existing `## ` sections most likely affected by the commit range
# so the prompt only includes their bodies (not the entire file). Three token
# sources:
#
#   1. Path tokens — file basenames + parent dirs from changed_files
#   2. Scope prefixes — `(scope-name)` extracted from commit subjects, with
#      hyphen→underscore variant generation (catches both "doc-drafter" and
#      "doc_drafter" forms in section text)
#   3. Significant subject words — ≥4 chars, stopword-filtered tokens from
#      commit subjects (catches conceptual hits in architecture docs that
#      describe systems by name rather than by file path)

_PATH_TOKEN_STOPWORDS = {"src", "lib", "app", "source", "init", "main"}
_SUBJECT_TOKEN_STOPWORDS = {
    "feat", "fix", "chore", "docs", "refactor", "perf", "test", "build", "ci",
    "the", "and", "for", "with", "from", "into", "this", "that", "when",
    "where", "what", "how", "add", "use", "make", "new", "remove",
    "update", "change", "fixed", "adds", "uses",
}


def _path_tokens(changed_files):
    """Tokens drawn from changed-file paths: basename(no ext), parent dir."""
    out = set()
    for p in changed_files or []:
        p = p.replace("\\", "/")
        parts = [x for x in p.split("/") if x]
        if not parts:
            continue
        base = parts[-1].rsplit(".", 1)[0]
        if base and base.lower() not in _PATH_TOKEN_STOPWORDS:
            out.add(base.lower())
            # Hyphen-variant for cross-style matching
            if "_" in base:
                out.add(base.lower().replace("_", "-"))
        if len(parts) >= 2:
            parent = parts[-2]
            if parent.lower() not in _PATH_TOKEN_STOPWORDS:
                out.add(parent.lower())
    return out


def _subject_tokens(commits):
    """Significant words from commit subjects + scope prefixes (both
    hyphen and underscore variants)."""
    out = set()
    for c in commits or []:
        subj = (c.get("subject") if isinstance(c, dict) else str(c)) or ""
        m = _SCOPE_PREFIX_RE.match("- " + subj)
        if m:
            scope = m.group(1).lower()
            out.add(scope)
            out.add(scope.replace("-", "_"))
            out.add(scope.replace("_", "-"))
        for tok in re.findall(r"\b\w{4,}\b", subj.lower()):
            if tok not in _SUBJECT_TOKEN_STOPWORDS:
                out.add(tok)
    return out


def _split_into_sections(text):
    """Split markdown text into [(title, body), ...] tuples on `## ` boundaries.

    Title is the line after `## ` (stripped). Body is everything from the
    line after the heading up to (but not including) the next `## ` heading
    or EOF. Body trailing whitespace is preserved as-is to round-trip cleanly
    with downstream patchers.
    """
    if not text:
        return []
    sections = []
    # finditer-based — non-greedy with lookahead to the next ## heading or EOF
    pattern = re.compile(
        r"(?ms)^##\s+(?P<title>[^\n]+)\n(?P<body>.*?)(?=^##\s|\Z)"
    )
    for m in pattern.finditer(text):
        sections.append((m["title"].strip(), m["body"].rstrip("\n")))
    return sections


def _select_candidate_sections(existing_text, changed_files, commits,
                                max_candidates=5, max_body_chars=8000):
    """Return list of (title, body) pairs scored by combined-token overlap
    with the section's title AND body. Three-signal token set (path, scope,
    subject words) handles both file-rooted documentation and concept-rooted
    documentation.

    Sections are scored by hit count across title + body. Title hits weigh
    3× body hits because matching a section's NAME is a much stronger
    signal than incidental body mentions.

    Fallback: if no section scores > 0, return the top `max_candidates`
    sections by raw size (largest first) so the model has substantive
    content. Body-char budget caps cumulative body size to bound prompt
    width — drops the lowest-scoring trailing candidates first.
    """
    sections = _split_into_sections(existing_text)
    if not sections:
        return []

    tokens = _path_tokens(changed_files) | _subject_tokens(commits)

    if not tokens:
        # No signal — fall back to top-K by size
        ordered = sorted(sections, key=lambda tb: -len(tb[1]))[:max_candidates]
    else:
        scored = []
        for title, body in sections:
            tl, bl = title.lower(), body.lower()
            title_hits = sum(1 for t in tokens if t in tl)
            body_hits  = sum(1 for t in tokens if t in bl)
            score = title_hits * 3 + body_hits
            if score > 0:
                scored.append((score, title, body))
        if scored:
            scored.sort(key=lambda t: -t[0])
            ordered = [(t, b) for _, t, b in scored[:max_candidates]]
        else:
            # No token alignment — fall back to top-K by size
            ordered = sorted(sections, key=lambda tb: -len(tb[1]))[:max_candidates]

    # Body-char budget
    out, total = [], 0
    for title, body in ordered:
        if total + len(body) > max_body_chars and out:
            break
        out.append((title, body))
        total += len(body)
    return out


def build_changelog_prompt(commits, classified, existing_unreleased,
                           project_name, project_desc,
                           changed_files, boundary_note):
    """Return (system_prompt, user_prompt) for the CHANGELOG tab.

    Existing [Unreleased] content is summarised as section name + bullet
    count + scope-prefix vocabulary, NOT dumped verbatim. The patcher
    handles dedup against on-disk content; the model only needs to know
    which section headers exist and what scope-prefix style to match.
    """
    from helpers.changelog_patch import read_section_bullets_from_text
    system = _CHANGELOG_SYSTEM
    parts = [
        f"Project: {project_name}",
    ]
    if project_desc:
        parts.append(f"Project description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    if commits:
        parts.append("Commits in range (use these to infer per-change "
                     "granularity — produce roughly one bullet per "
                     "distinct change, not one summary per topic):")
        for c in commits:
            sha = (c.get("hash") or "")[:8]
            subj = (c.get("subject") or "").strip()
            parts.append(f"  {sha}  {subj}")
        parts.append("")
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
    parts.append("")
    if changed_files:
        parts.append("[Changed files] (repo-root-relative paths):")
        for p in changed_files:
            parts.append(f"  {p}")
        parts.append("")

    # U2-CHANGELOG: section-summary + scope-prefix vocab, NOT a verbatim
    # dump of the existing [Unreleased] body. Dumping the full body would
    # invite the model to use it as a template and re-emit existing bullets.
    summary_lines = []
    all_existing_bullets = []
    for sec in ("Added", "Changed", "Fixed", "Removed"):
        ebs = read_section_bullets_from_text(existing_unreleased or "", sec)
        if not ebs:
            continue
        summary_lines.append(
            f"  ### {sec}: {len(ebs)} existing bullet(s) "
            "(patcher preserves verbatim — do NOT echo)"
        )
        all_existing_bullets.extend(ebs)

    if summary_lines:
        parts.append("Existing [Unreleased] sections (patcher will preserve "
                     "verbatim; your job is ONLY to add NEW bullets for the "
                     "commits above — do NOT repeat or paraphrase any "
                     "existing content):")
        parts.extend(summary_lines)
        scopes = _extract_scope_prefixes(all_existing_bullets)
        if scopes:
            parts.append("")
            parts.append("Scope prefixes used in this project — match this "
                         "vocabulary for your NEW bullets (pick the existing "
                         "one when applicable; only invent a new scope if "
                         "none fits):")
            parts.append("  " + ", ".join(f"({s})" for s in scopes))
    else:
        parts.append("[Unreleased] is currently empty — add the first bullets.")

    if is_sparse(commits):
        parts.append("")
        parts.append(
            "The provided commit subjects are unusually concise.  Infer the "
            "actual change scope from the changed file paths below "
            "(e.g. .py files in src/dialogs/ imply UI dialog logic).  Do NOT "
            "echo a terse commit subject verbatim — describe what changed."
        )
        if changed_files:
            parts.append("")
            parts.append("[Changed files]")
            for p in changed_files:
                parts.append(f"- {p}")

    return system, "\n".join(parts)


def build_readme_prompt(commits, classified, existing_highlights,
                        project_name, project_desc,
                        changed_files, boundary_note):
    """Return (system_prompt, user_prompt) for the README tab.

    ``existing_highlights`` is the current body of the 'Recent highlights'
    block (output of ``helpers.readme_patch.read_highlights``).
    """
    system = _README_SYSTEM
    parts = [
        f"Project: {project_name}",
    ]
    if project_desc:
        parts.append(f"Project description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    # Per-commit subject summary — mirrors the CHANGELOG prompt.  Gives the
    # model a per-commit view so it produces per-feature new bullets instead
    # of a single mega-bullet collapsing the whole range.
    if commits:
        parts.append("Commits in range (each represents a distinct feature "
                     "or fix — produce roughly one NEW bullet per commit, "
                     "do NOT collapse multiple commits into a single "
                     "comma-separated summary bullet):")
        for c in commits:
            sha = (c.get("hash") or "")[:8]
            subj = (c.get("subject") or "").strip()
            parts.append(f"  {sha}  {subj}")
        parts.append("")
    parts.append("Commits classified by conventional-commit prefix:")
    parts.append("")
    parts.append(_render_commit_list(classified) or "(no commits classified)")
    parts.append("")
    # ALWAYS include the changed-file list (Phase 1.8 — mirrors CHANGELOG).
    # Knowing exactly which files were touched lets the model produce
    # per-file granular new bullets even when commit subjects are detailed.
    if changed_files:
        parts.append("[Changed files] (repo-root-relative paths):")
        for p in changed_files:
            parts.append(f"  {p}")
        parts.append("")
    # Phase 2.0 — explicit candidate-header menu.  Small models follow
    # enumerated lists FAR better than they follow "look at the body and
    # infer the right header" instructions.  Reduces frequency of the
    # `Roadmap-N` literal-placeholder hallucination by giving the model a
    # concrete set of headers to choose from.  Hint only — the apply path
    # does NOT consult this list; `_normalise_subheader` in readme_patch.py
    # remains the single source of truth for header matching.
    existing_headers = _extract_subsection_headers(existing_highlights)
    if existing_headers:
        parts.append("Candidate sub-section headers from current highlights "
                     "body (REUSE one character-for-character if your bullets "
                     "thematically belong; otherwise create a new header "
                     "with a concrete roadmap number like Roadmap-6 or "
                     "Roadmap-7):")
        for h in existing_headers:
            parts.append(f"  {h}")
        parts.append("")
    # F2b: explicit MIRROR-CONTRACT framing instead of generic "current body"
    # dump. README's patcher REPLACES the entire matched sub-section, so
    # omitting a bullet DELETES it from the file — the existing-bullet echo
    # is structurally required, not optional. Spelling out the contract
    # explicitly raises compliance vs. asking the model to reverse-engineer
    # the requirement from a free-form body dump.
    if existing_highlights and existing_highlights.strip():
        parts.append("MIRROR-CONTRACT — when you pick a candidate sub-section "
                     "header above to extend, your output for that header MUST "
                     "start with the EXACT existing bullets shown below for "
                     "that sub-section, character-for-character VERBATIM and "
                     "IN THE SAME ORDER. Then add your NEW bullets BELOW the "
                     "existing ones. A code-side mirror-contract check "
                     "REJECTS any draft that drops more than 25% of the "
                     "existing bullets in the matched sub-section. The full "
                     "current highlights body is below; copy your matched "
                     "sub-section's bullets exactly:")
        parts.append("")
        parts.append(existing_highlights)
    else:
        parts.append("(Highlights block is currently empty — create the first "
                     "sub-section.)")

    if is_sparse(commits):
        parts.append("")
        parts.append(
            "The provided commit subjects are unusually concise.  Infer the "
            "actual change scope from the changed file paths above.  Do NOT "
            "echo a terse commit subject verbatim — describe what changed."
        )

    return system, "\n".join(parts)


# ── LLM dispatch ────────────────────────────────────────────────────────────

def _dispatch_agentic(llm_cfg, system_prompt, user_prompt,
                      project_path, tokensave_exe, timeout, stop_event):
    """Run a one-shot LocalAgent loop with tokensave tools and return (text, error).

    Used by dispatch_llm when enable_tokensave_tools=True and provider is
    ollama/openai_compatible.  The agent may call tokensave_search and
    tokensave_context before producing its final answer, which becomes the
    draft text returned to the dialog.
    """
    import threading

    try:
        from agent import LocalAgent
        from agent_tools import (
            make_tokensave_search_tool,
            make_tokensave_context_tool,
        )
    except ImportError:
        return None, "Agentic mode unavailable: agent module not found."

    tools = {}
    if tokensave_exe and project_path:
        ts_search = make_tokensave_search_tool(project_path, tokensave_exe)
        ts_ctx = make_tokensave_context_tool(project_path, tokensave_exe)
        tools[ts_search.name] = ts_search
        tools[ts_ctx.name] = ts_ctx

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    result_holder: list[str | None] = [None]
    error_holder:  list[str | None] = [None]
    done_event = threading.Event()

    def _on_done(text):
        result_holder[0] = text
        done_event.set()

    def _on_error(msg):
        error_holder[0] = msg
        done_event.set()

    agent = LocalAgent(cfg=llm_cfg, project_path=project_path, tools=tools,
                       max_iterations=6)
    agent_thread = threading.Thread(
        target=agent.run,
        kwargs=dict(
            messages=messages,
            on_done=_on_done,
            on_error=_on_error,
            stop_event=stop_event,
        ),
        daemon=True,
    )
    agent_thread.start()
    done_event.wait(timeout=timeout)
    if not done_event.is_set():
        return None, "Agentic call timed out."
    if error_holder[0]:
        return None, error_holder[0]
    text = result_holder[0]
    if not text:
        return None, "Agentic call returned no output."
    return text, None


def dispatch_llm(llm_cfg, system_prompt, user_prompt,
                 claude_cli_exe, cwd, timeout=120,
                 gen_params=None, examples=None,
                 enable_tokensave_tools=False, tokensave_exe="",
                 stop_event=None):
    """Call the configured LLM and return ``(text, error)``.

    Routes to ``call_claude_cli_print`` when ``llm_cfg["provider"] ==
    "claude_cli"``, otherwise to ``_call_llm`` (anthropic / openai /
    openai_compatible / ollama).  Returns ``(text, None)`` on success or
    ``(None, error_string)`` on failure.

    gen_params: dict of per-DocType overrides merged into llm_cfg
      (temperature, top_p, top_k, num_ctx).  Values in gen_params win.
    examples: list of (input_text, output_text) few-shot pairs spliced
      into user_prompt for ollama / openai_compatible providers.
    enable_tokensave_tools: when True and provider is ollama/openai_compatible,
      use a one-shot LocalAgent loop with tokensave_search + tokensave_context
      tools instead of a plain completion (Theme B2).  Claude CLI stays on the
      B1-injection path (documented asymmetry).
    tokensave_exe: path to tokensave binary; required when enable_tokensave_tools=True.
    stop_event: threading.Event for cooperative cancellation in agentic path.

    Pure dispatch — does NOT handle threading, cancellation, or UI.
    Caller wraps in a worker thread + stop_event check.
    """
    if gen_params:
        llm_cfg = {**(llm_cfg or {}), **gen_params}
    provider = (llm_cfg or {}).get("provider", "")

    # C4: splice few-shot examples for local providers (ollama / openai_compat)
    if examples and provider.lower() in ("ollama", "openai_compatible"):
        ex_parts = []
        for inp, out in examples[:2]:
            ex_parts.append(f"Example input:\n{inp}\n\nExpected output:\n{out}")
        ex_block = "\n\n---\n\n".join(ex_parts)
        user_prompt = ex_block + "\n\n---\n\n" + user_prompt

    # Theme B2: agentic path for local providers when tokensave tools requested.
    if enable_tokensave_tools and provider.lower() in ("ollama", "openai_compatible"):
        return _dispatch_agentic(
            llm_cfg, system_prompt, user_prompt,
            project_path=cwd or "",
            tokensave_exe=tokensave_exe or "",
            timeout=timeout,
            stop_event=stop_event,
        )

    try:
        if provider == "claude_cli":
            from helpers.claude_cli import call_claude_cli_print
            exe = (claude_cli_exe or "").strip()
            if not exe:
                return None, (
                    "Claude CLI not configured — set path in "
                    "Settings → Claude Code CLI."
                )
            model = (llm_cfg.get("model") or "").strip()
            # cwd=$HOME: Claude Code loads <cwd>/CLAUDE.md as project
            # context, which puts smaller models (Haiku) into "assistant
            # mode" — they reply conversationally to the prompt instead of
            # producing the requested draft. Pointing cwd at $HOME isolates
            # this call from any project CLAUDE.md. Project context that
            # actually matters (existing section text, commit list,
            # tokensave grounding) is already embedded in the prompt by
            # the caller, so the CLI process does not need to be in the
            # project directory. Same pattern used in
            # helpers.commit_messages._strat_claude_cli.
            result = call_claude_cli_print(
                exe, user_prompt,
                system_prompt=system_prompt,
                timeout=timeout,
                model=model,
                cwd=os.path.expanduser("~"),
            )
            if not result:
                return None, "Claude CLI returned no output (timeout or auth)."
            return result, None

        from helpers.llm import _call_llm
        # Adaptive token cap. After Roadmap-7 prompt hardening (U2 swapped
        # full existing-content dumps for header summaries / scope-prefix
        # vocab), typical prompts dropped below the previous "large" branch,
        # so the per-branch ceilings are tightened. The STOP marker added in
        # U1 also gives the model a positive termination signal, reducing
        # the need for headroom against ramble.
        prompt_chars = len(system_prompt or "") + len(user_prompt or "")
        if prompt_chars < 1500:
            max_tokens = 1000          # tiny range → speed mode
        elif prompt_chars < 4000:
            max_tokens = 1500          # was 2000 — typical post-U2
        else:
            max_tokens = 2000          # was 2500 — large-context ceiling
        result = _call_llm(llm_cfg, system_prompt, user_prompt,
                           max_tokens=max_tokens)
        if not result:
            return None, f"{provider or 'LLM'} returned empty result."
        return result, None
    except Exception as exc:
        return None, f"{provider or 'LLM'} call failed: {exc}"


# ── Bullet-quality filter helpers (moved from dialogs/doc_drafter.py) ────────
#
# These run on the AI's output BEFORE the patcher applies it.  Catch the two
# Ollama failure modes: truncation (bullet ends mid-clause) and redundancy
# (bullet restates an existing entry).  Moved to helpers so DocType callables
# can reference them without circular imports.

_TRUNCATION_TRAILING = {
    "for", "the", "to", "with", "and", "or", "of",
    "in", "on", "at", "by", "as", "is", "a", "an",
}

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than",
    "of", "to", "in", "on", "at", "by", "as", "for", "with", "from", "into",
    "via", "per", "between", "through", "across", "over", "under",
    "after", "before", "during", "while", "when",
    "be", "is", "are", "was", "were", "it", "its", "this", "that",
    "also", "now", "still", "even", "just", "only",
}

_NOOP_BULLET_PATTERNS = [
    re.compile(
        r"^-?\s*(none|n/?a|nothing|tbd|no\s+changes?"
        r"|nothing\s+to\s+(add|report|note|do)"
        r"|n\.?a\.?|empty|placeholder)\s*\.?$",
        re.IGNORECASE,
    ),
]

# Phase 2.0 — literal template placeholder detector.
_LITERAL_PLACEHOLDER_RE = re.compile(
    r"\bRoadmap[-\s](?:"
    r"[A-Za-z](?![A-Za-z0-9])"
    r"|<[^>]+>"
    r"|TODO\b|TBD\b|PLACEHOLDER\b|PENDING\b|NUM\b|NUMBER\b"
    r")|<sub-section[^>]*>",
    re.IGNORECASE,
)

_PRESERVATION_THRESHOLD = 0.40


def _is_noop_bullet(bullet):
    s = (bullet or "").strip().lstrip("-*").strip().strip("()").strip()
    return any(p.match(s) for p in _NOOP_BULLET_PATTERNS)


def _looks_truncated(bullet):
    s = (bullet or "").rstrip()
    if not s:
        return False
    for _ in range(3):
        before = s
        m = re.search(r"\s+\([^()]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        m = re.search(r"\s+\[[^\]]*\]\([^)]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        m = re.search(r"\s+`[^`]*`\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        if s == before:
            break
    if not s:
        return False
    if s.endswith((".", ":", "!", "?", ")", "`", '"', "'")):
        return False
    last_word = s.split()[-1].lower().rstrip(",;:")
    return last_word in _TRUNCATION_TRAILING


def _token_set(bullet):
    s = re.sub(r"[^\w\s]", " ", (bullet or "").lower())
    return {t for t in s.split() if t not in _STOP_WORDS and len(t) > 2}


def _is_duplicate_from_sets(a, b):
    """Cheap Jaccard/overlap duplicate check on pre-computed token sets.

    Callers that already have ``_token_set`` results in hand should use this
    to avoid rebuilding the token set on every comparison. ``_is_duplicate``
    below is a thin wrapper for the convenience case.
    """
    if not a or not b:
        return False
    union = a | b
    jaccard = len(a & b) / len(union) if union else 0.0
    overlap = len(a & b) / min(len(a), len(b))
    return jaccard >= 0.6 or overlap >= 0.65


def _is_duplicate(new_bullet, existing_bullet):
    return _is_duplicate_from_sets(
        _token_set(new_bullet), _token_set(existing_bullet)
    )


# Bullet-shape patterns used by parse_grouped_bullets / _filter_bullets to
# distinguish legitimate bullets and their indented continuations from
# prose paragraphs, footer text, and code-fence detritus the model can emit
# between bullet groups.
_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+\S")
_INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")


def _normalise_bullet(line):
    """Lowercased, marker-stripped, whitespace-collapsed comparison key.

    Used as the set-membership token for O(1) exact-dedup. Two bullets that
    differ only in marker style ("- foo" vs "* foo"), spacing, or casing
    collapse to the same key. Paraphrased near-duplicates still need the
    fuzzy `_is_duplicate_from_sets` fallback — this is the cheap first pass.
    """
    return re.sub(
        r"\s+", " ",
        re.sub(r"^\s*[-*]\s+", "", line or "").strip().lower(),
    )


def _sanitise_raw_draft(text):
    lines = text.splitlines()
    while lines:
        last = lines[-1].strip()
        if (not last
                or last.startswith("```")
                or last.startswith("<!--")
                or last in {"---", "***", "___"}
                or last.lower().startswith(("*generated", "_generated",
                                            "*draft", "*ai-generated"))):
            lines.pop()
            continue
        break
    while lines:
        first = lines[0].strip()
        if not first or first.startswith("```"):
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


def _preserve_score(a_set, b_set):
    if not a_set or not b_set:
        return 0.0
    intersection = a_set & b_set
    if len(intersection) < 2:
        return 0.0
    union = a_set | b_set
    jaccard = len(intersection) / len(union)
    overlap = len(intersection) / min(len(a_set), len(b_set))
    return max(jaccard, overlap)


def _mirror_contract_check(draft_bullets, existing_bullets,
                            min_preservation=0.75):
    if not existing_bullets:
        return True, 0, 0, []
    existing_compiled = [(eb, _token_set(eb)) for eb in existing_bullets]
    draft_compiled   = [(db, _token_set(db)) for db in draft_bullets]
    triples = []
    for ei, (_, a) in enumerate(existing_compiled):
        for di, (_, b) in enumerate(draft_compiled):
            s = _preserve_score(a, b)
            if s >= _PRESERVATION_THRESHOLD:
                triples.append((s, ei, di))
    triples.sort(key=lambda t: -t[0])
    claimed_e, claimed_d = set(), set()
    for _score, ei, di in triples:
        if ei in claimed_e or di in claimed_d:
            continue
        claimed_e.add(ei)
        claimed_d.add(di)
    matched = len(claimed_e)
    missing_list = [eb for ei, (eb, _) in enumerate(existing_compiled)
                    if ei not in claimed_e]
    missing = len(missing_list)
    ratio = matched / len(existing_bullets)
    if ratio < min_preservation and missing > 1:
        missing_examples = [
            (eb[:80] + "…" if len(eb) > 80 else eb)
            for eb in missing_list
        ][:3]
        return False, matched, missing, missing_examples
    return True, matched, missing, []


def _filter_bullets(bullets_md, existing_bullets, *,
                    dedup_against_existing=True):
    """Apply truncation / noop / dedup filters to a bullet body.

    Drops non-bullet lines entirely (prose paragraphs the model added between
    bullets, footer commentary, code-fence detritus). Blank lines are kept as
    group separators only; everything else that isn't a ``- `` / ``* ``
    prefixed bullet is silently discarded.

    Performance: pre-computes the exact-normalised-key set AND the token-set
    list for existing bullets ONCE outside the per-bullet loop, so the
    Jaccard fuzzy fallback no longer rebuilds the token set on every
    comparison. The O(1) exact check handles verbatim copies; the fuzzy
    check only runs when exact-match fails.
    """
    kept = []
    seen_norm = set()
    truncated_n = duplicate_n = noop_n = 0

    # Pre-compute existing-bullet comparison data.
    existing_norm = set()
    existing_token_sets = []
    if dedup_against_existing:
        for eb in existing_bullets:
            existing_norm.add(_normalise_bullet(eb))
            existing_token_sets.append(_token_set(eb))

    for line in bullets_md.splitlines():
        stripped = line.lstrip()
        if not (stripped.startswith("- ") or stripped.startswith("* ")):
            # Non-bullet line — keep ONLY blank separators; drop prose.
            if not stripped:
                kept.append(line)
            continue
        if _looks_truncated(stripped):
            truncated_n += 1
            continue
        if _is_noop_bullet(stripped):
            noop_n += 1
            continue

        norm = _normalise_bullet(stripped)

        # In-draft exact dedup (O(1)). Catches echo-loop bullets within the
        # same section or merged-from-multiple-occurrences body.
        if norm in seen_norm:
            duplicate_n += 1
            continue

        if dedup_against_existing:
            # Exact-match check first (O(1) set lookup).
            if norm in existing_norm:
                duplicate_n += 1
                continue
            # Fuzzy fallback for paraphrased near-duplicates, reusing the
            # pre-computed token sets.
            new_tokens = _token_set(stripped)
            if any(_is_duplicate_from_sets(new_tokens, eb_tokens)
                   for eb_tokens in existing_token_sets):
                duplicate_n += 1
                continue
            kept.append(line)
        else:
            # README path — also fuzzy-dedup against in-draft kept bullets to
            # collapse the "model emitted the same bullet twice" case, with
            # the existing quality-swap precedence (longer bullet wins).
            is_dup = False
            new_tokens = _token_set(stripped)
            for idx, kb in enumerate(kept):
                kb_stripped = kb.lstrip()
                if not kb_stripped.startswith(("- ", "* ")):
                    continue
                if _is_duplicate_from_sets(new_tokens, _token_set(kb_stripped)):
                    is_dup = True
                    if len(stripped) > len(kb_stripped) + 8:
                        kept[idx] = line
                    duplicate_n += 1
                    break
            if not is_dup:
                kept.append(line)

        seen_norm.add(norm)
    return "\n".join(kept).strip(), truncated_n, duplicate_n, noop_n


def parse_grouped_bullets(draft_text):
    """Parse '### Header / - bullet ...' grouped output into (header, body) pairs.

    Bullets-only collection: lines under a ``### Section`` header are kept
    only if they are bullet lines, indented continuations of a preceding
    bullet, or blank separators between bullets. Prose paragraphs, code
    fences, footer commentary ("These bullets capture..."), and indented
    paragraphs not following a bullet are silently dropped — they were the
    primary vector for echo-loop and prose-pollution failures.

    ``last_was_bullet_like`` state-gates BOTH continuations and blank lines:

      - A blank line is kept only if the previous kept line was a bullet
        (preserves the visual gap between bullet groups).
      - An indented continuation is kept only if the previous kept line
        was a bullet or another continuation (prevents a model-written
        indented paragraph from leaking through as if it were continuation).
    """
    pairs = []
    current_section = None
    current_lines = []
    last_was_bullet_like = False
    for ln in draft_text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("### "):
            if current_section is not None:
                while current_lines and not current_lines[-1].strip():
                    current_lines.pop()
                body = "\n".join(current_lines).strip()
                if body:
                    pairs.append((current_section, body))
            current_section = stripped[4:].strip()
            current_lines = []
            last_was_bullet_like = False
        elif current_section is not None:
            is_bullet = bool(_BULLET_LINE_RE.match(ln))
            is_blank  = (not stripped)
            is_cont   = (bool(_INDENTED_CONTINUATION_RE.match(ln))
                         and last_was_bullet_like)
            if is_bullet:
                current_lines.append(ln)
                last_was_bullet_like = True
            elif is_cont:
                current_lines.append(ln)
                # last_was_bullet_like stays True — bullet group continues
            elif is_blank and last_was_bullet_like:
                current_lines.append(ln)
                last_was_bullet_like = False
            # Everything else is silently dropped (prose, unindented prose,
            # code fences, footer markers, indented paragraphs that don't
            # follow a bullet).
    if current_section is not None:
        while current_lines and not current_lines[-1].strip():
            current_lines.pop()
        body = "\n".join(current_lines).strip()
        if body:
            pairs.append((current_section, body))
    return pairs


def split_readme_subsection(draft_text):
    """Extract first ``**Header**`` line + remaining bullets. Returns (header, bullets) or None."""
    lines = draft_text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if (stripped.startswith("**") and stripped.endswith("**")
                and len(stripped) > 4):
            header_idx = i
            break
        if (stripped.startswith("**") and "**" in stripped[2:]
                and stripped.endswith(":")):
            header_idx = i
            break
    if header_idx is None:
        return None
    header_line = lines[header_idx].strip()
    bullets = "\n".join(lines[header_idx + 1:]).strip("\n")
    return header_line, bullets


# ── DocType callable implementations ─────────────────────────────────────────
#
# These are the io_apply / compute_apply / filter_draft callables stored in
# the DocType registry.  Defined here (helpers) so they can be imported by
# helpers/doc_types.py without circular imports.

def changelog_filter_draft(raw_text, target_path):
    """Filter changelog draft. Returns (filtered_text, trunc_n, dup_n, noop_n).

    Pipeline:
      1. Strip <<<END_OF_DRAFT>>> marker and anything past it.
      2. Sanitise (whitespace, leading fences) via _sanitise_raw_draft.
      3. Parse `### Section / - bullet` groups (bullets-only — prose dropped).
      4. Merge same-section pairs so a model that emits "### Added" twice
         has both batches consolidated before dedup.
      5. Per section: dedup against on-disk bullets + filter noops/truncation.
    """
    from helpers.changelog_patch import read_section_bullets
    text = _strip_end_marker(raw_text or "")
    sanitised = _sanitise_raw_draft(text)
    pairs = parse_grouped_bullets(sanitised)

    # F1b: merge same-section pairs before filtering.
    merged: "dict[str, list[str]]" = {}
    order: "list[str]" = []
    for section, bullets in pairs:
        key = section.strip()
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(bullets)
    merged_pairs = [(sec, "\n".join(merged[sec])) for sec in order]

    kept_pairs = []
    total_trunc = total_dup = total_noop = 0
    for section, bullets in merged_pairs:
        existing = read_section_bullets(target_path, section)
        filtered, t_n, d_n, n_n = _filter_bullets(bullets, existing)
        total_trunc += t_n
        total_dup += d_n
        total_noop += n_n
        if filtered.strip():
            kept_pairs.append((section, filtered.strip()))
    chunks = [f"### {sec}\n{body}" for sec, body in kept_pairs]
    return "\n\n".join(chunks), total_trunc, total_dup, total_noop


def readme_filter_draft(raw_text, target_path):
    """Filter readme draft. Returns (filtered_text, trunc_n, dup_n, noop_n)."""
    from helpers.readme_patch import read_subsection_bullets
    text = _strip_end_marker(raw_text or "")
    sanitised = _sanitise_raw_draft(text)
    parsed = split_readme_subsection(sanitised)
    if parsed is None:
        return sanitised, 0, 0, 0
    header_line, bullets = parsed
    existing = read_subsection_bullets(target_path, header_line)
    filtered, t_n, d_n, n_n = _filter_bullets(
        bullets, existing, dedup_against_existing=False)
    if filtered.strip():
        return (f"{header_line}\n{filtered.strip()}", t_n, d_n, n_n)
    return "", t_n, d_n, n_n


def changelog_compute_apply(full_text, pairs_list):
    """PURE: apply grouped bullet pairs to full file text. Returns (new_text, ok, msg)."""
    from helpers.changelog_patch import _compute_insert_unreleased_bullets
    if not pairs_list:
        return full_text, False, "No bullet pairs to apply."
    simulated = full_text
    for section, bullets in pairs_list:
        simulated, ok, msg = _compute_insert_unreleased_bullets(
            simulated, section, bullets)
        if not ok:
            return full_text, False, f"{section}: {msg}"
    return simulated, True, "ok"


def readme_compute_apply(full_text, header_line, bullets):
    """PURE: splice readme sub-section into full file text. Returns (new_text, ok, msg)."""
    from helpers.readme_patch import _compute_insert_readme_highlights_subsection
    return _compute_insert_readme_highlights_subsection(
        full_text, header_line, bullets)


def changelog_io_apply(target_path, draft_text):
    """Apply changelog draft. Returns (ok, msg, stats_dict)."""
    from helpers.changelog_patch import read_section_bullets, insert_unreleased_bullets
    pairs = parse_grouped_bullets(draft_text)
    if not pairs:
        return False, (
            "Draft is missing '### Section' headers — CHANGELOG mode requires "
            "bullets grouped under ### Added / ### Fixed / ### Changed / "
            "### Removed."
        ), {}
    applied = []
    total_truncated = total_duplicates = total_noop = 0
    empty_after_filter = 0
    for section, bullets in pairs:
        existing = read_section_bullets(target_path, section)
        filtered, trunc_n, dup_n, noop_n = _filter_bullets(bullets, existing)
        total_truncated += trunc_n
        total_duplicates += dup_n
        total_noop += noop_n
        if not filtered.strip():
            empty_after_filter += 1
            continue
        ok, msg = insert_unreleased_bullets(target_path, section, filtered)
        if not ok:
            stats = {"truncated": total_truncated,
                     "duplicates": total_duplicates, "noop": total_noop}
            return False, f"{section}: {msg} (applied so far: {applied})", stats
        applied.append(section)
    stats = {"truncated": total_truncated,
             "duplicates": total_duplicates, "noop": total_noop}
    if not applied:
        return False, (
            f"All bullets filtered out ({total_truncated} truncated, "
            f"{total_duplicates} duplicates, {total_noop} placeholders).  "
            "Click Regenerate to retry."
        ), stats
    summary = f"appended to {len(applied)} section(s): {', '.join(applied)}"
    if empty_after_filter:
        summary += f" ({empty_after_filter} section(s) emptied by filter)"
    return True, summary, stats


def readme_io_apply(target_path, draft_text):
    """Apply readme sub-section draft. Returns (ok, msg, stats_dict)."""
    from helpers.readme_patch import (
        read_subsection_bullets, insert_readme_highlights_subsection,
        read_highlights,
    )
    parsed = split_readme_subsection(draft_text)
    if parsed is None:
        return False, (
            "Draft is missing a `**Bold header**` line — README mode requires "
            "a sub-section header as the first non-blank line."
        ), {}
    header_line, bullets = parsed
    if _LITERAL_PLACEHOLDER_RE.search(header_line):
        candidates = _extract_subsection_headers(read_highlights(target_path))
        candidates_msg = "\n  ".join("• " + h for h in candidates[:5])
        return False, (
            f"Header contains a literal template placeholder: "
            f"{header_line!r}.  Edit the draft to use a real header, "
            f"or click Regenerate.  Existing candidates to REUSE if "
            f"your bullets belong:\n  "
            f"{candidates_msg if candidates else '(none — your sub-section is brand new; use a concrete number like Roadmap-6 or Roadmap-7)'}"
        ), {}
    existing = read_subsection_bullets(target_path, header_line)
    filtered, trunc_n, dup_n, noop_n = _filter_bullets(
        bullets, existing, dedup_against_existing=False)
    stats = {"truncated": trunc_n, "duplicates": dup_n, "noop": noop_n}
    if not filtered.strip():
        return False, (
            f"All bullets filtered out ({trunc_n} truncated, {dup_n} "
            f"self-duplicates, {noop_n} placeholders).  README's REPLACE "
            "patcher would DELETE the existing sub-section if applied with "
            "no bullets.  Click Regenerate."
        ), stats
    draft_bullets = [ln.lstrip() for ln in filtered.splitlines()
                     if ln.lstrip().startswith(("- ", "* "))]
    ok, kept, missing, examples = _mirror_contract_check(
        draft_bullets, existing)
    if not ok:
        bullet_list = "\n  ".join("• " + ex for ex in examples)
        extra = (f" (showing {len(examples)} of {missing})"
                 if missing > len(examples) else "")
        return False, (
            f"Mirror-contract safety abort: draft preserves only "
            f"{kept}/{kept + missing} existing bullets "
            f"({int(100 * kept / max(1, kept + missing))}%).  "
            f"README's REPLACE patcher would DELETE {missing} "
            f"preserved bullet(s){extra}:\n  {bullet_list}\n\n"
            f"Click Regenerate to retry, or manually edit the draft "
            f"to add the missing bullets back before Apply."
        ), stats
    ok2, msg2 = insert_readme_highlights_subsection(
        target_path, header_line,
        filtered + "\n" if filtered else "\n")
    return ok2, msg2, stats


# ── Phase 2 DocType system prompts ────────────────────────────────────────────

_ARCHITECTURE_SYSTEM = (
    "You maintain architecture documentation. Your output is one or more "
    "`## SectionName` blocks, each followed by the updated section body. "
    "Nothing before the first `## `, nothing after the last block except "
    "the termination marker.\n\n"
    "Output format:\n"
    "  ## Section Name\n"
    "  <updated section body>\n\n"
    "Guidelines:\n"
    "- Pick section titles from the existing-document headings shown in the "
    "user prompt. Titles you emit must match an existing title exactly — "
    "the patcher refuses to auto-create new sections.\n"
    "- Preserve existing factual content that is still accurate; only "
    "ADD or UPDATE what the commits actually change.\n"
    "- Match the existing document's style: tree-formatted file listings, "
    "one-liner module descriptions, Key-exports annotations.\n"
    "- Cite exported symbol names in backticks: `read_unreleased`, "
    "`_compute_insert_*`.\n\n"
    "Example output (illustrative — the actual sections you update will "
    "come from the user prompt):\n\n"
    "## Daemon\n\n"
    "The daemon orchestrates tokensave's index-update lifecycle.\n\n"
    "- `_run_loop` (in `daemon.py`) drives the 60s polling cadence\n"
    "- `_should_index` consults `state.json` to skip noop runs\n\n"
    "<<<END_OF_DRAFT>>>"
) + _STATELESS_FILTER_RULE + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE

_ROADMAP_SYSTEM = (
    "You maintain roadmap documentation. Your output begins with "
    "`## Roadmap N — Theme Title` and contains one updated roadmap "
    "section.\n\n"
    "Output format:\n"
    "  ## Roadmap N — Theme Title\n"
    "  ### ✅ Item title\n"
    "  Description.\n"
    "  ### 🟡 Another item\n"
    "  Description.\n\n"
    "Status emojis: ✅ shipped  🟡 in-progress  🔮 planned  💭 idea  💤 stale.\n\n"
    "Guidelines:\n"
    "- Use the active roadmap number from the user prompt's headings. "
    "Adding a new `## Roadmap N` section is allowed for this DocType.\n"
    "- Each entry: `### <emoji> <title>` followed by 1–3 short technical "
    "sentences. No marketing voice.\n"
    "- Describe only work evidenced by the provided commits.\n\n"
    "Example output (illustrative):\n\n"
    "## Roadmap 7 — Doc-drafter hardening\n\n"
    "### ✅ STOP marker + anti-fabrication guardrails\n"
    "Output post-processing strips `<<<END_OF_DRAFT>>>` and anything past "
    "it. Anti-fabrication rule blocks invented symbol names in bullets.\n\n"
    "### 🟡 Multi-section drafting\n"
    "Replace-mode patchers now accept N sections per draft; titles "
    "validate against the existing document.\n\n"
    "<<<END_OF_DRAFT>>>"
) + _STATELESS_FILTER_RULE + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE

_MEMORY_SYSTEM = (
    "You author persistent-memory files. Your output is the body text only "
    "— no YAML frontmatter, no code fences, no preamble.\n\n"
    "Format: lead with the key fact, then a `**Why:**` line (reason / "
    "motivation), then a `**How to apply:**` line (when this guidance "
    "kicks in). Link related memories with `[[slug-name]]`.\n\n"
    "Guidelines:\n"
    "- Be specific and actionable. 'Always use X when Y' beats 'Consider X'.\n"
    "- No filler, no hedging.\n\n"
    "Example output (illustrative):\n\n"
    "When updating a doc-drafter prompt, run the smoke battery before "
    "shipping; the parser/filter tests catch echo-loop regressions early.\n\n"
    "**Why:** local LLMs drift toward template-echoing when prompts change; "
    "regressions in `parse_grouped_bullets` slip through pyflakes.\n\n"
    "**How to apply:** kicks in whenever any `_*_SYSTEM` constant or "
    "`build_*_prompt` function is touched. Re-run `_smoke_test_doc_drafter` "
    "before merging. Related: [[round1_results]].\n\n"
    "<<<END_OF_DRAFT>>>"
) + _STATELESS_FILTER_RULE + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE

_GENERIC_DOC_SYSTEM = (
    "You maintain markdown documentation. Your output is one or more "
    "`## SectionName` blocks, each followed by the updated section body. "
    "Nothing before the first `## `, nothing after the last block except "
    "the termination marker.\n\n"
    "Output format:\n"
    "  ## Section Name\n"
    "  <updated section body>\n\n"
    "Guidelines:\n"
    "- Pick section titles from the existing-document headings shown in the "
    "user prompt. Titles you emit must match an existing title exactly — "
    "the patcher refuses to auto-create new sections.\n"
    "- Preserve still-accurate content; only ADD or UPDATE what the "
    "commits actually change.\n"
    "- Match the existing document's voice and style.\n\n"
    "Example output (illustrative):\n\n"
    "## Installation\n\n"
    "Install via pip:\n\n"
    "```\npip install tokensave-manager\n```\n\n"
    "Configuration lives in `~/.config/tokensave/manager.toml`.\n\n"
    "<<<END_OF_DRAFT>>>"
) + _STATELESS_FILTER_RULE + _ANTI_FABRICATION_RULE + _STOP_MARKER_RULE


# ── Phase 2 build_prompt functions ───────────────────────────────────────────

def _render_commit_summary(commits, classified):
    """Shared commit block used by all Phase 2 prompt builders."""
    parts = []
    if commits:
        parts.append("Recent commits:")
        for c in commits[:40]:
            parts.append(f"  {c}")
    if classified:
        parts.append("")
        parts.append("Classified changes:")
        parts.append(_render_commit_list(classified))
    return "\n".join(parts)


def _build_replace_mode_prompt(system, doctype_label, commits, classified,
                                existing_full, project_name, project_desc,
                                changed_files, boundary_note,
                                grounding_block, output_reminder):
    """Shared builder for ARCHITECTURE / ROADMAP / GENERIC.

    Topology (per Theme A + Theme C):
      1. Project / description / boundary
      2. Commits + classified + changed files (the SIGNAL)
      3. Grounding block (tokensave context if available)
      4. Existing-document headings (navigation aid)
      5. CANDIDATE SECTIONS — body content for the most-likely-affected
         sections, picked via three-signal selector (path + scope + subject
         tokens). Bounded by max_body_chars to control prompt size.
      6. Closing "What to output" reminder — final-token-zone where
         attention concentrates. Reinforces output format, not file template.
    """
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if changed_files:
        parts.append("")
        parts.append("Changed files:")
        for f in changed_files[:60]:
            parts.append(f"  {f}")
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)

    # Navigation: headings of EVERY section so the model knows the full
    # set of valid titles (even if a section's body isn't in the candidates
    # block, its title is still a valid target). Compact — 1 line per
    # heading.
    parts.append("")
    parts.append(f"Existing {doctype_label} section headings:")
    parts.append(_summarise_existing_headings(existing_full, max_levels=3))

    # Candidate bodies: full text for the sections most likely affected by
    # this commit range. Bounded by max_body_chars so a 900-line ARCH file
    # doesn't blow the prompt out.
    candidates = _select_candidate_sections(
        existing_full or "", changed_files or [], commits or [])
    parts.append("")
    if candidates:
        parts.append("--- BEGIN CANDIDATE SECTIONS (most likely affected by "
                     "the commits above) ---")
        for title, body in candidates:
            parts.append(f"## {title}")
            parts.append(body)
            parts.append("")
        parts.append("--- END CANDIDATE SECTIONS ---")
    else:
        parts.append("(No existing sections matched the commit signals.)")

    # Final-position output reminder. Tokens at the end of the prompt
    # have outsized influence on what the model continues — so we use
    # them for output-format directives, not file content.
    parts.append("")
    parts.append(output_reminder)

    return system, "\n".join(parts)


def build_architecture_prompt(commits, classified, existing_full,
                               project_name, project_desc,
                               changed_files, boundary_note,
                               grounding_block=""):
    """Return (system, user) for the architecture tab."""
    reminder = (
        "What to output:\n"
        "- One or more `## Title` blocks updating sections from the "
        "candidates above.\n"
        "- Each title must match an existing heading exactly — capitalisation "
        "and wording.\n"
        "- End with `<<<END_OF_DRAFT>>>`."
    )
    return _build_replace_mode_prompt(
        _ARCHITECTURE_SYSTEM, "ARCHITECTURE.md",
        commits, classified, existing_full, project_name, project_desc,
        changed_files, boundary_note, grounding_block, reminder,
    )


def build_roadmap_prompt(commits, classified, existing_full,
                          project_name, project_desc,
                          changed_files, boundary_note,
                          grounding_block=""):
    """Return (system, user) for the roadmap tab."""
    reminder = (
        "What to output:\n"
        "- One `## Roadmap N — Theme` block (this DocType accepts either an "
        "update to an existing roadmap OR a brand-new `## Roadmap N` if the "
        "commits represent a new phase).\n"
        "- End with `<<<END_OF_DRAFT>>>`."
    )
    return _build_replace_mode_prompt(
        _ROADMAP_SYSTEM, "ROADMAP.md",
        commits, classified, existing_full, project_name, project_desc,
        changed_files, boundary_note, grounding_block, reminder,
    )


def build_memory_prompt(commits, classified, existing_body,
                         project_name, project_desc,
                         changed_files, boundary_note,
                         grounding_block=""):
    """Return (system, user) for the memory tab."""
    system = _MEMORY_SYSTEM
    parts = [f"Project: {project_name}"]
    if project_desc:
        parts.append(f"Description: {project_desc}")
    if boundary_note:
        parts.append("")
        parts.append(boundary_note)
    parts.append("")
    parts.append(_render_commit_summary(commits, classified))
    if grounding_block:
        parts.append("")
        parts.append(grounding_block)
    parts.append("")
    # U2-MEMORY: soft 4000-char ceiling. Preserves continuity for typical
    # memory files (they're meant to be continuous context) while bounding
    # worst-case prompt size for files that grow over time.
    _MAX_MEMORY_CHARS = 4000
    body_for_prompt = existing_body or ""
    if len(body_for_prompt) > _MAX_MEMORY_CHARS:
        body_for_prompt = (
            body_for_prompt[:_MAX_MEMORY_CHARS]
            + "\n\n[... rest of memory file omitted to bound prompt size; "
            "patcher operates on the full file at apply time ...]"
        )
    parts.append("Current memory body (extend or refine based on the commits "
                 "— patcher will splice your output into the full file at "
                 "apply time):")
    parts.append("")
    parts.append(body_for_prompt or "(currently empty)")
    return system, "\n".join(parts)


def build_generic_doc_prompt(commits, classified, existing_full,
                              project_name, project_desc,
                              changed_files, boundary_note,
                              grounding_block=""):
    """Return (system, user) for the docs_generic / tokensave_guide tab."""
    reminder = (
        "What to output:\n"
        "- One or more `## Title` blocks updating sections from the "
        "candidates above.\n"
        "- Each title must match an existing heading exactly.\n"
        "- End with `<<<END_OF_DRAFT>>>`."
    )
    return _build_replace_mode_prompt(
        _GENERIC_DOC_SYSTEM, "document",
        commits, classified, existing_full, project_name, project_desc,
        changed_files, boundary_note, grounding_block, reminder,
    )


# ── Phase 2 parse_draft functions ────────────────────────────────────────────
#
# Each returns a tuple that gets splatted: compute_apply(full_text, *parse_draft(draft))
# Return (None, ...) on failure so _simulate_body can detect it.

# Non-greedy with a forward look-ahead — captures one ## section body up to
# the next ## heading (or EOF). Prevents a model that emits two ## sections
# from having the second leak into the first's body and ends up applied.
# Named groups so the callers don't depend on positional indices that vary
# between the architecture and roadmap variants.
_SECTION_HEADING_RE = re.compile(
    r"(?ms)^## (?P<title>[^\n]+)\n(?P<body>.*?)(?=^## |\Z)"
)
_ROADMAP_HEADING_RE = re.compile(
    r"(?ms)^## Roadmap (?P<n>\d+)(?P<theme>[^\n]*)\n(?P<body>.*?)(?=^## |\Z)"
)


def architecture_parse_draft(draft_text):
    """Extract ALL `## SectionName` blocks from draft. Returns (sections_list,)
    where sections_list is [(title, body), ...]. Empty list on failure.

    Multi-section support (Theme B v3): finditer pulls every `## ` block;
    the non-greedy regex lookahead `(?=^## |\\Z)` makes each match stop at
    the next heading or EOF.
    """
    text = (draft_text or "").strip()
    sections = [
        (m["title"].strip(), m["body"].strip())
        for m in _SECTION_HEADING_RE.finditer(text)
    ]
    return (sections,)


def roadmap_parse_draft(draft_text):
    """Extract ALL `## Roadmap N — Theme` blocks. Returns (sections_list,)
    where each item is (roadmap_n, theme_title, content). Empty list on failure.
    """
    text = (draft_text or "").strip()
    sections = []
    for m in _ROADMAP_HEADING_RE.finditer(text):
        n = int(m["n"])
        theme_rest = m["theme"].strip().lstrip("—").strip()
        theme = theme_rest or f"Roadmap {n}"
        sections.append((n, theme, m["body"].strip()))
    return (sections,)


def memory_parse_draft(draft_text):
    """Return (body_text,) — the entire draft is the memory body."""
    body = (draft_text or "").strip()
    if not body:
        return (None,)
    return (body,)


def generic_parse_draft(draft_text):
    """Extract ALL `## SectionName` blocks. Returns (sections_list,)
    where each item is (title, body). Empty list on failure.
    """
    text = (draft_text or "").strip()
    sections = [
        (m["title"].strip(), m["body"].strip())
        for m in _SECTION_HEADING_RE.finditer(text)
    ]
    return (sections,)


# ── Phase 2 filter_draft functions ────────────────────────────────────────────

_CODE_FENCE_RE = re.compile(
    r"(?m)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
    re.DOTALL,
)

# A line that looks like "real section content": tree chars, headings,
# list markers, numbered items, or horizontal rules.
_CONTENT_LINE_RE = re.compile(
    r"^(?:[│├└─#\-\*>]|\d+\.|\s+[│├└─]|={3,}|-{3,})"
)


# Horizontal-rule line — only `-`, `*`, `_`, or `=` separated by whitespace.
# Matched FIRST so an HR can't be mistaken for a bullet via _SUBSTANTIVE.
_HORIZONTAL_RULE_RE = re.compile(r"^\s*([-*_=])\s*(?:\1\s*){2,}$")


# Tighter than _CONTENT_LINE_RE — excludes decorative artifacts (horizontal
# rules, bare blank lines) that would let model padding masquerade as real
# content. Used only by the footer-marker look-ahead so "* * *" after a
# footer doesn't disguise the footer as legitimate prose.
_SUBSTANTIVE_CONTENT_RE = re.compile(
    r"^(?:"
    r"\s*[-*]\s+\S"            # bullet
    r"|\s*\d+\.\s+\S"          # numbered list
    r"|#{1,6}\s+\S"            # heading
    r"|\s+[│├└─]"              # tree-formatted listing
    r")"
)


def _is_substantive(line):
    """Return True if `line` is a real bullet / heading / list / tree row.

    Explicitly rejects horizontal rules (`* * *`, `---`, `___`, `===`) which
    `_SUBSTANTIVE_CONTENT_RE` would otherwise misclassify as bullets, since
    the bullet pattern `\\s*[-*]\\s+\\S` would match `* * *` (asterisk +
    space + asterisk). The HR exclusion is what makes the footer-marker
    look-ahead robust to model-emitted decoration after wrap-up prose.
    """
    if _HORIZONTAL_RULE_RE.match(line):
        return False
    return bool(_SUBSTANTIVE_CONTENT_RE.match(line))


# Echo-loop / wrap-up phrasings the failing Ollama draft showed. Narrow on
# purpose — generic "Note:" / "Summary:" patterns occur in legitimate
# technical prose and must NOT trigger. These specific strings are almost
# exclusively meta-commentary the model adds AFTER what it thinks is its
# real output.
_FOOTER_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r"these\s+(?:bullets|changes|entries|updates)\s+(?:capture|describe|reflect|summari[sz]e)"
    r"|here(?:'s|\s+is)\s+(?:a|the)\s+(?:summary|breakdown|overview)\s+of"
    r"|in\s+summary\s*,"
    r"|to\s+summari[sz]e\s*[:,]"
    r"|let\s+me\s+know\s+if"
    r"|please\s+let\s+me\s+know"
    r")"
)


def _strip_trailing_prose(text: str) -> str:
    """Discard trailing prose paragraphs after the last real content line.

    "Real content" means tree-formatted lines, headings, list items, or
    horizontal rules.  A trailing paragraph like Haiku's '**Change summary:**'
    blob is dropped.
    """
    lines = text.splitlines()
    last_content = -1
    for i, ln in enumerate(lines):
        if _CONTENT_LINE_RE.match(ln):
            last_content = i
    if last_content < 0:
        return text
    return "\n".join(lines[:last_content + 1])


def _strip_preamble_and_fences(text: str) -> str:
    """Remove prose before the first '## ' heading, unwrap code fences,
    and strip trailing prose paragraphs after the last content line.

    Models sometimes wrap output in a prose preamble ("Based on my analysis…"),
    surround content with ```markdown fences, and/or add a trailing summary
    paragraph.  All three are stripped here so only the bare section content
    reaches the patcher.
    """
    # 1. Drop prose before the first ## heading.
    m = re.search(r"(?m)^## ", text)
    if m:
        text = text[m.start():]

    # 2. Unwrap code fences — keep only the body inside the backticks.
    text = _CODE_FENCE_RE.sub(lambda mo: mo.group(1), text)

    # 3. Footer-marker chop. Only fires if NO substantive content (real
    # bullets, headings, numbered list, tree-formatted listing) follows the
    # marker — decorative HRs, blank lines, or stray punctuation don't count
    # as "real content" and won't shield the footer. This avoids false-
    # positives on legitimate sentences mid-document that happen to start
    # with "These changes describe…" but are followed by real content.
    fm = _FOOTER_MARKER_RE.search(text)
    if fm:
        tail = text[fm.end():]
        has_real_content_after = any(
            _is_substantive(ln) for ln in tail.splitlines()
        )
        if not has_real_content_after:
            text = text[:fm.start()].rstrip()

    # 4. Drop trailing prose after the last real content line.
    text = _strip_trailing_prose(text)

    return text


def _filter_freeform(raw_text):
    """Lightweight filter for non-bullet drafts: strip preamble, fences,
    placeholders, and the <<<END_OF_DRAFT>>> marker."""
    text = _strip_end_marker(raw_text or "")
    sanitised = _sanitise_raw_draft(
        _strip_preamble_and_fences(text)
    )
    lines = sanitised.splitlines()
    noop_n = 0
    kept = []
    for ln in lines:
        stripped = ln.strip()
        if _LITERAL_PLACEHOLDER_RE.search(stripped):
            noop_n += 1
            continue
        kept.append(ln)
    return "\n".join(kept), 0, 0, noop_n


def architecture_filter_draft(raw_text, target_path):
    """Filter architecture draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def roadmap_filter_draft(raw_text, target_path):
    """Filter roadmap draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def memory_filter_draft(raw_text, target_path):
    """Filter memory draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


def generic_filter_draft(raw_text, target_path):
    """Filter generic doc draft. Returns (text, trunc_n, dup_n, noop_n)."""
    return _filter_freeform(raw_text)


# ── Phase 2 compute_apply (PURE) functions ────────────────────────────────────

def _apply_sections(full_text, sections, compute_fn, known_titles, *,
                     allow_new=False, title_for=None):
    """Shared multi-section apply helper.

    Iterates through ``sections``, validating each title against
    ``known_titles`` (unless ``allow_new`` is True). For each known title:
    runs ``compute_fn(simulated, *section_args)`` and accumulates the
    cumulative simulated state. Returns ``(simulated, ok, msg, stats)``.

    Args:
      full_text:    Starting document text.
      sections:     List of section tuples. Each item is splatted into compute_fn
                    AFTER ``simulated``: e.g. (title, body) for architecture,
                    (n, theme, body) for roadmap.
      compute_fn:   `_compute_insert_*` function. Signature: (text, *args) -> (new_text, ok, msg).
      known_titles: Set of lowercased titles that are valid update targets.
                    Unknown titles get rejected (refused, not auto-appended).
      allow_new:    If True, unknown titles are accepted (auto-append). Default
                    False — refuses hallucinations.
      title_for:    Callable section_args -> "human title" for skip reporting.
                    Default: stringify first arg.

    Stats dict contains ``applied`` (list of titles) and ``skipped``
    (list of (title, reason) pairs) for UI surfacing.
    """
    if title_for is None:
        title_for = lambda args: str(args[0])  # noqa: E731

    if not sections:
        return full_text, False, "Draft produced no `## Section` block.", {
            "applied": [], "skipped": []
        }

    simulated = full_text
    applied, skipped = [], []
    for section_args in sections:
        title = title_for(section_args)
        if not allow_new and title.lower() not in known_titles:
            skipped.append((
                title,
                "title not in existing document — refused to auto-append "
                "(likely hallucinated)"
            ))
            continue
        next_state, ok, msg = compute_fn(simulated, *section_args)
        if ok:
            simulated = next_state
            applied.append(title)
        else:
            skipped.append((title, msg))

    stats = {"applied": applied, "skipped": skipped}

    if not applied:
        return full_text, False, (
            "All sections rejected: "
            + "; ".join(f"{t}: {m}" for t, m in skipped)
        ), stats
    if skipped:
        msg = (f"Applied {len(applied)}/{len(sections)}; skipped: "
               + ", ".join(f"{t} ({m})" for t, m in skipped))
    else:
        msg = f"ok ({len(applied)} section(s))"
    return simulated, True, msg, stats


def architecture_compute_apply(full_text, sections):
    """PURE: apply N `## Title` sections. Returns (new_text, ok, msg).

    Validates titles against existing `## ` headings; unknown titles are
    REFUSED (not auto-appended). Multi-section partial-apply: known-good
    sections apply against the cumulative simulated state; skipped sections
    are reported in the msg suffix.
    """
    from helpers.architecture_patch import (
        _compute_insert_architecture_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_architecture_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    return simulated, ok, msg


def roadmap_compute_apply(full_text, sections):
    """PURE: apply N `## Roadmap N — Theme` sections. Returns (new_text, ok, msg).

    Unlike architecture/generic, roadmap ALLOWS new sections — adding a new
    `## Roadmap N` for a new phase is a valid user workflow.
    """
    from helpers.roadmap_patch import (
        _compute_insert_roadmap_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    # Title for skip reporting: "Roadmap N" prefix derived from the args
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_roadmap_section,
        known, allow_new=True,
        title_for=lambda args: f"Roadmap {args[0]} — {args[1]}",
    )
    return simulated, ok, msg


def memory_compute_apply(full_text, new_body):
    """PURE: replace memory body. Returns (new_text, ok, msg).

    Memory is single-section by design — the entire draft is the new body.
    No multi-section migration applies here.
    """
    from helpers.memory_patch import _compute_insert_memory_body
    if new_body is None:
        return full_text, False, "Draft body is empty."
    return _compute_insert_memory_body(full_text, new_body)


def generic_compute_apply(full_text, sections):
    """PURE: apply N `## Title` sections. Returns (new_text, ok, msg).

    Same shape as architecture_compute_apply — validates titles, refuses
    hallucinations, partial-apply on per-section failure.
    """
    from helpers.generic_doc_patch import (
        _compute_insert_generic_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_generic_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    return simulated, ok, msg


# ── Phase 2 io_apply functions ────────────────────────────────────────────────

def _read_file_text(path):
    """Read file as UTF-8 with BOM tolerance. Returns "" on missing or read error."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""


def architecture_io_apply(target_path, draft_text):
    """Apply architecture draft (multi-section). Returns (ok, msg, stats_dict).

    Reads file once → runs compute_apply (validates + iterates) → writes
    once atomically. Returns stats including applied/skipped section lists
    for UI surfacing.
    """
    from helpers.architecture_patch import (
        _compute_insert_architecture_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = architecture_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Section Name` heading — architecture "
            "mode requires the output to start with a level-2 heading."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_architecture_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats


def roadmap_io_apply(target_path, draft_text):
    """Apply roadmap draft (multi-section, allow_new=True). Returns (ok, msg, stats_dict)."""
    from helpers.roadmap_patch import (
        _compute_insert_roadmap_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = roadmap_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Roadmap N` heading — roadmap mode "
            "requires the output to start with '## Roadmap <number>'."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_roadmap_section,
        known, allow_new=True,
        title_for=lambda args: f"Roadmap {args[0]} — {args[1]}",
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats


def memory_io_apply(target_path, draft_text):
    """Apply memory draft. Returns (ok, msg, stats_dict). Single-section."""
    from helpers.memory_patch import insert_memory_body
    body = (draft_text or "").strip()
    if not body:
        return False, "Draft is empty.", {}
    ok, msg = insert_memory_body(target_path, body)
    return ok, msg, {}


def generic_io_apply(target_path, draft_text):
    """Apply generic doc draft (multi-section). Returns (ok, msg, stats_dict)."""
    from helpers.generic_doc_patch import (
        _compute_insert_generic_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = generic_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Section Name` heading — generic doc "
            "mode requires the output to start with a level-2 heading."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_generic_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats
