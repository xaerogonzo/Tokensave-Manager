"""Generic markdown section patch helper.

Phase 2.1 shape: pure ``_compute_*`` + IO wrapper.

Operates on any ``## HeadingName`` level-2 anchor in a markdown file.
Given a section header and content, the patcher:
  - Finds the ``## HeadingName`` line.
  - Replaces everything from the line after the heading to (but not
    including) the next ``## `` heading or EOF.
  - If the heading is absent and ``auto_create=True``, appends the new
    section at EOF (caller controls this via the ``append_if_absent``
    parameter).

Used by ``docs_generic`` and ``tokensave_guide`` DocTypes.

Atomicity: ``.tmp`` write + ``os.replace``.

Pure-function module — no Tkinter, no UI imports.  Safe to call from any
thread.
"""

from __future__ import annotations

import os
import re


_NEXT_L2_RE = re.compile(r"(?m)^## ")


def _find_section_bounds(text: str, section_header: str):
    """Return (header_start, body_start, body_end) or None.

    section_header: heading text WITHOUT leading ``## ``.
    body_start = char index immediately after the heading's newline.
    body_end   = start of next ``## `` heading, or EOF.
    """
    pattern = re.compile(
        r"(?m)^## " + re.escape(section_header.strip()) + r"[^\n]*\n"
    )
    m = pattern.search(text)
    if not m:
        return None
    body_start = m.end()
    nm = _NEXT_L2_RE.search(text, body_start)
    body_end = nm.start() if nm else len(text)
    return m.start(), body_start, body_end


def _compute_insert_generic_section(
    text: str,
    section_header: str,
    content: str,
    append_if_absent: bool = True,
) -> tuple[str, bool, str]:
    """Pure transformation — returns ``(new_text, ok, msg)`` without IO.

    Replaces the body of ``## section_header`` with ``content``.  If the
    heading is absent and ``append_if_absent`` is True, appends a new section
    at EOF; otherwise returns an error.

    Mirror-contract safe.
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

    if not append_if_absent:
        return text, False, f"heading '## {section_header}' not found"

    separator = "\n\n" if text and not text.endswith("\n\n") else (
        "\n" if text and not text.endswith("\n") else ""
    )
    new_section = f"## {section_header}\n\n{content_clean}\n"
    updated = text + separator + new_section
    return updated, True, f"section '{section_header}' appended"


def insert_generic_section(
    path: str,
    section_header: str,
    content: str,
    append_if_absent: bool = True,
) -> tuple[bool, str]:
    """Insert or replace a ``## SectionName`` block in any markdown file.

    IO wrapper around ``_compute_insert_generic_section``.  Creates the file
    if it does not exist (always treated as append_if_absent=True for missing
    files).

    Args:
        path:              Absolute path to the target markdown file.
        section_header:    Heading text WITHOUT leading ``## ``.
        content:           Replacement body (no heading line).
        append_if_absent:  If True (default), appends when heading absent.

    Returns:
        ``(success: bool, message: str)``.
    """
    if not os.path.exists(path):
        try:
            content_clean = (content or "").strip("\n")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"## {section_header}\n\n{content_clean}\n")
            return True, f"created with section '{section_header}'"
        except OSError as e:
            return False, f"Could not create {os.path.basename(path)}: {e}"

    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_insert_generic_section(
        text, section_header, content, append_if_absent=append_if_absent)
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


def read_generic_section(path: str, section_header: str) -> str:
    """Return the body of a ``## SectionName`` block (heading line excluded).

    Returns empty string if the file or heading does not exist.
    """
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    return read_generic_section_from_text(text, section_header)


def read_generic_section_from_text(text: str, section_header: str) -> str:
    """Pure-string companion to ``read_generic_section``."""
    if not text:
        return ""
    bounds = _find_section_bounds(text, section_header)
    if bounds is None:
        return ""
    _, body_start, body_end = bounds
    return text[body_start:body_end].strip()


def read_generic_full(path: str) -> str:
    """Return the entire file contents, or empty string if absent."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""
