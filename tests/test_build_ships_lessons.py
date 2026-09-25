"""tests/test_build_ships_lessons.py — the build must ship what delivery reads.

`helpers/lessons_delivery` reads every lesson from `template_dir` at run time, and
in a build that is the `templates\\` folder beside the exe. A source checkout has
the files sitting right there, so every test that reads them passes on this machine
while a `dist\\` that forgot them would deliver an empty corpus: each project's
baseline would index files nothing ever wrote.

`build.ps1` copies `templates\\*` recursively rather than listing files, which is
what makes the corpus ship without a hand-kept list. That property is one careless
edit from disappearing (an `-Exclude`, a dropped `-Recurse`), so it is asserted
here -- along with the exact files the delivery inventory needs.
"""
from __future__ import annotations

import os
import re

from helpers import lessons_delivery as ld

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _templates_copy_line() -> str:
    with open(os.path.join(_ROOT, "build.ps1"), encoding="utf-8-sig") as handle:
        for line in handle:
            if re.search(r'Copy-Item\s+"\$ROOT\\templates\\\*"', line):
                return line
    return ""


def test_the_parser_found_the_templates_copy():
    """A regex that matches nothing would make the next test vacuous."""
    assert _templates_copy_line(), "build.ps1 no longer copies templates\\*"


def test_templates_are_copied_recursively_and_unfiltered():
    line = _templates_copy_line()
    assert "-Recurse" in line, (
        "templates\\gotchas\\ is a subdirectory: without -Recurse the lessons "
        "would not ship")
    assert not re.search(r"-(Exclude|Filter|Include)\b", line), (
        "a filter on the templates copy can silently drop lessons")


def test_every_lesson_the_delivery_inventory_names_is_in_templates():
    missing = [spec.source_rel for spec in ld.LESSONS
               if not os.path.isfile(os.path.join(
                   _ROOT, "templates", *spec.source_rel.split("/")))]
    assert missing == []
