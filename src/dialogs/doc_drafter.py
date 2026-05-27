"""DocDrafterDialog — tabbed AI doc-update drafter (Tier B of the doc-update
automation roadmap).

Two tabs: CHANGELOG and README.  Each tab is independent — its own worker
thread, its own ``stop_event``, its own draft cache.  A slow draft on one
tab never overwrites a fast result on another.

Flow per tab:
  1. User picks a commit range from the header dropdown (resolved once,
     shared across tabs).
  2. Click **Generate** → spawn worker → ``helpers.doc_drafter.dispatch_llm``
     → text appears in the editable area.
  3. Optionally edit the draft.
  4. Click **Apply** → spawn worker → ``ProposalBridge`` shows old-vs-new
     diff → on accept, the appropriate patcher writes atomically and the
     parent's commit-offer callback fires.

Closing the dialog (X button) signals stop_event on ALL tab workers and
destroys cleanly.  No leaked threads.

All Tk widget mutations from worker threads go through ``self.after(0, …)``
and are guarded with ``winfo_exists()`` + ``try/except TclError`` (same
pattern as the gitignore AI-Suggest worker).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

from constants import C
import re



# ── Bullet-quality helpers (truncation + redundancy filters) ────────────────
#
# These run on the AI's output BEFORE the patcher applies it.  Catch the two
# Ollama failure modes observed during dogfooding:
#   1. Truncation — bullet ends mid-clause with no closing punctuation and
#      a trailing stop-word ("for", "the", "to", ...)
#   2. Redundancy — bullet semantically restates an existing entry that the
#      patcher would happily duplicate.
#
# Same prompt-then-code-defence pattern as the gitignore AI Suggest dedup.

_TRUNCATION_TRAILING = {
    "for", "the", "to", "with", "and", "or", "of",
    "in", "on", "at", "by", "as", "is", "a", "an",
}

_STOP_WORDS = {
    # articles / conjunctions
    "the", "a", "an", "and", "or", "but", "if", "then", "than",
    # prepositions (commonly bloat dedup denominator without semantic weight)
    "of", "to", "in", "on", "at", "by", "as", "for", "with", "from", "into",
    "via", "per", "between", "through", "across", "over", "under",
    "after", "before", "during", "while", "when",
    # copulas / pronouns
    "be", "is", "are", "was", "were", "it", "its", "this", "that",
    # adverbs that surface a lot in commit prose with no scope info
    "also", "now", "still", "even", "just", "only",
}


# Placeholder bullets the model writes despite the prompt telling it to omit
# empty sections.  ``### Fixed\n- None`` happens regularly with small models;
# anchor with ``^...$`` so legitimate bullets that merely START with one of
# these words ("- None of the existing patches handle case X") stay through.
_NOOP_BULLET_PATTERNS = [
    re.compile(
        r"^-?\s*(none|n/?a|nothing|tbd|no\s+changes?"
        r"|nothing\s+to\s+(add|report|note|do)"
        r"|n\.?a\.?|empty|placeholder)\s*\.?$",
        re.IGNORECASE,
    ),
]


# Phase 2.0 — literal template placeholder detector for README sub-section
# headers.  Catches the leakage where a small model copies the prompt's
# template literally instead of substituting real values.
#
# What MATCHES (rejected at apply-time with the candidate list in the error):
#   - `Roadmap-<single ASCII letter>` where the letter is NOT followed by
#     another letter or digit (so `Roadmap-N`, `Roadmap-X`, `Roadmap-Y`,
#     `Roadmap-Z` match but `Roadmap-Native` and `Roadmap-N-API` do not)
#   - `Roadmap-<angle-bracket placeholder>` (e.g. `Roadmap-<num>`)
#   - Common placeholder words after `Roadmap-`: TODO, TBD, PLACEHOLDER,
#     PENDING, NUM, NUMBER (case-insensitive)
#   - The bare template marker `<sub-section header>` (from prompt example)
#
# What does NOT match:
#   - Real numeric roadmaps: `Roadmap-6`, `Roadmap-77`, `Roadmap-123`
#   - Multi-letter suffixes that may be legitimate: `Roadmap-Native`,
#     `Roadmap-N-API-binding`, `Roadmap-Foo`
#   - Non-Roadmap headers entirely
#
# Intentionally conservative — only flags patterns that NEVER appear in
# legitimate human-written headers.  Avoids the regex whack-a-mole that
# would come from trying to catch every shape a model could invent.
_LITERAL_PLACEHOLDER_RE = re.compile(
    r"\bRoadmap[-\s](?:"
    r"[A-Za-z](?![A-Za-z0-9])"
    r"|<[^>]+>"
    r"|TODO\b|TBD\b|PLACEHOLDER\b|PENDING\b|NUM\b|NUMBER\b"
    r")|<sub-section[^>]*>",
    re.IGNORECASE,
)


def _is_noop_bullet(bullet):
    """True if the bullet is a literal placeholder with no content.

    Catches: ``- None``, ``- N/A``, ``- Nothing``, ``- TBD``,
    ``- no changes``, ``- nothing to add``, ``- nothing to report``,
    ``- (none)``.  Conservative — the regex is anchored with ``^...$`` so a
    bullet that merely STARTS with one of these words is preserved (e.g.
    ``- None of the existing patches handle case X``).

    The normalisation chain MUST start with .strip() (NOT .lstrip("-")) so
    Windows CRLF carriage returns are removed BEFORE the dash-strip.
    Ollama on Windows occasionally emits ``\\r\\n`` line endings inside
    the stream.
    """
    # ``lstrip("-*")`` covers both dash- and asterisk-prefixed bullets
    # (Phase 1.8).  Ollama emits ``-`` consistently; Claude CLI commonly
    # emits ``*``.  Without the ``*`` here, ``* None`` would slip past
    # the noop regex (which begins with ``^-?``, not ``^[-*]?``).
    s = (bullet or "").strip().lstrip("-*").strip().strip("()").strip()
    return any(p.match(s) for p in _NOOP_BULLET_PATTERNS)


def _looks_truncated(bullet):
    """Return True if ``bullet`` looks cut off mid-sentence.

    Phase 1.9 rewrite: strips trailing metadata BEFORE the punctuation
    check, otherwise a bullet like ``"...and (`path`)."`` would
    early-return False on the trailing period and never expose the
    truncated ``and``.

    Strips up to 3 layers of trailing citation patterns in a loop:
      1. Parenthetical: ``"...prose (`path`)"`` (with optional ``.``)
      2. Markdown link:  ``"...prose [text](url)"`` (with optional ``.``)
      3. Inline code:    ``"...prose `path`"`` (with optional ``.``)

    Each pattern's anchor permits OPTIONAL trailing punctuation
    (``[:!?.]?$``) so common Claude CLI output ``"...and (path)."``
    matches on pass 1 of the loop, not bypassed.  Once metadata is
    peeled, the punctuation + stop-word check runs on the actual prose
    tail.

    Known limitation: nested parens like ``(as verified in foo())``
    aren't stripped (regex stops at inner ``)``).  False negative only —
    never causes a write-side data loss.

    Conservative on positives:
      - bullets ending in non-stop-word ("... fix bug") → not flagged
      - bullets ending in proper punctuation → not flagged
      - bullets where stripped-prose ends in stop-word → FLAGGED
    """
    s = (bullet or "").rstrip()
    if not s:
        return False

    # Strip up to 3 layers of trailing metadata.  Each pattern permits
    # OPTIONAL trailing punctuation so "...path)." matches the paren
    # citation, not just "...path)".  Loop terminates when nothing
    # strips on a given pass.
    for _ in range(3):
        before = s
        # 1. Parenthetical citation
        m = re.search(r"\s+\([^()]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        # 2. Markdown link
        m = re.search(r"\s+\[[^\]]*\]\([^)]*\)\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        # 3. Inline code backtick
        m = re.search(r"\s+`[^`]*`\s*[:!?.]?\s*$", s)
        if m:
            s = s[:m.start()].rstrip()
            continue
        if s == before:
            break

    if not s:
        return False
    # NOW the punctuation check is safe — citations have been peeled.
    if s.endswith((".", ":", "!", "?", ")", "`", '"', "'")):
        return False
    last_word = s.split()[-1].lower().rstrip(",;:")
    return last_word in _TRUNCATION_TRAILING


def _token_set(bullet):
    """Lowercase-tokenise + strip stop-words + drop short tokens.

    Used by ``_is_duplicate``.  Length filter (>2 chars) discards noise like
    digits, single letters, and one-char punctuation residue without losing
    meaningful short tokens (gitignore semantics catches "AI", "PR", "UI",
    "CLI" at length 2-3 — keep >2 threshold).
    """
    s = re.sub(r"[^\w\s]", " ", (bullet or "").lower())
    return {t for t in s.split() if t not in _STOP_WORDS and len(t) > 2}


def _is_duplicate(new_bullet, existing_bullet):
    """Combined Jaccard + containment check against ONE existing bullet.

    Jaccard ≥ 0.6 catches "mostly the same words" cases.  The containment
    (overlap) coefficient at 0.70 catches the asymmetric case where one
    bullet is much shorter than the other but its tokens are largely a
    SUBSET of the larger one — plain Jaccard misses these because the
    union inflates the denominator.

    Threshold 0.65 was chosen by dogfooding against the realistic failure
    case: existing 13-token detailed bullet, new 6-token generic summary
    bullet describing the same feature ('add automated CHANGELOG updates
    via AI'), shared 4 tokens → 4/6 = 0.67 overlap.  Stricter 0.85 missed
    every paraphrase-style failure; lower than 0.60 starts catching
    unrelated bullets that happen to share scope prefix words.  0.65 is
    the sweet spot from the live test data.
    """
    a = _token_set(new_bullet)
    b = _token_set(existing_bullet)
    if not a or not b:
        return False
    union = a | b
    jaccard = len(a & b) / len(union) if union else 0.0
    overlap = len(a & b) / min(len(a), len(b))
    return jaccard >= 0.6 or overlap >= 0.65


def _sanitise_raw_draft(text):
    """Strip head/tail artefacts the model sometimes appends before parsing.

    Local models occasionally wrap their output with markdown code fences,
    italic 'generated by AI' footers, HTML comments, or horizontal-rule
    separators.  None of these are bullets, and if they slip past the
    section parser into a bullet body they distort the tokeniser /
    truncation guard / dedup math (e.g. ``- ``` text`` would not be
    flagged as truncated because it ends in a backtick).

    Conservative — only strips lines that are *clearly* not bullets and
    only at the head/tail.  In-body content is untouched, so a bullet
    that legitimately mentions a code fence inside its text survives.

    Patterns removed (lines, after .strip()):
      - empty / whitespace-only
      - ``\\`\\`\\``` opener / closer (with or without language hint)
      - ``<!--`` HTML comment opener
      - ``---`` / ``***`` / ``___`` horizontal rules
      - lines starting with ``*generated``, ``_generated``, ``*draft``,
        ``*ai-generated`` (italic AI-footer markers)
    """
    lines = text.splitlines()
    # Trailing junk
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
    # Leading junk (e.g. ``` opener or blank lines)
    while lines:
        first = lines[0].strip()
        if not first or first.startswith("```"):
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


# ── Mirror-contract safety net (Phase 1.9) ──────────────────────────────────
#
# README's `insert_readme_highlights_subsection` patcher uses REPLACE
# semantics: when a matching sub-section already exists, the patcher
# OVERWRITES it with the draft.  The model is told to mirror existing
# bullets back as part of the output (Phase 1.6 design), but small
# models occasionally break that contract — most dramatically when
# Phase 1.8's granularity rules pushed qwen-14b so hard toward NEW
# bullets that it dropped all existing ones.
#
# Phase 1.9 adds the LOAD-BEARING enforcement: before any README write,
# verify the draft preserves enough existing bullets.  Prompt rules are
# advisory; this gate is hard.

# Floor for "this pair counts as preserved" after pre-scoring.  Looser
# than the Phase 1.6 dedup threshold (0.65) because preservation needs
# HIGH RECALL (catch rephrased mirrors) rather than HIGH PRECISION.
# The min-intersection-size floor inside _preserve_score (≥ 2 shared
# tokens) prevents single-word false matches that 0.40 alone would let
# through.
_PRESERVATION_THRESHOLD = 0.40


def _preserve_score(a_set, b_set):
    """Return preservation similarity (0..1) for two pre-tokenised bullets.

    Combines Jaccard and overlap with a MIN-INTERSECTION-SIZE floor:
    two bullets need to share ≥ 2 non-stop-word tokens before either
    metric counts.  Without this, a 2-token existing bullet like
    ``{"script", "cleanup"}`` would be falsely "preserved" by any long
    draft that happens to contain "script" (overlap = 1/2 = 0.50 trips
    any reasonable threshold despite zero semantic relation).

    Returns 0.0 when the floor isn't met; otherwise ``max(jaccard,
    overlap)``.  The score is used for highest-score-first matching in
    `_mirror_contract_check` — the threshold is applied AFTER bipartite
    assignment so no greedy ordering cascade can mask drift.
    """
    if not a_set or not b_set:
        return 0.0
    intersection = a_set & b_set
    if len(intersection) < 2:
        return 0.0                  # single-token overlap is noise
    union = a_set | b_set
    jaccard = len(intersection) / len(union)
    overlap = len(intersection) / min(len(a_set), len(b_set))
    return max(jaccard, overlap)


def _mirror_contract_check(draft_bullets, existing_bullets,
                           min_preservation=0.75):
    """Verify a README draft preserves enough existing sub-section bullets.

    Returns ``(ok, kept_count, missing_count, missing_examples)``.

    Algorithm:
      1. Pre-tokenise both lists ONCE (no per-comparison retokenisation).
      2. Score all (existing, draft) pairs via `_preserve_score`.  Keep
         only pairs scoring ≥ _PRESERVATION_THRESHOLD.
      3. Sort kept pairs by descending score.
      4. **Highest-score-first greedy bipartite**: walk sorted pairs;
         each existing claims at MOST ONE draft; each draft serves at
         MOST ONE existing.  Prevents a short existing bullet from
         falsely claiming a long unrelated draft on a single shared
         word.
      5. **Quantization tolerance**: reject only when ``ratio <
         min_preservation AND missing > 1``.  Single-drop is always
         tolerated regardless of ratio (legitimate small-list pruning).

    On empty ``existing_bullets`` (genuinely new sub-section) returns
    ``(True, 0, 0, [])`` — there is no contract to enforce.
    """
    if not existing_bullets:
        return True, 0, 0, []       # genuinely new sub-section

    existing_compiled = [(eb, _token_set(eb)) for eb in existing_bullets]
    draft_compiled   = [(db, _token_set(db)) for db in draft_bullets]

    triples = []                    # (score, existing_idx, draft_idx)
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
    """Apply truncation + dedup + noop-placeholder filters to a bullet block.

    Args:
        bullets_md:               newline-separated bullet block from the LLM
        existing_bullets:         list of existing bullet lines to dedup against
        dedup_against_existing:   keyword-only.

            **True (CHANGELOG mode, default)** — drop any new bullet that
            semantically duplicates anything in ``existing_bullets``.  The
            CHANGELOG drafter prompt asks the model to output ONLY new
            content under append-only semantics, so any output bullet that
            mirrors an existing on-disk bullet is unwanted duplication.

            **False (README mode)** — the README drafter prompt asks the
            model to mirror ALL existing bullets PLUS new ones, because the
            ``insert_readme_highlights_subsection`` patcher REPLACES the
            whole sub-section (omitting a mirrored bullet deletes it from
            the file).  Dropping bullets that match existing would be
            DESTRUCTIVE.  Instead, dedup is performed against ``kept``
            (the bullets already accepted from THIS DRAFT) so the model
            duplicating its OWN new bullets is still caught.  Quality-swap
            precedence: when a duplicate is detected, the LONGER bullet
            wins by +8 character slack — prevents a truncated paraphrase
            earlier in the stream from displacing a polished later version.

    Returns ``(kept_md, truncated_n, duplicate_n, noop_n)`` where
    ``kept_md`` is the filtered bullets joined back into a newline-separated
    string.  Counters track each rejection class so the status bar can
    surface a per-class summary.
    """
    kept = []
    truncated_n = 0
    duplicate_n = 0
    noop_n = 0
    for line in bullets_md.splitlines():
        stripped = line.lstrip()
        # Pass through non-bullet lines untouched (lets prose interleave —
        # not common in our output but harmless).
        if not (stripped.startswith("- ") or stripped.startswith("* ")):
            kept.append(line)
            continue
        if _looks_truncated(stripped):
            truncated_n += 1
            continue
        if _is_noop_bullet(stripped):
            noop_n += 1
            continue
        if dedup_against_existing:
            # CHANGELOG mode — drop if it matches anything on disk.
            if any(_is_duplicate(stripped, eb) for eb in existing_bullets):
                duplicate_n += 1
                continue
            kept.append(line)
        else:
            # README mode — dedup only against bullets we've already kept
            # FROM THIS SAME DRAFT.  Existing-on-disk bullets are NOT a
            # target; the model is supposed to mirror them back so the
            # REPLACE patcher preserves them.  Quality-swap when matched:
            # longer (more detailed) bullet wins.
            is_dup = False
            for idx, kb in enumerate(kept):
                kb_stripped = kb.lstrip()
                if not kb_stripped.startswith(("- ", "* ")):
                    continue
                if _is_duplicate(stripped, kb_stripped):
                    is_dup = True
                    # Swap when the incoming bullet is materially longer
                    # (~ a phrase more detail).  +8 slack avoids
                    # thrashing on single-word differences.  Ties favour
                    # the earlier-kept line for stable diff order.
                    if len(stripped) > len(kb_stripped) + 8:
                        kept[idx] = line
                    duplicate_n += 1
                    break
            if not is_dup:
                kept.append(line)
    return "\n".join(kept).strip(), truncated_n, duplicate_n, noop_n
from helpers.release import _classify_commits_for_changelog
from helpers import doc_drafter as dd
from helpers.doc_types import REGISTRY

if TYPE_CHECKING:
    from typing import Callable
    from state import ManagerConfig


# Commit-range modes (mirrors helpers.doc_drafter.resolve_commit_range)
_RANGE_MODES = [
    ("Since last doc commit",    "since_last_doc"),
    ("Since last commit",        "since_last_commit"),
    ("Since last release tag",   "since_last_tag"),
    ("Custom range…",            "custom"),
]

# Per-session backend override values for the dialog header dropdown.
# Picked per-draft, never persisted to Settings.  The override applies only
# to the NEXT Generate click (_llm_cfg_resolved snapshots at Generate time;
# mid-flight workers continue with their captured config).
_BACKEND_DEFAULT     = "Default (ask_tab_llm)"
_BACKEND_OLLAMA      = "Force Ollama"
_BACKEND_CLAUDE_CLI  = "Force Claude CLI"
_BACKEND_OPTIONS     = [_BACKEND_DEFAULT, _BACKEND_OLLAMA, _BACKEND_CLAUDE_CLI]


@contextlib.contextmanager
def _suppressed_modified(state: dict):
    """v4.4 (Gemini #2): guarantee the suppress_modified flag clears.

    Tk's ``<<Modified>>`` virtual event fires on programmatic
    ``text.insert(...)`` too, which would otherwise clear the warning
    banner the moment a draft lands. The dialog uses
    ``state["suppress_modified"]`` to gate the handler. Without
    ``try``/``finally``, any exception mid-insert (a TclError, a Unicode
    glitch, a worker race) leaves the flag stuck True for the rest of
    the session — the banner can never auto-clear again.

    This context manager wraps every programmatic mutation site so the
    flag is always reset, regardless of what happens inside the block.
    """
    state["suppress_modified"] = True
    try:
        yield
    finally:
        state["suppress_modified"] = False


class DocDrafterDialog(tk.Toplevel):
    """Tabbed doc-update drafter (CHANGELOG + README)."""

    def __init__(self, parent, project_path,
                 cfg: "ManagerConfig",
                 on_log: "Callable[[str, str | None], None] | None" = None,
                 on_commit_offer: "Callable[[str, str], None] | None" = None):
        super().__init__(parent)
        self._app             = parent
        self._project_path    = project_path
        self._cfg             = cfg
        self._on_log          = on_log or (lambda msg, c=None: None)
        self._on_commit_offer = on_commit_offer or (lambda path, label: None)

        self.title(f"📝 Draft Doc Updates — {os.path.basename(project_path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(720, 580)
        self.grab_set()

        # ── State ──────────────────────────────────────────────────────────
        self._range_data = None    # set by _refresh_range
        self._project_name = os.path.basename(project_path)
        self._project_desc = self._read_project_description()
        self._tab_state = {
            key: {"stop": threading.Event(), "thread": None, "draft": "",
                  "last_rejection": ""}
            for key in REGISTRY
        }
        self._tab_widgets: dict = {}   # populated by _build_tab
        # Per-session backend override (Phase 1.7).  Picked from a dropdown
        # in the header; never persisted to Settings.  Defaults to the
        # "Default" label which falls through to the configured provider.
        # _llm_cfg_resolved() reads this var on every Generate click;
        # _backend_summary() reflects it in the header label.
        self._backend_override_var = tk.StringVar(value=_BACKEND_DEFAULT)

        # C5: warm-up flag — ensure we only fire once per dialog session.
        self._warmup_done = False

        # ── UI ─────────────────────────────────────────────────────────────
        self._build_header_section()
        self._build_notebook()
        self._build_footer_section()
        self._maybe_warmup_ollama()

        # Resolve initial range so tabs can Generate immediately.
        self._refresh_range()
        self._centre_on_parent(parent)

        # Close-button wiring — cancel all tab workers before destroy.
        orig_destroy = self.destroy
        def _safe_destroy():
            for state in self._tab_state.values():
                state["stop"].set()
            orig_destroy()
        self.protocol("WM_DELETE_WINDOW", _safe_destroy)

    # ── Header (range picker + backend label) ──────────────────────────────

    def _build_header_section(self):
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=12)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📝  Doc Updates", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)

        # Range row
        row = tk.Frame(hdr, bg=C["base"])
        row.pack(fill=tk.X, pady=(8, 4))
        tk.Label(row, text="Commit range:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._range_var = tk.StringVar(value=_RANGE_MODES[0][0])
        self._range_cb = ttk.Combobox(
            row, textvariable=self._range_var,
            values=[m[0] for m in _RANGE_MODES],
            state="readonly", width=28,
        )
        self._range_cb.pack(side=tk.LEFT)
        self._range_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_range())

        self._custom_var = tk.StringVar()
        self._custom_entry = ttk.Entry(row, textvariable=self._custom_var,
                                        width=22, font=("Consolas", 9))
        self._custom_entry.pack(side=tk.LEFT, padx=(6, 0))
        self._custom_entry.configure(state=tk.DISABLED)
        self._custom_entry.bind("<Return>", lambda _e: self._refresh_range())

        ttk.Button(row, text="⟳ Apply range",
                   command=self._refresh_range).pack(side=tk.LEFT, padx=(6, 0))

        # Range-resolution info
        self._range_info_var = tk.StringVar(value="(resolving…)")
        tk.Label(hdr, textvariable=self._range_info_var,
                 bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(anchor=tk.W, pady=(2, 0))

        # Backend row: dropdown override + summary label.
        # The dropdown is a per-session override (not persisted to Settings).
        # The label reflects whatever _llm_cfg_resolved() will return on the
        # next Generate click; visual style changes when the override is
        # active so the user can't miss they're on a transient setting.
        backend_row = tk.Frame(hdr, bg=C["base"])
        backend_row.pack(fill=tk.X, pady=(2, 0))
        tk.Label(backend_row, text="Backend override:", bg=C["base"],
                 fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._backend_override_cb = ttk.Combobox(
            backend_row, textvariable=self._backend_override_var,
            values=_BACKEND_OPTIONS,
            state="readonly", width=24,
        )
        self._backend_override_cb.pack(side=tk.LEFT)
        self._backend_override_cb.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._on_backend_override_changed())

        self._backend_var = tk.StringVar(value=self._backend_summary())
        self._backend_lbl = tk.Label(hdr, textvariable=self._backend_var,
                                       bg=C["base"], fg=C["overlay0"],
                                       font=("Segoe UI", 8))
        self._backend_lbl.pack(anchor=tk.W, pady=(2, 0))

    def _on_backend_override_changed(self):
        """Dropdown handler — refresh the summary label + visual style.

        Does NOT touch any in-flight worker.  Workers snapshot their config
        at Generate-click time via _llm_cfg_resolved(); changing the
        dropdown while a worker is running only affects the NEXT click.
        """
        try:
            self._backend_var.set(self._backend_summary())
            override = self._backend_override_var.get()
            if override == _BACKEND_DEFAULT:
                # Restore the dim grey for the default config-driven backend.
                self._backend_lbl.configure(fg=C["overlay0"])
            else:
                # Peach accent makes it obvious we're on a transient override.
                self._backend_lbl.configure(fg=C["peach"])
        except tk.TclError:
            pass

    def _backend_summary(self):
        cfg = self._llm_cfg_resolved()
        prov = cfg.get("provider", "(none configured)")
        model = cfg.get("model") or "(default)"
        override = self._backend_override_var.get()
        if override != _BACKEND_DEFAULT:
            return f"⚡ OVERRIDE → {prov} / {model}"
        which = "ask_tab_llm" if (self._cfg.raw or {}).get("ask_tab_llm") \
                else "commit_message_llm"
        return f"Backend: {which} → {prov} / {model}"

    def _llm_cfg_resolved(self):
        """Resolve which LLM config dict to use for the next Generate click.

        Honours the per-session backend override dropdown.  When the
        override is active, builds a synthetic config by WHITELISTING only
        the fields the target provider's dispatcher reads — avoids leaking
        an Anthropic api_key_env into a forced Ollama call, etc.
        """
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        base = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
        override = self._backend_override_var.get()
        if override == _BACKEND_OLLAMA:
            # _call_llm openai_compatible path reads: provider, model,
            # base_url, enabled, timeout_seconds.  Only INHERIT the base
            # config's base_url if base was already an Ollama-compatible
            # provider — otherwise an Anthropic / OpenAI base_url would
            # silently get used for a forced Ollama call and fail.
            base_provider = (base.get("provider") or "").lower()
            base_url_safe = base.get("base_url") if base_provider in (
                "ollama", "openai_compatible") else None
            return {
                "enabled":  True,
                "provider": "ollama",
                "model":    base.get("model") or "qwen2.5-coder:14b",
                "base_url": base_url_safe or "http://localhost:11434",
            }
        if override == _BACKEND_CLAUDE_CLI:
            # dispatch_llm claude_cli path reads provider + model from the
            # config; exe path / cwd / timeout come from explicit args.
            return {
                "enabled":  True,
                "provider": "claude_cli",
                "model":    self._cfg.claude_cli_model or "",
            }
        return base

    def _refresh_range(self):
        """Resolve the selected range mode + update range_info + clear drafts.

        Called on dialog open AND every time the user changes the range
        dropdown / custom-ref / Apply range button.  Resolving a new range
        invalidates any in-flight drafts because their context is stale.
        """
        # Map label → mode token
        label = self._range_var.get()
        mode = next((m for lbl, m in _RANGE_MODES if lbl == label),
                    "since_last_doc")
        # Toggle custom-entry editability
        self._custom_entry.configure(
            state=(tk.NORMAL if mode == "custom" else tk.DISABLED))

        custom_ref = self._custom_var.get() if mode == "custom" else ""

        try:
            self._range_data = dd.resolve_commit_range(
                self._project_path, mode, custom_ref, self._cfg.git_exe)
        except Exception as exc:
            self._range_data = None
            self._range_info_var.set(f"Range resolution failed: {exc}")
            return

        rd = self._range_data
        n = len(rd.get("commits") or [])
        if n == 0:
            self._range_info_var.set(
                f"{rd['range_label']} — no commits in range")
        else:
            self._range_info_var.set(
                f"{rd['range_label']} — {n} commit(s); "
                f"sparse={dd.is_sparse(rd['commits'])}")

        # Cancel any in-flight workers (their context just became stale).
        for state in self._tab_state.values():
            state["stop"].set()

    def _refresh_range_for_active_tab(self):
        """Re-resolve the commit range using the currently selected tab's
        ``doc_anchor_paths`` and update the displayed range label.

        Without this, the header label always reflects the global
        CHANGELOG / README anchor, hiding the fact that each DocType
        actually has its own per-file anchor (used at Generate time).
        """
        try:
            current_idx = self._notebook.index(self._notebook.select())
        except (tk.TclError, IndexError):
            return
        keys = getattr(self, "_visible_tab_keys", []) or []
        if current_idx < 0 or current_idx >= len(keys):
            return
        key = keys[current_idx]
        dt = REGISTRY.get(key)
        if dt is None:
            return

        label = self._range_var.get()
        mode = next((m for lbl, m in _RANGE_MODES if lbl == label),
                    "since_last_doc")
        custom_ref = self._custom_var.get() if mode == "custom" else ""

        try:
            rd = dd.resolve_commit_range(
                self._project_path, mode, custom_ref, self._cfg.git_exe,
                pathspecs=dt.doc_anchor_paths or None,
            )
        except Exception as exc:
            self._range_data = None
            self._range_info_var.set(f"Range resolution failed: {exc}")
            return

        self._range_data = rd
        n = len(rd.get("commits") or [])
        if n == 0:
            self._range_info_var.set(
                f"{rd['range_label']} — no commits in range")
        else:
            self._range_info_var.set(
                f"{rd['range_label']} — {n} commit(s); "
                f"sparse={dd.is_sparse(rd['commits'])}")

    # ── Notebook + per-tab construction ────────────────────────────────────

    def _build_notebook(self):
        nb_wrap = tk.Frame(self, bg=C["base"], padx=18)
        nb_wrap.pack(fill=tk.BOTH, expand=True)
        self._notebook = ttk.Notebook(nb_wrap)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        # Track the order in which tabs are added so <<NotebookTabChanged>>
        # can map the selected tab index back to a REGISTRY key (REGISTRY
        # contains keys whose tabs are hidden, e.g. missing target file).
        self._visible_tab_keys: list = []

        for key, dt in REGISTRY.items():
            if dt.target_filename:
                target = os.path.join(self._project_path, dt.target_filename)
                if not (os.path.exists(target) or dt.auto_create):
                    continue
            # file-picker types (target_filename == "") always shown
            generate_label = f"Generate {dt.label}"
            self._build_tab(key, dt.target_filename, generate_label)
            self._visible_tab_keys.append(key)

        # Per-tab commit-range refresh: each DocType's doc_anchor_paths
        # produces its own "since last doc commit" SHA, so switching tabs
        # must re-resolve the range and update the displayed label.
        self._notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _e: self._refresh_range_for_active_tab(),
        )

    def _build_tab(self, key, target_file, generate_label):
        frame = tk.Frame(self._notebook, bg=C["base"], padx=8, pady=8)
        self._notebook.add(frame, text=f"  {key.upper()}  ")

        is_file_picker = (target_file == "")
        target_var = None

        if is_file_picker:
            # File-picker row: combobox + refresh button
            picker_row = tk.Frame(frame, bg=C["base"])
            picker_row.pack(fill=tk.X, pady=(0, 4))
            tk.Label(picker_row, text="Target file:",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Consolas", 8)).pack(side=tk.LEFT)
            target_var = tk.StringVar()
            file_list = self._list_picker_files(key)
            cb = ttk.Combobox(picker_row, textvariable=target_var,
                              values=file_list, width=40, state="normal")
            if file_list:
                target_var.set(file_list[0])
            cb.pack(side=tk.LEFT, padx=(6, 0))

            def _refresh_picker(k=key, c=cb, v=target_var):
                new_list = self._list_picker_files(k)
                c["values"] = new_list
                if new_list and not v.get():
                    v.set(new_list[0])

            ttk.Button(picker_row, text="↻",
                       command=_refresh_picker).pack(side=tk.LEFT, padx=(4, 0))
        else:
            # Static target label
            target_path = os.path.join(self._project_path, target_file)
            tk.Label(frame,
                     text=f"Target: {target_file}    ({target_path})",
                     bg=C["base"], fg=C["overlay0"],
                     font=("Consolas", 8)).pack(anchor=tk.W, pady=(0, 4))

        # v4 Fix 2a: read-only warning banner above the text widget.
        # Surfaces simulate-time title-rejection / mismatch warnings
        # WITHOUT polluting the editable text payload — keeps the Apply
        # button's data clean. pack_forget'd by default; the
        # `_show_warning_banner` / `_hide_warning_banner` helpers
        # toggle visibility.
        warning_var = tk.StringVar(value="")
        warning_lbl = tk.Label(
            frame, textvariable=warning_var,
            bg=C["base"], fg=C["red"], font=("Segoe UI", 9, "bold"),
            wraplength=900, justify=tk.LEFT, anchor=tk.W,
        )
        # Not packed yet — visibility managed by _show_warning_banner.

        # Editable text area for the draft
        txt_wrap = tk.Frame(frame, bg=C["mantle"])
        txt_wrap.pack(fill=tk.BOTH, expand=True)
        txt = tk.Text(txt_wrap, font=("Consolas", 9),
                      bg=C["mantle"], fg=C["text"],
                      insertbackground=C["text"],
                      relief=tk.FLAT, padx=6, pady=4, wrap=tk.WORD,
                      height=20)
        vsb = ttk.Scrollbar(txt_wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Empty-state placeholder
        txt.insert("1.0",
                   "(no draft yet — click Generate to draft from the "
                   "selected commit range)")
        txt.tag_add("placeholder", "1.0", tk.END)
        txt.tag_configure("placeholder", foreground=C["overlay0"])

        # <<Modified>> binding — user-typed edits re-enable Apply if the
        # buffer holds non-placeholder content.  Apply may have been hard-
        # disabled by a previous "all filtered" generate result; without
        # this binding the user couldn't rescue the situation by typing
        # their own bullet.  Tk fires <<Modified>> once per flag toggle;
        # _on_text_modified resets the flag so subsequent edits also fire.
        txt.bind("<<Modified>>",
                  lambda _e, k=key: self._on_text_modified(k))
        # Clear the modified flag set by the programmatic insert above so
        # the first USER edit (not our setup insert) fires the binding.
        try:
            txt.edit_modified(False)
        except tk.TclError:
            pass

        # Buttons + status
        btn_row = tk.Frame(frame, bg=C["base"])
        btn_row.pack(fill=tk.X, pady=(6, 0))
        gen_btn = ttk.Button(btn_row, text=f"🔄 {generate_label}",
                             command=lambda k=key: self._on_generate(k))
        gen_btn.pack(side=tk.LEFT)
        copy_btn = ttk.Button(btn_row, text="📋 Copy",
                              command=lambda k=key: self._on_copy(k))
        copy_btn.pack(side=tk.LEFT, padx=(6, 0))
        apply_btn = ttk.Button(btn_row, text="✓ Apply via Proposal",
                               command=lambda k=key: self._on_apply(k))
        apply_btn.pack(side=tk.LEFT, padx=(6, 0))

        # C3: retry-with-feedback button — hidden until apply is rejected.
        feedback_btn = ttk.Button(btn_row, text="🔁 Regenerate with feedback",
                                  command=lambda k=key: self._on_regenerate_with_feedback(k))
        # NOT packed yet — revealed by _on_apply_result on failure.

        # B2: per-tab checkbox to enable agentic tokensave tool use.
        ts_tools_var = tk.BooleanVar(value=True)
        ts_tools_chk = ttk.Checkbutton(btn_row, text="🔍 Tokensave tools",
                                        variable=ts_tools_var)
        ts_tools_chk.pack(side=tk.RIGHT, padx=(6, 0))

        # v4.1: tooltip explaining the Ollama-only asymmetry. The checkbox
        # only routes to the agentic LocalAgent loop for ollama /
        # openai_compatible providers — Claude CLI and Anthropic use
        # single-shot completion regardless. The always-on tokensave
        # GROUNDING block (separate from this checkbox) runs for every
        # backend; the checkbox specifically enables runtime tool-use.
        from theme import _Tooltip
        _Tooltip(
            ts_tools_chk,
            "Enables an Ollama-only agentic loop where the local model "
            "can call tokensave_search and tokensave_context as tools "
            "mid-generation. Silently ignored for Claude CLI / Anthropic "
            "/ OpenAI — those backends use single-shot completion. "
            "Disable if Ollama drafts time out. "
            "(The always-on tokensave grounding block runs separately "
            "for all backends.)",
        )

        status_var = tk.StringVar(value="")
        # wraplength keeps long filter messages from pushing buttons off-screen
        # on narrow window sizes; status text wraps inside the row instead.
        status_lbl = tk.Label(btn_row, textvariable=status_var,
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8),
                              wraplength=420, justify=tk.LEFT)
        status_lbl.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)

        self._tab_widgets[key] = {
            "frame":          frame,
            "text":           txt,
            "txt_wrap":       txt_wrap,    # v4: pack-anchor for warning banner
            "gen_btn":        gen_btn,
            "apply_btn":      apply_btn,
            "feedback_btn":   feedback_btn,
            "ts_tools_var":   ts_tools_var,
            "btn_row":        btn_row,
            "status_var":     status_var,
            "status_lbl":     status_lbl,
            "warning_var":    warning_var,   # v4: simulate-time warnings
            "warning_lbl":    warning_lbl,
            "target":         target_file,
            "target_var":     target_var,   # None for fixed-target DocTypes
        }

    # ── File-picker helpers ────────────────────────────────────────────────

    # Glob patterns relative to the project root. Each value is a list so a
    # picker can pull from multiple locations within the repo. External
    # locations are added via the dedicated discovery functions below.
    _PICKER_GLOBS = {
        "memory":       ["memory/*.md"],
        "docs_generic": ["docs/*.md"],
    }

    def _list_picker_files(self, key):
        """Return display paths for the file-picker combobox of ``key``.

        Aggregates files from:
          1. Repo-local globs in _PICKER_GLOBS for this key
          2. Optional user-config `<key>_paths` glob list in manager-config.json
          3. (memory only) Auto-discovered CCD-managed memory directory
        """
        import glob
        paths: list = []

        for pat in self._PICKER_GLOBS.get(key, ["*.md"]):
            paths += sorted(glob.glob(os.path.join(self._project_path, pat)))

        # Config-provided extra glob patterns (per-key field, e.g. "memory_paths")
        cfg_paths = (self._cfg.raw or {}).get(f"{key}_paths") or []
        for pat in cfg_paths:
            for p in sorted(glob.glob(os.path.expanduser(pat))):
                if os.path.isfile(p) and p.lower().endswith(".md"):
                    paths.append(p)

        if key == "memory":
            paths += self._discover_ccd_memory_files()

        # Stable de-dup while preserving order
        seen: set = set()
        unique: list = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        return [self._display_path(p) for p in unique]

    def _discover_ccd_memory_files(self):
        """Find CCD-managed memory files for this project.

        Primary path (fast): construct the CCD-encoded directory name by
        replacing ``:``, ``\\``, ``/``, and SPACE with ``-``. This matches
        the actual on-disk encoding verified via live listing of
        ``~/.claude/projects/`` (e.g. ``D:\\Claude Co worker\\Token Save
        Manager Source`` → ``D--Claude-Co-worker-Token-Save-Manager-Source``).

        Fallback path (durable): if the encoded directory doesn't exist,
        scan ``~/.claude/projects/*/memory`` and pick the directory whose
        name has the highest token-overlap score against the project root
        path. This survives future encoding changes because we never
        reverse-engineer the encoding — we match purely by content.
        """
        import glob
        ccd_root = os.path.expanduser("~/.claude/projects")
        if not os.path.isdir(ccd_root):
            return []

        # Primary: verified encoding
        encoded = self._project_path
        for ch in (":", "\\", "/", " "):
            encoded = encoded.replace(ch, "-")
        primary = os.path.join(ccd_root, encoded, "memory")
        if os.path.isdir(primary):
            return sorted(glob.glob(os.path.join(primary, "*.md")))

        # Fallback: token-overlap scan
        import re as _re
        project_tokens = {
            t.lower() for t in _re.split(r"[\\/ \-_]+", self._project_path)
            if len(t) > 2 and t.lower() not in {"src", "lib", "etc"}
        }
        if not project_tokens:
            return []
        best_dir, best_score = None, 0
        try:
            for entry in os.listdir(ccd_root):
                entry_tokens = {
                    t.lower() for t in _re.split(r"[\\/ \-_]+", entry)
                    if len(t) > 2
                }
                score = len(project_tokens & entry_tokens)
                if score > best_score:
                    memdir = os.path.join(ccd_root, entry, "memory")
                    if os.path.isdir(memdir):
                        best_dir, best_score = memdir, score
        except OSError:
            return []
        if best_dir:
            return sorted(glob.glob(os.path.join(best_dir, "*.md")))
        return []

    def _display_path(self, abs_path):
        """Return project-relative display path when possible; absolute path
        when the file lives outside the project tree (CCD memory files,
        external user-configured paths)."""
        try:
            rel = os.path.relpath(abs_path, self._project_path)
            # Detect "way outside" — multiple .. segments. Prefer absolute
            # display in that case so the dropdown doesn't look like garbage.
            if rel.count(".." + os.sep) >= 2:
                return abs_path.replace("\\", "/")
            return rel.replace("\\", "/")
        except ValueError:
            return abs_path.replace("\\", "/")

    def _resolve_target_path(self, key):
        """Return the absolute target path for ``key``, or None if unresolvable."""
        dt = REGISTRY[key]
        if dt.target_filename:
            return os.path.join(self._project_path, dt.target_filename)
        var = (self._tab_widgets.get(key) or {}).get("target_var")
        if var:
            rel = var.get().strip()
            if rel:
                return os.path.join(self._project_path, rel)
        return None

    def _maybe_warmup_ollama(self):
        """C5: Fire a warm-up ping once per session if the setting is on."""
        if self._warmup_done:
            return
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        if not raw.get("ollama_warmup", False):
            return
        llm_cfg = self._llm_cfg_resolved()
        provider = (llm_cfg or {}).get("provider", "").lower()
        if provider not in ("ollama", "openai_compatible"):
            return
        base_url = (llm_cfg or {}).get("base_url", "") or "http://localhost:11434"
        model = (llm_cfg or {}).get("model", "")
        self._warmup_done = True

        def _ping():
            from helpers.llm import warmup_ollama
            warmup_ollama(base_url, model, timeout=10)

        threading.Thread(target=_ping, daemon=True,
                         name="doc-drafter-warmup").start()

    def _build_footer_section(self):
        ftr = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        ftr.pack(fill=tk.X)
        ttk.Button(ftr, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _centre_on_parent(self, parent):
        self.update_idletasks()
        w, h = 880, 660
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Project description (for prompt context) ───────────────────────────

    def _read_project_description(self):
        """First non-blank, non-header line of README.md, or empty string."""
        readme = os.path.join(self._project_path, "README.md")
        if not os.path.isfile(readme):
            return ""
        try:
            with open(readme, encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    return s[:200]
        except OSError:
            pass
        return ""

    # ── Generate flow ──────────────────────────────────────────────────────

    def _show_generate_blocker(self, key, msg):
        """Show an early-exit message in BOTH the small status label and
        the main text widget so it can't be missed.

        Used for the early-return paths in ``_on_generate`` (no range data,
        no LLM config, no commits) and for ``_on_generate_error`` failures.
        Replaces the existing draft text — that's intentional, because none
        of those paths ever produced a real draft to overwrite.
        """
        self._set_status(key, msg, C["red"])
        try:
            txt = self._tab_widgets[key]["text"]
            state = self._tab_state.get(key, {})
            with _suppressed_modified(state):
                txt.delete("1.0", tk.END)
                txt.insert("1.0", f"⚠  {msg}")
                txt.tag_add("placeholder", "1.0", tk.END)
                try:
                    txt.edit_modified(False)
                except tk.TclError:
                    pass
        except (tk.TclError, KeyError):
            pass

    def _on_generate(self, key):
        """Spawn a background worker that builds the prompt + dispatches LLM."""
        try:
            self._on_generate_impl(key)
        except Exception as exc:
            # Safety net: any uncaught exception inside the click handler
            # (e.g. a stale attribute reference like the old self._mode_var
            # bug) used to vanish into stderr, leaving the dialog looking
            # like the button was dead. Surface it in the text widget so
            # the failure is impossible to miss next time.
            import traceback
            tb = traceback.format_exc(limit=4)
            self._show_generate_blocker(
                key, f"Generate aborted: {type(exc).__name__}: {exc}\n\n{tb}")

    def _on_generate_impl(self, key):
        """Actual implementation — wrapped by _on_generate's safety net."""
        if not self._range_data:
            self._show_generate_blocker(
                key, "Resolve a commit range first.")
            return

        llm_cfg = self._llm_cfg_resolved()
        if not llm_cfg or not llm_cfg.get("provider"):
            self._show_generate_blocker(
                key,
                "No AI configured — set a provider in Settings → Ask Tab AI.")
            return

        # Per-tab anchor: recompute the range using this DocType's own
        # doc_anchor_paths so each tab finds commits since ITS file was
        # last updated, not since the shared CHANGELOG/README anchor.
        # NOTE: self._range_var holds the human-readable LABEL; map back
        # to the mode token the same way _refresh_range does. There is
        # no self._mode_var — referencing one (as this method used to)
        # raised AttributeError on every click, silently aborting Generate.
        dt_for_range = REGISTRY[key]
        label = self._range_var.get()
        mode = next((m for lbl, m in _RANGE_MODES if lbl == label),
                    "since_last_doc")
        custom_ref = self._custom_var.get() if mode == "custom" else ""
        rd = dd.resolve_commit_range(
            self._project_path, mode, custom_ref, self._cfg.git_exe,
            pathspecs=dt_for_range.doc_anchor_paths or None,
        )

        if not rd.get("commits"):
            self._show_generate_blocker(
                key,
                f"No commits in range for this doc "
                f"({rd.get('range_label', '?')}) — nothing to draft.")
            return

        # v4 Fix 5b: Python-side mismatch detection for generic-doc /
        # tokensave_guide tabs. Cheap commit-tokens-only `aligned` check
        # on the main thread before any LLM commitment. If misaligned,
        # ask the user via stateless askyesno — no leaky Override mode,
        # no LLM-conditional routing.
        if key in ("docs_generic", "tokensave_guide"):
            target_path = self._resolve_target_path(key)
            if target_path and os.path.exists(target_path):
                aligned = True   # fail-open default
                try:
                    with open(target_path, encoding="utf-8-sig") as f:
                        existing_full = f.read()
                    # commits-only token set — skip the synchronous git
                    # call for changed_files (the worker does the full
                    # version with path tokens later). Subject + scope
                    # tokens are sufficient to catch the obvious mismatch.
                    _, aligned = dd._select_candidate_sections(
                        existing_full, [], rd["commits"])
                except (OSError, AttributeError):
                    pass   # fail-open
                if not aligned:
                    target_basename = os.path.basename(target_path)
                    confirm = messagebox.askyesno(
                        "No section alignment",
                        f"The commit subjects don't appear to align with "
                        f"any section in {target_basename}.\n\n"
                        f"This file may not be the right target for the "
                        f"current commit range.\n\n"
                        f"Generate anyway?",
                        parent=self,   # X11 z-order anchor
                        default="no",
                    )
                    if not confirm:
                        self._show_warning_banner(
                            key,
                            f"Mismatch — commit subjects don't align "
                            f"with any section in {target_basename}. "
                            f"Pick a different target file, widen the "
                            f"commit range, or click Generate to override.")
                        return

        # Cancel any in-flight generate for THIS tab.
        self._tab_state[key]["stop"].set()
        self._tab_state[key]["stop"] = threading.Event()
        stop_event = self._tab_state[key]["stop"]

        # v4: clear any stale warning banner from a previous generation.
        self._hide_warning_banner(key)

        self._tab_widgets[key]["gen_btn"].configure(state=tk.DISABLED)
        self._set_status(key, "Drafting…", C["overlay0"])

        # v4 Fix 4c: start the elapsed-time tick. Self-cancels on
        # completion / dialog destruction / stop-event identity change.
        self._start_draft_tick(key, stop_event)

        # Snapshot context for the worker.
        commits     = rd["commits"]
        classified  = _classify_commits_for_changelog(commits)
        boundary    = rd.get("boundary_note")
        range_spec  = rd.get("range_spec", "")
        project     = self._project_path
        tokensave_exe = self._cfg.tokensave_exe

        # C3: capture any pending feedback from a failed apply, then clear it
        # so that a second generate is back to normal (one-shot injection).
        last_rejection = self._tab_state[key].get("last_rejection", "")
        self._tab_state[key]["last_rejection"] = ""
        # Hide the feedback button since a new generate is starting.
        try:
            self._tab_widgets[key]["feedback_btn"].pack_forget()
        except (tk.TclError, KeyError):
            pass

        def _worker():
            # Both tabs get the always-on changed-file context (Phase 1.8).
            # Pushes the model toward per-file granularity regardless of
            # commit-message quality — symmetric for CHANGELOG and README.
            # The 60-path cap inside changed_file_paths bounds prompt size.
            changed_files = dd.changed_file_paths(
                project, range_spec, self._cfg.git_exe)

            dt = REGISTRY[key]
            target_path = self._resolve_target_path(key)
            if not target_path:
                self.after(0, lambda: self._on_generate_error(
                    key, "No target file selected — pick a file first."))
                return
            existing = dt.read_existing(target_path)

            # Theme B1: tokensave grounding injection.  build_grounding_block
            # returns "" silently when .tokensave/ is absent or the recipe
            # is unset — the prompt is always valid with or without it.
            #
            # v4.1: also runs build_codegraph_block in parallel (silently
            # skipped if codegraph isn't installed / index is unhealthy)
            # and combines both via build_combined_grounding for per-source
            # cap + line-level dedup.
            from helpers.doc_grounding import (
                build_grounding_block, build_codegraph_block,
                build_combined_grounding,
            )
            tokensave_block = ""
            if dt.tokensave_recipe:
                tokensave_block = build_grounding_block(
                    project, dt.tokensave_recipe,
                    commit_range=range_spec,
                    tokensave_exe=tokensave_exe,
                )
            codegraph_block = ""
            if getattr(dt, "codegraph_recipe", None):
                # v4.3: ensure the index is fresh before building the block.
                _cg_exe = self._cfg.codegraph_exe or ""
                if _cg_exe:
                    try:
                        from helpers.codegraph_freshness import ensure_fresh
                        ensure_fresh(project, _cg_exe)
                    except Exception:
                        pass
                codegraph_block = build_codegraph_block(
                    project, dt.codegraph_recipe,
                    commit_range=range_spec,
                    changed_files=changed_files,
                    codegraph_exe=_cg_exe,
                )
            grounding = build_combined_grounding(
                tokensave_block, codegraph_block)

            # v4: build_prompt returns PromptBuildResult — named-field
            # access. backend_hint=True triggers smaller candidate-body
            # budget for local agentic backends (Ollama).
            _provider = (llm_cfg.get("provider") or "").lower()
            is_local_backend = (
                _provider == "ollama"
                or (_provider == "openai_compatible"
                    and "localhost" in (llm_cfg.get("base_url") or "").lower())
            )
            prompt_result = dt.build_prompt(
                commits, classified, existing,
                self._project_name, self._project_desc,
                changed_files, boundary,
                grounding_block=grounding,
                backend_hint=is_local_backend)
            system = prompt_result.system
            user = prompt_result.user
            # prompt_result.aligned / .warnings consumed by step 5
            # (banner + mismatch askyesno). Not used in the build path
            # itself — the worker already committed to dispatch_llm.

            # C3: append rejection feedback as a note in the user prompt
            # (one-shot — last_rejection was cleared before the worker started).
            if last_rejection:
                user = (user + "\n\n---\nNote: your previous draft was rejected "
                        f"for this reason — please address it:\n{last_rejection}")

            # C1: merge global num_ctx setting into per-call gen_params so
            # dispatch_llm can pass it to _call_llm / _call_openai_compat.
            raw_cfg = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
            global_num_ctx = raw_cfg.get("ollama_num_ctx", 0)
            gen_params = dict(dt.gen_params)
            if global_num_ctx and "num_ctx" not in gen_params:
                gen_params["num_ctx"] = global_num_ctx

            if stop_event.is_set():
                return

            # B2: read per-tab tokensave-tools checkbox.
            use_ts_tools = bool(
                self._tab_widgets.get(key, {}).get("ts_tools_var",
                    tk.BooleanVar()).get()
            )
            ts_exe = getattr(self._cfg, "tokensave_exe", "") or ""

            # v4 Fix 4b: timeout bumped 120 → 300 s. Local agentic loops
            # (Ollama qwen2.5-coder:14b) with multiple candidate-section
            # bodies were timing out on ROADMAP / DOCS_GENERIC at the
            # previous ceiling. 300 s matches the integration-check CLI
            # audit timeout in update_poller.
            text, err = dd.dispatch_llm(
                llm_cfg, system, user,
                claude_cli_exe=self._cfg.claude_cli_exe,
                cwd=project, timeout=300,
                gen_params=gen_params or None,
                examples=dt.examples or None,
                enable_tokensave_tools=use_ts_tools,
                tokensave_exe=ts_exe,
                stop_event=stop_event,
            )

            if stop_event.is_set():
                return

            if err or not text:
                self.after(0, lambda m=(err or "empty result"), se=stop_event:
                           self._on_generate_error(key, m, captured_stop_event=se))
                return
            # v4: pass the captured stop_event so _on_generate_done can
            # verify identity. If the user clicked Generate again while
            # this worker was running, the stop_event in _tab_state has
            # been replaced and this result is stale.
            self.after(0, lambda t=text, se=stop_event:
                       self._on_generate_done(key, t, captured_stop_event=se))

        t = threading.Thread(target=_worker, daemon=True,
                              name=f"doc-drafter:{key}")
        self._tab_state[key]["thread"] = t
        t.start()

    def _filter_draft_text(self, key, raw_text):
        """Filter raw LLM output at generate time.

        Delegates to the DocType's filter_draft callable.
        Returns ``(filtered_text, trunc_n, dup_n, noop_n)``.
        """
        dt = REGISTRY[key]
        target_path = self._resolve_target_path(key) or ""
        return dt.filter_draft(raw_text, target_path)

    def _compute_mirror_warning(self, key, draft_text):
        """Return a status-bar warning string if README mirror-contract would
        fail on this draft, else None.  CHANGELOG always returns None.
        """
        if key != "readme" or not draft_text or not draft_text.strip():
            return None
        try:
            from helpers.doc_drafter import (
                split_readme_subsection, _LITERAL_PLACEHOLDER_RE,
                _mirror_contract_check,
            )
            from helpers.readme_patch import read_subsection_bullets
            target_path = os.path.join(self._project_path,
                                        self._tab_widgets[key]["target"])
            parsed = split_readme_subsection(draft_text)
            if parsed is None:
                return None
            header_line, bullets = parsed
            if _LITERAL_PLACEHOLDER_RE.search(header_line):
                return (f"⚠ HEADER ERROR: contains template placeholder "
                        f"({header_line!r}).  Apply will be rejected.  "
                        f"Regenerate or edit the header.")
            existing = read_subsection_bullets(target_path, header_line)
            if not existing:
                return None
            draft_bullets = [ln.lstrip() for ln in bullets.splitlines()
                              if ln.lstrip().startswith(("- ", "* "))]
            ok, kept, missing, _examples = _mirror_contract_check(
                draft_bullets, existing)
            if ok:
                return None
            return (f"⚠ DESTRUCTIVE: only {kept}/{kept + missing} existing "
                    f"bullets preserved.  Apply would DELETE {missing} "
                    f"from README.  Regenerate or edit draft to add back.")
        except (tk.TclError, RuntimeError, OSError):
            return None

    def _on_generate_done(self, key, raw_text, captured_stop_event=None):
        try:
            if not self.winfo_exists():
                return
            # v4: stop the draft tick + stop-event identity guard. If the
            # captured Event is no longer the current one, a newer
            # Generate has been started — drop this stale result.
            self._stop_draft_tick(key)
            if (captured_stop_event is not None
                    and captured_stop_event is not self._tab_state.get(
                        key, {}).get("stop")):
                return
            # Filter raw model output against existing target content
            filtered_text, t_n, d_n, n_n = self._filter_draft_text(
                key, raw_text)
            w = self._tab_widgets[key]
            txt = w["text"]
            # v4.4 (Gemini #2): the context manager guarantees
            # suppress_modified is reset even if insert() raises.
            with _suppressed_modified(self._tab_state[key]):
                txt.delete("1.0", tk.END)
                has_content = bool(filtered_text.strip())
                if has_content:
                    txt.insert("1.0", filtered_text.strip())
                    self._tab_state[key]["draft"] = filtered_text.strip()
                else:
                    # Visual placeholder ONLY — Apply button is hard-disabled
                    # below so this string never reaches the patchers.  README
                    # gets an extra-explicit message because its REPLACE patcher
                    # would delete the existing sub-section if applied with
                    # nothing.  CHANGELOG's append-only patcher is recoverable
                    # (no destructive failure mode).
                    if key == "readme":
                        placeholder = (
                            "(all bullets filtered — README's REPLACE patcher "
                            "would DELETE the existing sub-section if Applied "
                            "with no bullets.  Apply is disabled.  Click "
                            "Generate to retry.)"
                        )
                    else:
                        placeholder = ("(all bullets filtered — click "
                                       "Generate to retry)")
                    txt.insert("1.0", placeholder)
                    txt.tag_add("placeholder", "1.0", tk.END)
                    self._tab_state[key]["draft"] = ""
                # Reset the text-widget modified flag so the <<Modified>>
                # binding fires on subsequent USER edits (not our insert).
                try:
                    txt.edit_modified(False)
                except tk.TclError:
                    pass
            w["gen_btn"].configure(state=tk.NORMAL)
            # LOAD-BEARING: Apply enabled only when filtered text has content.
            # Empty filtered result means the visible text widget holds the
            # placeholder phrase — applying that would splice the literal
            # placeholder string into CHANGELOG.md / README.md.
            w["apply_btn"].configure(
                state=tk.NORMAL if has_content else tk.DISABLED)
            suffix = self._format_filter_suffix(t_n, d_n, n_n)
            if has_content:
                # Phase 1.9: compute mirror-contract warning for README.
                # If the draft would destroy existing bullets, surface the
                # warning in the status bar so user sees BEFORE clicking Apply
                # (Apply is also gated by the apply-time check, but a
                # generate-time warning lets the user Regenerate proactively).
                mirror_warn = self._compute_mirror_warning(key, filtered_text)
                base = "Draft ready — review and Apply."
                if mirror_warn:
                    self._set_status(key, base + "  " + mirror_warn, C["red"])
                else:
                    self._set_status(key, base + suffix, C["green"])
            else:
                self._set_status(
                    key,
                    f"All bullets filtered.{suffix}  Click Generate to retry.",
                    C["red"])
        except (tk.TclError, RuntimeError):
            # v4.4: the _suppressed_modified context manager guarantees the
            # flag is cleared via try/finally before this except runs, so no
            # explicit cleanup is needed here.
            return

    def _on_text_modified(self, key):
        """Re-enable Apply when the user manually types non-placeholder content.

        Without this binding, a user faced with an "all filtered" state who
        wants to write their own bullet would find Apply locked because the
        disable was set by the worker and never re-evaluated.

        Tk only fires <<Modified>> once per modified-flag toggle; we have to
        reset edit_modified(False) explicitly to keep receiving events.

        v4 extensions:
          - Skip when `state["suppress_modified"]` is True (programmatic
            insert in progress — _on_generate_done sets/clears this so its
            own insert doesn't self-clear the banner).
          - Clear the warning banner on actual user content mutation. The
            user has visually acknowledged the warning by starting to fix
            the draft; if they Ctrl-Z back to bad state, Apply-time
            validation re-surfaces the rejection.
        """
        try:
            if not self.winfo_exists():
                return
            state = self._tab_state.get(key) or {}
            if state.get("suppress_modified"):
                # Programmatic insert in progress — don't react.
                w = self._tab_widgets[key]
                try:
                    w["text"].edit_modified(False)
                except tk.TclError:
                    pass
                return
            w = self._tab_widgets[key]
            txt = w["text"]
            if not txt.edit_modified():
                return
            current = txt.get("1.0", "end-1c").strip()
            is_placeholder = (current.startswith("(all bullets filtered")
                              or current.startswith("(no draft yet"))
            if current and not is_placeholder:
                w["apply_btn"].configure(state=tk.NORMAL)
            # v4: actual user edit → clear warning banner.
            self._hide_warning_banner(key)
            txt.edit_modified(False)
            # Phase 1.9: debounced README mirror-warning re-check.  If the
            # user manually adds the missing bullets back, the status bar
            # warning from _on_generate_done is stale — refresh after a
            # 300 ms typing pause so each keystroke doesn't recompute.
            if key == "readme":
                if getattr(self, "_readme_check_after_id", None):
                    try:
                        self.after_cancel(self._readme_check_after_id)
                    except tk.TclError:
                        pass
                self._readme_check_after_id = self.after(
                    300, lambda: self._refresh_readme_warning())
        except (tk.TclError, RuntimeError):
            return

    def _refresh_readme_warning(self):
        """Debounced post-edit re-check of the README mirror-contract warning.

        Reads the current README text widget, recomputes the warning, and
        updates the status bar.  Keeps the UI honest about whether the
        current widget content (after manual edits) is still destructive.
        Called via 300 ms ``after()`` from `_on_text_modified` to avoid
        per-keystroke recomputation.
        """
        try:
            self._readme_check_after_id = None
            if not self.winfo_exists():
                return
            w = self._tab_widgets.get("readme")
            if not w:
                return
            current = w["text"].get("1.0", "end-1c").strip()
            if not current or current.startswith(("(all bullets filtered",
                                                    "(no draft yet")):
                return
            mirror_warn = self._compute_mirror_warning("readme", current)
            if mirror_warn:
                self._set_status("readme",
                                  "Draft edited.  " + mirror_warn, C["red"])
            else:
                self._set_status("readme",
                                  "Draft edited — review and Apply.",
                                  C["green"])
        except (tk.TclError, RuntimeError):
            return

    def _on_generate_error(self, key, msg, captured_stop_event=None):
        try:
            if not self.winfo_exists():
                return
            # v4: stop the tick + stop-event identity guard against stale
            # error callbacks from a superseded generation.
            self._stop_draft_tick(key)
            if (captured_stop_event is not None
                    and captured_stop_event is not self._tab_state.get(
                        key, {}).get("stop")):
                return
            self._tab_widgets[key]["gen_btn"].configure(state=tk.NORMAL)
            # Mirror the error into the text widget so it isn't hidden in
            # the 8 pt status label next to the Generate button. Earlier
            # silent-failure reports turned out to be users not noticing
            # the small label.
            self._show_generate_blocker(key, f"Error: {msg}")
        except (tk.TclError, RuntimeError):
            return

    # ── Copy / Apply flow ──────────────────────────────────────────────────

    def _on_copy(self, key):
        try:
            txt = self._tab_widgets[key]["text"].get("1.0", tk.END).rstrip()
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.update()
            self._set_status(key, "Copied to clipboard.", C["overlay0"])
        except tk.TclError:
            pass

    def _on_apply(self, key):
        """Push the current draft text through ProposalBridge → patcher."""
        # Snapshot the user-edited text on the main thread.
        text = self._tab_widgets[key]["text"].get("1.0", tk.END).rstrip()
        if not text or text.startswith("(no draft yet"):
            self._set_status(key, "Nothing to apply — generate a draft first.",
                             C["red"])
            return

        target_path = self._resolve_target_path(key)
        if not target_path:
            self._set_status(key, "No target file selected — pick a file first.",
                             C["red"])
            return

        # Build the WriteProposal (old vs new).  Phase 2.1: the proposed
        # content is the SIMULATED post-apply body (current body + this
        # draft fed through the same compute helpers the apply path uses).
        # This makes the diff honest — pre-2.1 the right pane showed only
        # the new bullets, which looked like an obliterating REPLACE even
        # though both patcher paths are append/insert.  See the user
        # screenshot in roadmap6_results.md (Phase 2.1 originator bug).
        #
        # Edit semantics survive: if the user trims/edits the proposed
        # body and accepts, the apply path's dedup filter (CHANGELOG) /
        # mirror-contract (README) handles the re-parse — adding existing
        # bullets twice is dropped by dedup, dropping mirrored bullets is
        # blocked by the 40 % preservation floor.
        dt = REGISTRY[key]
        # Multi-section simulator returns (before, after) tuple — both sides
        # are concatenated affected-section bodies. Single-section returns
        # the after-side body only (legacy contract).
        existing = dt.read_existing(target_path)
        sim_result = self._simulate_body(dt, target_path, text)
        if isinstance(sim_result, tuple) and len(sim_result) == 2:
            simulated_existing, simulated_proposed = sim_result
        else:
            simulated_existing, simulated_proposed = existing, sim_result
        _RATIONALE = {
            "changelog": (
                "Append-only CHANGELOG apply.  Diff shows current "
                "[Unreleased] body (left) vs simulated post-apply body "
                "(right).  Bullets appended into matching ### sub-sections; "
                "new ### sub-sections appended at the end of [Unreleased]."
            ),
            "readme": (
                "README highlights apply.  Diff shows current 'Recent "
                "highlights' body (left) vs simulated post-apply body "
                "(right).  Matching `**Header**` sub-section is REPLACED "
                "(mirror-contract enforces ≥40 % bullet preservation); "
                "non-matching headers create a new sub-section at the top."
            ),
        }
        rationale = _RATIONALE.get(
            key,
            f"{dt.label} apply.  Diff shows the affected sections only "
            "(left = before, right = after).  Each section is REPLACED in "
            "place; unmatched titles are refused as likely hallucinations."
        )
        # Fallback: if simulation failed (parse error / missing anchor),
        # fall back to the raw draft so the user can still see / edit it.
        proposed_for_dialog = simulated_proposed if simulated_proposed else text
        original_for_dialog = simulated_existing or existing or "(empty)"

        self._set_status(key, "Opening proposal…", C["overlay0"])
        self._tab_widgets[key]["apply_btn"].configure(state=tk.DISABLED)

        # Spawn worker — ProposalBridge.invoke() blocks on a threading.Event,
        # MUST NOT be called from the Tk main thread.
        def _worker():
            from dialogs.proposal import ProposalBridge, WriteProposal
            proposal = WriteProposal(
                filepath=target_path,
                original_content=original_for_dialog,
                proposed_content=proposed_for_dialog,
                rationale=rationale,
            )
            bridge = ProposalBridge(self._app, proposal, timeout_s=300.0)
            accepted, final_content = bridge.invoke()
            if not accepted or not final_content:
                self.after(0, lambda: self._on_apply_result(
                    key, False, "Proposal rejected or timed out."))
                return

            # Apply the final (possibly user-edited in the proposal dialog)
            # content via the appropriate patcher.  Both paths use
            # append-only insertion to avoid the small-model "drop existing
            # content" failure mode discovered during dogfooding.
            dt = REGISTRY[key]
            ok, msg, stats = dt.io_apply(target_path, final_content)
            self.after(0, lambda o=ok, m=msg, s=stats:
                       self._on_apply_result(key, o, m, s))

        threading.Thread(target=_worker, daemon=True,
                         name=f"doc-drafter-apply:{key}").start()

    def _on_apply_result(self, key, ok, msg, stats=None):
        try:
            if not self.winfo_exists():
                return
            self._tab_widgets[key]["apply_btn"].configure(state=tk.NORMAL)
            stats = stats or {}
            trunc_n = stats.get("truncated", 0)
            dup_n   = stats.get("duplicates", 0)
            noop_n  = stats.get("noop", 0)
            filter_suffix = self._format_filter_suffix(trunc_n, dup_n, noop_n)
            target_path = self._resolve_target_path(key)
            target = (os.path.relpath(target_path, self._project_path)
                      if target_path else self._tab_widgets[key]["target"])
            if ok:
                status_msg = f"✓ {msg}{filter_suffix}"
                self._set_status(key, status_msg, C["green"])
                self._on_log(f"[doc-drafter] {target}: {msg}{filter_suffix}",
                             C["green"])
                # Offer to commit the change (matches CHANGELOG drafter UX).
                self._on_commit_offer(self._project_path,
                                       f"{target} (doc-drafter)")
                # C3: clear rejection state on success.
                self._tab_state[key]["last_rejection"] = ""
                try:
                    self._tab_widgets[key]["feedback_btn"].pack_forget()
                except (tk.TclError, KeyError):
                    pass
            else:
                status_msg = f"✗ {msg}{filter_suffix}"
                self._set_status(key, status_msg, C["red"])
                self._on_log(f"[doc-drafter] {target}: {msg}{filter_suffix}",
                             C["red"])
                # C3: store rejection reason and reveal the retry button.
                short_reason = msg[:300] if msg else "apply rejected"
                self._tab_state[key]["last_rejection"] = short_reason
                try:
                    btn_row = self._tab_widgets[key]["btn_row"]
                    fb_btn  = self._tab_widgets[key]["feedback_btn"]
                    fb_btn.pack(in_=btn_row, side=tk.LEFT, padx=(6, 0))
                except (tk.TclError, KeyError):
                    pass
        except (tk.TclError, RuntimeError):
            return

    def _on_regenerate_with_feedback(self, key):
        """C3: Re-run Generate with the last rejection reason injected once."""
        try:
            self._tab_widgets[key]["feedback_btn"].pack_forget()
        except (tk.TclError, KeyError):
            pass
        self._on_generate(key)

    @staticmethod
    def _format_filter_suffix(trunc_n, dup_n, noop_n=0):
        """Format filter counts as a compact tail suffix.

        Bounded length so an unexpected 45+ count doesn't push UI off-screen.
        Returns empty string when ALL counts are zero (clean drafts get no
        noise).  ``noop_n`` defaults to 0 for backward compat with any older
        call sites that haven't been updated to the 4-tuple filter return.
        """
        if not trunc_n and not dup_n and not noop_n:
            return ""
        bits = []
        if trunc_n:
            bits.append(f"{trunc_n} truncated")
        if dup_n:
            bits.append(f"{dup_n} duplicate{'s' if dup_n != 1 else ''}")
        if noop_n:
            bits.append(f"{noop_n} placeholder{'s' if noop_n != 1 else ''}")
        return f"  ⚠ {', '.join(bits)} dropped — Regenerate to retry"

    # ── CHANGELOG grouped-bullets parser (delegates to helpers) ──────────────

    @staticmethod
    def _parse_grouped_bullets(draft_text):
        """Delegate to helpers.doc_drafter.parse_grouped_bullets."""
        from helpers.doc_drafter import parse_grouped_bullets
        return parse_grouped_bullets(draft_text)

    # DocTypes whose patchers operate at the FULL-file level on multiple
    # `## Section` blocks. For these, the simulator builds a synthetic
    # before/after document containing only the affected sections so the
    # ProposalBridge diff lines up section-by-section. Single-section types
    # (changelog, readme, memory) use the legacy read_existing_from_text path.
    _MULTI_SECTION_KEYS = {"architecture", "roadmap", "docs_generic",
                            "tokensave_guide"}

    def _simulate_body(self, dt, target_path, draft_text):
        """Return (before, after) for the ProposalBridge diff.

        Returns either a (before_text, after_text) tuple — when the DocType
        is multi-section — or a single string (after-only — legacy single-
        section path). Callers must handle both shapes.

        Multi-section path (Theme B v3):
          1. Parse the draft into a list of sections
          2. Run compute_apply to get the simulated post-apply file state
          3. Build synthetic before/after documents containing only the
             affected section titles — both sides share identical structure
             so the diff lines up cleanly
        """
        try:
            with open(target_path, encoding="utf-8-sig") as f:
                full_text = f.read()
        except OSError:
            return None
        parsed = dt.parse_draft(draft_text)
        if not parsed or parsed[0] is None:
            return None

        # Multi-section path: parsed is (sections_list,) where sections_list
        # is a non-empty list of section tuples. Empty list means the parser
        # found no `## Section` blocks — simulation impossible.
        is_multi = dt.key in self._MULTI_SECTION_KEYS
        if is_multi:
            sections_list = parsed[0]
            if not sections_list:
                return None
            simulated, ok, _msg = dt.compute_apply(full_text, sections_list)
            if not ok:
                return None
            # Affected titles for the synthetic diff. Roadmap titles come
            # back as `Roadmap N — Theme`; arch/generic use `(title, body)`.
            if dt.key == "roadmap":
                titles = [f"Roadmap {n} — {theme}"
                          for n, theme, _body in sections_list]
                from helpers.roadmap_patch import extract_sections_as_document
            elif dt.key == "architecture":
                titles = [t for t, _body in sections_list]
                from helpers.architecture_patch import extract_sections_as_document
            else:  # docs_generic / tokensave_guide
                titles = [t for t, _body in sections_list]
                from helpers.generic_doc_patch import extract_sections_as_document
            before = extract_sections_as_document(full_text, titles)
            after  = extract_sections_as_document(simulated, titles)
            return (before, after)

        # Legacy single-section path (changelog, readme, memory).
        simulated, ok, _msg = dt.compute_apply(full_text, *parsed)
        if not ok:
            return None
        return dt.read_existing_from_text(simulated)

    # ── README sub-section parser (delegates to helpers) ─────────────────────

    @staticmethod
    def _split_readme_subsection(draft_text):
        """Delegate to helpers.doc_drafter.split_readme_subsection."""
        from helpers.doc_drafter import split_readme_subsection
        return split_readme_subsection(draft_text)

    # ── Status helper ──────────────────────────────────────────────────────

    def _set_status(self, key, msg, fg):
        try:
            self._tab_widgets[key]["status_var"].set(msg)
            # Reach back to the status label widget for fg colour change.
            # Layout: btn_row → [gen_btn, copy_btn, apply_btn, status_lbl]
            #   status_lbl is the 4th child of the row.
            # Easier: just store a reference. Add it now.
            lbl = self._tab_widgets[key].get("status_lbl")
            if lbl is not None:
                lbl.configure(fg=fg)
        except (tk.TclError, RuntimeError):
            pass

    # ── v4 Fix 2a: warning banner helpers ─────────────────────────────────
    #
    # The banner is a read-only `tk.Label` packed above the text widget.
    # Used to surface simulate-time title-rejection warnings and the
    # mismatch-detect dialog's "no LLM call made" message. Critically:
    # the banner is NOT part of the editable Apply payload, so the user
    # can't accidentally write the warning into a markdown file.

    def _show_warning_banner(self, key, message):
        """Display `message` in the warning banner above the tab's text widget."""
        try:
            w = self._tab_widgets[key]
            w["warning_var"].set(f"⚠  {message}")
            lbl = w["warning_lbl"]
            # Pack just above the text widget — txt_wrap is the pack anchor.
            lbl.pack(in_=w["frame"], fill=tk.X, padx=8, pady=(0, 4),
                     before=w["txt_wrap"])
        except (tk.TclError, KeyError, RuntimeError):
            pass

    def _hide_warning_banner(self, key):
        """Hide the warning banner without clearing its underlying text
        (so a later show with the same message is cheap)."""
        try:
            w = self._tab_widgets[key]
            w["warning_lbl"].pack_forget()
            w["warning_var"].set("")
        except (tk.TclError, KeyError, RuntimeError):
            pass

    # ── v4 Fix 4c: draft-tick (elapsed time during generation) ───────────

    def _backend_label_for_status(self, key):
        """Short human-readable backend name for the 'Drafting on X' tick.

        Reads the resolved LLM config at tick time so a backend-override
        change during generation reflects in the visible label.
        """
        try:
            cfg = self._llm_cfg_resolved() or {}
        except (AttributeError, RuntimeError):
            return "LLM"
        provider = (cfg.get("provider") or "").lower()
        if provider == "claude_cli":
            return "Claude CLI"
        if provider == "ollama":
            return "Ollama"
        if provider == "anthropic":
            return "Anthropic"
        if provider == "openai":
            return "OpenAI"
        if provider == "openai_compatible":
            base = (cfg.get("base_url") or "").lower()
            return "Ollama" if "localhost" in base else "OpenAI-compat"
        return provider or "LLM"

    # G6 (v4.4): tick enforces this hard timeout beyond dispatch_llm's
    # 300 s limit. The extra 10 s grace absorbs Python thread-scheduling
    # lag. When triggered, the captured stop_event is signalled so the
    # worker can break its iteration loop; a hard error message is
    # surfaced in the status bar. Note: Python cannot forcibly kill a
    # hung subprocess, so the OS-level Ollama process may remain running.
    _DRAFT_TICK_HARD_TIMEOUT = 310   # seconds; dispatch_llm timeout is 300

    def _draft_tick(self, key):
        """Periodic status-bar tick during a long Generate run.

        Updates the status text every second with elapsed seconds, e.g.
        `"Drafting on Ollama (12s)…"`. Lets the user see the background
        loop is alive even on 300 s agentic runs (without this, a 5-min
        block looks like a hang). Self-cancels when:
          - dialog destroyed (winfo_exists guard)
          - draft_start_ts cleared (completion / error)
          - stop_event identity changed (newer generation in flight)
          - elapsed time exceeds _DRAFT_TICK_HARD_TIMEOUT (G6 v4.4)
        """
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        state = self._tab_state.get(key) or {}
        start = state.get("draft_start_ts")
        if not start:
            return   # completed / cancelled
        # Stop-event identity guard against stale tick loops left over
        # from a previous generation that was superseded.
        captured = state.get("draft_tick_stop_event")
        if captured is not None and captured is not state.get("stop"):
            return
        elapsed = int(time.monotonic() - start)
        # G6: self-enforced hard timeout. If dispatch_llm's own 300 s
        # limit didn't fire (OS-level Ollama hang), we unblock the UI
        # and signal the stop_event so the worker can exit its loop.
        if elapsed > self._DRAFT_TICK_HARD_TIMEOUT:
            if captured is not None:
                captured.set()
            state["draft_start_ts"] = None
            self._set_status(
                key,
                f"⚠ Drafting timed out after {elapsed}s — backend hung "
                "(no Python exception). Stop signalled. Try again or "
                "switch to a different backend.",
                C["red"],
            )
            return
        backend = self._backend_label_for_status(key)
        self._set_status(key, f"Drafting on {backend} ({elapsed}s)…",
                         C["overlay0"])
        try:
            state["draft_tick_after"] = self.after(
                1000, lambda k=key: self._draft_tick(k))
        except tk.TclError:
            pass

    def _start_draft_tick(self, key, stop_event):
        """Start the per-second draft tick. Called from _on_generate."""
        state = self._tab_state.setdefault(key, {})
        state["draft_start_ts"] = time.monotonic()
        state["draft_tick_stop_event"] = stop_event
        try:
            state["draft_tick_after"] = self.after(
                1000, lambda k=key: self._draft_tick(k))
        except tk.TclError:
            pass

    def _stop_draft_tick(self, key):
        """Cancel the tick. Called from _on_generate_done / _on_generate_error."""
        state = self._tab_state.get(key) or {}
        state["draft_start_ts"] = None
        state["draft_tick_stop_event"] = None
        after_id = state.pop("draft_tick_after", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
