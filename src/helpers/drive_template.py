"""drive_template — the live-driver starting point, scaffolded into NEW projects.

Three projects each built a driver, each discovered it was throwing information
away, and each patched it separately. `templates/drive/` holds what they
converged on: a toolkit-neutral evidence ledger, a small Tk driver on top of it,
and a README stating why every rule exists.

**Its lifecycle is not the lessons'.** The baseline is compiled and synced, and
the gotchas are synced to the existing fleet (`lessons_delivery`); this template
is only ever COPIED once, into a project that is being scaffolded. It is a
starting point a person then edits, so it is never refreshed, never judged
current or outdated, and never overwritten -- an existing file is left exactly as
it is. Retrofitting it into projects that already have a driver would replace the
one thing that project's author tuned.

`drive_ledger.py` here is byte-identical to `src/helpers/drive_ledger.py` (a test
holds them together), so the template cannot quietly fall behind the Manager's own
driver.
"""

from __future__ import annotations

import os

from helpers.io_utils import _atomic_write

#: Where the template lands, relative to the project root. A clearly-named
#: staging folder: the driver has to be moved next to the app and adapted, and
#: nothing here knows the project's layout.
DEST_DIR = "drive_template"

#: The whole template, by name. Fixed, like the lessons: no directory walk.
FILES = ("README.md", "drive_ledger.py", "debug_drive_tk.py")


def _read(path: str) -> str:
    """The file's text with its own line endings, or "" if unreadable."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ""


def scaffold_drive_template(template_dir: str, project_root: str) -> "list[str]":
    """Copy the template into `<project_root>/drive_template/`.

    Returns one line per file saying what happened. Never overwrites: a file
    that is already there is left byte-for-byte alone and named as skipped.
    """
    lines = []
    for name in FILES:
        rel = "%s/%s" % (DEST_DIR, name)
        text = _read(os.path.join(template_dir, "drive", name))
        if not text:
            lines.append("%s: template file could not be read - skipped" % rel)
            continue
        path = os.path.join(project_root, DEST_DIR, name)
        if os.path.lexists(path):
            lines.append("%s already exists - left alone" % rel)
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as exc:
            lines.append("%s: could not create the folder - %s" % (rel, exc))
            continue
        ok, message = _atomic_write(path, text, rel)
        lines.append("Created %s" % rel if ok else message)
    return lines


def wrote_anything(lines) -> bool:
    return any(line.startswith("Created ") for line in lines)
