"""Atomic CHANGELOG.md patch helper.

Single canonical implementation of CHANGELOG section insertion. Idempotent:
if a section for the given version already exists, its block is REPLACED
(from the version header line up to the next `## [` line or EOF). If absent,
the new section is inserted directly below the `## [Unreleased]` anchor.

The replacement boundary is `^## \\[` (start-of-line `## [`), so when
replacing a section we stop EXACTLY at the next version header and never
accidentally consume the sections below. Without this, a naive
`replace(old_block, new_block)` could eat unrelated history if the version
strings happen to recur in body text.

What gets replaced: everything from the matched ``## [version]`` header
through (but not including) the next ``## [`` line or EOF. Manual notes
the user typed within the section being replaced ARE replaced too — they're
logically part of that section. Notes typed between OTHER sections are
untouched.

Atomicity: `.tmp` write + `os.replace`.

Pure-function module — no Tkinter, no UI imports. Safe to call from any thread.

Example showing the next-section boundary protection::

    Before patching with version "1.2.0":

        ## [Unreleased]

        ## [1.2.0] — 2026-05-20
        ### Added
        - old text

        ## [1.1.0] — 2026-04-01
        ### Fixed
        - earlier bug

    After insert_changelog_release(path, "1.2.0", new_block):

        ## [Unreleased]

        <new_block>

        ## [1.1.0] — 2026-04-01
        ### Fixed
        - earlier bug

    The ``## [1.1.0]`` section survives untouched because the boundary
    regex stops at its header line.
"""

from __future__ import annotations

import os
import re


def update_unreleased(
    changelog_path: str,
    notes_md: str,
) -> tuple[bool, str]:
    """Replace the content of the ## [Unreleased] block in CHANGELOG.md.

    The replacement spans from the line AFTER the ``## [Unreleased]`` header
    to (but not including) the next ``## [`` line, or EOF if no versioned
    section exists yet (new projects with only an [Unreleased] block).

    If the block already has user-written content it is passed to the caller
    as ``existing_content`` via the returned message so the LLM can integrate
    it. This function does the atomic write; the caller is responsible for
    having already folded existing content into ``notes_md``.

    Args:
        changelog_path: Absolute path to CHANGELOG.md.
        notes_md:       Replacement markdown content for the [Unreleased]
                        block, NOT including the ``## [Unreleased]`` header
                        line itself.  Should end with a trailing newline.

    Returns:
        (success: bool, message: str).  On failure, nothing is written.
    """
    if not os.path.exists(changelog_path):
        return False, "CHANGELOG.md not found"

    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read CHANGELOG.md: {e}"

    anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
    am = anchor_re.search(text)
    if not am:
        return False, "Could not locate '## [Unreleased]' anchor"

    block_start = am.end()

    # Find the next versioned section header (^## [), or use EOF.
    # Treating EOF as a valid boundary is critical for projects that have
    # an [Unreleased] section but no versioned release yet.
    next_re = re.compile(r"(?m)^## \[")
    nm = next_re.search(text, block_start)
    block_end = nm.start() if nm else len(text)

    new_block = "\n" + notes_md.strip("\n") + "\n\n"
    updated = text[:block_start] + new_block + text[block_end:]

    tmp_path = changelog_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, changelog_path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing changelog: {e}"

    return True, "unreleased block updated"


def read_unreleased_from_text(text: str) -> str:
    """Return the ## [Unreleased] body extracted from an in-memory CHANGELOG.

    Phase 2.1: pure-string companion to ``read_unreleased``.  Used by the
    doc-drafter dialog to extract the simulated post-apply body from a
    ``_compute_insert_unreleased_bullets`` result so the ProposalBridge
    diff is honest (current body on the left, post-apply body on the
    right).
    """
    if not text:
        return ""
    anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
    am = anchor_re.search(text)
    if not am:
        return ""

    block_start = am.end()
    next_re = re.compile(r"(?m)^## \[")
    nm = next_re.search(text, block_start)
    block_end = nm.start() if nm else len(text)
    return text[block_start:block_end].strip()


def read_unreleased(changelog_path: str) -> str:
    """Return the current content of the ## [Unreleased] block (body only).

    Returns an empty string if the file doesn't exist, has no [Unreleased]
    anchor, or if the block body is empty.
    """
    if not os.path.exists(changelog_path):
        return ""
    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    return read_unreleased_from_text(text)


# ── Append-only sub-section insertion inside [Unreleased] ────────────────────
#
# Used by the Doc Updates dialog INSTEAD of update_unreleased.  Discovered
# during dogfooding: small local models (qwen2.5-coder:14b) consolidate
# aggressively when asked to "preserve existing bullets + add new ones",
# wiping detail.  The fix mirrors the README append-only pattern: drafter
# generates only NEW bullets grouped by ### header, patcher splices them
# into the existing sub-sections without touching what's already there.

# Common drift variants → canonical Keep-a-Changelog headers.  All compared
# lowercased.  If a section heading doesn't match any variant, the patcher
# falls back to Title Case of the input (won't silently lose it — the user
# sees the unfamiliar header in the diff).
_SECTION_ALIASES = {
    "Added":   {"added", "add", "additions", "new"},
    "Changed": {"changed", "change", "changes", "modified", "updated"},
    "Fixed":   {"fixed", "fix", "fixes", "bugfix", "bug fix", "bugfixes"},
    "Removed": {"removed", "remove", "removals", "deprecated", "deleted"},
}


def _canonicalise_section(raw):
    """Map a drift variant ('### fixes', '### CHANGED') to canonical title.

    Falls back to ``raw.strip().title()`` when no alias matches so a
    section the model invented still appears (in the diff) rather than
    being silently dropped.
    """
    s = (raw or "").strip().lower()
    for canonical, variants in _SECTION_ALIASES.items():
        if s in variants:
            return canonical
    return (raw or "").strip().title()


def read_section_bullets(changelog_path, section):
    """Return existing bullet lines from ``### {section}`` inside ``[Unreleased]``.

    Used by the doc-drafter dialog for Jaccard+overlap dedup against existing
    content before appending new bullets via ``insert_unreleased_bullets``.

    Returns ``[]`` if the changelog is missing, the ``[Unreleased]`` anchor is
    missing, or the requested section doesn't exist.  Section name matching is
    case-insensitive via the same ``_canonicalise_section`` alias map used by
    the inserter (``"fixed"`` / ``"FIXED"`` / ``"Fixes"`` all resolve to
    ``"Fixed"``).

    Only bullet-shaped lines are returned — comments, blank lines, and
    free-form prose inside the section are silently skipped.  This keeps the
    dedup signal high (the drafter compares bullet-to-bullet) while
    tolerating user-authored notes mixed into the section.
    """
    if not os.path.exists(changelog_path):
        return []
    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return []
    return read_section_bullets_from_text(text, section)


def read_section_bullets_from_text(text, section):
    """Text-mode mirror of ``read_section_bullets`` — operates on a string
    instead of a path. Used by the doc-drafter prompt builders so existing
    bullets can be summarised without a second disk read (the dialog already
    holds the unreleased body in memory)."""
    if not text:
        return []
    anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
    am = anchor_re.search(text)
    if not am:
        # No anchor — caller might already have passed just the body of the
        # block (the dialog's `existing_unreleased` is exactly that). Treat
        # the entire text as the [Unreleased] body in that case.
        block = text
    else:
        next_version_re = re.compile(r"(?m)^## \[")
        nm = next_version_re.search(text, am.end())
        block_end = nm.start() if nm else len(text)
        block = text[am.end():block_end]

    sub_re = re.compile(r"(?m)^###\s+([^\n]+)\n")
    canonical = _canonicalise_section(section)
    subsections = list(sub_re.finditer(block))
    for i, m in enumerate(subsections):
        if _canonicalise_section(m.group(1)) != canonical:
            continue
        sec_start = m.end()
        sec_end = (subsections[i + 1].start()
                   if i + 1 < len(subsections) else len(block))
        bullets = []
        for ln in block[sec_start:sec_end].splitlines():
            stripped = ln.lstrip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                bullets.append(stripped)
        return bullets
    return []


def _compute_insert_unreleased_bullets(text, section, bullets_md):
    """Pure transformation — returns ``(new_text, ok, msg)`` without IO.

    Phase 2.1: extracted from ``insert_unreleased_bullets`` so the
    doc-drafter dialog can precompute the post-apply file state for an
    HONEST ProposalBridge diff (the previous diff displayed the full
    existing [Unreleased] vs the bare new bullets, which looked
    catastrophically destructive even though the actual apply path
    appends).

    All transformation logic lives here; the IO wrapper below adds
    file-read + atomic-write.  Same return convention as the wrapper:
    ``(updated_full_text, True, action_msg)`` on success or
    ``(text_unchanged, False, error_msg)`` on failure.  Caller is
    responsible for IO + error handling.
    """
    bullets_clean = (bullets_md or "").strip("\n").strip()
    if not bullets_clean:
        return text, False, "no bullets to insert"

    # Locate [Unreleased] block boundaries
    anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
    am = anchor_re.search(text)
    if not am:
        return text, False, "Could not locate '## [Unreleased]' anchor"

    block_start = am.end()
    next_version_re = re.compile(r"(?m)^## \[")
    nm = next_version_re.search(text, block_start)
    block_end = nm.start() if nm else len(text)

    block = text[block_start:block_end]
    canonical = _canonicalise_section(section)

    # Find existing ### sub-sections inside the [Unreleased] block.
    sub_re = re.compile(r"(?m)^###\s+([^\n]+)\n")
    subsections = []
    for m in sub_re.finditer(block):
        header_canonical = _canonicalise_section(m.group(1))
        subsections.append({
            "match": m,
            "canonical": header_canonical,
        })

    # Compute each sub-section's end = next sub-section's start OR block end.
    for i, sub in enumerate(subsections):
        if i + 1 < len(subsections):
            sub["end"] = subsections[i + 1]["match"].start()
        else:
            sub["end"] = len(block)

    # ── Append path: matching sub-section exists ─────────────────────
    for sub in subsections:
        if sub["canonical"] != canonical:
            continue
        section_start = sub["match"].end()      # after "### Header\n"
        section_end   = sub["end"]
        existing_body = block[section_start:section_end].rstrip()
        new_body = existing_body + "\n" + bullets_clean + "\n"
        # One blank line separates this sub-section from the next divider
        if section_end < len(block):
            new_body = new_body + "\n"
        new_block = block[:section_start] + new_body + block[section_end:]
        action = f"appended to {canonical}"
        break
    else:
        # ── Insert path: new sub-section at end of [Unreleased] ──────
        block_rstripped = block.rstrip()
        prefix = block_rstripped + ("\n\n" if block_rstripped else "\n")
        new_subsection = f"### {canonical}\n{bullets_clean}\n"
        suffix_blank = "\n" if nm is not None else ""
        new_block = prefix + new_subsection + suffix_blank
        action = f"inserted new {canonical} sub-section"

    updated = text[:block_start] + new_block + text[block_end:]
    return updated, True, action


def insert_unreleased_bullets(changelog_path, section, bullets_md):
    """Append bullets to a sub-section of [Unreleased] (IO wrapper).

    Reads the file, delegates the transformation to
    ``_compute_insert_unreleased_bullets``, then atomically writes the
    result.  Backward-compatible with the pre-Phase-2.1 monolithic
    function — same signature, same return contract.

    See ``_compute_insert_unreleased_bullets`` for the behaviour
    specification (append / insert / whitespace discipline / fresh-
    CHANGELOG isolation / case-insensitive section matching).

    Args:
        changelog_path: Absolute path to CHANGELOG.md.
        section:        Sub-section heading (drift-tolerant — e.g. 'fixed',
                        'CHANGED', 'Fixes' all map correctly).
        bullets_md:     Newline-separated bullet lines.

    Returns:
        ``(success: bool, message: str)``.
    """
    if not os.path.exists(changelog_path):
        return False, "CHANGELOG.md not found"
    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read CHANGELOG.md: {e}"

    updated, ok, msg = _compute_insert_unreleased_bullets(
        text, section, bullets_md)
    if not ok:
        return False, msg

    tmp_path = changelog_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, changelog_path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing changelog: {e}"

    return True, msg


def insert_changelog_release(
    changelog_path: str,
    version: str,
    notes_md: str,
) -> tuple[bool, str]:
    """Insert or replace a release section in CHANGELOG.md (idempotent).

    Args:
        changelog_path: Absolute path to CHANGELOG.md.
        version:        Version string without the leading ``v`` (e.g.
                        ``"1.4.0"``). Used to detect an existing section.
        notes_md:       FULLY RENDERED markdown for the section, including
                        its own ``## [version] — date`` header on line 1.
                        Produced by `helpers.release._render_release_notes`.

    Returns:
        (success: bool, message: str). On the absent-anchor failure path
        nothing is written, so the file is never left in a malformed state.
    """
    clean_version = version.lstrip("v")
    if not os.path.exists(changelog_path):
        return False, "CHANGELOG.md not found"

    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read CHANGELOG.md: {e}"

    # Normalise the block: ensure exactly one trailing blank line after, so
    # the spacing between sections stays consistent whether we insert or
    # replace.
    new_block = notes_md.rstrip("\n") + "\n\n"

    # Idempotent replace path: does a `## [version]` section already exist?
    # Boundary is the next `^## \[` line (or EOF) — preserves any
    # user-authored text between sections.
    section_re = re.compile(
        rf"(?ms)^## \[{re.escape(clean_version)}\][^\n]*\n.*?(?=^## \[|\Z)"
    )
    m = section_re.search(text)
    if m:
        updated = text[:m.start()] + new_block + text[m.end():]
        action = "replaced"
    else:
        anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
        am = anchor_re.search(text)
        if not am:
            return False, "Could not locate '## [Unreleased]' anchor"
        insert_at = am.end()
        # Skip any blank lines right after the anchor so the new section
        # ends up with exactly one blank line of padding above it.
        tail = text[insert_at:]
        leading_blanks = len(tail) - len(tail.lstrip("\n"))
        updated = (text[:insert_at]
                   + "\n"
                   + new_block
                   + tail[leading_blanks:])
        action = "inserted"

    tmp_path = changelog_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, changelog_path)
    except OSError as e:
        # Best-effort cleanup of stray .tmp on failure
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing changelog: {e}"

    return True, f"section {action}"
