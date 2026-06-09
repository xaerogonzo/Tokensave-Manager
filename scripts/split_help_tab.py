"""One-shot splitter for controllers/help_tab.py (Roadmap-8 god-file split).

Moves the 22 static help-topic renderer methods into three topic modules
as module-level functions taking the controller (`ctl`), and replaces
them with 2-line delegates so the section dispatch table is unchanged.

Run from repo root:  python scripts/split_help_tab.py
"""
from __future__ import annotations

import ast
import sys
from collections import OrderedDict
from pathlib import Path

SRC = Path("src/controllers/help_tab.py")

MODULES: "OrderedDict[str, list[str]]" = OrderedDict([
    ("help_topics_basics", [
        "_help_switching", "_help_window_tray", "_help_context_menu",
        "_help_scaffold", "_help_retrofit", "_help_nuitka",
        "_help_scaffold_column", "_help_autodetect", "_help_init_vs_sync",
        "_help_categories",
    ]),
    ("help_topics_git", [
        "_help_git_concepts", "_help_git_workflow", "_help_git_tab",
        "_help_github_setup",
    ]),
    ("help_topics_tools", [
        "_help_codegraph", "_help_ai_features", "_help_precommit_hook",
        "_help_run_checks", "_help_integration_check",
        "_help_settings_reference", "_help_file_locations", "_help_about",
    ]),
])

MODULE_DOCS = {
    "help_topics_basics": "Help-tab topic renderers — projects, scaffold, tray basics.",
    "help_topics_git": "Help-tab topic renderers — git concepts, workflow, GitHub.",
    "help_topics_tools": "Help-tab topic renderers — CodeGraph, AI, checks, settings, about.",
}

EXT_IMPORTS = [
    ("os",           "import os"),
    ("re",           "import re"),
    ("subprocess",   "import subprocess"),
    ("sys",          "import sys"),
    ("threading",    "import threading"),
    ("tk",           "import tkinter as tk"),
    ("ttk",          "from tkinter import ttk"),
    ("C",            "from constants import C"),
    ("LOG_FILE",     "from constants import LOG_FILE"),
    ("_BASE_DIR",    "from constants import _BASE_DIR"),
    ("_CONFIG_PATH", "from constants import _CONFIG_PATH"),
]


def main() -> None:
    src_text = SRC.read_text(encoding="utf-8")
    lines = src_text.splitlines(keepends=True)
    tree = ast.parse(src_text)

    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "HelpTabController")
    methods = {m.name: m for m in cls.body if isinstance(m, ast.FunctionDef)}

    moved = [n for names in MODULES.values() for n in names]
    missing = [n for n in moved if n not in methods]
    if missing:
        sys.exit(f"Methods not found: {missing}")

    first_moved = min(methods[n].lineno for n in moved)
    last_moved = max(methods[n].end_lineno for n in moved)
    # All moved methods must be contiguous at the end of the class (and file)
    # so the controller rewrite is a clean truncate + delegate append.
    for m in cls.body:
        if isinstance(m, ast.FunctionDef) and m.name not in moved:
            if m.lineno > first_moved:
                sys.exit(f"Non-moved method {m.name} sits inside the moved block")
    if last_moved != cls.end_lineno or cls.end_lineno != len(lines):
        sys.exit("Moved block does not end the class/file — manual review needed")

    def transform(name: str) -> tuple[str, set]:
        node = methods[name]
        body = lines[node.lineno - 1:node.end_lineno]
        out = []
        for i, ln in enumerate(body):
            ln = ln[4:] if ln.startswith("    ") else ln
            if i == 0:
                ln = ln.replace(f"def {name}(self):", f"def {name[6:]}(ctl):")
            out.append(ln.replace("self.", "ctl."))
        refs = {s.id for s in ast.walk(node) if isinstance(s, ast.Name)}
        return "".join(out), refs

    ctl_dir = SRC.parent
    for mod, names in MODULES.items():
        bodies, refs = [], set()
        for name in names:
            text, r = transform(name)
            bodies.append(text)
            refs |= r
        header = [
            f'"""{MODULE_DOCS[mod]}\n\n'
            f'Split out of controllers/help_tab.py (Roadmap-8 god-file split).\n'
            f'Each function takes the HelpTabController (``ctl``) and renders\n'
            f'its topic via ``ctl._hw()`` / ``ctl._help_show()`` exactly as the\n'
            f'original method did; the controller keeps 2-line delegates.\n"""\n',
            "\nfrom __future__ import annotations\n\n",
        ]
        for ref, stmt in EXT_IMPORTS:
            if ref in refs:
                header.append(stmt + "\n")
        out = ctl_dir / f"{mod}.py"
        out.write_text("".join(header) + "\n\n" + "\n".join(bodies),
                       encoding="utf-8", newline="\n")
        print(f"wrote {out}  ({len(names)} topics)")

    # Controller: keep everything above the moved block, then delegates.
    kept = "".join(lines[:first_moved - 1])
    delegates = []
    for mod, names in MODULES.items():
        for name in names:
            delegates.append(
                f"    def {name}(self):\n"
                f"        {mod}.{name[6:]}(self)\n\n")
    kept = kept.replace(
        "from constants import C, LOG_FILE, _BASE_DIR, _CONFIG_PATH\n",
        "from constants import C, LOG_FILE, _BASE_DIR, _CONFIG_PATH\n"
        "from controllers import (\n"
        "    help_topics_basics,\n"
        "    help_topics_git,\n"
        "    help_topics_tools,\n"
        ")\n",
        1,
    )
    SRC.write_text(kept + "".join(delegates).rstrip("\n") + "\n",
                   encoding="utf-8", newline="\n")
    print(f"rewrote {SRC} with {len(delegates)} delegates")


if __name__ == "__main__":
    main()
