"""tests/test_build_ships_drive_template.py — the build must ship the template.

Scaffolding reads `templates/drive/` from `template_dir` at run time; in a build
that is the `templates\\` folder beside the exe. A source checkout has the files
right there, so every other test passes while a `dist\\` that dropped them would
scaffold nothing. `build.ps1` copies `templates\\*` recursively rather than
listing files; this pins that property and the exact files the scaffold needs.
"""
from __future__ import annotations

import os
import re

from helpers import drive_template

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _templates_copy_line() -> str:
    with open(os.path.join(_ROOT, "build.ps1"), encoding="utf-8-sig") as handle:
        for line in handle:
            if re.search(r'Copy-Item\s+"\$ROOT\\templates\\\*"', line):
                return line
    return ""


def test_the_parser_found_the_templates_copy():
    assert _templates_copy_line(), "build.ps1 no longer copies templates\\*"


def test_templates_are_copied_recursively_and_unfiltered():
    line = _templates_copy_line()
    assert "-Recurse" in line, "templates\\drive\\ is a subdirectory"
    assert not re.search(r"-(Exclude|Filter|Include)\b", line)


def test_every_file_the_scaffold_copies_is_in_templates():
    missing = [n for n in drive_template.FILES
               if not os.path.isfile(os.path.join(_ROOT, "templates", "drive", n))]
    assert missing == []
