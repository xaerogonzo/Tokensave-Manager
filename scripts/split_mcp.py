"""One-shot splitter for helpers/mcp.py (Roadmap-16 god-file split).

Moves top-level defs/consts into six family modules and rewrites
helpers/mcp.py as a re-export facade. Adapted from
``scripts/split_doc_drafter.py``, which did the same job for the other god
file; the differences are the module map, ``AnnAssign`` support (mcp.py
annotates three of its tables), and one extra rule below.

**The extra rule: `mcp_paths` is the leaf, and it exists for a reason.**
``helpers/mcp_shadow.py`` and ``helpers/mcp_desktop.py`` already reach into
``helpers.mcp`` for private names. If they keep importing the facade while
the facade imports the family modules, the first family module that needs
either of them closes a runtime import cycle. Everything those two siblings
reach for is low-level, so it all lands in ``mcp_paths`` and they are
repointed there in the same commit. Family modules never import the facade.

Known quirk: the import chooser keys off AST `Name` references, so it
emits a module-level import even when every reference sits under a
deliberate function-scoped import of the same module. That happened
exactly once -- `shutil` in `mcp_scope` -- and the module-level line was
removed by hand afterwards. Re-running this script reintroduces it;
pyflakes catches it immediately.

Run from repo root:  python scripts/split_mcp.py
"""
from __future__ import annotations

import ast
import sys
from collections import OrderedDict
from pathlib import Path

SRC = Path("src/helpers/mcp.py")

# Order is load-bearing: a module may only import from one listed BEFORE it.
# The script exits rather than emitting a cycle, so a mapping mistake fails
# here instead of at import time in a user's session.
MODULES: "OrderedDict[str, list[str]]" = OrderedDict([
    ("mcp_paths", [
        "_resolve_desktop_cfg_path", "_mcp_desktop_cfg_path",
        "_mcp_code_cfg_path", "_mcp_configs", "_wrapper_path",
        "PROJECT_PATH_ARG", "GITIGNORE_PROJECT_MCP_KEY",
        "USER_SCOPE_RETIRED_KEY", "DESKTOP_SCOPE_RETIRED_KEY",
        "_project_mcp_path", "_same_project",
        "_write_json_atomic", "_DRIVE_RE", "_claude_json_path",
    ]),
    ("mcp_projects", [
        "normalize_project_key", "read_claude_projects",
        "matching_project_keys", "_SESSION_FIELDS", "_has_session",
        "canonical_launch_dir", "duplicate_project_keys",
    ]),
    ("mcp_classify", [
        "_canonical_mcp_entry", "_canonical_project_entry", "_McpCtx",
        "_chk_bundled_wrapper", "_chk_python_wrapper", "_chk_project_scoped",
        "_chk_direct_serve", "_MCP_CMD_CHECKERS", "_retired_absence",
        "_classify_mcp_entry", "_apply_mcp_fix", "remove_mcp_entry",
    ]),
    ("mcp_approval", [
        "APPROVAL_APPROVED", "APPROVAL_PENDING", "APPROVAL_REJECTED",
        "APPROVAL_AMBIGUOUS", "APPROVAL_UNKNOWN", "McpJsonApproval",
        "_settings_approval", "_entry_approval", "local_settings_approval",
        "mcpjson_approval", "local_scope_shadow", "approve_project_binding",
        "ADVISORY_STATES", "annotate_project_binding",
        # Lives here, not in mcp_projects, because deciding whether a
        # duplicate key is *stale* consults whether it was approved -- that
        # is a binding decision that happens to be about keys, and the
        # alternative was a mutual import between the two modules.
        "stale_duplicate_keys",
    ]),
    ("mcp_scope", [
        "SCOPE_PROJECT", "SCOPE_USER", "SCOPE_LOCAL", "SCOPE_ABSENT",
        "SCOPE_UNKNOWN", "EffectiveScope", "_parse_mcp_get",
        "describe_effective", "effective_scope", "_CLAUDE_JSON_ACTIVE_SECS",
        "claude_code_active", "_is_claude_running",
    ]),
    ("mcp_agents", [
        "_claude_code_mcp_has_codegraph", "_CODEGRAPH_AGENTS",
        "_codegraph_agent_destination_path", "_codegraph_agent_installed",
        "_TOKENSAVE_AGENTS", "_TOKENSAVE_AGENT_PATHS",
        "_tokensave_agent_path_candidates",
        "_tokensave_agent_destination_path", "_tokensave_agent_installed",
        "_tokensave_agent_wired",
    ]),
])

MODULE_DOCS = {
    "mcp_paths": ("Where MCP config lives, and the primitives everything "
                  "else needs.\n\nThe leaf of the family: it imports no "
                  "sibling. `helpers/mcp_shadow.py` and "
                  "`helpers/mcp_desktop.py` import from HERE rather than "
                  "from the facade, so the facade can import this module "
                  "without closing a cycle."),
    "mcp_projects": "Project keys in ~/.claude.json — normalising, matching, deduplicating.",
    "mcp_classify": "What an MCP entry is, what is wrong with it, and how to repair it.",
    "mcp_approval": "Whether a project's .mcp.json has actually been approved.",
    "mcp_scope": "Which scope wins, and whether Claude Code is live to care.",
    "mcp_agents": "Per-agent wiring tables for codegraph and tokensave.",
}

# External imports available to every module; emitted only when referenced.
EXT_IMPORTS = [
    ("dataclasses",      "import dataclasses"),
    ("glob",             "import glob"),
    ("json",             "import json"),
    ("os",               "import os"),
    ("posixpath",        "import posixpath"),
    ("re",               "import re"),
    ("shutil",           "import shutil"),
    ("subprocess",       "import subprocess"),
    ("sys",              "import sys"),
    ("time",             "import time"),
    ("_BASE_DIR",        "from constants import _BASE_DIR"),
    ("CREATE_NO_WINDOW", "from constants import CREATE_NO_WINDOW"),
]

FACADE_DOC = '''"""MCP configuration logic — re-export facade.

Roadmap-16 god-file split: 1590 lines became six family modules. This
facade re-exports **every** name, private ones included, because 20 modules
import from here and several take privates directly
(`dialogs/mcp_config.py` alone pulls seven). A public-only facade would have
turned a mechanical split into a 20-file change.

The dependency direction is one-way and must stay that way: this facade
imports the family modules, and no family module imports this facade.
`helpers/mcp_shadow.py` and `helpers/mcp_desktop.py` therefore import
`helpers.mcp_paths` directly. `tests/test_graph_trust.py`-style guards in
`tests/test_mcp_split.py` fail if either rule is broken.

New code may import from a family module directly; nothing has to.
"""
'''


def main() -> None:
    src_text = SRC.read_text(encoding="utf-8")
    lines = src_text.splitlines(keepends=True)
    tree = ast.parse(src_text)

    name_to_module: dict = {}
    for mod, names in MODULES.items():
        for n in names:
            name_to_module[n] = mod

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
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
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

    seg_text: dict = {m: [] for m in MODULES}
    refs: dict = {m: set() for m in MODULES}
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
            '"""' + MODULE_DOCS[mod] + "\n\n"
            "Split out of helpers/mcp.py (Roadmap-16 god-file split).\n"
            "Importable via the ``helpers.mcp`` facade, which re-exports\n"
            "every name, so existing call sites and tests are unchanged.\n"
            'This module must never import that facade.\n"""\n',
            "\nfrom __future__ import annotations\n\n",
        ]
        for ref_name, stmt in EXT_IMPORTS:
            if ref_name in refs[mod]:
                header.append(stmt + "\n")
        mod_idx = list(MODULES).index(mod)
        for other_idx, (other, names) in enumerate(MODULES.items()):
            if other == mod:
                continue
            needed = sorted((set(names) & refs[mod]) - set(MODULES[mod]))
            if needed:
                if other_idx > mod_idx:
                    sys.exit(
                        f"Cycle risk: {mod} needs {needed} from later module "
                        f"{other}. Reorder MODULES or move the name.")
                header.append(
                    f"from helpers.{other} import (\n    "
                    + ",\n    ".join(needed) + ",\n)\n")
        body = "\n".join(seg_text[mod])
        out = helpers_dir / f"{mod}.py"
        out.write_text("".join(header) + "\n\n" + body,
                       encoding="utf-8", newline="\n")
        print(f"wrote {out}  ({len(seg_text[mod])} segments)")

    facade = [FACADE_DOC, "\nfrom __future__ import annotations\n\n"]
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
