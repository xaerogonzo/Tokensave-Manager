"""DocType registry — curated set of markdown document types the doc-drafter
can generate and apply.

Each ``DocType`` encapsulates ALL the per-target logic (prompt building,
draft filtering, simulation, and IO write) behind a uniform interface so
``DocDrafterDialog`` can be fully registry-driven with no hard-coded
``if key == 'changelog'`` branches.

Mandatory invariants (carried from Roadmap-6 Phase 2.1 hardening):
  - ``compute_apply`` is PURE — no IO, no side effects, byte-identical to
    write-then-read (mirror-contract).
  - ``io_apply`` is the ONLY write path, and it always routes through the
    appropriate Phase 2.1 patcher (append/splice semantics, never replace-all).
  - ALL write proposals flow through ``dialogs.proposal.ProposalBridge``
    before ``io_apply`` is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Any

from helpers.changelog_patch import (
    read_unreleased, read_unreleased_from_text,
)
from helpers.readme_patch import (
    read_highlights, read_highlights_from_text,
)
from helpers.doc_drafter import (
    build_changelog_prompt,
    build_readme_prompt,
    build_architecture_prompt,
    build_roadmap_prompt,
    build_memory_prompt,
    build_generic_doc_prompt,
    changelog_filter_draft,
    readme_filter_draft,
    architecture_filter_draft,
    roadmap_filter_draft,
    memory_filter_draft,
    generic_filter_draft,
    changelog_compute_apply,
    readme_compute_apply,
    architecture_compute_apply,
    roadmap_compute_apply,
    memory_compute_apply,
    generic_compute_apply,
    changelog_io_apply,
    readme_io_apply,
    architecture_io_apply,
    roadmap_io_apply,
    memory_io_apply,
    generic_io_apply,
    architecture_parse_draft,
    roadmap_parse_draft,
    memory_parse_draft,
    generic_parse_draft,
    parse_grouped_bullets,
    split_readme_subsection,
    _CHANGELOG_SYSTEM,
    _README_SYSTEM,
    _ARCHITECTURE_SYSTEM,
    _ROADMAP_SYSTEM,
    _MEMORY_SYSTEM,
    _GENERIC_DOC_SYSTEM,
)
from helpers.architecture_patch import (
    read_architecture_full,
)
from helpers.roadmap_patch import (
    read_roadmap_full,
)
from helpers.memory_patch import (
    read_memory_body, read_memory_body_from_text,
)
from helpers.generic_doc_patch import (
    read_generic_full,
)


@dataclass(frozen=True)
class DocType:
    """Describes one documentable target (CHANGELOG, README, architecture, …).

    Fields:
        key             Unique registry key, e.g. "changelog".
        label           Tab label in DocDrafterDialog, e.g. "CHANGELOG.md".
        target_filename Relative path from project root; "" for file-picker types.
        auto_create     True → show tab even when target_filename doesn't exist.
        system_prompt   LLM system prompt string (informational; build_prompt
                        also returns it, but stored here for quick reference).
        build_prompt    (commits, classified, existing, name, desc, files,
                        boundary, grounding_block="") -> (system, user)
        compute_apply   PURE (full_file_text, *parse_draft_result) ->
                        (new_full_text, ok, msg).  No IO.
        io_apply        (target_path, draft_text) -> (ok, msg, stats_dict).
                        Actual write; routes through Phase 2.1 patcher.
        read_existing   (path) -> str  — current on-disk section content.
        read_existing_from_text  (text) -> str  — extract section from text.
        parse_draft     (draft_text) -> value  — parsed form for compute_apply.
                        Return type is opaque; compute_apply unpacks with *.
        filter_draft    (raw_text, target_path) -> (text, trunc_n, dup_n, noop_n)
        sanity_checks   Additional Callable checks run before apply.
        tokensave_recipe  Recipe key in doc_grounding.py (Theme B); None = skip.
        gen_params      {temperature, top_p, top_k, num_ctx} overrides (Theme C).
        examples        Few-shot (input, output) pairs for Ollama (Theme C).
    """
    key:                     str
    label:                   str
    target_filename:         str
    auto_create:             bool
    system_prompt:           str
    build_prompt:            Callable
    compute_apply:           Callable
    io_apply:                Callable
    read_existing:           Callable
    read_existing_from_text: Callable
    parse_draft:             Callable
    filter_draft:            Callable
    sanity_checks:           list = field(default_factory=list)
    tokensave_recipe:        Any = None
    gen_params:              dict = field(default_factory=dict)
    examples:                list = field(default_factory=list)


# ── Registry ──────────────────────────────────────────────────────────────────

REGISTRY: dict[str, DocType] = {}


# ── Per-type parse_draft wrappers ─────────────────────────────────────────────
#
# parse_draft returns a value that gets splatted into compute_apply:
#   compute_apply(full_text, *parse_draft(draft_text))
#
# changelog: returns a 1-tuple (pairs_list,) so compute_apply receives
#   (full_text, pairs_list)  — a list of (section, bullets) tuples
#
# readme: returns a 2-tuple (header_line, bullets) directly, so compute_apply
#   receives (full_text, header_line, bullets)

def _changelog_parse_draft(draft_text):
    pairs = parse_grouped_bullets(draft_text)
    return (pairs,)


def _readme_parse_draft(draft_text):
    parsed = split_readme_subsection(draft_text)
    if parsed is None:
        return (None, "")
    return parsed   # (header_line, bullets)


# ── Prompt wrappers (add grounding_block kwarg for Theme B) ───────────────────

def _changelog_build_prompt(commits, classified, existing, name, desc,
                             files, boundary, grounding_block=""):
    system, user = build_changelog_prompt(
        commits, classified, existing, name, desc, files, boundary)
    if grounding_block:
        # Splice between commit list section and existing content.
        marker = "Current [Unreleased] content"
        idx = user.find(marker)
        if idx >= 0:
            user = user[:idx] + grounding_block + "\n\n" + user[idx:]
    return system, user


def _readme_build_prompt(commits, classified, existing, name, desc,
                          files, boundary, grounding_block=""):
    system, user = build_readme_prompt(
        commits, classified, existing, name, desc, files, boundary)
    if grounding_block:
        marker = "Current README"
        idx = user.find(marker)
        if idx >= 0:
            user = user[:idx] + grounding_block + "\n\n" + user[idx:]
    return system, user


# ── Initial registry entries ──────────────────────────────────────────────────

_CHANGELOG_EXAMPLE_IN = (
    "Project: MyApp\n"
    "Recent commits:\n"
    "  abc123 add dark-mode toggle to settings\n"
    "  def456 fix crash when project list is empty\n"
    "  ghi789 remove legacy daemon polling code"
)
_CHANGELOG_EXAMPLE_OUT = (
    "### Added\n"
    "- Dark-mode toggle in Settings (`_var_dark_mode` in `dialogs/settings.py`)\n\n"
    "### Fixed\n"
    "- Crash when project list is empty (`ProjectsTabController._refresh`)\n\n"
    "### Removed\n"
    "- Legacy daemon polling code and 5-second polling loop"
)

_README_EXAMPLE_IN = (
    "Project: MyApp\n"
    "Recent commits:\n"
    "  abc123 add dark-mode toggle to settings\n"
    "  def456 fix crash when project list is empty"
)
_README_EXAMPLE_OUT = (
    "**Roadmap-7 — Dark mode + stability**\n"
    "- ⚙ Dark-mode toggle in Settings dialog (`_var_dark_mode`)\n"
    "- 🐛 Fix crash on empty project list (`ProjectsTabController._refresh`)"
)

REGISTRY["changelog"] = DocType(
    key="changelog",
    label="CHANGELOG.md",
    target_filename="CHANGELOG.md",
    auto_create=False,
    system_prompt=_CHANGELOG_SYSTEM,
    build_prompt=_changelog_build_prompt,
    compute_apply=changelog_compute_apply,
    io_apply=changelog_io_apply,
    read_existing=read_unreleased,
    read_existing_from_text=read_unreleased_from_text,
    parse_draft=_changelog_parse_draft,
    filter_draft=changelog_filter_draft,
    tokensave_recipe="commit_range_context",
    gen_params={},
    examples=[(_CHANGELOG_EXAMPLE_IN, _CHANGELOG_EXAMPLE_OUT)],
)

REGISTRY["readme"] = DocType(
    key="readme",
    label="README.md",
    target_filename="README.md",
    auto_create=False,
    system_prompt=_README_SYSTEM,
    build_prompt=_readme_build_prompt,
    compute_apply=readme_compute_apply,
    io_apply=readme_io_apply,
    read_existing=read_highlights,
    read_existing_from_text=read_highlights_from_text,
    parse_draft=_readme_parse_draft,
    filter_draft=readme_filter_draft,
    tokensave_recipe="commit_range_context",
    gen_params={},
    examples=[(_README_EXAMPLE_IN, _README_EXAMPLE_OUT)],
)

REGISTRY["architecture"] = DocType(
    key="architecture",
    label="ARCHITECTURE.md",
    target_filename="docs/ARCHITECTURE.md",
    auto_create=False,
    system_prompt=_ARCHITECTURE_SYSTEM,
    build_prompt=build_architecture_prompt,
    compute_apply=architecture_compute_apply,
    io_apply=architecture_io_apply,
    read_existing=read_architecture_full,
    read_existing_from_text=lambda text: text,
    parse_draft=architecture_parse_draft,
    filter_draft=architecture_filter_draft,
    tokensave_recipe="architecture_overview",
    gen_params={"temperature": 0.5},
    examples=[],
)

REGISTRY["roadmap"] = DocType(
    key="roadmap",
    label="ROADMAP.md",
    target_filename="docs/ROADMAP.md",
    auto_create=False,
    system_prompt=_ROADMAP_SYSTEM,
    build_prompt=build_roadmap_prompt,
    compute_apply=roadmap_compute_apply,
    io_apply=roadmap_io_apply,
    read_existing=read_roadmap_full,
    read_existing_from_text=lambda text: text,
    parse_draft=roadmap_parse_draft,
    filter_draft=roadmap_filter_draft,
    tokensave_recipe="roadmap_evidence",
    gen_params={},
    examples=[],
)

# File-picker DocTypes — target_filename is "" because the target is
# selected at runtime via a combobox in the dialog.
REGISTRY["memory"] = DocType(
    key="memory",
    label="Memory file",
    target_filename="",
    auto_create=True,
    system_prompt=_MEMORY_SYSTEM,
    build_prompt=build_memory_prompt,
    compute_apply=memory_compute_apply,
    io_apply=memory_io_apply,
    read_existing=read_memory_body,
    read_existing_from_text=read_memory_body_from_text,
    parse_draft=memory_parse_draft,
    filter_draft=memory_filter_draft,
    tokensave_recipe="module_deep_dive",
    gen_params={"temperature": 0.5},
    examples=[],
)

REGISTRY["docs_generic"] = DocType(
    key="docs_generic",
    label="Docs file",
    target_filename="",
    auto_create=True,
    system_prompt=_GENERIC_DOC_SYSTEM,
    build_prompt=build_generic_doc_prompt,
    compute_apply=generic_compute_apply,
    io_apply=generic_io_apply,
    read_existing=read_generic_full,
    read_existing_from_text=lambda text: text,
    parse_draft=generic_parse_draft,
    filter_draft=generic_filter_draft,
    tokensave_recipe=None,
    gen_params={},
    examples=[],
)

REGISTRY["tokensave_guide"] = DocType(
    key="tokensave_guide",
    label="TOKENSAVE_GUIDE.md",
    target_filename="TOKENSAVE_GUIDE.md",
    auto_create=False,
    system_prompt=_GENERIC_DOC_SYSTEM,
    build_prompt=build_generic_doc_prompt,
    compute_apply=generic_compute_apply,
    io_apply=generic_io_apply,
    read_existing=read_generic_full,
    read_existing_from_text=lambda text: text,
    parse_draft=generic_parse_draft,
    filter_draft=generic_filter_draft,
    tokensave_recipe=None,
    gen_params={},
    examples=[],
)
