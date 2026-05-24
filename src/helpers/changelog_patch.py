"""Atomic CHANGELOG.md patch helper.

Pure-function module. Locates the standard `## [Unreleased]` anchor in a
CHANGELOG.md file and inserts a structured release block directly beneath it.
Uses a .tmp write + os.replace for atomicity.

No Tkinter or UI imports — this module is safe to call from any thread.
"""

from __future__ import annotations

import os


def insert_changelog_release(
    changelog_path: str,
    new_version: str,
    notes_body: str,
) -> tuple[bool, str]:
    """Insert a release block under the `## [Unreleased]` anchor.

    Args:
        changelog_path: Absolute path to CHANGELOG.md.
        new_version:    Version string, e.g. "1.4.0".
        notes_body:     Release notes text (will be trimmed). Typically already
                        formatted as Markdown bullet points.

    Returns:
        (success: bool, message: str)
    """
    if not os.path.exists(changelog_path):
        return False, "CHANGELOG.md not found"

    try:
        with open(changelog_path, encoding="utf-8-sig") as f:
            content = f.read()
    except OSError as e:
        return False, f"Could not read CHANGELOG.md: {e}"

    anchor = "## [Unreleased]"
    if anchor not in content:
        return False, "Could not locate '## [Unreleased]' anchor in your changelog file"

    insertion_text = f"\n\n## [{new_version}] - {notes_body.strip()}"
    patched = content.replace(anchor, anchor + insertion_text, 1)

    tmp_path = changelog_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(patched)
        os.replace(tmp_path, changelog_path)
    except OSError as e:
        return False, f"Failed writing changelog: {e}"

    return True, "Changelog successfully updated."
