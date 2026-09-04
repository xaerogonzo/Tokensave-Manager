"""One-shot splitter for MCPConfigDialog (Roadmap-16 god-class split).

`dialogs/mcp_config.py` held a 1373-line class. This lifts three cohesive
panels into mixin modules and mixes them back in, which is **not** the
`SettingsDialog` section-class pattern — this dialog already established its
own: `DesktopMigrationMixin` in `dialogs/mcp_desktop_panel.py` was extracted
from this same class and is mixed into it today. Following the precedent the
file already sets beats importing a second pattern beside it.

What stays behind is composition and the things every panel needs:
`__init__`, `_render`, the running-guards, `_apply`, and
`_render_projects_section` — that last one deliberately, because it is a
known Doctor complexity offender and moving it would put two changes in one
commit.

Run from repo root:  python scripts/split_mcp_config_dialog.py
"""
from __future__ import annotations

import ast
import sys
from collections import OrderedDict
from pathlib import Path

SRC = Path("src/dialogs/mcp_config.py")
CLASS = "MCPConfigDialog"

MIXINS: "OrderedDict[str, tuple]" = OrderedDict([
    ("mcp_duplicates_panel", (
        "DuplicateKeysMixin",
        "Renders the duplicate `~/.claude.json` project-key panel.",
        ["_render_duplicate_keys", "_toggle_dups"],
    )),
    ("mcp_migration_panel", (
        "UserScopeMigrationMixin",
        "Renders and drives the user-scope tokensave retirement, including\n"
        "the background verification pass.",
        ["_migration_status", "_render_migration", "_render_user_scope_migration",
         "_remove_user_scoped", "_approve_one", "_approve_all",
         "_unapproved_roots", "_unretire_user_scoped", "_verify_migration",
         "_start_verification", "_mark_verifying", "_apply_verification"],
    )),
    ("mcp_blocks_panel", (
        "EntryBlocksMixin",
        "Renders one block per MCP config file: header, badge, diff, actions.",
        ["_toggle_bound", "_render_block", "_render_block_header",
         "_badge_colour", "_render_block_diff", "_render_block_actions"],
    )),
])

EXT_IMPORTS = [
    ("json",       "import json"),
    ("os",         "import os"),
    ("threading",  "import threading"),
    ("tk",         "import tkinter as tk"),
    ("ttk",        "from tkinter import ttk"),
    ("messagebox", "from tkinter import messagebox"),
    ("C",          "from constants import C"),
    ("bind_mousewheel", "from theme import bind_mousewheel"),
]

# Names re-exported by the helpers.mcp facade that a panel may reference.
MCP_NAMES = [
    "_mcp_configs", "_classify_mcp_entry", "_apply_mcp_fix",
    "_is_claude_running", "_mcp_code_cfg_path", "_project_mcp_path",
    "effective_scope", "remove_mcp_entry", "ADVISORY_STATES",
    "USER_SCOPE_RETIRED_KEY", "approve_project_binding",
    "_canonical_mcp_entry", "annotate_project_binding", "describe_effective",
    "duplicate_project_keys", "read_claude_projects",
]


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)

    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == CLASS), None)
    if cls is None:
        sys.exit(f"{CLASS} not found in {SRC}")

    name_to_mod = {}
    for mod, (_cls, _doc, names) in MIXINS.items():
        for n in names:
            name_to_mod[n] = mod

    # Segment each method from the end of the previous one, so decorators and
    # the comment banners above them travel with the method they belong to.
    segments = []
    prev_end = cls.body[0].end_lineno if isinstance(cls.body[0], ast.Expr) else cls.lineno
    for node in cls.body:
        if isinstance(node, ast.Expr):            # class docstring
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sys.exit(f"Unexpected {type(node).__name__} in class at line {node.lineno}")
        segments.append((node.name, prev_end + 1, node.end_lineno, node))
        prev_end = node.end_lineno

    missing = set(name_to_mod) - {n for n, *_ in segments}
    if missing:
        sys.exit(f"Mapped methods not found: {sorted(missing)}")

    moved, kept = [], []
    for name, start, end, node in segments:
        (moved if name in name_to_mod else kept).append((name, start, end, node))

    for mod, (mixin_cls, doc, names) in MIXINS.items():
        chosen = [s for s in moved if name_to_mod[s[0]] == mod]
        refs = set()
        for _n, _s, _e, node in chosen:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    refs.add(sub.id)

        header = [
            f'"""{doc}\n\n'
            f"Split out of ``dialogs/mcp_config.py`` (Roadmap-16 god-class\n"
            f"split), following the `DesktopMigrationMixin` precedent that\n"
            f"file already set rather than introducing a second pattern.\n"
            f'"""\n',
            "\nfrom __future__ import annotations\n\n",
        ]
        for ref, stmt in EXT_IMPORTS:
            if ref in refs:
                header.append(stmt + "\n")
        needed = sorted(set(MCP_NAMES) & refs)
        if needed:
            header.append("from helpers.mcp import (\n    "
                          + ",\n    ".join(needed) + ",\n)\n")

        body = [f"\n\nclass {mixin_cls}:\n"]
        expects = sorted({r for r in refs if r in {"self"}} )  # noop, kept clear
        body.append(f'    """{doc}\n\n'
                    f"    A mixin, so it reads ``self`` attributes the host\n"
                    f"    dialog owns (``_body``, ``_cfg``, ``_render()``,\n"
                    f"    ``_post()``, ``_log_to_app()``). It is never\n"
                    f"    instantiated on its own.\n"
                    f'    """\n')
        for _n, start, end, _node in chosen:
            body.append("".join(lines[start - 1:end]))

        out = SRC.parent / f"{mod}.py"
        out.write_text("".join(header) + "".join(body),
                       encoding="utf-8", newline="\n")
        print(f"wrote {out}  ({len(chosen)} methods)")

    # Rewrite the dialog: drop moved methods, add the mixins to the bases.
    drop = set()
    for _n, start, end, _node in moved:
        drop.update(range(start, end + 1))
    remaining = [ln for i, ln in enumerate(lines, 1) if i not in drop]
    new_text = "".join(remaining)

    imports = "\n".join(
        f"from dialogs.{mod} import {cls_name}"
        for mod, (cls_name, _d, _n) in MIXINS.items())
    anchor = "from dialogs.mcp_desktop_panel import DesktopMigrationMixin"
    if anchor not in new_text:
        sys.exit("anchor import not found")
    new_text = new_text.replace(anchor, anchor + "\n" + imports, 1)

    old_bases = f"class {CLASS}(DesktopMigrationMixin, UiPumpMixin, tk.Toplevel):"
    mixin_names = ", ".join(c for _m, (c, _d, _n) in MIXINS.items())
    new_bases = (f"class {CLASS}(DesktopMigrationMixin, {mixin_names},\n"
                 f"                     UiPumpMixin, tk.Toplevel):")
    if old_bases not in new_text:
        sys.exit("class declaration not found in expected form")
    new_text = new_text.replace(old_bases, new_bases, 1)

    SRC.write_text(new_text, encoding="utf-8", newline="\n")
    print(f"rewrote {SRC}: moved {len(moved)} methods, kept {len(kept)}")


if __name__ == "__main__":
    main()
