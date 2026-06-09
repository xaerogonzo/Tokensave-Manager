"""Draft quality filters, bullet parsing, and parse_draft fns.

Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).
Import via the ``helpers.doc_drafter`` facade — it re-exports
every name, so call sites and tests are unchanged.
"""

from __future__ import annotations

import re
from helpers.doc_drafter_prompts import (
    _strip_end_marker,
)




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


# Structural-markup characters that indicate a bullet has intentional
# technical content (not a raw prose fragment). Prose punctuation
# (comma, semicolon, single-quote) is intentionally excluded so bullets
# like "- updated the connection pool, " or "- said 'hello' to" are
# still flagged as truncated.
#
# G1 (v4.4): restricted from broad [^\w\s\-] to this explicit set.
# Note: \b\w+[._]\w+\b matches BOTH snake_case identifiers (db_pool)
# AND dotted filenames/extensions (config.json, utils.py) — treating
# both as structural markers. Prose periods that end a sentence are
# handled by the early endswith(".") check, so mid-token dots are safe.
_STRUCTURAL_MARKUP_RE = re.compile(r"[`()\[\]/=<>{}|&*]|\b\w+[._]\w+\b")


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
    """Heuristic detector for mid-clause Ollama truncations.

    v4 rule (combines structural markup + word-count + trailing-operator
    checks). Returns True only when ALL of:

      * The bullet ends on a function-word stopword (`for`, `to`, ...)
        OR a dangling operator/bracket (`(`, `/`, `=`, `+`, `[`, ...)
      * AND the bullet has no structural markup (commas, periods,
        backticks, parens, snake_case identifiers)
      * AND the bullet is ≤ 12 words

    Real bullets pass at least one of these gates. The rule is
    deliberately specific: it catches genuine truncation while letting
    substantive content through.
    """
    s = (bullet or "").rstrip()
    if not s:
        return False

    # Strip trailing decorations like `... (note)`, `[label](url)`,
    # ``` `inline-code` ``` — they may hide the real ending word.
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

    # Terminal sentence punctuation — not truncated.
    if s.endswith((".", ":", "!", "?", ")", "`", '"', "'")):
        return False

    # Trailing-operator garbage: bullet ends mid-clause on `(`, `/`, `=`,
    # etc. Gemini's specific examples — none end on a stopword (last_word
    # check below misses them) but they're clearly truncated mid-expression.
    if s[-1:] in "/=({[<&|+*":
        return True
    # Unmatched-opener: more `(` than `)` → dangling clause.
    if s.count("(") > s.count(")"):
        return True

    last_word = s.split()[-1].lower().rstrip(",;:")
    if last_word not in _TRUNCATION_TRAILING:
        return False

    # High-word-count safety valve. A bullet ≥ 12 words is statistically
    # unlikely to be a true Ollama truncation (those tend to be short).
    bare = re.sub(r"^\s*[-*]\s+", "", s).strip()
    if len(bare.split()) > 12:
        return False

    # Structural markup integrity. Any non-alphabetic technical character
    # (`.`, `,`, `/`, `=`, etc.) OR snake_case identifier (`db_pool`) signals
    # structural intent. `_` is in `\w`, so a bare `[^\w\s\-]` check misses
    # snake_case — the alternation explicitly catches it.
    #
    # IMPORTANT: strip the leading conventional-commit scope prefix
    # (`- (scope-name) `) before checking — scope parens are markers, not
    # structural content. Otherwise `- (foo) added X for` would falsely
    # pass via the `()` chars in the scope marker.
    s_for_markup = re.sub(r"^\s*[-*]\s+\([^()]*\)\s*", "", s)
    # G1 (v4.4): structural chars only — prose commas/periods/quotes
    # intentionally excluded so "- updated the pool, " is still flagged.
    has_markup = bool(_STRUCTURAL_MARKUP_RE.search(s_for_markup))
    if has_markup:
        return False

    return True



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



def _merge_wrapped_bullets(bullets_md):
    """Join indented continuation lines into their preceding bullet.

    v4.1 Revision B: Ollama qwen2.5-coder wraps long bullets aggressively,
    producing output like:

        - move parser state machine into
          shared retry coordinator

    The first line ends on a stopword with no markup and would be flagged
    as truncated by ``_looks_truncated``; the second line is the
    continuation. Without this preprocessor, the v4 truncation rule
    would falsely reject the bullet.

    Rules:
      - A line matching `^\\s{2,}\\S` after a bullet line gets joined onto
        the bullet with a single space separator.
      - Tree-character continuations (`├`, `└`, `│`, `─`) are EXCLUDED so
        tree-formatted listings inside bullets stay multi-line (defensive
        guard — _filter_bullets isn't called on doctypes that emit trees,
        but cost-free to be paranoid).
      - The function is idempotent: bullets already on one line pass
        through unchanged.
    """
    out_lines = []
    for line in bullets_md.splitlines():
        if (out_lines
                and re.match(r"^\s{2,}\S", line)
                and not re.match(r"^\s{2,}[│├└─]", line)
                and out_lines[-1].lstrip().startswith(("- ", "* "))):
            out_lines[-1] = out_lines[-1].rstrip() + " " + line.strip()
        else:
            out_lines.append(line)
    return "\n".join(out_lines)



def _filter_bullets(bullets_md, existing_bullets, *,
                    dedup_against_existing=True):
    """Apply truncation / noop / dedup filters to a bullet body.

    v4.1: pre-processes wrapped bullets via _merge_wrapped_bullets so
    `_looks_truncated` evaluates each bullet as a complete semantic unit
    rather than fragments.

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
    bullets_md = _merge_wrapped_bullets(bullets_md)
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
