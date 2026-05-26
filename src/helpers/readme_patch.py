"""Atomic README.md 'Recent highlights' section patch helper.

Mirrors helpers/changelog_patch.py — single canonical implementation of
README highlights-block replacement.  Uses STRUCTURAL boundary detection
(not a hard line count) so a legitimately long highlights section can
contain hundreds of bullets without tripping a safety abort.

What counts as a structural break:
  - any Markdown header line (``^#{1,6} ``)
  - a horizontal rule (``^---+\\s*$``)
  - a Markdown table row (``^|``)
  - a fenced code block (``^```\\S*$``)

Bold sub-headers like ``**Roadmap-6 — Ask tab + gitignore AI**`` are
NOT boundaries.  They live INSIDE the highlights block and act as
internal sub-section dividers.  Catching them as a boundary would
truncate the block at the first sub-section and corrupt the README.

If the anchor exists but no structural boundary is found before EOF,
the patcher REFUSES to write rather than chew everything to EOF.  This
is the user-data-preservation guarantee — the dialog surfaces the error
in its status bar so the user knows to fix the README header structure.

Atomicity: ``.tmp`` write + ``os.replace``.

Pure-function module — no Tkinter, no UI imports.  Safe to call from
any thread.

Example.  Before patching::

    See [CHANGELOG.md] for the full history.

    **Recent highlights (Unreleased)** — see CHANGELOG.md…

    **Roadmap-6 — Ask tab + gitignore AI**
    - 🤖 Ask tab — separate ``ask_tab_llm`` config
    - 🤖 Gitignore AI Suggest

    **Major: local-AI integration**
    - 🤖 Ask tab — Stage 2 chat interface
    - 🔍 AI Code Review

    ---

    ## Credits

After ``update_readme_highlights(path, new_body)``::

    See [CHANGELOG.md] for the full history.

    **Recent highlights (Unreleased)** — see CHANGELOG.md…

    <new_body>

    ---

    ## Credits

The ``---`` rule on the second-to-last line is the structural boundary
that stops the replacement.  Everything from the anchor newline through
the line before ``---`` is replaced with ``new_body``.
"""

from __future__ import annotations

import os
import re


# Anchor: bolded `**Recent highlights*` (with or without parenthetical) at
# start of a line.  ``[^*]*`` between the bold markers tolerates any
# non-asterisk text the user may have added (e.g. " (Unreleased)").
_ANCHOR_RE = re.compile(
    r"(?m)^\*\*Recent highlights[^*]*\*\*[^\n]*\n"
)

# Structural boundary patterns — terminate the highlights block at the
# first match found after the anchor.  All four are start-of-line
# patterns; multiline mode (?m) makes ``^`` match line starts.
_BOUNDARY_RE = re.compile(
    r"(?m)^(?:"
    r"#{1,6}\s"            # markdown header  e.g. ## Credits
    r"|-{3,}\s*$"          # horizontal rule  ---  or ----
    r"|\|"                 # markdown table   | col |
    r"|```"                # fenced code      ```python
    r")"
)


def _find_block_bounds(text):
    """Return (start_char, end_char) of the highlights body, or None.

    start_char = char index immediately after the anchor line's newline.
    end_char   = char index of the boundary line's first char (exclusive).

    Returns None if either the anchor is missing OR no structural
    boundary exists in the rest of the file.  Callers MUST treat None as
    a refuse-to-write signal — never fall through to "replace to EOF".
    """
    am = _ANCHOR_RE.search(text)
    if not am:
        return None
    block_start = am.end()
    bm = _BOUNDARY_RE.search(text, block_start)
    if not bm:
        return None
    return block_start, bm.start()


def update_readme_highlights(readme_path, notes_md):
    """Replace the body of the 'Recent highlights' block in README.md.

    Atomic + idempotent.  The replacement spans from the line AFTER the
    anchor line to (but not including) the next structural-break line.

    Aborts safely if either the anchor or its terminating structural
    boundary cannot be located — refuses to truncate to EOF, returns
    ``(False, <diagnostic>)`` for the dialog to surface.

    Args:
        readme_path: Absolute path to README.md.
        notes_md:    Replacement markdown for the highlights body, NOT
                     including the anchor line itself.  Trailing newline
                     handling is taken care of internally; pass the raw
                     bullet list and bold sub-headers.

    Returns:
        ``(success: bool, message: str)``.  On failure, nothing is written.
    """
    if not os.path.exists(readme_path):
        return False, "README.md not found"

    try:
        with open(readme_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read README.md: {e}"

    bounds = _find_block_bounds(text)
    if bounds is None:
        # Distinguish the two failure modes for a useful dialog message.
        if not _ANCHOR_RE.search(text):
            return False, (
                "Could not locate '**Recent highlights' anchor in README.md. "
                "Add a line starting with '**Recent highlights' to mark where "
                "the auto-updater should write."
            )
        return False, (
            "Ambiguous terminal boundary — refusing to truncate README. "
            "Verify the highlights section is followed by a markdown header "
            "(## …), horizontal rule (---), table, or fenced code block."
        )

    block_start, block_end = bounds

    # Normalise spacing: one blank line above the new body, one blank
    # line below before the boundary.  This keeps the diff stable when
    # the same content is re-applied later.
    new_block = "\n" + notes_md.strip("\n") + "\n\n"
    updated = text[:block_start] + new_block + text[block_end:]

    tmp_path = readme_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, readme_path)
    except OSError as e:
        # Best-effort cleanup of stray .tmp on failure.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing README.md: {e}"

    return True, "highlights block updated"


def read_highlights(readme_path):
    """Return the current content of the 'Recent highlights' block (body only).

    Returns an empty string if the file doesn't exist, has no highlights
    anchor, or if the block bounds can't be safely determined.  Callers
    typically use this to seed the LLM with the current content so the
    re-draft can preserve / consolidate / re-order it.
    """
    if not os.path.exists(readme_path):
        return ""
    try:
        with open(readme_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    bounds = _find_block_bounds(text)
    if bounds is None:
        return ""
    block_start, block_end = bounds
    return text[block_start:block_end].strip()


# ── Append-only sub-section insertion ────────────────────────────────────────
#
# Small local models (e.g. qwen2.5-coder:14b) struggle to re-emit a
# 200-line highlights block character-for-character when asked to "preserve
# existing content and append new".  They drop sub-sections.  The drafter
# uses the function below INSTEAD of update_readme_highlights so the model
# only has to generate the new sub-section (small, easy) and the patcher
# handles the structural splice.
#
# Sub-section convention inside the highlights block:
#   **Header text**         ← start of sub-section
#   - bullet
#   - bullet
#                           ← blank line ok
#   **Next Header**         ← end of previous sub-section, start of next
#
# A sub-section ends at: the next ``**...**`` bold-header line at
# start-of-line, OR the highlights block's structural boundary.

_SUBHEADER_RE = re.compile(r"(?m)^\*\*[^\n*]+\*\*[^\n]*\n")


def _normalise_subheader(line):
    """Normalise a sub-header line for matching (strip ws/colon/trailing context).

    Two sub-headers ``"**Roadmap-6 — Ask tab + gitignore AI**"`` and
    ``"**Roadmap-6 — Ask tab + gitignore AI**\\n"`` should match.  Also
    tolerate a trailing colon (``"**Header**:"``) and surrounding spaces.
    """
    return line.strip().rstrip(":").strip()


def insert_readme_highlights_subsection(readme_path, header_md, bullets_md):
    """Insert OR replace a single sub-section inside the highlights block.

    Behaviour:
      - If a sub-section whose header normalises to ``header_md`` already
        exists, REPLACE its bullets (header line stays).
      - Otherwise INSERT a new sub-section at the TOP of the highlights
        block, right below the anchor line, so newest work appears first.

    Atomic + idempotent.  Aborts safely (no write) if the highlights
    block bounds can't be located — same boundary safety as
    ``update_readme_highlights``.

    Args:
        readme_path: Absolute path to README.md.
        header_md:   Full bold-header line including the ``**`` markers,
                     e.g. ``"**Roadmap-6 — Ask tab + gitignore AI**"``.
                     Newlines stripped automatically.
        bullets_md:  Bullets ONLY, no leading header line.  Should end
                     with a trailing newline.

    Returns:
        ``(success: bool, message: str)``.  Message indicates "replaced"
        vs "inserted" on success.
    """
    if not os.path.exists(readme_path):
        return False, "README.md not found"
    try:
        with open(readme_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read README.md: {e}"

    bounds = _find_block_bounds(text)
    if bounds is None:
        if not _ANCHOR_RE.search(text):
            return False, "Could not locate '**Recent highlights' anchor in README.md"
        return False, (
            "Ambiguous terminal boundary — refusing to write.  Verify the "
            "highlights section is followed by a structural break (header, "
            "horizontal rule, table, or fenced code block)."
        )
    block_start, block_end = bounds
    block_text = text[block_start:block_end]

    target_norm = _normalise_subheader(header_md)
    new_subsection = header_md.strip() + "\n" + bullets_md.strip("\n") + "\n"

    # ── Replace path: existing sub-section with matching header ─────────
    for m in _SUBHEADER_RE.finditer(block_text):
        existing_line = block_text[m.start():m.end()]
        if _normalise_subheader(existing_line) != target_norm:
            continue
        # Found it.  End of this sub-section = next sub-header OR block end.
        nm = _SUBHEADER_RE.search(block_text, m.end())
        sub_end = nm.start() if nm else len(block_text)
        # Replace from this sub-section's header through (exclusive) the
        # next sub-section's header.
        new_block = (block_text[:m.start()]
                     + new_subsection
                     + ("\n" if not block_text[sub_end - 1:sub_end] == "\n"
                        else "")
                     + block_text[sub_end:])
        updated = text[:block_start] + new_block + text[block_end:]
        action = "replaced"
        break
    else:
        # ── Insert path: no matching sub-section → insert at top ────────
        # Right after the leading whitespace inside the block, so the new
        # sub-section appears as the FIRST one under the anchor.
        leading_ws_len = len(block_text) - len(block_text.lstrip("\n"))
        new_block = (block_text[:leading_ws_len]
                     + "\n" + new_subsection + "\n"
                     + block_text[leading_ws_len:])
        updated = text[:block_start] + new_block + text[block_end:]
        action = "inserted"

    tmp_path = readme_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, readme_path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing README.md: {e}"

    return True, f"highlights sub-section {action}"
