"""Atomic memory file patch helper.

Phase 2.1 shape: pure ``_compute_*`` + IO wrapper.

Memory files live in ``memory/<slug>.md`` and may have YAML frontmatter::

    ---
    name: some-slug
    description: one-liner
    metadata:
      type: feedback
    ---

    Memory body text here.

The patcher preserves the frontmatter block intact and replaces everything
AFTER the closing ``---`` fence (or the entire file if no frontmatter).

Atomicity: ``.tmp`` write + ``os.replace``.

Pure-function module — no Tkinter, no UI imports.  Safe to call from any
thread.
"""

from __future__ import annotations

import os
import re


# Matches a YAML frontmatter block: starts with "---\n" at the very
# beginning of the file, ends at the next "---\n" (or "---" at EOF).
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n.*?\n---[ \t]*\r?\n",
    re.DOTALL,
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` — frontmatter may be empty string."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        return text[:m.end()], text[m.end():]
    return "", text


def _compute_insert_memory_body(text: str, new_body: str) -> tuple[str, bool, str]:
    """Pure: replace the body portion of a memory file with ``new_body``.

    Preserves any YAML frontmatter.  Mirror-contract safe.
    """
    new_body_clean = (new_body or "").strip("\n")
    if not new_body_clean:
        return text, False, "no body content to write"

    frontmatter, _ = _split_frontmatter(text)
    updated = frontmatter + new_body_clean + "\n"
    return updated, True, "memory body updated"


def insert_memory_body(path: str, new_body: str) -> tuple[bool, str]:
    """Replace the body of a memory markdown file.  IO wrapper.

    Preserves YAML frontmatter.  Creates the file if it does not exist.

    Args:
        path:     Absolute path to the memory ``.md`` file.
        new_body: New body content (without frontmatter).

    Returns:
        ``(success: bool, message: str)``.
    """
    if not os.path.exists(path):
        try:
            body_clean = (new_body or "").strip("\n")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body_clean + "\n")
            return True, "memory file created"
        except OSError as e:
            return False, f"Could not create {os.path.basename(path)}: {e}"

    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_insert_memory_body(text, new_body)
    if not ok:
        return False, msg

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, path)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False, f"Failed writing {os.path.basename(path)}: {e}"

    return True, msg


def read_memory_body(path: str) -> str:
    """Return the body portion of a memory file (frontmatter excluded)."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    return read_memory_body_from_text(text)


def read_memory_body_from_text(text: str) -> str:
    """Pure-string companion to ``read_memory_body``."""
    if not text:
        return ""
    _, body = _split_frontmatter(text)
    return body.strip()
