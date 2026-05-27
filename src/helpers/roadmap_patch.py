"""Atomic docs/ROADMAP.md patch helper.

Phase 2.1 shape: pure ``_compute_*`` helpers + IO wrappers.

Three operations:
  1. Insert a new roadmap section heading + content block.
  2. Promote the status emoji on an existing entry's title line.
  3. Append a freeform note under an existing entry.

Roadmap structure assumed:
    ## Roadmap N — Theme Title
    ### ✅ Item title
    ...body / notes...
    ### 🟡 Another item
    ...

Status emoji set: ✅ 🟡 🔮 💭 💤

Atomicity: ``.tmp`` write + ``os.replace``.

Pure-function module — no Tkinter, no UI imports.  Safe to call from any
thread.
"""

from __future__ import annotations

import os
import re


_STATUS_EMOJIS = {"✅", "🟡", "🔮", "💭", "💤"}

# Matches "## Roadmap N" lines (N = one or more digits, optional suffix).
_ROADMAP_ANCHOR_RE = re.compile(r"(?m)^## Roadmap (\d+)[^\n]*\n")

# Matches the start of the NEXT "## " level-2 heading (section boundary).
_NEXT_L2_RE = re.compile(r"(?m)^## ")

# Matches a level-3 entry heading that starts with a status emoji + space.
_ENTRY_RE = re.compile(
    r"(?m)^### (?:✅|🟡|🔮|💭|💤) [^\n]+\n"
)


def _find_roadmap_block(text: str, roadmap_n: int):
    """Return (block_start, block_end) for Roadmap N's body.

    block_start = char index immediately after the ``## Roadmap N`` heading.
    block_end   = start of the next ``## `` heading, or EOF.

    Returns None if the heading is not present.
    """
    for m in _ROADMAP_ANCHOR_RE.finditer(text):
        if int(m.group(1)) == roadmap_n:
            block_start = m.end()
            nm = _NEXT_L2_RE.search(text, block_start)
            block_end = nm.start() if nm else len(text)
            return block_start, block_end
    return None


def _find_entry_in_block(block: str, item_title: str):
    """Return (entry_start, body_start, entry_end) within ``block``.

    Matches case-insensitively on ``item_title`` (ignoring leading status emoji).
    entry_start = start of the ``### `` line.
    body_start  = char after the heading newline.
    entry_end   = start of the next ``### `` or end of block.
    """
    title_norm = item_title.strip().lower()
    entries = list(_ENTRY_RE.finditer(block))
    for i, m in enumerate(entries):
        heading = m.group(0).strip()
        # Strip the status emoji prefix (up to first space after emoji)
        parts = heading.split(" ", 2)
        title_part = " ".join(parts[2:]).lower() if len(parts) >= 3 else ""
        if title_part == title_norm:
            entry_start = m.start()
            body_start = m.end()
            entry_end = entries[i + 1].start() if i + 1 < len(entries) else len(block)
            return entry_start, body_start, entry_end
    return None


# ── 1. Insert new roadmap section ────────────────────────────────────────────

def _compute_insert_roadmap_section(
    text: str, roadmap_n: int, theme_title: str, content: str
) -> tuple[str, bool, str]:
    """Pure: insert ``## Roadmap N — theme_title`` block.

    If the heading already exists the content block is replaced (idempotent).
    If absent, appended at EOF.  Mirror-contract safe.
    """
    content_clean = (content or "").strip("\n")

    bounds = _find_roadmap_block(text, roadmap_n)
    if bounds is not None:
        block_start, block_end = bounds
        new_body = "\n" + content_clean + "\n\n"
        updated = text[:block_start] + new_body + text[block_end:]
        return updated, True, f"Roadmap {roadmap_n} section updated"

    heading = f"## Roadmap {roadmap_n} — {theme_title.strip()}\n"
    separator = "\n\n" if text and not text.endswith("\n\n") else (
        "\n" if text and not text.endswith("\n") else ""
    )
    block = heading + "\n" + content_clean + "\n"
    updated = text + separator + block
    return updated, True, f"Roadmap {roadmap_n} section appended"


def insert_roadmap_section(
    path: str, roadmap_n: int, theme_title: str, content: str
) -> tuple[bool, str]:
    """Insert or replace a ``## Roadmap N`` section.  IO wrapper."""
    if not os.path.exists(path):
        try:
            heading = f"## Roadmap {roadmap_n} — {theme_title.strip()}\n"
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(heading + "\n" + (content or "").strip() + "\n")
            return True, f"created with Roadmap {roadmap_n}"
        except OSError as e:
            return False, f"Could not create {os.path.basename(path)}: {e}"

    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_insert_roadmap_section(
        text, roadmap_n, theme_title, content)
    if not ok:
        return False, msg

    return _atomic_write(path, updated, msg)


# ── 2. Promote status ─────────────────────────────────────────────────────────

def _compute_promote_status(
    text: str, roadmap_n: int, item_title: str, new_status: str
) -> tuple[str, bool, str]:
    """Pure: change the status emoji on ``### <emoji> item_title``.

    new_status must be one of the valid status emojis.
    """
    if new_status not in _STATUS_EMOJIS:
        return text, False, f"Unknown status '{new_status}'"

    bounds = _find_roadmap_block(text, roadmap_n)
    if bounds is None:
        return text, False, f"Roadmap {roadmap_n} section not found"
    block_start, block_end = bounds
    block = text[block_start:block_end]

    result = _find_entry_in_block(block, item_title)
    if result is None:
        return text, False, f"Entry '{item_title}' not found in Roadmap {roadmap_n}"

    entry_start, body_start, entry_end = result
    old_heading_line = block[entry_start:body_start]
    # Replace the emoji token (first word after "### ")
    parts = old_heading_line.lstrip("#").strip().split(" ", 2)
    new_heading = f"### {new_status} {' '.join(parts[2:])}\n" if len(parts) >= 3 else old_heading_line
    new_block = block[:entry_start] + new_heading + block[body_start:]
    updated = text[:block_start] + new_block + text[block_end:]
    return updated, True, f"'{item_title}' promoted to {new_status}"


def promote_status(
    path: str, roadmap_n: int, item_title: str, new_status: str
) -> tuple[bool, str]:
    """Promote the status emoji on a roadmap entry.  IO wrapper."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_promote_status(text, roadmap_n, item_title, new_status)
    if not ok:
        return False, msg

    return _atomic_write(path, updated, msg)


# ── 3. Append note ────────────────────────────────────────────────────────────

def _compute_append_note(
    text: str, roadmap_n: int, item_title: str, note: str
) -> tuple[str, bool, str]:
    """Pure: append ``note`` under the body of ``### <emoji> item_title``."""
    note_clean = (note or "").strip("\n")
    if not note_clean:
        return text, False, "note is empty"

    bounds = _find_roadmap_block(text, roadmap_n)
    if bounds is None:
        return text, False, f"Roadmap {roadmap_n} section not found"
    block_start, block_end = bounds
    block = text[block_start:block_end]

    result = _find_entry_in_block(block, item_title)
    if result is None:
        return text, False, f"Entry '{item_title}' not found in Roadmap {roadmap_n}"

    entry_start, body_start, entry_end = result
    existing_body = block[body_start:entry_end].rstrip("\n")
    new_body = existing_body + "\n" + note_clean + "\n"
    if entry_end < len(block):
        new_body += "\n"
    new_block = block[:body_start] + new_body + block[entry_end:]
    updated = text[:block_start] + new_block + text[block_end:]
    return updated, True, f"note appended to '{item_title}'"


def append_note(
    path: str, roadmap_n: int, item_title: str, note: str
) -> tuple[bool, str]:
    """Append a freeform note under a roadmap entry.  IO wrapper."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return False, f"Could not read {os.path.basename(path)}: {e}"

    updated, ok, msg = _compute_append_note(text, roadmap_n, item_title, note)
    if not ok:
        return False, msg

    return _atomic_write(path, updated, msg)


# ── Shared IO helpers ─────────────────────────────────────────────────────────

# Re-export from helpers.io_utils so the existing import sites (insert_roadmap_section
# etc.) keep working. The shared module lets architecture_patch and
# generic_doc_patch use the same atomic-write pattern without duplicating it.
from helpers.io_utils import _atomic_write   # noqa: F401


def read_roadmap_section(path: str, roadmap_n: int) -> str:
    """Return the body of ``## Roadmap N`` (heading line excluded)."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return ""
    return read_roadmap_section_from_text(text, roadmap_n)


def read_roadmap_section_from_text(text: str, roadmap_n: int) -> str:
    """Pure-string companion to ``read_roadmap_section``."""
    if not text:
        return ""
    bounds = _find_roadmap_block(text, roadmap_n)
    if bounds is None:
        return ""
    block_start, block_end = bounds
    return text[block_start:block_end].strip()


_ROADMAP_HEADING_LINE_RE = re.compile(
    r"(?m)^## Roadmap (\d+)(?:\s*[—–-]\s*([^\n]+))?$"
)


def _list_section_titles(text: str) -> list[str]:
    """Return roadmap headings as 'Roadmap N — Theme' strings, in document order.

    Theme B title-validation uses these as the allowed-titles set. Caller
    can match against `## Roadmap N` prefix to validate even when the model
    drifts on the theme suffix.
    """
    titles = []
    for m in _ROADMAP_HEADING_LINE_RE.finditer(text or ""):
        n = m.group(1)
        theme = (m.group(2) or "").strip()
        if theme:
            titles.append(f"Roadmap {n} — {theme}")
        else:
            titles.append(f"Roadmap {n}")
    return titles


def extract_sections_as_document(text: str, titles: list[str]) -> str:
    """Build a synthetic document containing only the named roadmap sections.

    `titles` are strings of the form 'Roadmap N' or 'Roadmap N — Theme'.
    Used by the simulator for aligned before/after diff sides.
    """
    if not titles:
        return ""
    parts = []
    for title in titles:
        # Extract roadmap number from "Roadmap N" or "Roadmap N — Theme"
        m = re.match(r"Roadmap (\d+)", title)
        if not m:
            continue
        n = int(m.group(1))
        body = read_roadmap_section_from_text(text or "", n)
        if body:
            parts.append(f"## {title}\n\n{body}")
        else:
            parts.append(f"## {title}\n\n_(new roadmap section — no prior content)_")
    return "\n\n".join(parts)


def read_roadmap_full(path: str) -> str:
    """Return the entire ROADMAP.md contents."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""
