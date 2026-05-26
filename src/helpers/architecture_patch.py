"""Atomic docs/ARCHITECTURE.md section patch helper.

Phase 2.1 shape: pure ``_compute_*`` + IO wrapper.

Operates on named ``## SectionName`` level-2 headings.  Given a section
header and replacement content, the patcher locates the heading, replaces
everything from the line after the heading through (but not including) the
next ``## `` heading or EOF, and atomically writes the result.

If the target heading is absent the content is APPENDED at EOF — this
supports the ``auto_create=False`` DocType without special-casing.

Atomicity: ``.tmp`` write + ``os.replace``.

Pure-function module — no Tkinter, no UI imports.  Safe to call from any
thread.
"""

from __future__ import annotations

import os
import re


# Matches any level-2 heading line:  ## Some Heading Text
_SECTION_ANCHOR_RE = re.compile(r"(?m)^## [^\n]+\n")

# Matches the start of the NEXT level-2 heading (used as boundary).
_NEXT_SECTION_RE = re.compile(r"(?m)^## ")


def _find_section_bounds(text: str, section_header: str):
    """Return (header_line_start, body_start, body_end) for a ## section.

    section_header: the heading text WITHOUT leading ``## `` and WITHOUT
    trailing newline, e.g. ``"Repository Layout"`` or ``"Purpose"``.

    Returns None if the heading is not found.

    body_start = char index immediately after the heading line newline.
    body_end   = char index of the NEXT ``## `` heading's first char, or EOF.
    """
    pattern = re.compile(
        r"(?m)^## " + re.escape(section_header.strip()) + r"[^\n]*\n"
    )
    m = pattern.search(text)
    if not m:
        return None
    body_start = m.end()
    nm = _NEXT_SECTION_RE.search(text, body_start)
    body_end = nm.start() if nm else len(text)
    return m.start(), body_start, body_end


def _compute_insert_architecture_section(
    text: str, section_header: str, content: str
) -> tuple[str, bool, str]:
    """Pure transformation — returns ``(new_text, ok, msg)`` without IO.

    Replaces the body of ``## section_header`` with ``content``.  If the
    heading is absent, appends a new ``## section_header`` block at EOF.

    All transformation logic lives here; the IO wrapper adds file-read +
    atomic-write.  Mirror-contract: output is byte-identical to write-then-read.
    """
    content_clean = (content or "").strip("\n")
    if not content_clean:
        return text, False, "no content to insert"

    bounds = _find_section_bounds(text, section_header)
    if bounds is not None:
        header_start, body_start, body_end = bounds
        new_body = "\n" + content_clean + "\n\n"
        updated = text[:body_start] + new_body + text[body_end:]
        return updated, True, f"section '{section_header}' updated"

    # Section absent — append at EOF
    separator = "\n\n" if text and not text.endswith("\n\n") else (
        "\n" if text and not text.endswith("\n") else ""
    )
    new_section = f"## {section_header}\n\n{content_clean}\n"
    updated = text + separator + new_section
    return updated, True, f"section '{section_header}' appended"


def insert_architecture_section(
    path: str, section_header: str, content: str
) -> tuple[bool, str]:
    """Insert or replace a ``## SectionName`` block in an architecture doc.

    IO wrapper around ``_compute_insert_architecture_section``: reads the
    file, delegates the transformation, atomically writes.

    Args:
        path:           Absolute path to the architecture markdown file.
        section_header: Heading text WITHOUT leading ``## ``, e.g. ``"Purpose"``.
        content:        Replacement body (no heading line; trailing newlines
                        handled internally).

    Returns:
        ``(success: bool, message: str)``.
    """
    if not os.path.exists(path):
        # auto_create path: write a minimal skeleton
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"## {section_header}\n\n{(content or '').strip()}\n")
            return True, f"created with section '{section_header}'"
        except OSError as e:
            return False, f"Could not create {os.path.basename(path)}: {e}"

    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_insert_architecture_section(
        text, section_header, content)
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


def read_architecture_section(path: str, section_header: str) -> str:
    """Return the body of a ``## SectionName`` block (header line excluded).

    Returns empty string if the file or heading does not exist.
    """
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    return read_architecture_section_from_text(text, section_header)


def read_architecture_section_from_text(text: str, section_header: str) -> str:
    """Pure-string companion to ``read_architecture_section``."""
    if not text:
        return ""
    bounds = _find_section_bounds(text, section_header)
    if bounds is None:
        return ""
    _, body_start, body_end = bounds
    return text[body_start:body_end].strip()


def read_architecture_full(path: str) -> str:
    """Return the entire architecture file contents.

    Used by the DocType ``read_existing`` callsite — architecture drafts
    are generated from the full document, not just a sub-section.
    """
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""
