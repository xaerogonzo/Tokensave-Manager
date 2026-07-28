"""System prompts + prompt builders for every doc type.

Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).
Import via the ``helpers.doc_drafter`` facade — it re-exports
every name, so call sites and tests are unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from helpers.doc_drafter_git import (
    is_sparse,
)




@dataclass(frozen=True)
class PromptBuildResult:
    """Return type for every ``build_*_prompt`` callable.

    Round-4 architectural prerequisite: as orchestration metadata
    (alignment flags, warnings, backend hints, section traces, etc.)
    accretes, growing the tuple length at every callsite is brittle.
    A frozen dataclass with named fields makes the contract explicit
    and additive — new fields default to safe values, existing callers
    keep working.

    Fields:
      system:    System prompt string.
      user:      User prompt string.
      aligned:   True when the candidate-section selector found
                 token-aligned matches. False only from
                 ``build_generic_doc_prompt`` when no section scored
                 highly enough to suggest the commits actually relate
                 to the target file. Drives the mismatch-detect
                 askyesno dialog upstream.
      warnings:  Tuple of user-visible warnings to surface in the
                 dialog's banner. Currently unused (banner content
                 is computed at simulate time), but reserved for
                 future build-time warnings.
    """
    system:   str
    user:     str
    aligned:  bool                 = True
    warnings: "tuple[str, ...]"    = ()



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



_COMMIT_SCOPE_RE = re.compile(r"^\s*(\w+)\(([^)]{1,30})\)\s*[:!]")



def _scope_prefix_tokens(commits):
    """Extract `(scope-name)` from conventional-commit subjects, with
    hyphen and underscore variants generated for cross-style matching
    against section text (`doc-drafter` and `doc_drafter` both hit).

    Recognises:
      ``feat(scope): description``
      ``fix(scope)!: breaking change``
      ``refactor(scope): ...``
      etc. — the leading type word is ignored; only the inner scope
      name is extracted.

    v4.1 Revision C: separated from _subject_tokens so the candidate
    selector can weight scope tokens HIGHER than free-form subject
    words — scope prefixes are project-vocabulary signal, generic
    subject words are noise-prone.
    """
    out = set()
    for c in commits or []:
        subj = (c.get("subject") if isinstance(c, dict) else str(c)) or ""
        m = _COMMIT_SCOPE_RE.match(subj)
        if m:
            scope = m.group(2).lower()
            out.add(scope)
            out.add(scope.replace("-", "_"))
            out.add(scope.replace("_", "-"))
    return out



def _subject_tokens(commits):
    """Free-form significant words from commit subjects (≥ 4 chars,
    stopword-filtered).

    v4.1 Revision C: NO LONGER includes scope prefixes — those moved to
    ``_scope_prefix_tokens``. Caller weights this set lower than path
    and scope tokens because subject words can be generic ("worker",
    "timeout", "prompt") and produce lexical-poisoning false matches.
    """
    out = set()
    for c in commits or []:
        subj = (c.get("subject") if isinstance(c, dict) else str(c)) or ""
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



_ALIGNMENT_THRESHOLD = 2   # v4: top weighted score must reach this to count

                            # as "aligned". 1 title hit = 3 points, 1 body
                            # hit = 1 point. Threshold 2 catches title hits
                            # AND multi-token body matches; excludes
                            # incidental single-token noise.


def _select_candidate_sections(existing_text, changed_files, commits,
                                max_candidates=5, max_body_chars=8000):
    """Return ``(candidates, aligned)`` — candidates is a list of
    ``(title, body)`` pairs scored by combined-token overlap with the
    section's title AND body; ``aligned`` is True iff the top section
    reached the alignment threshold (≥ 2 weighted points), indicating
    the commit signals genuinely map to at least one existing section.

    Three-signal token set (path, scope, subject words) handles both
    file-rooted documentation and concept-rooted documentation.

    Sections are scored by hit count across title + body. Title hits
    weigh 3× body hits — matching a section's NAME is a stronger signal
    than incidental body mentions.

    Fallback: if no section scores > 0, return top-K by size so the
    model has substantive content (but ``aligned=False``). Body-char
    budget caps cumulative body size — wholesale-block drop at
    boundaries (no mid-section slicing, no unclosed code fences).

    Args:
        max_body_chars: Per-call body budget. v4: callers pass 4000 for
                        local backends (Ollama), 8000 for cloud (Haiku).
    """
    sections = _split_into_sections(existing_text)
    if not sections:
        return [], False

    # v4.1 Revision C: tiered token sets. Path tokens (literal files touched)
    # and scope-prefix tokens (project-vocabulary commit scopes) carry
    # CONCRETE evidence — weight 2. Subject-word tokens are weaker —
    # weight 1. Title hits multiply 3× the base weight regardless of tier.
    strong_tokens = _path_tokens(changed_files) | _scope_prefix_tokens(commits)
    weak_tokens   = _subject_tokens(commits) - strong_tokens

    aligned = False
    if not strong_tokens and not weak_tokens:
        # No signal at all — fall back to top-K by size (aligned stays False)
        ordered = sorted(sections, key=lambda tb: -len(tb[1]))[:max_candidates]
    else:
        scored = []
        for title, body in sections:
            tl, bl = title.lower(), body.lower()
            # Title hits — base weight 3.
            strong_title_hits = sum(1 for t in strong_tokens if t in tl)
            weak_title_hits   = sum(1 for t in weak_tokens   if t in tl)
            # Body hits — base weight 1.
            strong_body_hits  = sum(1 for t in strong_tokens if t in bl)
            weak_body_hits    = sum(1 for t in weak_tokens   if t in bl)
            # Tier multiplier: strong × 2, weak × 1.
            # G3 (v4.4): cap weak-body contribution at 1 (saturating) so
            # that two stray subject-word body hits (e.g. "update" +
            # "worker" in any generic doc) score 1, not 2, and therefore
            # cannot cross the alignment threshold on their own.  Reaching
            # the threshold of 2 now requires at least one path/scope body
            # hit (×2), any title hit, or a title+body weak combination.
            score = (
                (strong_title_hits * 3 + strong_body_hits) * 2
                + (weak_title_hits * 3 + min(weak_body_hits, 1)) * 1
            )
            if score > 0:
                scored.append((score, title, body))
        if scored:
            scored.sort(key=lambda t: -t[0])
            # v4 threshold (≥ 2) preserved but semantically tightened by the
            # tier weighting — a single weak-body hit = 1 point doesn't
            # cross the bar; a single strong-body hit = 2 points does;
            # a title hit (any tier) easily passes.
            aligned = scored[0][0] >= _ALIGNMENT_THRESHOLD
            ordered = [(t, b) for _, t, b in scored[:max_candidates]]
        else:
            # No token alignment — fall back to top-K by size
            ordered = sorted(sections, key=lambda tb: -len(tb[1]))[:max_candidates]

    # Body-char budget — wholesale-block drop at boundaries (never
    # mid-section slicing, which could leave an unclosed code fence).
    # The `and out` guard ensures the top-scoring section is always
    # included even if its body alone exceeds the budget.
    out, total = [], 0
    for title, body in ordered:
        if total + len(body) > max_body_chars and out:
            break
        out.append((title, body))
        total += len(body)
    return out, aligned



def build_changelog_prompt(commits, classified, existing_unreleased,
                           project_name, project_desc,
                           changed_files, boundary_note,
                           backend_hint=None):
    """Return ``PromptBuildResult`` for the CHANGELOG tab.

    Existing [Unreleased] content is summarised as section name + bullet
    count + scope-prefix vocabulary, NOT dumped verbatim. The patcher
    handles dedup against on-disk content; the model only needs to know
    which section headers exist and what scope-prefix style to match.

    `backend_hint` is unused for CHANGELOG (append-mode patcher, no
    candidate-section selector). Accepted for uniform builder signature.
    """
    del backend_hint  # reserved for uniform signature; unused here
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

    return PromptBuildResult(system=system, user="\n".join(parts))



def build_readme_prompt(commits, classified, existing_highlights,
                        project_name, project_desc,
                        changed_files, boundary_note,
                        backend_hint=None):
    """Return ``PromptBuildResult`` for the README tab.

    ``existing_highlights`` is the current body of the 'Recent highlights'
    block (output of ``helpers.readme_patch.read_highlights``).

    `backend_hint` is unused for README (mirror-contract patcher dumps
    the existing body unconditionally — no body-budget reduction).
    Accepted for uniform builder signature.
    """
    del backend_hint  # reserved for uniform signature; unused here
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

    return PromptBuildResult(system=system, user="\n".join(parts))



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
    "`_compute_insert_*`.\n"
    # v4 Fix 2b: hard path-grounding rule with explicit new-module exception.
    # Local models switch from transformation to synthesis mode otherwise,
    # producing generic Python project templates like `src/main.py` /
    # `utils/helpers.py` that don't exist in the actual project.
    "- File paths in tree-formatted listings must come from one of: the "
    "candidate section's existing body, OR the changed-files list in the "
    "user prompt below. Do NOT invent paths that appear in neither source. "
    "Concrete negative example: do not output `src/main.py`, "
    "`utils/helpers.py`, or `modules/module1/` unless those paths appear "
    "verbatim in the prompt.\n"
    "- Exception for new modules: if commits introduce a deep new file "
    "like `src/api/v2/routes/users.py`, the implicit parent directories "
    "(`src/api/v2/`, `src/api/v2/routes/`) may appear in the tree even "
    "though only the leaf file is in the changed-files list. Render the "
    "new file at its actual path; the directory hierarchy is inferred "
    "from that path.\n\n"
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
    # v4 Fix 5a: Context block — clarifies that the picker workflow has
    # already chosen the file. Haiku was falling into "what would you
    # like me to help with?" mode because the prompt looked like a
    # meta-instruction.
    "Context: the user has already selected the memory file (its current "
    "body appears at the end of the user prompt). Your task is to produce "
    "NEW or UPDATED content for THAT file based on the commits. Do not "
    "ask which file; do not ask what update is wanted — extend the body "
    "in the style of the existing content.\n\n"
    "Format: lead with the key fact, then a `**Why:**` line (reason / "
    "motivation), then a `**How to apply:**` line (when this guidance "
    "kicks in). Link related memories with `[[slug-name]]`.\n\n"
    "Guidelines:\n"
    "- Be specific and actionable. 'Always use X when Y' beats 'Consider X'.\n"
    "- No filler, no hedging.\n\n"
    # v4 Fix 3: Related: line omitted from example to prevent local
    # coders from literally echoing the slug. The `[[slug-name]]` syntax
    # is still documented in the format spec above — model can use it
    # when context warrants, just not primed to invent a fake slug.
    "Example output (illustrative — your actual content comes from the "
    "user prompt's commits and memory body):\n\n"
    "When updating a doc-drafter prompt, run the smoke battery before "
    "shipping; the parser/filter tests catch echo-loop regressions early.\n\n"
    "**Why:** local LLMs drift toward template-echoing when prompts change; "
    "regressions in `parse_grouped_bullets` slip through pyflakes.\n\n"
    "**How to apply:** kicks in whenever any `_*_SYSTEM` constant or "
    "`build_*_prompt` function is touched. Re-run `_smoke_test_doc_drafter` "
    "before merging.\n\n"
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
                                grounding_block, output_reminder,
                                backend_hint=None):
    """Shared builder for ARCHITECTURE / ROADMAP / GENERIC.

    Returns ``PromptBuildResult`` — aligned reflects the three-signal
    selector's verdict (True iff a section reached the alignment
    threshold; False signals the doc may not match the commits and
    drives the upstream mismatch-detect askyesno dialog).

    `backend_hint`: pass True when the dispatch target is a local
    backend (Ollama / openai_compatible on localhost). Halves the
    candidate-body budget (8000 → 4000 chars) to keep Ollama agentic
    runs under the 300 s timeout.

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
    # doesn't blow the prompt out. Halve the budget for local backends.
    max_body_chars = 4000 if backend_hint else 8000
    candidates, aligned = _select_candidate_sections(
        existing_full or "", changed_files or [], commits or [],
        max_body_chars=max_body_chars)
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

    return PromptBuildResult(
        system=system, user="\n".join(parts), aligned=aligned,
    )



def build_architecture_prompt(commits, classified, existing_full,
                               project_name, project_desc,
                               changed_files, boundary_note,
                               grounding_block="", backend_hint=None):
    """Return ``PromptBuildResult`` for the architecture tab."""
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
        backend_hint=backend_hint,
    )



def build_roadmap_prompt(commits, classified, existing_full,
                          project_name, project_desc,
                          changed_files, boundary_note,
                          grounding_block="", backend_hint=None):
    """Return ``PromptBuildResult`` for the roadmap tab."""
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
        backend_hint=backend_hint,
    )



def build_memory_prompt(commits, classified, existing_body,
                         project_name, project_desc,
                         changed_files, boundary_note,
                         grounding_block="", backend_hint=None):
    """Return ``PromptBuildResult`` for the memory tab.

    `backend_hint` is unused for MEMORY (single-target splice patcher,
    no candidate-section selector). Accepted for uniform builder signature.
    """
    del backend_hint  # reserved for uniform signature; unused here
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
    return PromptBuildResult(system=system, user="\n".join(parts))



def build_generic_doc_prompt(commits, classified, existing_full,
                              project_name, project_desc,
                              changed_files, boundary_note,
                              grounding_block="", backend_hint=None):
    """Return ``PromptBuildResult`` for docs_generic / tokensave_guide tabs."""
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
        backend_hint=backend_hint,
    )
