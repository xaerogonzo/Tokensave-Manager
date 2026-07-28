"""One-shot splitter for helpers/doc_drafter.py (Roadmap-8 god-file split).

Moves top-level defs/consts into five family modules and rewrites
helpers/doc_drafter.py as a re-export facade. Import needs are computed
from AST name references; cross-module deps generate explicit imports.

Run from repo root:  python scripts/split_doc_drafter.py
"""
from __future__ import annotations

import ast
import sys
from collections import OrderedDict
from pathlib import Path

SRC = Path("src/helpers/doc_drafter.py")

MODULES: "OrderedDict[str, list[str]]" = OrderedDict([
    ("doc_drafter_git", [
        "_DOC_PATHSPECS", "_SPARSE_AVG_THRESHOLD", "_last_doc_commit_sha",
        "_commit_touches_code", "resolve_commit_range",
        "_resolve_since_last_doc", "_resolve_since_last_tag",
        "_resolve_custom", "_commits_in_range", "changed_file_paths",
        "read_blueprint_context", "is_sparse",
    ]),
    ("doc_drafter_prompts", [
        "PromptBuildResult", "_END_MARKER_RE", "_strip_end_marker",
        "_STOP_MARKER_RULE", "_ANTI_FABRICATION_RULE",
        "_STATELESS_FILTER_RULE", "_CHANGELOG_SYSTEM", "_README_SYSTEM",
        "_SUBSECTION_HEADER_RE", "_extract_subsection_headers",
        "_render_commit_list", "_SCOPE_PREFIX_RE", "_extract_scope_prefixes",
        "_summarise_existing_headings", "_PATH_TOKEN_STOPWORDS",
        "_SUBJECT_TOKEN_STOPWORDS", "_path_tokens", "_COMMIT_SCOPE_RE",
        "_scope_prefix_tokens", "_subject_tokens", "_split_into_sections",
        "_ALIGNMENT_THRESHOLD", "_select_candidate_sections",
        "build_changelog_prompt", "build_readme_prompt",
        "_ARCHITECTURE_SYSTEM", "_ROADMAP_SYSTEM", "_MEMORY_SYSTEM",
        "_GENERIC_DOC_SYSTEM", "_render_commit_summary",
        "_build_replace_mode_prompt", "build_architecture_prompt",
        "build_roadmap_prompt", "build_memory_prompt",
        "build_generic_doc_prompt",
    ]),
    ("doc_drafter_dispatch", [
        "_dispatch_agentic", "dispatch_llm",
    ]),
    ("doc_drafter_filters", [
        "_TRUNCATION_TRAILING", "_STRUCTURAL_MARKUP_RE", "_STOP_WORDS",
        "_NOOP_BULLET_PATTERNS", "_LITERAL_PLACEHOLDER_RE",
        "_PRESERVATION_THRESHOLD", "_is_noop_bullet", "_looks_truncated",
        "_token_set", "_is_duplicate_from_sets", "_is_duplicate",
        "_BULLET_LINE_RE", "_INDENTED_CONTINUATION_RE", "_normalise_bullet",
        "_sanitise_raw_draft", "_preserve_score", "_mirror_contract_check",
        "_merge_wrapped_bullets", "_filter_bullets", "parse_grouped_bullets",
        "split_readme_subsection", "changelog_filter_draft",
        "readme_filter_draft", "_SECTION_HEADING_RE", "_ROADMAP_HEADING_RE",
        "architecture_parse_draft", "roadmap_parse_draft",
        "memory_parse_draft", "generic_parse_draft", "_CODE_FENCE_RE",
        "_CONTENT_LINE_RE", "_HORIZONTAL_RULE_RE", "_SUBSTANTIVE_CONTENT_RE",
        "_is_substantive", "_FOOTER_MARKER_RE", "_strip_trailing_prose",
        "_strip_preamble_and_fences", "_filter_freeform",
        "architecture_filter_draft", "roadmap_filter_draft",
        "memory_filter_draft", "generic_filter_draft",
    ]),
    ("doc_drafter_apply", [
        "changelog_compute_apply", "readme_compute_apply",
        "changelog_io_apply", "readme_io_apply", "_apply_sections",
        "architecture_compute_apply", "roadmap_compute_apply",
        "memory_compute_apply", "generic_compute_apply", "_read_file_text",
        "architecture_io_apply", "roadmap_io_apply", "memory_io_apply",
        "generic_io_apply",
    ]),
])

MODULE_DOCS = {
    "doc_drafter_git": "Commit-range resolution + git plumbing for the doc drafter.",
    "doc_drafter_prompts": "System prompts + prompt builders for every doc type.",
    "doc_drafter_dispatch": "LLM dispatch — Claude CLI / _call_llm / agentic loop routing.",
    "doc_drafter_filters": "Draft quality filters, bullet parsing, and parse_draft fns.",
    "doc_drafter_apply": "compute_apply (pure) + io_apply (write) per doc type.",
}

# External imports available to every module; emitted only when referenced.
EXT_IMPORTS = [
    ("os",                "import os"),
    ("re",                "import re"),
    ("subprocess",        "import subprocess"),
    ("dataclass",         "from dataclasses import dataclass"),
    ("CREATE_NO_WINDOW",  "from constants import CREATE_NO_WINDOW"),
    ("_commits_since",    "from helpers.release import _commits_since"),
    ("_last_release_tag", "from helpers.release import _last_release_tag"),
]


def main() -> None:
    src_text = SRC.read_text(encoding="utf-8")
    lines = src_text.splitlines(keepends=True)
    tree = ast.parse(src_text)

    name_to_module: dict[str, str] = {}
    for mod, names in MODULES.items():
        for n in names:
            name_to_module[n] = mod

    # Collect named top-level segments. Segment i spans from the previous
    # node's end (captures banner comments) to this node's end.
    named_nodes = []
    prev_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            prev_end = node.end_lineno
            continue
        if isinstance(node, ast.Expr):           # module docstring
            prev_end = node.end_lineno
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        else:
            sys.exit(f"Unhandled node {type(node).__name__} at line {node.lineno}")
        named_nodes.append((name, prev_end + 1, node.end_lineno, node))
        prev_end = node.end_lineno

    unmapped = [n for n, *_ in named_nodes if n not in name_to_module]
    if unmapped:
        sys.exit(f"Unmapped top-level names: {unmapped}")
    mapped_only = set(name_to_module) - {n for n, *_ in named_nodes}
    if mapped_only:
        sys.exit(f"Mapped names not found in file: {sorted(mapped_only)}")

    # Per-module: ordered segments + referenced names.
    seg_text: dict[str, list[str]] = {m: [] for m in MODULES}
    refs: dict[str, set] = {m: set() for m in MODULES}
    for name, start, end, node in named_nodes:
        mod = name_to_module[name]
        seg_text[mod].append("".join(lines[start - 1:end]))
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                refs[mod].add(sub.id)

    helpers_dir = SRC.parent
    all_names_in_order = [n for n, *_ in named_nodes]

    for mod in MODULES:
        header = [
            f'"""{MODULE_DOCS[mod]}\n\n'
            f'Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).\n'
            f'Import via the ``helpers.doc_drafter`` facade — it re-exports\n'
            f'every name, so call sites and tests are unchanged.\n"""\n',
            "\nfrom __future__ import annotations\n\n",
        ]
        for ref_name, stmt in EXT_IMPORTS:
            if ref_name in refs[mod]:
                header.append(stmt + "\n")
        # Cross-module imports (only earlier-defined modules can be imported
        # to keep the graph acyclic; a later-module dep is a mapping error).
        mod_idx = list(MODULES).index(mod)
        for other_idx, (other, names) in enumerate(MODULES.items()):
            if other == mod:
                continue
            needed = sorted(set(names) & refs[mod] - set(MODULES[mod]))
            if needed:
                if other_idx > mod_idx:
                    sys.exit(f"Cycle risk: {mod} needs {needed} from later module {other}")
                header.append(
                    f"from helpers.{other} import (\n    "
                    + ",\n    ".join(needed) + ",\n)\n")
        body = "\n".join(seg_text[mod])
        out = helpers_dir / f"{mod}.py"
        out.write_text("".join(header) + "\n\n" + body, encoding="utf-8", newline="\n")
        print(f"wrote {out}  ({len(seg_text[mod])} segments)")

    # Facade: re-export every name in original order, grouped by module.
    facade = [
        '"""Doc-update drafter logic — re-export facade.\n\n'
        'Roadmap-8 god-file split: the implementation now lives in five\n'
        'family modules (git / prompts / dispatch / filters / apply).\n'
        'This facade re-exports every name so existing call sites and\n'
        'tests (`from helpers.doc_drafter import X`, `dd.X`) are\n'
        'unchanged. New code may import from the family modules directly.\n'
        '"""\n',
        "\nfrom __future__ import annotations\n\n",
    ]
    for mod, names in MODULES.items():
        ordered = [n for n in all_names_in_order if n in set(names)]
        facade.append(
            f"from helpers.{mod} import (\n    "
            + ",\n    ".join(ordered) + ",\n)\n")
    facade.append(
        "\n__all__ = [\n    "
        + ",\n    ".join(f'"{n}"' for n in all_names_in_order)
        + ",\n]\n")
    SRC.write_text("".join(facade), encoding="utf-8", newline="\n")
    print(f"rewrote {SRC} as facade ({len(all_names_in_order)} names)")


if __name__ == "__main__":
    main()
