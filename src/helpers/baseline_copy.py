"""baseline_copy — the per-project copy of the shared baseline.

## Why a copy, and not the include every project used to carry

Projects included the baseline by absolute path into `template_dir`. Claude
Code does not load an include from outside the project until a person approves
external includes for that project, and the desktop Code tab never asks.
Measured 2026-09-13 on three projects: the flag was `false` everywhere, the
session transcripts' instruction lists stopped at `BASIC_INSTRUCTIONS.md`, and a
canary placed in an external include appeared only after the flag was set by
hand. The baseline had reached no session at all while the Instructions panel
said "chain resolves".

So each project carries `<root>/project-baseline.md`, committed, which also
makes worktrees carry it. The template in `template_dir` stays the one file a
person edits; this module renders, reads and judges the copies.

The mechanics -- the provenance header, the three hashes, the strict grammar,
compare-and-apply writes -- live in `helpers/managed_copy.py`, which every
Manager-written copy shares (the lessons under `docs/gotchas/` too). This module
is the baseline's spelling of it: same names it always had, one fixed source.
Nothing here re-implements a rule.
"""

from __future__ import annotations

from helpers import managed_copy as mc
from helpers.managed_copy import (  # noqa: F401 - the module's public surface
    COPY_ABSENT, COPY_CURRENT, COPY_EDITED, COPY_INVALID, COPY_OUTDATED,
    COPY_UNMANAGED, COPY_UNREADABLE, HEADER_ABSENT, HEADER_INVALID, HEADER_OK,
    CopyFacts, CopyWrite, content_sha, lf,
)

COPY_BASENAME = "project-baseline.md"
#: The include line a localized `BASIC_INSTRUCTIONS.md` carries.
LOCAL_INCLUDE_LINE = "@" + COPY_BASENAME


def parse_copy(text: str) -> CopyFacts:
    """Facts from a file's text. Pure."""
    return mc.parse_copy(text, COPY_BASENAME)


def read_copy(path: str) -> CopyFacts:
    """Facts about the file at *path*. Reads that one file and nothing else."""
    return mc.read_copy(path, COPY_BASENAME)


def classify_copy(facts: CopyFacts, template_sha: str) -> str:
    """The copy's state against the template as it is now. Pure."""
    return mc.classify_copy(facts, template_sha)


def render_copy(template_text: str) -> str:
    """The exact bytes a copy of *template_text* holds. Deterministic."""
    return mc.render_copy(template_text, COPY_BASENAME)


def write_copy(project_root: str, template_text: str,
               expect_recorded_sha: "str | None" = None) -> CopyWrite:
    """Create or refresh `<root>/project-baseline.md`, then verify it."""
    return mc.write_managed(
        project_root, mc.ManagedCopySpec(COPY_BASENAME, COPY_BASENAME,
                                         template_text),
        expect_recorded_sha)


def read_template(baseline_path: str) -> str:
    """The template's text with its own line endings, or "" if unreadable."""
    return mc.read_source(baseline_path)
