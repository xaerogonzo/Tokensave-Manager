"""Parse docs/ROADMAP.md into structured RoadmapEntry instances.

Handles the canonical roadmap format:
    ## Roadmap N — Theme Title
    ### ✅ Item title
    body lines...
    ### 🟡 Another item
    ...

Status emoji set: ✅ 🟡 🔮 💭 💤

Pure-function module — no IO, no Tkinter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_ROADMAP_HEADING_RE = re.compile(r"^## Roadmap (\d+)[^\n]*$", re.MULTILINE)
_ENTRY_HEADING_RE   = re.compile(r"^### (✅|🟡|🔮|💭|💤) (.+)$", re.MULTILINE)

# Backtick-quoted token that looks like a file path (has an extension).
_FILE_RE   = re.compile(r"`([a-zA-Z0-9_./-]+\.[a-zA-Z]{1,8})`")
# Backtick-quoted PascalCase or _snake identifier (likely a symbol name).
_SYMBOL_RE = re.compile(r"`([A-Z][a-zA-Z0-9_]+|_[a-zA-Z][a-zA-Z0-9_]{2,})`")


@dataclass
class RoadmapEntry:
    """One ``### <status> Title`` entry inside a ``## Roadmap N`` section."""
    roadmap_n:         int
    status:            str         # one of: ✅ 🟡 🔮 💭 💤
    title:             str
    body:              str
    line_start:        int         # 1-based line number of the ### heading
    line_end:          int         # 1-based line number of last body line
    mentioned_files:   list = field(default_factory=list)
    mentioned_symbols: list = field(default_factory=list)


def parse_roadmap_md(text: str) -> list[RoadmapEntry]:
    """Return all RoadmapEntry objects parsed from ``text``."""
    if not text:
        return []

    entries: list[RoadmapEntry] = []
    roadmap_matches = list(_ROADMAP_HEADING_RE.finditer(text))

    for ri, rm in enumerate(roadmap_matches):
        roadmap_n   = int(rm.group(1))
        block_start = rm.end()
        block_end   = roadmap_matches[ri + 1].start() if ri + 1 < len(roadmap_matches) else len(text)
        block       = text[block_start:block_end]

        entry_matches = list(_ENTRY_HEADING_RE.finditer(block))

        for ei, em in enumerate(entry_matches):
            status = em.group(1)
            title  = em.group(2).strip()

            body_start_in_block = em.end()
            body_end_in_block   = (entry_matches[ei + 1].start()
                                   if ei + 1 < len(entry_matches)
                                   else len(block))
            body = block[body_start_in_block:body_end_in_block].strip()

            # Compute 1-based line numbers in the full text.
            char_heading_start = block_start + em.start()
            char_body_end      = block_start + body_end_in_block
            line_start = text[:char_heading_start].count("\n") + 1
            line_end   = text[:char_body_end].count("\n") + 1

            full_text = em.group(0) + "\n" + body
            mentioned_files   = list({m.group(1) for m in _FILE_RE.finditer(full_text)})
            mentioned_symbols = list({m.group(1) for m in _SYMBOL_RE.finditer(full_text)})

            entries.append(RoadmapEntry(
                roadmap_n=roadmap_n,
                status=status,
                title=title,
                body=body,
                line_start=line_start,
                line_end=line_end,
                mentioned_files=mentioned_files,
                mentioned_symbols=mentioned_symbols,
            ))

    return entries


def next_roadmap_n(entries: list[RoadmapEntry]) -> int:
    """Return the next unused Roadmap number (max existing N + 1, or 1)."""
    if not entries:
        return 1
    return max(e.roadmap_n for e in entries) + 1
