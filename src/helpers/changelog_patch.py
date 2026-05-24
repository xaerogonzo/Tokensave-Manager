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
